"""Any source -> artifact -> any sink, for the phone agent (hub: artifact_store).

Three built-in tools: export_to_artifact(source), deliver_artifact(artifact_id, to),
move_file(source, to) = both. URIs:
  sources: url:https://..  upload:up_<hex>  doc:doc_<hex>[?format=pdf|md|txt]
           device:/abs/path  <root>:/rel/path  artifact:<id>
  sinks:   <root>:/rel/path (trailing / = keep the name)  email:<addr>[,<addr>]
           doc:new/<title>  device:/abs/path  artifact:<id>
<root> is a folder named in artifact_roots.json (default: documents, downloads).
`device:` reads and writes anywhere on this Mac outside a deny list, so it needs
a Full Access session; everything else stays inside the store or a listed folder.
An outbound send (email) never fires on the first call: it returns a preview and
a single-use confirm_token that the agent passes back only after the user agrees.
Read-only sessions never see these tools (WRITE_SIDE_EFFECT_TOOLS).
"""
import hashlib, hmac, ipaddress, json, mimetypes, os, re, socket, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path, PurePosixPath

import artifact_store as store
from artifact_store import ArtifactError, safe_name

MAX_BYTES = store.MAX_BYTES  # 50 MiB
EMAIL_MAX = 25 * 1024 * 1024
OUTBOUND = {"email"}
TOOL_NAMES = {"export_to_artifact", "deliver_artifact", "move_file"}
_SECRET = os.urandom(32)
_USED = {}
_LOCK = threading.Lock()


class TransferError(ValueError):
    pass


# ----------------------------------------------------------------------------- URIs
def _rel(path, what="path"):
    path = urllib.parse.unquote(path or "").replace("\\", "/")
    if "\x00" in path or any(ord(c) < 32 for c in path):
        raise TransferError(f"{what} contains control characters")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise TransferError(f"{what} cannot contain '..'")
    return "/".join(parts)


def parse_uri(uri, role, roots=()):
    """Parse a transfer URI into a dict. role is 'source' or 'sink'. Pure: no I/O."""
    uri = str(uri or "").strip()
    if not uri:
        raise TransferError(f"{role} URI is required")
    if re.match(r"https?://", uri, re.I):
        uri = "url:" + uri
    if ":" not in uri:
        raise TransferError(f"{role} must look like scheme:rest, e.g. documents:/Notes/a.txt")
    scheme, rest = uri.split(":", 1)
    scheme = scheme.lower()
    if scheme == "url":
        if role != "source":
            raise TransferError("url: is a source only")
        if not re.match(r"https?://", rest, re.I):
            raise TransferError("url: needs an http(s) address")
        return {"kind": "url", "url": rest}
    rest, _, query = rest.partition("?")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    if scheme == "email":
        if role == "source":
            raise TransferError("email attachments are not a transfer source; use the email tools")
        addrs = [a.strip() for a in urllib.parse.unquote(rest).split(",") if a.strip()]
        if not addrs or len(addrs) > 10 or not all(re.fullmatch(r"[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}", a) for a in addrs):
            raise TransferError("email sink is email:<address>[,<address>] (max 10)")
        return {"kind": "email", "to": ",".join(addrs), "subject": q.get("subject", ""), "body": q.get("body", "")}
    if scheme == "upload":
        if role != "source" or not re.fullmatch(r"up_[0-9a-fA-F]{16}", rest):
            raise TransferError("upload source is upload:up_<16 hex> (a chat attachment id)")
        return {"kind": "upload", "id": rest}
    if scheme == "artifact":
        if not re.fullmatch(r"[a-f0-9]{32}", rest):
            raise TransferError("artifact: needs a 32-hex artifact_id")
        return {"kind": "artifact", "artifact_id": rest}
    if scheme == "doc":
        if role == "source":
            if not re.fullmatch(r"doc_[0-9a-f]{16}", rest):
                raise TransferError("doc source is doc:doc_<16 hex>[?format=pdf|md]")
            fmt = (q.get("format") or "md").lower()
            if fmt not in ("md", "txt", "pdf"):
                raise TransferError("doc format must be md, txt or pdf")
            return {"kind": "doc", "id": rest, "format": fmt}
        m = re.fullmatch(r"new/(.{1,120})", rest)
        if not m:
            raise TransferError("doc sink is doc:new/<title>")
        return {"kind": "doc", "title": urllib.parse.unquote(m.group(1)).strip()}
    if scheme == "device":
        if not rest.startswith("/"):
            raise TransferError("device is device:/absolute/path on this Mac")
        path = urllib.parse.unquote(rest)
        if "\x00" in path or "\n" in path or "\r" in path or ".." in PurePosixPath(path).parts:
            raise TransferError("device path cannot contain '..' or control characters")
        path = str(PurePosixPath(path)) + ("/" if path.endswith("/") and len(path) > 1 else "")
        return {"kind": "device", "path": path}
    if scheme in roots:  # documents, downloads, ...
        rel = _rel(rest, "path")
        return {"kind": "folder", "root": scheme, "rel": rel, "is_dir": rest.endswith("/") or not rel}
    raise TransferError(f"unknown {role} scheme '{scheme}'")


