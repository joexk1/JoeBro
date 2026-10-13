"""APNs push for the phone app: registration, honest receipts, Live Activity.

Stdlib only, like the rest of the backend. The provider token (ES256 JWT) is
signed by shelling out to /usr/bin/openssl, and the HTTP/2 request is a call to
/usr/bin/curl (macOS ships curl with nghttp2). Nothing is imported from outside
the standard library, and nothing here runs until the user has saved an Apple
key in Settings > Mobile > Notifications.

Configuration is a private apns.json in the data folder:
  {"team_id": "ABCDE12345", "key_id": "ABCDE12345", "key_path": ".../apns/AuthKey_X.p8",
   "bundle_id": "<the app's own bundle id>"}
Device installation IDs, never model-supplied device tokens, select registered
devices. Push is optional: with no key, notify() keeps the message in the Inbox.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from jb_core import data_dir

OPENSSL = "/usr/bin/openssl"
CURL = "/usr/bin/curl"
_LOCK = threading.Lock()
_JWT = {"signature": None, "token": None, "at": 0}
JWT_TTL = 3000          # APNs accepts a provider token for an hour; refresh at 50 minutes
_BUNDLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{2,154}")


class PushError(ValueError):
    pass


# ---- configuration ------------------------------------------------------------

def config_path(store=None):
    env = os.environ.get("JOEBRO_APNS_CONFIG")
    return Path(env).expanduser() if env else data_dir(store) / "apns.json"


def _binary(path, name):
    """The system tool, or whatever is on PATH, or None."""
    return path if os.access(path, os.X_OK) else shutil.which(name)


def _config(store=None):
    try:
        data = json.loads(config_path(store).read_text())
        if not isinstance(data, dict):
            raise ValueError()
    except (OSError, ValueError):
        raise PushError("Push is not set up: add your Apple APNs key in Settings > Mobile > Notifications") from None
    for name in ("team_id", "key_id"):
        if not re.fullmatch(r"[A-Z0-9]{10}", str(data.get(name, ""))):
            raise PushError(f"APNs {name} is missing or invalid")
    key = Path(str(data.get("key_path", ""))).expanduser()
    if not key.is_absolute() or not key.is_file():
        raise PushError("The APNs signing key file is missing; save the key again")
    bundle = str(data.get("bundle_id") or "")
    if not _BUNDLE_RE.fullmatch(bundle):
        raise PushError("APNs bundle_id is missing: enter the bundle ID of the iPhone app you built")
    return {**data, "key_path": str(key), "bundle_id": bundle}


def _configured_bundle(store=None):
    try:
        return _config(store)["bundle_id"]
    except PushError:
        return ""


# ---- ES256 provider token, signed with the system openssl ----------------------

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def der_to_raw(der, size=32):
    """An ECDSA signature from openssl is DER (SEQUENCE of two INTEGERs). JWS wants
    the raw r||s, each left-padded to `size` bytes. A DER integer can carry a
    leading 00 (to keep it positive) or be shorter than `size`; both are handled."""
    if len(der) < 8 or der[0] != 0x30:
        raise PushError("unexpected signature format from openssl")
    i = 2
    if der[1] & 0x80:                       # long-form sequence length
        i = 2 + (der[1] & 0x7F)
    ints = []
    for _ in range(2):
        if der[i] != 0x02:
            raise PushError("unexpected signature format from openssl")
        n = der[i + 1]
        value = der[i + 2:i + 2 + n]
        ints.append(value.lstrip(b"\x00"))
        i += 2 + n
    if any(len(v) > size for v in ints):
        raise PushError("unexpected signature size from openssl")
    return b"".join(v.rjust(size, b"\x00") for v in ints)


def sign_es256(key_path, signing_input):
    """Raw 64-byte ES256 signature of `signing_input` (bytes) with a PEM P-256 key."""
    tool = _binary(OPENSSL, "openssl")
    if not tool:
        raise PushError("openssl was not found on this Mac")
    try:
        proc = subprocess.run([tool, "dgst", "-sha256", "-sign", str(key_path)], input=signing_input,
                              capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise PushError("APNs signing key could not produce an ES256 provider token") from None
    if proc.returncode != 0 or not proc.stdout:
        raise PushError("APNs signing key could not produce an ES256 provider token")
    return der_to_raw(proc.stdout)


def make_jwt(config, now=None):
    now = int(now or time.time())
    head = _b64url(json.dumps({"alg": "ES256", "kid": config["key_id"]}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iss": config["team_id"], "iat": now}, separators=(",", ":")).encode())
    signing_input = head + b"." + claims
    return (signing_input + b"." + _b64url(sign_es256(config["key_path"], signing_input))).decode()


def _provider_token(config):
    signature = (config["key_path"], Path(config["key_path"]).stat().st_mtime_ns, config["team_id"], config["key_id"])
    with _LOCK:
        now = time.time()
        if _JWT["signature"] == signature and now - _JWT["at"] < JWT_TTL:
            return _JWT["token"]
        token = make_jwt(config, now)
        _JWT.update(signature=signature, token=token, at=now)
        return token


# ---- HTTP/2 via curl ------------------------------------------------------------

def _post(host, device_token, headers, body, timeout=8):
    """POST `body` to APNs. Returns (status_code, response_text). The bearer token
    goes in a 0600 header file, never on the command line where `ps` could show it."""
    tool = _binary(CURL, "curl")
    if not tool:
        raise PushError("curl was not found on this Mac")
    fd, hdr_path = tempfile.mkstemp(prefix="jb-apns-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(f"{k}: {v}\n" for k, v in headers.items()))
        proc = subprocess.run(
            [tool, "--http2", "-sS", "-X", "POST", "-H", "@" + hdr_path, "--data-binary", "@-",
             "--max-time", str(int(timeout)), "-w", "\n%{http_code}", f"https://{host}/3/device/{device_token}"],
            input=body, capture_output=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError):
        raise PushError("APNsNetworkUnavailable") from None
    finally:
        try:
            os.unlink(hdr_path)
        except OSError:
            pass
    out = proc.stdout.decode("utf-8", "replace")
    text, _, code = out.rpartition("\n")
    if proc.returncode != 0 or not code.strip().isdigit():
        raise PushError("APNsNetworkUnavailable")
    return int(code), text


def _reason(text):
    """Apple's finite reason token from an error body, never the body itself."""
    try:
        reason = json.loads(text).get("reason", "APNsRejected")
    except Exception:
        reason = "APNsRejected"
    return reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z]{1,80}", reason) else "APNsRejected"


