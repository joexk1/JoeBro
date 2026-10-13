"""Phone access: pairing, the separate LAN listener, and what the phone may not do.

The normal server (127.0.0.1:8765) is untouched and token-less, so the Mac app
needs no change. Phone access is a SECOND listener, off until the user turns it on,
that shares the same store but ALWAYS demands a bearer token and refuses to start
without one. It binds the Tailscale address by default (the one bind that is not
visible on the local Wi-Fi). Admin routes that could run code on the Mac are
refused on it even with a valid token, so a lost phone is not a shell on the Mac:
  * /api/mobile/* (pairing, the token itself, the APNs key) is loopback only,
    and answers 404 on the phone listener (except /api/mobile/hello);
  * writes to /api/mcp*, /api/plugins*, /api/tools*, the model endpoints, the
    email and calendar connections, and the secret/mobile_* prefs are 403;
  * chat permission is clamped to sandbox unless the user switched on
    "Let the phone agent run commands" (pref mobile_allow_full).
The token lives in <data dir>/mobile.json (mode 0600) and is shown only by the
loopback GET /api/mobile.
"""
import ipaddress
import re
import secrets
import time

from jb_core import *  # noqa: F401,F403
from jb_core import data_dir

LAN_PORT = 8766
TAILSCALE_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
BIND_MODES = ("tailscale", "lan", "all")
SECRET_PREF_KEYS = {"bot_token", "caldav_password", "calendar_connection", "bot_allowed_ids",
                    "bot_enabled", "bot_push_chat_id"}
SECRET_PREF_PREFIXES = ("apns_", "mobile_", "advisor_", "macos_")
_TS_CACHE = {"at": 0.0, "val": (None, None)}


# ---- mobile.json ------------------------------------------------------------------

def mobile_path(store):
    return data_dir(store) / "mobile.json"


def load_mobile(store):
    try:
        cfg = json.loads(mobile_path(store).read_text())
        if not isinstance(cfg, dict):
            raise ValueError()
    except (OSError, ValueError):
        cfg = {}
    return {"enabled": bool(cfg.get("enabled")), "token": str(cfg.get("token") or ""),
            "port": int(cfg.get("port") or LAN_PORT), "mode": cfg.get("mode") if cfg.get("mode") in BIND_MODES else "tailscale",
            "bind": str(cfg.get("bind") or ""), "created_at": cfg.get("created_at") or ""}


def save_mobile(store, cfg):
    path = mobile_path(store)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def new_token():
    return secrets.token_urlsafe(32)      # 43 URL-safe characters


# ---- addresses --------------------------------------------------------------------

def _run(argv, timeout=3):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def tailscale_info():
    """(ipv4, MagicDNS name) from the Tailscale CLI, or (None, None). Best effort,
    silent on failure, cached for 30 seconds."""
    now = time.time()
    if now - _TS_CACHE["at"] < 30:
        return _TS_CACHE["val"]
    binary = TAILSCALE_BIN if os.access(TAILSCALE_BIN, os.X_OK) else (shutil.which("tailscale") or "")
    ip, dns = None, None
    if binary:
        first = _run([binary, "ip", "-4"]).split()
        if first and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", first[0]):
            ip = first[0]
        try:
            name = (json.loads(_run([binary, "status", "--json"]) or "{}").get("Self") or {}).get("DNSName") or ""
            dns = name.rstrip(".") or None
        except ValueError:
            pass
    _TS_CACHE.update(at=now, val=(ip, dns))
    return ip, dns


