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


def notification_text(markdown, limit=None):
    """Readable APNs text. Parse the full Markdown before limiting the preview.

    Notification alert bodies are plain strings, not attributed Markdown. Keep
    code literal, meaningful line breaks, list structure and image/link labels;
    leave the original chat message and push routing metadata untouched.
    """
    import html
    text = str(markdown or '').replace('\r\n', '\n').replace('\r', '\n')
    protected = []
    def hold(value):
        token = '\x00P%d\x00' % len(protected)
        protected.append(value)
        return token

    # Code fences can contain characters that look like Markdown emphasis.
    lines, output, code, fence = text.split('\n'), [], [], None
    for line in lines:
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                output.append(hold('\n'.join(code))); code, fence = [], None
            else:
                code.append(line)
        elif marker:
            fence = marker[1]
        else:
            output.append(line)
    if fence: output.append(hold('\n'.join(code)))
    text = '\n'.join(output)
    text = re.sub(r'(`+)(.+?)\1(?!`)', lambda m: hold(re.sub(r'\s+', ' ', m[2]).strip()), text, flags=re.S)
    text = re.sub(r'\\([\\`*_{}\[\]()#+.!|>~\-])', lambda m: hold(m[1]), text)
    text = re.sub(r'^ {0,3}\[[^\]]+\]:[ \t]*\S.*$', '', text, flags=re.M)
    # Inline links/images with balanced one-level parentheses in destinations.
    text = re.sub(r'!?\[([^\]]*)\]\((?:[^()\n]|\([^()\n]*\))*\)', r'\1', text)
    text = re.sub(r'!?\[([^\]]+)\]\[[^\]]*\]', r'\1', text)
    text = re.sub(r'<((?:https?://|mailto:)[^>\s]+)>', lambda m: m[1].removeprefix('mailto:'), text)
    text = re.sub(r'</?[A-Za-z][A-Za-z0-9]*(?:\s[^>]*|\s*)/?>', '', text)
    text = re.sub(r'^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$', '', text, flags=re.M)
    text = re.sub(r'^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', r'\1', text, flags=re.M)
    text = re.sub(r'^ {0,3}(?:>[ \t]*)+', '', text, flags=re.M)
    text = re.sub(r'^([ \t]*)[-+*][ \t]+\[([ xX])\][ \t]+',
                  lambda m: m[1] + ('☑ ' if m[2].lower() == 'x' else '☐ '), text, flags=re.M)
    text = re.sub(r'^([ \t]*)[-+*][ \t]+', r'\1• ', text, flags=re.M)
    for _ in range(2):
        text = re.sub(r'(\*\*|__)(?=\S)(.+?)\1', lambda m: m[2].rstrip(), text, flags=re.S)
        text = re.sub(r'(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\*)', r'\1', text, flags=re.S)
        text = re.sub(r'(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)', r'\1', text, flags=re.S)
        text = re.sub(r'~~(?=\S)(.+?)(?<=\S)~~', r'\1', text, flags=re.S)
    text = re.sub(r'^ {0,3}(?:[-=_][ \t]*){3,}$', '', text, flags=re.M)
    # Tables become compact readable rows rather than a pipe/alignment wall.
    lines = text.split('\n'); table_lines = set()
    for i, line in enumerate(lines):
        if re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', line):
            table_lines.add(i)
            j = i - 1
            while j >= 0 and '|' in lines[j] and lines[j].strip(): table_lines.add(j); j -= 1
            j = i + 1
            while j < len(lines) and '|' in lines[j] and lines[j].strip(): table_lines.add(j); j += 1
    for i in table_lines:
        if re.fullmatch(r'[\s|:\-]+', lines[i]): lines[i] = ''
        else: lines[i] = ' · '.join(x.strip() for x in lines[i].strip().strip('|').split('|'))
    text = html.unescape('\n'.join(lines))
    text = re.sub(r'\x00P(\d+)\x00', lambda m: protected[int(m[1])], text)
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if limit is not None and len(text) > limit:
        if limit < 2: return '…'[:max(0,limit)]
        prefix = text[:limit-1].rstrip()
        if len(prefix) < len(text) and prefix and prefix[-1].isalnum() and text[len(prefix)].isalnum():
            boundary = max(prefix.rfind(' '), prefix.rfind('\n'))
            if boundary >= int(limit * .65): prefix = prefix[:boundary].rstrip()
        text = prefix + '…'
    return text


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