def _host(environment):
    return "api.sandbox.push.apple.com" if environment == "development" else "api.push.apple.com"


# ---- devices ----------------------------------------------------------------------

def register_device(store, args):
    install = args.get("install_id")
    if not isinstance(install, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,100}", install):
        raise PushError("install_id must be a persistent app installation UUID")
    enabled = args.get("enabled", True)
    hidden = args.get("hide_previews", True)
    if not isinstance(enabled, bool) or not isinstance(hidden, bool):
        raise PushError("enabled and hide_previews must be booleans")
    if not enabled:
        store.exec("UPDATE push_devices SET enabled=0,updated_at=? WHERE install_id=?", (time.time(), install))
        return {"ok": True, "registered": False, "enabled": False}
    token = args.get("device_token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32,200}", token) or len(token) % 2:
        raise PushError("device_token must be a hexadecimal APNs token")
    environment = args.get("environment")
    if environment not in ("development", "production"):
        raise PushError("environment must match the signed app entitlement")
    bundle = str(args.get("bundle_id") or "")
    if not _BUNDLE_RE.fullmatch(bundle):
        raise PushError("bundle_id is missing or invalid")
    mine = _configured_bundle(store)
    if mine and bundle != mine:
        raise PushError("bundle_id does not match the app this Mac is set up to notify")
    # Cap registered installations, and retire previous installs of the same token.
    existing = store.one("SELECT install_id FROM push_devices WHERE install_id=?", (install,))
    count = store.one("SELECT COUNT(*) AS n FROM push_devices WHERE enabled=1") or {}
    if not existing and count.get("n", 0) >= 20:
        raise PushError("Push device limit reached")
    store.exec("UPDATE push_devices SET enabled=0 WHERE token=? AND environment=? AND install_id<>?",
               (token.lower(), environment, install))
    store.exec("""INSERT INTO push_devices(install_id,token,environment,bundle_id,enabled,hide_previews,updated_at)
        VALUES(?,?,?,?,1,?,?) ON CONFLICT(install_id) DO UPDATE SET token=excluded.token,
        environment=excluded.environment,bundle_id=excluded.bundle_id,enabled=1,
        hide_previews=excluded.hide_previews,updated_at=excluded.updated_at,last_error=NULL""",
               (install, token.lower(), environment, bundle, int(hidden), time.time()))
    return {"ok": True, "registered": True, "enabled": True, "environment": environment}


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else ""