def split_folder_target(parsed, filename=None):
    rel = parsed["rel"]
    if parsed["is_dir"]:
        return rel, filename
    folder, _, name = rel.rpartition("/")
    return folder, filename or name


def is_outbound(parsed):
    return parsed["kind"] in OUTBOUND


# ----------------------------------------------------------------------------- confirmation
def _digest(artifact_id, sha, dest, extra):
    blob = json.dumps([artifact_id, sha, dest, extra], sort_keys=True)
    return hmac.new(_SECRET, blob.encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(artifact_id, sha, dest, extra):
    d = _digest(artifact_id, sha, dest, extra)
    with _LOCK:
        for k in [k for k, v in _USED.items() if v < time.time()]:
            _USED.pop(k)
        _USED[d] = time.time() + 600
    return d


def redeem_token(token, artifact_id, sha, dest, extra):
    d = _digest(artifact_id, sha, dest, extra)
    with _LOCK:
        ok = bool(token) and hmac.compare_digest(str(token), d) and _USED.get(d, 0) > time.time()
        if ok:
            _USED.pop(d)  # single use
    return ok


# ----------------------------------------------------------------------------- URL fetch (SSRF-safe)
def _check_host(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise TransferError("only plain http(s) URLs are allowed")
    if p.port not in (None, 80, 443, 8080, 8443):
        raise TransferError("URL port is not allowed")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise TransferError("URL host does not resolve") from None
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            raise TransferError("URL points at a private or local address")


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url, tag="url", filename=None):
    _check_host(url)
    opener = urllib.request.build_opener(_Redirect())
    req = urllib.request.Request(url, headers={"User-Agent": "JoeBro-Transfer/1.0", "Accept": "*/*"})
    try:
        with opener.open(req, timeout=30) as resp:
            if int(resp.headers.get("Content-Length") or 0) > MAX_BYTES:
                raise TransferError("file exceeds the 50 MiB limit")
            data = resp.read(MAX_BYTES + 1)
            mime = resp.headers.get_content_type()
            cd = resp.headers.get("Content-Disposition") or ""
            final = resp.geturl()
    except TransferError:
        raise
    except urllib.error.HTTPError as exc:
        raise TransferError(f"download failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise TransferError("download failed") from None
    if len(data) > MAX_BYTES:
        raise TransferError("file exceeds the 50 MiB limit")
    if not data:
        raise TransferError("download was empty")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    name = filename or (urllib.parse.unquote(m.group(1)) if m else "") or Path(urllib.parse.urlsplit(final).path).name or "download"
    name = safe_name(name)
    if not Path(name).suffix and mime:
        name += mimetypes.guess_extension(mime) or ""
    return store.save_bytes(data, name, mime, source_url=final, source=tag)


# ----------------------------------------------------------------------------- this Mac (device:)
def _deny_segments():
    """Places that hold credentials, startup items or JoeBro's own data. Matched
    anywhere in the path, case-insensitively."""
    return ("/.ssh", "/.gnupg", "/.aws", "/.config", "/.joebro", "/library/keychains",
            "/library/launchagents", "/library/launchdaemons", "/library/application support/com.apple.tcc",
            "/library/application support/joebro")


_DEVICE_SYSTEM = ("/etc", "/system", "/usr", "/bin", "/sbin", "/private/etc", "/var", "/dev", "/proc", "/sys", "/boot", "/root")


def device_check(path, write):
    low = path.lower().rstrip("/") + "/"
    if any(seg + "/" in low or low.endswith(seg + "/") for seg in _deny_segments()):
        raise TransferError("that location holds credentials, startup items or JoeBro's own data and is off limits")
    try:
        if str(Path(path).resolve()).startswith(str(store.data_dir())):
            raise TransferError("JoeBro's own data folder is off limits")
    except OSError:
        pass
    if write and (any(low == s + "/" or low.startswith(s + "/") for s in _DEVICE_SYSTEM)
                  or PurePosixPath(path).name.startswith((".zsh", ".bash", ".profile"))):
        raise TransferError("refusing to write to a system or shell-startup location")


def _need_full(h):
    form = getattr(h, "_turn_form", None) or {}
    if (form.get("permission_mode") or "") != "full":
        raise TransferError("device: paths on this Mac need a Full Access chat; use a folder from artifact_roots instead")


def export_device(h, p):
    _need_full(h)
    device_check(p["path"], False)
    path = Path(p["path"])
    if path.is_symlink() or not path.is_file():
        raise TransferError(f"file not found: {p['path']}")
    if path.stat().st_size > MAX_BYTES:
        raise TransferError("file exceeds the 50 MiB limit")
    return store.save_bytes(path.read_bytes(), path.name, source="device")


def deliver_device(h, p, ref, data, filename):
    _need_full(h)
    path = p["path"]
    if path.endswith("/"):
        path += safe_name(filename or ref["filename"])
    device_check(path, True)
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise TransferError("destination exists; choose another filename")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return {"delivered": "device", "path": str(target), "size_bytes": len(data)}


# ----------------------------------------------------------------------------- email
def send_email_with_artifact(h, p, ref, data):
    if len(data) > EMAIL_MAX:
        raise TransferError("attachment exceeds the 25 MB email limit")
    uid = "up_" + os.urandom(8).hex()
    name = safe_name(ref["filename"])
    updir = h.store.root / "uploads"
    updir.mkdir(exist_ok=True)
    dest = updir / f"{uid}-{name}"
    dest.write_bytes(data)
    h.store.exec("insert into uploads(id,name,path,mime,created_at) values(?,?,?,?,?)",
                 (uid, name, str(dest), ref["mime_type"], time.strftime("%Y-%m-%dT%H:%M:%S")))
    try:
        out = h.email_send_payload({"to": p["to"], "subject": p["subject"], "body": p["body"], "attachment_tokens": [uid]})
    finally:
        h.store.exec("delete from uploads where id=?", (uid,))
        dest.unlink(missing_ok=True)
    return {"delivered": "email", "to": p["to"], "attachment": name, "result": json.loads(out)}


# ----------------------------------------------------------------------------- export / deliver
def _roots():
    try:
        return set(store.configured_roots())
    except ArtifactError:
        return set()


def export_to_artifact(h, source, filename=None):
    p = parse_uri(source, "source", _roots())
    k = p["kind"]
    if k == "artifact":
        ref = store.artifact_info(p["artifact_id"])
    elif k == "url":
        ref = fetch_url(p["url"], filename=filename)
    elif k == "folder":
        if not p["rel"] or p["is_dir"]:
            raise TransferError("source must name a file, not a folder")
        ref = store.import_file(p["root"], p["rel"], tag="folder")
    elif k == "device":
        ref = export_device(h, p)
    elif k == "upload":
        row = h.store.one("select name,path,mime from uploads where id=?", (p["id"],))
        path = Path(row["path"]).resolve() if row else None
        if not row or path.parent != (h.store.root / "uploads").resolve() or not path.is_file():
            raise TransferError("attachment not found or expired")
        if path.stat().st_size > MAX_BYTES:
            raise TransferError("file exceeds the 50 MiB limit")
        ref = store.save_bytes(path.read_bytes(), row["name"], row.get("mime") or None, source="upload")
    elif k == "doc":
        row = h.store.one("select title,current_content from documents where id=?", (p["id"],))
        if not row:
            raise TransferError("document not found")
        title = safe_name(row.get("title") or "document")
        title = Path(title).stem if Path(title).suffix.lower() in (".md", ".txt", ".pdf") else title
        if p["format"] == "pdf":
            ref = store.save_bytes(h.simple_pdf(row.get("title") or "Document", row.get("current_content") or ""), title + ".pdf", "application/pdf", source="doc")
        else:
            ref = store.save_bytes((row.get("current_content") or "").encode(), f"{title}.{p['format']}", "text/plain", source="doc")
    else:
        raise TransferError(f"{k} cannot be a source")
    if filename and k not in ("url",) and safe_name(filename) != ref.get("filename"):
        ref = dict(ref, suggested_filename=safe_name(filename))
    return ref


def deliver_artifact(h, artifact_id, to, a):
    """a: filename, subject, body, confirm_token."""
    p = parse_uri(to, "sink", _roots())
    k = p["kind"]
    ref = store.artifact_info(artifact_id)
    filename = a.get("filename")
    if filename and safe_name(filename) != filename:
        raise TransferError("filename must be a simple safe basename")
    if k == "email":
        p["subject"] = a.get("subject") or p["subject"] or ref["filename"]
        p["body"] = a.get("body") or p["body"] or f"Attached: {ref['filename']}"
    if is_outbound(p):
        dest = {key: p[key] for key in sorted(p) if key != "kind"}
        extra = [k, filename or ""]
        if not redeem_token(a.get("confirm_token"), artifact_id, ref["sha256"], dest, extra):
            return {"status": "needs_confirmation", "sent": False,
                    "preview": {"channel": k, "to": p.get("to"), "file": filename or ref["filename"],
                                "mime_type": ref["mime_type"], "size_bytes": ref["size_bytes"],
                                "subject": p["subject"], "body": p["body"]},
                    "confirm_token": issue_token(artifact_id, ref["sha256"], dest, extra),
                    "next": "NOTHING WAS SENT. Show the user the recipient, file and text and ask. Only after they say yes, call again with the same arguments plus this confirm_token (valid 10 min, single use)."}
    path = store.resolve_artifact(artifact_id)
    if k == "artifact":
        return {"delivered": "artifact", "artifact": ref}
    if k == "folder":
        folder, name = split_folder_target(p, filename)
        r = store.copy_to_folder(artifact_id, p["root"], folder, name)
        return {"delivered": "folder", "root": p["root"], "path": r["path"], "size_bytes": r["size_bytes"]}
    data = path.read_bytes()
    if k == "device":
        return deliver_device(h, p, ref, data, filename)
    if k == "email":
        return send_email_with_artifact(h, p, dict(ref, filename=filename or ref["filename"]), data)
    if k == "doc":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise TransferError("only text files can become documents") from None
        if len(data) > 1_000_000:
            raise TransferError("document text is limited to 1 MB")
        doc_id, ts = "doc_" + os.urandom(8).hex(), time.strftime("%Y-%m-%dT%H:%M:%S")
        title = p["title"] or "Untitled"
        h.store.exec("""insert into documents(id,title,language,current_content,version_count,file_path,archived,created_at,updated_at)
                        values(?,?,?,?,?,?,?,?,?)""", (doc_id, title, Path(title).suffix.lstrip(".") or "markdown", text, 1, "", 0, ts, ts))
        return {"delivered": "document", "document_id": doc_id, "title": title}
    raise TransferError(f"{k} cannot be a destination")


def move_file(h, source, to, a):
    kind = parse_uri(source, "source", _roots())["kind"]
    ref = export_to_artifact(h, source, a.get("filename") if kind == "url" else None)
    out = deliver_artifact(h, ref["artifact_id"], to, a)
    gone = False
    # The intermediate copy has served its purpose once actually delivered (not a
    # preview, not a sink that IS the store).
    if out.get("delivered") and out.get("delivered") != "artifact" and kind != "artifact":
        try:
            gone = store.discard_artifact(ref["artifact_id"])
        except Exception:
            gone = False
    return {"artifact_id": ref["artifact_id"], "source": source, **({"intermediate_discarded": True} if gone else {}), **out}


# ----------------------------------------------------------------------------- tool entry
_ALIASES = {"from": "source", "src": "source", "source_uri": "source", "dest": "to", "destination": "to", "to_uri": "to",
            "id": "artifact_id", "name": "filename", "message": "body", "text": "body"}


def run_transfer_tool(h, name, args):
    args = {_ALIASES.get(str(k), str(k)): v for k, v in (args or {}).items()}
    try:
        if name == "export_to_artifact":
            return json.dumps({"artifact": export_to_artifact(h, args.get("source"), args.get("filename")),
                               "next": "pass artifact_id to deliver_artifact"}, ensure_ascii=False)
        if name == "deliver_artifact":
            return json.dumps(deliver_artifact(h, str(args.get("artifact_id") or ""), args.get("to"), args), ensure_ascii=False)
        if name == "move_file":
            return json.dumps(move_file(h, args.get("source"), args.get("to"), args), ensure_ascii=False)
    except (TransferError, ArtifactError) as exc:
        raise ValueError(str(exc)) from None
    raise ValueError("unknown transfer tool " + name)