def _reserve_personal_delivery(store, message_id, install_id):
    """Persist before transport: a timeout must not trigger a duplicate push."""
    store.exec('''CREATE TABLE IF NOT EXISTS push_message_deliveries (
        message_id TEXT NOT NULL, install_id TEXT NOT NULL, state TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(message_id,install_id))''')
    with store.lock, store.db() as db:
        cursor = db.execute('''INSERT OR IGNORE INTO push_message_deliveries
            (message_id,install_id,state,created_at,updated_at) VALUES(?,?,'reserved',?,?)''',
            (message_id,install_id,time.time(),time.time()))
        created = cursor.rowcount == 1
        row = db.execute('SELECT state FROM push_message_deliveries WHERE message_id=? AND install_id=?',
                         (message_id,install_id)).fetchone()
        db.commit()
        return created, row['state']


def _finish_personal_delivery(store, message_id, install_id, state):
    store.exec('UPDATE push_message_deliveries SET state=?,updated_at=? WHERE message_id=? AND install_id=?',
               (state,time.time(),message_id,install_id))


def send_push(store, title, body, session=None, category=None, data=None):
    if not isinstance(title, str) or len(title) > 150 or not isinstance(body, str) or not body.strip() or len(body) > 3000:
        raise PushError("Push title/body invalid (title <=150, nonempty body <=3000 characters)")
    if session is not None and (not isinstance(session, str) or len(session) > 100):
        raise PushError("Push session must be a short chat identifier")
    personal_message = None
    if category == "PERSONAL_MESSAGE":      # a saved phone-chat message: pushed at most once per device
        personal_message = str((data or {}).get("message_id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", personal_message):
            raise PushError("Personal pushes require a saved chat message ID")
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
    accepted, failures, deduplicated, seen_devices = 0, [], 0, set()
    deadline = time.monotonic() + 20
    for row in rows:
        target = (row["token"], row["environment"])
        if target in seen_devices:
            continue
        seen_devices.add(target)
        left = deadline - time.monotonic()
        if left <= 0:
            failures.append("delivery_time_budget_exceeded")
            break
        alert = {"title": "JoeBro" if row["hide_previews"] else (title or "JoeBro"),
                 "body": "You have a new JoeBro notification. Open the app to read it." if row["hide_previews"] else body.strip()}
        payload = {"aps": {"alert": alert, "sound": "default"}, "joebro": {"kind": "inbox"}}
        if session:
            payload["joebro"]["session"] = session
        if personal_message:
            payload["aps"]["thread-id"] = "joebro-personal"
        if category:   # actionable notification (e.g. CODING_APPROVAL -> Approve / Deny buttons)
            payload["aps"]["category"] = str(category)[:40]
        if isinstance(data, dict):
            payload["joebro"].update({str(k)[:20]: str(v)[:200] for k, v in list(data.items())[:6]})
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 4096:
            failures.append("payload_too_large")
            continue
        headers = {"authorization": "bearer " + auth, "apns-topic": config["bundle_id"],
                   "apns-push-type": "alert", "apns-priority": "10",
                   "apns-expiration": str(int(time.time()) + 86400)}
        if personal_message:
            headers["apns-collapse-id"] = "jb-personal-" + personal_message
            fresh, state = _reserve_personal_delivery(store, personal_message, row["install_id"])
            if not fresh:       # persisted before transport: a timeout must not cause a duplicate push
                deduplicated += 1
                if state == "accepted":
                    accepted += 1
                else:
                    failures.append("personal_message_delivery_already_attempted")
                continue
        try:
            code, text = _post(_host(row["environment"]), row["token"], headers, raw, timeout=min(8, left))
        except PushError:
            failures.append("APNsNetworkUnavailable")
            store.exec("UPDATE push_devices SET last_error=? WHERE install_id=?", ("APNsNetworkUnavailable", row["install_id"]))
            continue
        if code == 200:
            if personal_message:
                _finish_personal_delivery(store, personal_message, row["install_id"], "accepted")
            accepted += 1
            store.exec("UPDATE push_devices SET last_success_at=?,last_error=NULL WHERE install_id=?", (time.time(), row["install_id"]))
            continue
        reason = _reason(text)
        invalid = code == 410 or reason in ("BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered")
        store.exec("UPDATE push_devices SET last_error=?,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE install_id=?",
                   (reason, int(invalid), row["install_id"]))
        failures.append(reason)
    return {"channel": "mobile", "accepted": accepted, "deduplicated": deduplicated, "failed": len(failures), "delivered": accepted > 0,
            "detail": "Accepted by APNs; device display is not confirmed" if accepted else "No mobile notification accepted",
            "errors": failures}


# ---- Live Activity push (ends the Dynamic Island when work finishes) -----------------

_ACTIVITY_SCHEMA_DONE = set()


def _activity_schema(store):
    """push_activities gains install_id/activity_id; push_start_tokens is new. Idempotent."""
    key = id(store)
    if key in _ACTIVITY_SCHEMA_DONE:
        return
    store.exec("CREATE TABLE IF NOT EXISTS push_activities(token TEXT PRIMARY KEY, environment TEXT NOT NULL, updated_at REAL NOT NULL)")
    for col in ("install_id", "activity_id"):
        try:
            store.exec("ALTER TABLE push_activities ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % col)
        except Exception:
            pass
    store.exec("""CREATE TABLE IF NOT EXISTS push_start_tokens(install_id TEXT PRIMARY KEY, token TEXT NOT NULL,
        environment TEXT NOT NULL, updated_at REAL NOT NULL)""")
    _ACTIVITY_SCHEMA_DONE.add(key)


_HEX_RX = re.compile(r"[a-fA-F0-9]{32,400}")


def register_activity(store, args):
    """Live Activity tokens. One body may carry a push-to-start token (`push_to_start_token` +
    `install_id`), a per-activity update token (`token`, or `push_token`, optional install_id/activity_id),
    both, or `ended: true` (the app ended the activity itself: drop that install's update tokens)."""
    if not isinstance(args, dict):
        raise PushError("invalid activity token")
    _activity_schema(store)
    env = args.get("environment")
    install = str(args.get("install_id") or "")[:100]
    start = args.get("push_to_start_token")
    token = args.get("token") or args.get("push_token")
    now = time.time()
    store.exec("DELETE FROM push_activities WHERE updated_at<?", (now - 86400,))
    if args.get("ended") and install:
        store.exec("DELETE FROM push_activities WHERE install_id=?", (install,))
        if not start and not token:
            return {"ok": True, "start_token": False, "activity_token": False}
    if start is None and token is None:
        raise PushError("invalid activity token")
    if env not in ("development", "production"):
        raise PushError("invalid activity token")
    out = {"ok": True, "start_token": False, "activity_token": False}
    if start is not None:
        if not install or not isinstance(start, str) or not _HEX_RX.fullmatch(start):
            raise PushError("invalid push-to-start token")
        store.exec("INSERT INTO push_start_tokens(install_id,token,environment,updated_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(install_id) DO UPDATE SET token=excluded.token,environment=excluded.environment,"
                   "updated_at=excluded.updated_at", (install, start.lower(), env, now))
        out["start_token"] = True
    if token is not None:
        if not isinstance(token, str) or not _HEX_RX.fullmatch(token):
            raise PushError("invalid activity token")
        aid = str(args.get("activity_id") or "")[:100]
        if install and aid:   # a newer activity of the same install replaces its older update token
            store.exec("DELETE FROM push_activities WHERE install_id=? AND activity_id=? AND token<>?", (install, aid, token.lower()))
        store.exec("INSERT INTO push_activities(token,environment,updated_at,install_id,activity_id) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(token) DO UPDATE SET updated_at=excluded.updated_at,environment=excluded.environment,"
                   "install_id=excluded.install_id,activity_id=excluded.activity_id", (token.lower(), env, now, install, aid))
        out["activity_token"] = True
    return out


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


def update_activities(store, state, stale_in=300, priority=10):
    """liveactivity UPDATE push to every registered activity (app in the background).
    `state` is the ContentState dict; startedAt is seconds since 2001-01-01."""
    rows = store.rows("SELECT * FROM push_activities")
    if not rows:
        return 0
    config = _config(store)
    auth = _provider_token(config)
    now = int(time.time())
    sent = 0
    raw = json.dumps({"aps": {"timestamp": now, "event": "update", "stale-date": now + stale_in, "content-state": state}}).encode()
    headers = {"authorization": "bearer " + auth, "apns-topic": config["bundle_id"] + ".push-type.liveactivity",
               "apns-push-type": "liveactivity", "apns-priority": str(priority)}
    for row in rows:
        try:
            code, _ = _post(_host(row["environment"]), row["token"], headers, raw)
        except PushError:
            continue
        sent += code == 200
        if code == 410:          # the activity is gone on the device
            store.exec("DELETE FROM push_activities WHERE token=?", (row["token"],))
    return sent


def has_activities(store):
    return bool(store.one("SELECT 1 AS x FROM push_activities LIMIT 1"))


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
            try:   # a coding agent still working keeps the island up
                from jb_coding import live_coding_count
                if live_coding_count(store):
                    return
            except Exception:
                pass
            end_activities(store)
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


# ---- Live Activity push-to-start / update / end (feature-api.md section 4) ---------------

DEAD_REASONS = ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken")
START_EVERY, UPDATE_EVERY, STRUCT_EVERY, KEEPALIVE = 60, 3, 15, 120
WAKE = threading.Event()      # set by coding_event: the island loop re-checks within about a second


def has_start_tokens(store):
    _activity_schema(store)
    return bool(store.one("SELECT 1 AS x FROM push_start_tokens LIMIT 1"))


def _relevance(state):
    return 100 if state.get("waiting") else 80 if any(r.get("kind") == "coding" for r in state.get("runs", [])) else 50


class LiveActivityPusher:
    """Decides which start/update/end liveactivity pushes to send. `send(env, token, payload, priority)`
    returns (http_code, text); the default talks to APNs, tests inject a mock."""

    def __init__(self, store, send=None, scene=None):
        self.store, self.scene, self._send = store, scene, send
        self.last_sig = None
        self.last_struct = None
        self.last_update = self.last_struct_push = 0.0
        self.last_start = {}
        self.started = None
        self.seen_tokens = set()

    def _default_send(self, env, token, payload, priority):
        config = _config(self.store)
        auth = _provider_token(config)
        headers = {"authorization": "bearer " + auth, "apns-topic": config["bundle_id"] + ".push-type.liveactivity",
                   "apns-push-type": "liveactivity", "apns-priority": str(priority)}
        return _post(_host(env), token, headers, json.dumps(payload, ensure_ascii=False).encode())

    def _push(self, kind, row, payload, priority):
        try:
            code, text = (self._send or self._default_send)(row["environment"], row["token"], payload, priority)
        except PushError:
            return False
        if code == 200:
            return True
        if code == 410 or _reason(text) in DEAD_REASONS:
            table = "push_start_tokens" if kind == "start" else "push_activities"
            self.store.exec("DELETE FROM %s WHERE token=?" % table, (row["token"],))
        return False

    def state(self, runs, now):
        """ContentState from island runs (each may carry subagents/waiting)."""
        runs = list(runs)
        self.started = self.started or now
        st = {"count": len(runs), "runs": runs[:4], "startedAt": self.started - 978307200, "finished": None, "headline": None,
              "subagents": sum(int(r.get("subagents") or 0) for r in runs),
              "waiting": sum(1 for r in runs if r.get("waiting"))}
        if runs and all(r.get("kind") == "reply" for r in runs):
            st["headline"] = "JoeBro is replying" if len(runs) == 1 else "%d chats replying" % len(runs)
        return st

    def tick(self, runs, now=None):
        """One pass. Returns {'start': n, 'update': n, 'end': n} pushes accepted."""
        now = now or time.time()
        _activity_schema(self.store)
        out = {"start": 0, "update": 0, "end": 0}
        if not runs:
            if self.last_sig not in (None, "[]"):
                out["end"] = self._end(now)
            self.last_sig, self.last_struct, self.started = "[]", None, None
            return out
        st = self.state(runs, now)
        sig = json.dumps([(r["id"], str(r.get("detail", ""))[:40], r.get("queued"), r.get("subagents", 0), bool(r.get("waiting"))) for r in runs])
        struct = json.dumps([(r["id"], r.get("subagents", 0), bool(r.get("waiting"))) for r in runs])
        installs_with_activity = {r["install_id"] for r in self.store.rows("SELECT install_id FROM push_activities") if r["install_id"]}
        for row in self.store.rows("SELECT * FROM push_start_tokens"):
            if row["install_id"] in installs_with_activity or now - self.last_start.get(row["install_id"], 0) < START_EVERY:
                continue
            self.last_start[row["install_id"]] = now
            attrs = {"id": "agents", "destination": "joebro://agents"}
            if self.scene:
                attrs["sceneName"] = str(self.scene)
            payload = {"aps": {"timestamp": int(now), "event": "start", "attributes-type": "JoeBroActivityAttributes",
                               "attributes": attrs, "content-state": st, "stale-date": int(now) + 900,
                               "relevance-score": _relevance(st),
                               "alert": {"title": "JoeBro", "body": "%s started" % (runs[0].get("title") or "Agent")}}}
            out["start"] += self._push("start", row, payload, 10)
        changed = sig != self.last_sig
        fresh = {r["token"] for r in self.store.rows("SELECT token FROM push_activities")} - self.seen_tokens
        if fresh and now - self.last_update >= UPDATE_EVERY:    # a just-started activity gets the current state
            changed = True
            self.seen_tokens |= fresh
        if (changed and now - self.last_update >= UPDATE_EVERY) or now - self.last_update >= KEEPALIVE:
            structural = struct != self.last_struct
            prio = 10 if structural and now - self.last_struct_push >= STRUCT_EVERY else 5
            payload = {"aps": {"timestamp": int(now), "event": "update", "stale-date": int(now) + 300,
                               "relevance-score": _relevance(st), "content-state": st}}
            rows = self.store.rows("SELECT * FROM push_activities")
            for row in rows:
                out["update"] += self._push("update", row, payload, prio)
            if rows or not changed:
                self.last_update = now
                self.last_sig, self.last_struct = sig, struct
                if prio == 10 and structural:
                    self.last_struct_push = now
            elif not rows:
                self.last_sig, self.last_struct = sig, struct   # nothing to update: don't retry every tick
        return out

    def _end(self, now):
        st = {"count": 0, "runs": [], "startedAt": (self.started or now) - 978307200, "headline": "JoeBro finished",
              "finished": "done", "subagents": 0, "waiting": 0}
        payload = {"aps": {"timestamp": int(now), "event": "end", "dismissal-date": int(now) + 300, "content-state": st}}
        n = 0
        for row in self.store.rows("SELECT * FROM push_activities"):
            n += self._push("end", row, payload, 10)
            self.store.exec("DELETE FROM push_activities WHERE token=?", (row["token"],))
        return n