def list_devices(store):
    """The registered phones, for the Mac's "Connected phones" list. Never the token."""
    out = []
    for r in store.rows("SELECT * FROM push_devices ORDER BY updated_at DESC"):
        out.append({"id": r["install_id"], "install_id": r["install_id"],
                    "name": "iPhone " + r["install_id"][-4:].upper(),
                    "environment": r["environment"], "bundle_id": r["bundle_id"],
                    "enabled": bool(r["enabled"]), "push": bool(r["enabled"]),
                    "hide_previews": bool(r["hide_previews"]),
                    "last_seen": _iso(r["updated_at"]), "last_success": _iso(r.get("last_success_at")),
                    "last_error": r.get("last_error") or ""})
    return out


def remove_device(store, install_id):
    store.exec("DELETE FROM push_devices WHERE install_id=?", (install_id,))
    return {"ok": True}


def push_status(store):
    counts = store.one("SELECT COUNT(*) AS registered,SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled FROM push_devices") or {}
    result = {"configured": False, "registered_devices": counts.get("registered", 0),
              "enabled_devices": counts.get("enabled", 0) or 0, "bundle_id": _configured_bundle(store)}
    try:
        _config(store)
        if not _binary(OPENSSL, "openssl") or not _binary(CURL, "curl"):
            raise PushError("openssl or curl is missing on this Mac")
        result["configured"] = True
    except PushError as exc:
        result["blocker"] = str(exc)
    if result["configured"] and not result["enabled_devices"]:
        result["blocker"] = "Turn on notifications in the iPhone app to register that phone"
    return result


# ---- sending ------------------------------------------------------------------------