def lan_ip():
    """This Mac's primary IPv4 address. Connecting a UDP socket sends nothing."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None


def mac_name():
    name = _run(["/usr/sbin/scutil", "--get", "ComputerName"]).strip()
    return name or socket.gethostname().split(".")[0] or "Mac"


def bind_address(mode):
    """The address to listen on for a mode, or raises ValueError with what to fix."""
    if mode == "all":
        return "0.0.0.0"
    if mode == "lan":
        ip = lan_ip()
        if not ip:
            raise ValueError("No Wi-Fi/LAN address found; connect to a network first")
        return ip
    ip = tailscale_info()[0]
    if not ip:
        raise ValueError("Tailscale is not running on this Mac. Install and sign in to Tailscale, "
                         "or choose Wi-Fi in Reachable over")
    return ip


def pair_urls(cfg):
    port = cfg["port"]
    urls = []
    lan = lan_ip()
    ts_ip, ts_dns = tailscale_info()
    if cfg["mode"] in ("lan", "all") and lan:
        urls.append({"label": "LAN", "url": f"http://{lan}:{port}"})
    if cfg["mode"] in ("tailscale", "all") and ts_ip:
        urls.append({"label": "Tailscale", "url": f"http://{ts_ip}:{port}"})
        if ts_dns:
            urls.append({"label": "Tailscale name", "url": f"http://{ts_dns}:{port}"})
    return urls


def pair_uri(url, token, name):
    q = urllib.parse.quote
    return f"joebro://pair?u={q(url, safe='')}&t={q(token, safe='')}&n={q(name, safe='')}"


# ---- the second listener ------------------------------------------------------------

class LanListener:
    """Owns the phone-facing ThreadingHTTPServer so it can be stopped and started
    in-process when the user flips the switch."""

    def __init__(self):
        self.server = None
        self.thread = None
        self.address = None
        self.error = ""

    def running(self):
        return self.server is not None

    def start(self, handler_cls, store, cfg):
        if not cfg.get("token"):
            raise ValueError("phone access needs a token and none is set")   # never start token-less
        self.stop()
        addr = cfg.get("host") or bind_address(cfg["mode"])     # "host" = explicit --lan-host
        try:
            server = ThreadingHTTPServer((addr, cfg["port"]), handler_cls)
        except OSError as exc:
            raise ValueError(f"could not listen on {addr}:{cfg['port']} ({exc.strerror or exc})") from None
        server.store = store
        server.allowed_hosts = None        # any Host: the token replaces the rebinding check
        server.auth_token = cfg["token"]
        server.is_lan = True
        self.server, self.address = server, (addr, cfg["port"])
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def set_token(self, token):
        if self.server is not None:
            self.server.auth_token = token

    def stop(self):
        server, self.server, self.address = self.server, None, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass


LAN = LanListener()


def start_lan_if_enabled(handler_cls, store):
    """Called at boot: bring the phone listener up if the user left it on."""
    cfg = load_mobile(store)
    if cfg["enabled"] and cfg["token"]:
        try:
            LAN.start(handler_cls, store, cfg)
            print(f"Phone access listening on http://{LAN.address[0]}:{LAN.address[1]} (token required)", flush=True)
        except ValueError as exc:
            LAN.error = str(exc)
            print("Phone access is on but could not start:", exc, flush=True)


# ---- what a LAN request may do ---------------------------------------------------------

def lan_denied(method, path):
    """(status, detail) when the phone listener must refuse this request, else None."""
    m = method.upper()
    if path.startswith("/api/mobile") and path != "/api/mobile/hello":
        return 404, "Not found"
    if m == "GET":
        return None
    if re.match(r"/api/(mcp|plugins|tools)(/|$)", path):
        return 403, "This can only be changed on the Mac"
    if path.startswith("/api/model-endpoints") and not (m == "PUT" and path.endswith("/models")):
        return 403, "This can only be changed on the Mac"
    if re.match(r"/api/(email/(connect|disconnect)|calendar/(connect|disconnect))", path):
        return 403, "This can only be changed on the Mac"
    if m in ("PUT", "POST") and path.startswith("/api/prefs/"):
        key = path.rsplit("/", 1)[-1]
        if key in SECRET_PREF_KEYS or key.startswith(SECRET_PREF_PREFIXES):
            return 403, "This setting can only be changed on the Mac"
    return None


def clamp_form(form, allow_full):
    """Hold a phone-originated chat request at sandbox unless the user allowed more."""
    if not allow_full:
        if str(form.get("permission_mode") or "").lower() == "full":
            form["permission_mode"] = "sandbox"
        form.pop("allow_bash", None)
    return form


def _on(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_multipart_fields(content_type, body):
    """{name: str | (filename, bytes)} from a multipart/form-data body."""
    raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        data = part.get_payload(decode=True) or b""
        fn = part.get_filename()
        out[name] = (fn, data) if fn else data.decode("utf-8", "replace").strip()
    return out


# ---- the handler methods (loopback listener) ---------------------------------------------

class MobileMixin:
    def mobile_status(self):
        from jb_push import push_status, list_devices
        cfg = load_mobile(self.store)
        urls = pair_urls(cfg) if cfg["token"] else []
        name = mac_name()
        first = urls[0]["url"] if urls else ""
        return {"enabled": cfg["enabled"], "running": LAN.running(), "port": cfg["port"],
                "mode": cfg["mode"], "bind": cfg["bind"] or (LAN.address[0] if LAN.address else ""),
                "token_set": bool(cfg["token"]), "token": cfg["token"], "name": name,
                "urls": urls,
                "pair_uri": pair_uri(first, cfg["token"], name) if first and cfg["token"] else "",
                "error": LAN.error, "devices": len(list_devices(self.store)),
                "push": push_status(self.store)}

    def mobile_enable(self, body):
        cfg = load_mobile(self.store)
        enabled = bool(body.get("enabled"))
        mode = body.get("bind") or cfg["mode"]
        if mode not in BIND_MODES:
            return {"ok": False, "detail": "bind must be tailscale, lan or all"}, 400
        cfg["mode"] = mode
        if not enabled:
            cfg["enabled"] = False
            save_mobile(self.store, cfg)
            LAN.stop()
            LAN.error = ""
            return {"ok": True, **self.mobile_status()}, 200
        if not cfg["token"]:
            cfg["token"], cfg["created_at"] = new_token(), now_iso()
        try:
            cfg["bind"] = bind_address(mode)
            cfg["enabled"] = True
            save_mobile(self.store, cfg)
            LAN.start(type(self), self.store, cfg)
            LAN.error = ""
        except ValueError as exc:
            cfg["enabled"] = False
            save_mobile(self.store, cfg)
            LAN.stop()
            LAN.error = str(exc)
            return {"ok": False, "detail": str(exc)}, 400
        return {"ok": True, **self.mobile_status()}, 200

    def mobile_rotate(self):
        cfg = load_mobile(self.store)
        cfg["token"], cfg["created_at"] = new_token(), now_iso()
        save_mobile(self.store, cfg)
        LAN.set_token(cfg["token"])            # the old token stops working immediately
        status = self.mobile_status()
        return {"ok": True, "pair_uri": status["pair_uri"], "token": cfg["token"]}

    def mobile_hello(self):
        from jb_push import push_status
        return {"ok": True, "name": mac_name(), "version": BACKEND_VERSION,
                "features": {"phone_agent": True, "push": bool(push_status(self.store).get("configured")),
                             "spend": False, "location": _on(self.pref("location_enabled")),
                             "muse": True, "heartbeat": _on(self.pref("heartbeat_enabled"))}}

    def mobile_apns_save(self):
        from jb_push import _BUNDLE_RE, sign_es256, _JWT
        fields = parse_multipart_fields(self.headers.get("content-type", ""), self._body_bytes())
        team, key_id, bundle = fields.get("team_id", ""), fields.get("key_id", ""), fields.get("bundle_id", "")
        key = fields.get("key")
        if not isinstance(key, tuple) or not key[1]:
            return {"ok": False, "detail": "choose the AuthKey .p8 file"}, 400
        if not re.fullmatch(r"[A-Z0-9]{10}", str(team)):
            return {"ok": False, "detail": "Team ID is 10 letters and digits"}, 400
        if not re.fullmatch(r"[A-Z0-9]{10}", str(key_id)):
            return {"ok": False, "detail": "Key ID is 10 letters and digits"}, 400
        if not _BUNDLE_RE.fullmatch(str(bundle)):
            return {"ok": False, "detail": "Bundle ID looks wrong (for example com.yourname.joebro)"}, 400
        pem = key[1]
        if len(pem) > 8192 or b"PRIVATE KEY" not in pem:
            return {"ok": False, "detail": "that file is not an Apple .p8 private key"}, 400
        root = data_dir(self.store)
        keydir = root / "apns"
        keydir.mkdir(exist_ok=True, mode=0o700)
        keypath = keydir / f"AuthKey_{key_id}.p8"
        fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        os.chmod(keypath, 0o600)
        try:
            sign_es256(keypath, b"joebro-check")       # prove openssl can use this key
        except ValueError as exc:
            keypath.unlink(missing_ok=True)
            return {"ok": False, "detail": f"The key could not be used: {exc}"}, 400
        cfg = {"team_id": team, "key_id": key_id, "key_path": str(keypath), "bundle_id": bundle}
        cpath = root / "apns.json"
        fd = os.open(cpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.chmod(cpath, 0o600)
        _JWT.update(signature=None, token=None, at=0)
        from jb_push import push_status
        return {"ok": True, "push": push_status(self.store)}, 200

    def mobile_apns_delete(self):
        from jb_push import _JWT
        root = data_dir(self.store)
        (root / "apns.json").unlink(missing_ok=True)
        keydir = root / "apns"
        if keydir.is_dir() and not keydir.is_symlink():
            shutil.rmtree(keydir, ignore_errors=True)
        _JWT.update(signature=None, token=None, at=0)
        return {"ok": True}