def send_push(store, title, body, session=None):
    if not isinstance(title, str) or len(title) > 150 or not isinstance(body, str) or not body.strip() or len(body) > 3000:
        raise PushError("Push title/body invalid (title <=150, nonempty body <=3000 characters)")
    if session is not None and (not isinstance(session, str) or len(session) > 100):
        raise PushError("Push session must be a short chat identifier")
    status = push_status(store)
    if not status["configured"] or not status["enabled_devices"]:
        return {"channel": "mobile", "accepted": 0, "failed": 0, "delivered": False,
                "detail": status.get("blocker", "Mobile push unavailable")}
    config = _config(store)
    try:
        auth = _provider_token(config)
    except PushError as exc:
        return {"channel": "mobile", "accepted": 0, "failed": 0, "delivered": False, "detail": str(exc)}
    rows = store.rows("SELECT * FROM push_devices WHERE enabled=1 AND bundle_id=? ORDER BY updated_at DESC LIMIT 20",
                      (config["bundle_id"],))
    accepted, failures = 0, []
    deadline = time.monotonic() + 20
    for row in rows:
        left = deadline - time.monotonic()
        if left <= 0:
            failures.append("delivery_time_budget_exceeded")
            break
        alert = {"title": "JoeBro" if row["hide_previews"] else (title or "JoeBro"),
                 "body": "You have a new JoeBro notification. Open the app to read it." if row["hide_previews"] else body.strip()}
        payload = {"aps": {"alert": alert, "sound": "default"}, "joebro": {"kind": "inbox"}}
        if session:
            payload["joebro"]["session"] = session
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 4096:
            failures.append("payload_too_large")
            continue
        headers = {"authorization": "bearer " + auth, "apns-topic": config["bundle_id"],
                   "apns-push-type": "alert", "apns-priority": "10",
                   "apns-expiration": str(int(time.time()) + 86400)}
        try:
            code, text = _post(_host(row["environment"]), row["token"], headers, raw, timeout=min(8, left))
        except PushError:
            failures.append("APNsNetworkUnavailable")
            store.exec("UPDATE push_devices SET last_error=? WHERE install_id=?", ("APNsNetworkUnavailable", row["install_id"]))
            continue
        if code == 200:
            accepted += 1
            store.exec("UPDATE push_devices SET last_success_at=?,last_error=NULL WHERE install_id=?", (time.time(), row["install_id"]))
            continue
        reason = _reason(text)
        invalid = code == 410 or reason in ("BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered")
        store.exec("UPDATE push_devices SET last_error=?,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE install_id=?",
                   (reason, int(invalid), row["install_id"]))
        failures.append(reason)
    return {"channel": "mobile", "accepted": accepted, "failed": len(failures), "delivered": accepted > 0,
            "detail": "Accepted by APNs; device display is not confirmed" if accepted else "No mobile notification accepted",
            "errors": failures}


# ---- Live Activity push (ends the Dynamic Island when work finishes) -----------------

def register_activity(store, args):
    token, env = args.get("token"), args.get("environment")
    if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32,400}", token) or env not in ("development", "production"):
        raise PushError("invalid activity token")
    store.exec("DELETE FROM push_activities WHERE updated_at<?", (time.time() - 86400,))
    store.exec("INSERT INTO push_activities(token,environment,updated_at) VALUES(?,?,?) "
               "ON CONFLICT(token) DO UPDATE SET updated_at=excluded.updated_at", (token.lower(), env, time.time()))
    return {"ok": True}


def end_activities(store, headline="Done", state="done"):
    """Send a liveactivity end push (done content state) to every registered activity."""
    rows = store.rows("SELECT * FROM push_activities")
    if not rows:
        return 0
    config = _config(store)
    auth = _provider_token(config)
    now = int(time.time())
    sent = 0
    # The content-state keys are fixed by the iOS JoeBroActivityAttributes; do not
    # rename them without changing the app. startedAt is seconds since 2001-01-01.
    body = {"aps": {"timestamp": now, "event": "end", "dismissal-date": now + 300, "content-state": {
        "count": 0, "runs": [], "startedAt": now - 978307200, "headline": headline, "finished": state}}}
    raw = json.dumps(body).encode()
    headers = {"authorization": "bearer " + auth, "apns-topic": config["bundle_id"] + ".push-type.liveactivity",
               "apns-push-type": "liveactivity", "apns-priority": "10"}
    for row in rows:
        try:
            code, _ = _post(_host(row["environment"]), row["token"], headers, raw)
            sent += code == 200
        except PushError:
            continue
        store.exec("DELETE FROM push_activities WHERE token=?", (row["token"],))
    return sent


def end_if_idle(store, busy_turns):
    """Called after a chat turn / agent closes: end the island only when nothing
    else is running. No-op (and no network) when no activity was registered."""
    def work():
        try:
            time.sleep(1)
            if busy_turns():
                return
            n = store.one("SELECT COUNT(*) AS n FROM agents WHERE status IN ('queued','running') AND kind<>'briefing'") or {}
            if n.get("n", 0):
                return
            end_activities(store)
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()
