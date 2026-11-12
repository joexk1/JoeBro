"""Workdir browsing, uploads, the folder picker, and device-bridge file routing."""
from jb_core import *  # noqa: F401,F403

# Folders a phone may never bind a chat or agent to, however it asks: credentials,
# keychains, launch items and JoeBro's own data. Matched anywhere in the path.
PHONE_DENY = ("/.ssh", "/.gnupg", "/.aws", "/.config", "/.joebro", "/library", "/.trash")


def phone_folder_check(folder):
    """A folder a phone-originated request may bind: it must resolve to somewhere
    inside the user's home, and not through a credentials or app-data folder.
    Returns the resolved path as a string; raises ValueError otherwise."""
    if not folder:
        return ""
    p = Path(str(folder)).expanduser()
    if not p.is_absolute():
        raise ValueError("folder must be an absolute path")
    real = p.resolve()
    home = Path.home().resolve()
    if real != home and not real.is_relative_to(home):
        raise ValueError("the phone can only use folders inside your home folder")
    low = str(real).lower() + "/"
    if real == home or any(seg + "/" in low for seg in PHONE_DENY):
        raise ValueError("that folder is off limits to the phone (credentials, app data or the whole home folder)")
    return str(real)


class FilesMixin:

    # --- bridge: route tools to a session's remote device when bound there ----
    def _session_host(self, form):
        sid = (form or {}).get("session") or ""
        host = ""
        workdir = ""
        if sid:
            row = self.store.one("select host_id, workdir from sessions where id=?", (sid,))
            host = ((row or {}).get("host_id") or "").strip()
            workdir = ((row or {}).get("workdir") or "").strip()
        # Only TRULY unbound chats (no host AND no bound folder) bind to the
        # device the request came from, so device tools / computer use act on the
        # local machine. A chat bound to a backend-local folder (host='' but
        # workdir set, e.g. a synced folder on the backend host) must stay on the backend —
        # otherwise its file ops get routed to the requesting device and fail.
        if not host and not workdir:
            host = ((form or {}).get("device") or (form or {}).get("host_id") or "").strip()
        return host

    def _device_bridge(self, host_id):
        """(bridge_url, token) for a remote device, or None when the host is the
        backend itself ('' or its own id, see LOCAL_HOST_IDS) or unknown."""
        if not host_id or host_id in LOCAL_HOST_IDS:
            return None
        row = self.store.one("select bridge_url, token, is_self from devices where id=?", (host_id,))
        if not row or row.get("is_self"):
            return None
        url = (row.get("bridge_url") or "").strip()
        return (url.rstrip("/"), (row.get("token") or "").strip() or None) if url else None

    def _remote_workdir(self, form):
        sid = (form or {}).get("session") or ""
        row = self.store.one("select workdir from sessions where id=?", (sid,)) if sid else None
        return ((row or {}).get("workdir") or "").strip()

    def _remote_path(self, form, rel):
        import posixpath
        rel = (rel or "").strip()
        if rel.startswith("/"):
            return posixpath.normpath(rel)
        base = self._remote_workdir(form)
        return posixpath.normpath(posixpath.join(base, rel)) if base else rel

    @staticmethod
    def _bridge_exec_output(host, body):
        if body is None:
            raise ValueError(f"device '{host}' bridge is unreachable")
        out = (body.get("stdout") or "").rstrip()
        err = (body.get("stderr") or "").rstrip()
        if err:
            out = (out + "\nSTDERR: " + err).strip() if out else "STDERR: " + err
        if (body.get("exit_code") or 0) != 0:
            # Surface the failure as a failure (matches local bash_tool), and name
            # the actual cause when it's macOS privacy blocking the bridge.
            hint = ("\nmacOS privacy is blocking the bridge on that device: give the bridge "
                    "process Full Disk Access (System Settings > Privacy & Security), then restart it. "
                    "Tell the user this — do not work around it."
                    if "Operation not permitted" in err else "")
            raise ValueError((out or f"command failed on '{host}'") + hint)
        return out or "(no output)"

    def _bridge_read_bytes(self, bridge, host, path):
        """Raw file bytes from a device — /v1/read_file is text-only, so pull
        base64 over exec."""
        url, token = bridge
        r = bridge_call(url, token, "/v1/exec", {"command": "base64 < " + shlex.quote(path)})
        if r is None or r.get("exit_code") != 0:
            raise ValueError(f"read {path} @ {host}: " + ((r or {}).get("stderr") or "bridge unreachable").strip())
        return base64.b64decode((r.get("stdout") or "").replace("\n", ""))

    def _bridge_docx_text(self, bridge, host, path):
        import tempfile
        data=self._bridge_read_bytes(bridge, host, path)
        with tempfile.NamedTemporaryFile(suffix=".docx") as file:
            file.write(data);file.flush()
            return read_doc_context(Path(file.name))

    def _bridge_apkg_text(self, bridge, host, path):
        """Read a .apkg ON A DEVICE as Q/A text (zip bytes would mangle as text)."""
        from jb_apkg import apkg_to_text
        return apkg_to_text(self._bridge_read_bytes(bridge, host, path))

    def _gather_sources_bridged(self, bridge, host, path):
        """Learn-from-sources for a folder that lives on a DEVICE (e.g. a laptop
        while this backend runs on a home server): list over exec, text over /v1/read_file,
        binary formats as base64 through the local converters."""
        import tempfile
        import time as _time
        url, token = bridge
        r = bridge_call(url, token, "/v1/exec",
                        {"command": "find " + shlex.quote(path) + " -maxdepth 2 -type f -size -5M | head -40"})
        if r is None or r.get("exit_code") != 0:
            raise ValueError(f"could not list that folder on '{host}': "
                             + ((r or {}).get("stderr") or "bridge unreachable").strip())
        docs = []
        deadline = _time.time() + 120   # pulling scans over the bridge must not stall the job
        for fp in (r.get("stdout") or "").splitlines():
            if _time.time() > deadline:
                break
            fp = fp.strip()
            ext = os.path.splitext(fp)[1].lower()
            if not fp or os.path.basename(fp).startswith(".") or ext not in self._SOURCE_EXTS:
                continue
            try:
                if ext in (".pdf", ".docx", ".doc", ".xlsx", ".apkg"):
                    data = self._bridge_read_bytes(bridge, host, fp)
                    with tempfile.NamedTemporaryFile(suffix=ext) as tf:
                        tf.write(data); tf.flush()
                        t = Path(tf.name)
                        text = self.read_pdf_text(t) if ext == ".pdf" else read_doc_text(t)
                else:
                    rr = bridge_call(url, token, "/v1/read_file", {"path": fp, "max_chars": 200_000})
                    text = (rr or {}).get("content") or ""
            except Exception:
                continue
            if (text or "").strip():
                docs.append((os.path.basename(fp), text.strip()))
            if len(docs) >= 24:
                break
        return docs


    def _bound_remote_path(self, sid, sub):
        import posixpath
        root = posixpath.normpath(self._remote_workdir({"session": sid}))
        if not root.startswith("/"):
            raise ValueError("No absolute folder bound to this session")
        target = posixpath.normpath(posixpath.join(root, sub or ""))
        if target != root and not target.startswith(root.rstrip("/") + "/"):
            raise ValueError("Path escapes workdir")
        return target

    def _bridge_write_bytes(self, bridge, host, path, data):
        """Write a real binary file atomically on the device, retaining a backup."""
        script = "import os,base64,tempfile,shutil,time,hashlib\np=" + repr(path) + "\nraw=base64.b64decode(" + repr(base64.b64encode(data).decode()) + ")\n" + """fd,tmp=tempfile.mkstemp(prefix='.joebro-',dir=os.path.dirname(p))
try:
 with os.fdopen(fd,'wb') as out:
  out.write(raw); out.flush(); os.fsync(out.fileno())
 if os.path.exists(p):
  shutil.copymode(p,tmp)
  backups=os.path.join(os.path.dirname(p),'.joebro-backups')
  os.makedirs(backups,exist_ok=True)
  tag=hashlib.sha256(p.encode()).hexdigest()[:20]+'-'
  shutil.copy2(p,os.path.join(backups,tag+str(time.time_ns())+'.bak'))
  old=sorted(n for n in os.listdir(backups) if n.startswith(tag))
  for name in old[:-5]: os.unlink(os.path.join(backups,name))
 os.replace(tmp,p)
finally:
 if os.path.exists(tmp): os.unlink(tmp)
"""
        url, token = bridge
        result = bridge_call(url, token, "/v1/exec", {"command": "python3 -c " + shlex.quote(script)})
        if result is None or result.get("exit_code") != 0:
            raise ValueError(f"write {path} @ {host}: " + ((result or {}).get("stderr") or "bridge unreachable").strip())

    def _bridge_apkg_write(self, bridge, host, path, text):
        """Write Q/A text to a device as a real .apkg — raw text over
        /v1/write_file would corrupt the deck.
        ponytail: deck bytes ride base64 inline in the command — fine at flashcard sizes."""
        from jb_apkg import text_to_apkg
        self._bridge_write_bytes(bridge, host, path,
                                 text_to_apkg(text, os.path.splitext(os.path.basename(path))[0]))

    def _remote_tool(self, bridge, host, name, body, form):
        """Run a file/bash tool on a remote device via its bridge agent."""
        url, token = bridge
        wd = self._remote_workdir(form)
        if name in ("bash", "python", "list_files"):
            if name == "bash":
                cmd = body
            elif name == "python":
                cmd = "python3 - <<'JBPY_EOF'\n" + (body or "") + "\nJBPY_EOF"
            else:
                # list_files: honour the sub-folder path (one level, folders marked /)
                rel = (self.parse_tool_fields(body).get("path") or "").strip()
                target = self._remote_path(form, rel) if rel else (wd or ".")
                cmd = "ls -1Ap " + shlex.quote(target)
            # bash/python tools may run a genuinely long command — keep a long timeout here only.
            return self._bridge_exec_output(host, bridge_call(url, token, "/v1/exec", {"command": cmd, "cwd": wd}, timeout=130))
        fields = self.parse_tool_fields(body)
        rel = (fields.get("path") or fields.get("title") or "").strip()
        if not rel and name == "edit_document":
            rel = ((self.open_doc_context(form or {}) or {}).get("path") or "").strip()
        if not rel:
            raise ValueError(f"{name} requires path")
        path = self._remote_path(form, rel)
        if name == "read_file" and os.path.splitext(rel)[1].lower() in IMAGE_EXTS:
            # Image: the bytes are attached to the conversation by run_agent.
            return f"[image {rel} is shown to you below — look at it directly]"
        if name == "read_pdf" or (name == "read_file" and os.path.splitext(rel)[1].lower() == ".pdf"):
            # PDF: /v1/read_file returns raw bytes as text (garbage). Pull the
            # bytes over the bridge and extract text on this host instead.
            r = bridge_call(url, token, "/v1/exec", {"command": "base64 < " + shlex.quote(path)})
            if r is None:
                raise ValueError(f"device '{host}' bridge is unreachable")
            if r.get("exit_code") != 0:
                raise ValueError(f"read_pdf @ {host}: {(r.get('stderr') or 'could not read PDF').strip()}")
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
                tf.write(base64.b64decode((r.get("stdout") or "").replace("\n", "")))
                tf.flush()
                return self.read_pdf_text(Path(tf.name), fields.get("pages"))
        if name == "read_file" and rel.lower().endswith(".docx"):
            return self._slice_lines(self._bridge_docx_text(bridge, host, path), fields.get("lines") or fields.get("pages"))
        if name == "read_file":
            r = bridge_call(url, token, "/v1/read_file", {"path": path, "max_chars": 100_000_000})
            if r is None:
                raise ValueError(f"device '{host}' bridge is unreachable")
            if r.get("error"):
                raise ValueError(f"read_file @ {host}: {r['error']}")
            return self._slice_lines(r.get("content") or "", fields.get("lines") or fields.get("pages"))
        if rel.lower().endswith(".apkg") and name in ("read_file", "create_document", "edit_document"):
            if name == "read_file":
                return self._bridge_apkg_text(bridge, host, path)
            if name == "edit_document":
                text = self._bridge_apkg_text(bridge, host, path)
                if "content" in fields:
                    updated = fields["content"]
                else:
                    updated, _note = apply_find_replace(
                        text, fields.get("find"), fields.get("replace", ""),
                        replace_all=str(fields.get("replace_all") or "").lower() in ("1", "true", "yes"),
                        occurrence=fields.get("occurrence"), name=f"{rel} @ {host}")
            else:
                updated = fields.get("content") or ""
            self._bridge_apkg_write(bridge, host, path, updated)
            return f"File written: {path} @ {host}"
        if name == "create_document":
            content = fields.get("content") or ""
            append = str(fields.get("append") or "").lower() in ("1", "true", "yes")
            if append:
                old = bridge_call(url, token, "/v1/read_file", {"path": path, "max_chars": 100_000_000})
                if old is None or old.get("error"):
                    raise ValueError(f"append @ {host}: {(old or {}).get('error', 'bridge unreachable')}")
                content = (old.get("content") or "") + content
            r = bridge_call(url, token, "/v1/write_file", {"path": path, "content": content})
            if r is None:
                raise ValueError(f"device '{host}' bridge is unreachable")
            if r.get("error"):
                raise ValueError(f"write_file @ {host}: {r['error']}")
            return f"File written: {path} @ {host}"
        if name == "edit_document":
            r = bridge_call(url, token, "/v1/read_file", {"path": path, "max_chars": 1_000_000})
            if r is None or r.get("error"):
                raise ValueError(f"edit_document @ {host}: {(r or {}).get('error', 'bridge unreachable')}")
            text = r.get("content") or ""
            if "content" in fields:
                updated = fields["content"]
            else:
                find = fields.get("find")
                if find is None:
                    raise ValueError("edit_document requires find/replace or content")
                if find not in text:
                    raise ValueError(f"could not find text in {rel} @ {host}")
                updated = text.replace(find, fields.get("replace", ""), 1)
            w = bridge_call(url, token, "/v1/write_file", {"path": path, "content": updated})
            if w is None or w.get("error"):
                raise ValueError(f"edit_document @ {host}: {(w or {}).get('error', 'bridge unreachable')}")
            return f"File edited: {path} @ {host}"
        if name == "suggest_document":
            return self.suggest_document_tool(None, body, allow_outside=True)  # suggestion only, no disk
        raise ValueError(f"tool {name} cannot run on a remote device")

    def _multipart(self, field):
        """Minimal multipart/form-data parser via the email package —
        the cgi module it replaces was removed in Python 3.13."""
        ctype = self.headers.get("content-type", "")
        raw = b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + self._body_bytes()
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        out = []
        for part in msg.iter_parts():
            if part.get_param("name", header="content-disposition") != field:
                continue
            filename = part.get_filename()
            if filename:
                out.append((filename, part.get_payload(decode=True) or b""))
        return out

    def handle_upload(self, field, for_email=False):
        files = []
        for filename, data in self._multipart(field):
            uid = "up_" + os.urandom(8).hex()
            name = Path(filename).name
            dest = self.store.root / "uploads" / f"{uid}-{name}"
            with open(dest, "wb") as fh:
                fh.write(data)
            mime = mimetypes.guess_type(name)[0] or ""
            self.store.exec(
                "insert into uploads(id,name,path,mime,created_at) values(?,?,?,?,?)",
                (uid, name, str(dest), mime, now_iso()),
            )
            files.append({"id": uid, "name": name, "mime": mime})
        if for_email:
            first = files[0] if files else {"id": "", "name": ""}
            return self.json({"token": first["id"], "filename": first["name"]})
        return self.json({"files": files})

    def serve_upload(self, uid):
        row = self.store.one("select * from uploads where id=?", (uid,))
        if not row or not Path(row["path"]).exists():
            return self.not_found()
        self._send(200, row.get("mime") or "application/octet-stream")
        with open(row["path"], "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def browse_host(self, q):
        """GET /api/fs/browse?path=[&device=] : the folder picker.

        With `device` set to a linked device, lists that device's folders over
        its bridge. Otherwise lists this host: limited to the user's home, no
        dotfiles, never following a symlink out of it. Pref `fs_browse_anywhere`
        (off by default; for a backend on a home server whose shared folders
        live outside the home) lifts the home limit and lists folders only."""
        host = (q.get("device") or q.get("host") or "").strip()
        raw = (q.get("path") or "").strip()
        bridge = self._device_bridge(host)
        if bridge:
            url, token = bridge
            base = raw or "~"
            r = bridge_call(url, token, "/v1/exec",
                            {"command": "cd " + shlex.quote(base) + " && pwd && ls -1Ap | grep '/$'"})
            if r is None:
                return self.json({"error": f"device '{host}' unreachable", "entries": []}, 502)
            lines = (r.get("stdout") or "").splitlines()
            cwd = lines[0] if lines else base
            entries = [{"name": d.rstrip("/"), "is_dir": True} for d in lines[1:] if d.endswith("/")]
            return self.json({"path": cwd, "entries": entries})
        if str(self.pref("fs_browse_anywhere")).lower() in ("1", "true", "yes", "on"):
            root = Path(raw).expanduser() if raw else Path.home()
            try:
                root = root.resolve()
                entries = [{"name": p.name, "path": str(p), "is_dir": True}
                           for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
            except OSError as e:
                return self.json({"error": str(e), "entries": [], "path": str(root)}, 400)
            return self.json({"path": str(root), "entries": entries})
        home = Path.home().resolve()
        try:
            target = (Path(raw).expanduser() if raw else home)
            if ".." in target.parts:
                raise ValueError("path cannot contain '..'")
            real = target.resolve()
            if real != home and not real.is_relative_to(home):
                raise ValueError("browsing is limited to your home folder")
            if not real.is_dir():
                return self.json({"detail": "not a folder"}, 404)
        except (ValueError, OSError) as exc:
            return self.json({"detail": str(exc)}, 400)
        entries = []
        try:
            for p in sorted(real.iterdir(), key=lambda x: x.name.lower()):
                if p.name.startswith("."):
                    continue
                try:
                    if p.is_symlink() and not p.resolve().is_relative_to(home):
                        continue
                    is_dir = p.is_dir()
                    entry = {"name": p.name, "path": str(p), "is_dir": is_dir}
                    if not is_dir:
                        entry["size"] = p.stat().st_size
                except OSError:
                    continue
                entries.append(entry)
                if len(entries) >= 500:
                    break
        except OSError as exc:
            return self.json({"detail": str(exc)}, 403)
        return self.json({"path": str(real), "entries": entries})

    def workdir_get(self, path, q):
        sid = path.split("/")[3]
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        wd = (session or {}).get("workdir") or ""
        # The bare /workdir endpoint reports the bound path. The app chooses
        # the folder; agent permission mode decides whether tools stay inside it.
        if path.endswith("/workdir"):
            return self.json({"workdir": wd or None})
        host = self._session_host({"session": sid})
        bridge = self._device_bridge(host)
        if bridge:
            try:
                target_remote = self._bound_remote_path(sid, q.get("sub", ""))
                if path.endswith("/raw"):
                    raw = self._bridge_read_bytes(bridge, host, target_remote)
                    self._send(200, "application/octet-stream")
                    self.wfile.write(raw)
                    return
                if path.endswith("/list"):
                    script = "import os,json; p=" + repr(target_remote) + "; print(json.dumps([{'name':e.name,'is_dir':e.is_dir(),'type':'dir' if e.is_dir() else 'file'} for e in os.scandir(p) if not e.name.startswith('.')]))"
                    url, token = bridge
                    result = bridge_call(url, token, "/v1/exec", {"command": "python3 -c " + shlex.quote(script)})
                    if result is None or result.get("exit_code") != 0:
                        raise ValueError((result or {}).get("stderr") or "bridge unreachable")
                    return self.json({"entries": json.loads(result.get("stdout") or "[]")})
                if path.endswith("/docx-extras"):
                    import tempfile
                    with tempfile.TemporaryDirectory() as td:
                        tmp = Path(td) / "document.docx"
                        tmp.write_bytes(self._bridge_read_bytes(bridge, host, target_remote))
                        return self.json(docx_extras(tmp))
            except (ValueError, OSError) as exc:
                return self.json({"detail": str(exc)}, 400)
            return self.not_found()
        root = Path(wd)
        if not wd or not root.exists():
            return self.json({"workdir": None, "entries": []})
        sub = q.get("sub", "")
        target = (root / sub).resolve()
        if not self._inside(root, target):
            return self.json({"detail": "Path escapes workdir"}, 400)
        if path.endswith("/list"):
            entries = []
            if target.exists() and target.is_dir():
                for p in target.iterdir():
                    if not p.name.startswith("."):
                        entries.append({"name": p.name, "is_dir": p.is_dir(), "type": "dir" if p.is_dir() else "file"})
            return self.json({"entries": entries})
        if path.endswith("/raw") and target.exists() and target.is_file():
            self._send(200, "application/octet-stream")
            with open(target, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        if path.endswith("/docx-extras") and target.exists() and is_docx(target):
            return self.json(docx_extras(target))
        return self.not_found()

    @staticmethod
    def _inside(root, target):
        try:
            r = root.resolve()
            return target == r or str(target).startswith(str(r) + "/")
        except OSError:
            return False

    def workdir_post(self, path):
        sid = path.split("/")[3]
        if path.endswith("/workdir"):
            body = self._json_body()
            # host_id binds the chat to a device: '' (or this backend's own id)
            # = this host; otherwise the agent's file tools route to that device.
            host_id = (body.get("host_id") or body.get("device") or "").strip()
            folder = body.get("workdir")
            if getattr(self.server, "is_lan", False) and folder and not self._device_bridge(host_id):
                try:                                    # phone: home only, no app data
                    folder = phone_folder_check(folder)
                except ValueError as exc:
                    return self.json({"detail": str(exc)}, 403)
            self.store.exec("update sessions set workdir=?, host_id=? where id=?",
                            (folder, host_id, sid))
            return self.json({"ok": True})
        body = self._json_body()
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root = Path((session or {}).get("workdir") or "")
        sub = body.get("sub", "")
        # Device-bound folders (e.g. a laptop folder while this backend runs on
        # a home server) live on the device, not here — route rename/delete over the
        # bridge so they actually take effect on the right machine.
        host = self._session_host({"session": sid})
        bridge = self._device_bridge(host)
        if bridge:
            try:
                remote = self._bound_remote_path(sid, sub)
                if path.endswith("/open-file"):
                    import tempfile
                    ext = os.path.splitext(remote)[1].lower()
                    raw = self._bridge_read_bytes(bridge, host, remote)
                    with tempfile.TemporaryDirectory() as td:
                        tmp = Path(td) / ("document" + ext)
                        tmp.write_bytes(raw)
                        content = read_doc_text(tmp)
                    return self.json({"title": os.path.basename(remote), "language": ext.lstrip(".") or "text", "current_content": content})
                if path.endswith("/save-raw"):
                    try:
                        raw = base64.b64decode(body.get("b64") or "", validate=True)
                    except Exception:
                        return self.json({"detail": "bad base64"}, 400)
                    self._bridge_write_bytes(bridge, host, remote, raw)
                    return self.json({"ok": True})
                if path.endswith("/save-file") and sub.lower().endswith(".xlsx"):
                    from jb_xlsx import csv_to_xlsx
                    self._bridge_write_bytes(bridge, host, remote, csv_to_xlsx(body.get("content") or ""))
                    return self.json({"ok": True})
                if path.endswith("/docx-extra"):
                    import tempfile
                    with tempfile.TemporaryDirectory() as td:
                        tmp = Path(td) / "document.docx"
                        tmp.write_bytes(self._bridge_read_bytes(bridge, host, remote))
                        docx_write_extra(tmp, body.get("region") or "", body.get("content") or "")
                        self._bridge_write_bytes(bridge, host, remote, tmp.read_bytes())
                    return self.json({"ok": True})
            except (ValueError, OSError) as exc:
                return self.json({"detail": str(exc)}, 400)
        if bridge and (path.endswith("/rename-file") or path.endswith("/delete-file")):
            import posixpath
            url, token = bridge
            src = self._remote_path({"session": sid}, sub)
            if path.endswith("/rename-file"):
                new = (body.get("new_name") or "").strip()
                if not new or "/" in new or new in (".", ".."):
                    return self.json({"detail": "bad name"}, 400)
                cmd = "mv -n -- " + shlex.quote(src) + " " + shlex.quote(posixpath.join(posixpath.dirname(src), new))
            else:
                cmd = "rm -rf -- " + shlex.quote(src)
            r = bridge_call(url, token, "/v1/exec", {"command": cmd})
            if r is None:
                return self.json({"detail": f"device '{host}' bridge unreachable"}, 502)
            if int(r.get("exit_code", 0) or 0) != 0:
                return self.json({"detail": (r.get("stderr") or "operation failed").strip()[:200]}, 400)
            return self.json({"ok": True})
        target = (root / sub).resolve()
        if not self._inside(root, target):
            return self.json({"detail": "Path escapes workdir"}, 400)
        if path.endswith("/open-file") and target.exists() and target.is_file():
            data = read_doc_text(target)
            return self.json({"id": "file_" + os.urandom(6).hex(), "title": target.name, "language": target.suffix[1:] or "text", "current_content": data, "version_count": 1})
        if path.endswith("/save-file"):
            # Write text back to a bound-folder file. A device-bound folder lives
            # on another machine (e.g. a laptop's folder while this backend
            # runs on a home server), so route the write over the bridge — otherwise it
            # silently lands on the wrong host. Local folders write straight to disk.
            content = body.get("content") or ""
            if bridge and sub.lower().endswith(".apkg"):
                try:
                    self._bridge_apkg_write(bridge, host, self._remote_path({"session": sid}, sub), content)
                except ValueError as e:
                    return self.json({"detail": str(e)[:200]}, 400)
                return self.json({"ok": True})
            if bridge:
                url, token = bridge
                r = bridge_call(url, token, "/v1/write_file",
                                {"path": self._remote_path({"session": sid}, sub), "content": content})
                if r is None:
                    return self.json({"detail": f"device '{host}' bridge unreachable"}, 502)
                if r.get("error"):
                    return self.json({"detail": str(r.get("error"))[:200]}, 400)
                return self.json({"ok": True})
            with lock_for_path(target):
                backup_existing(target)
                write_doc_text(target, content)
            return self.json({"ok": True})
        if path.endswith("/save-raw"):
            # Write raw bytes back to a bound-folder file — used to persist a
            # .doc/.docx the app downloaded, edited natively, and re-serialized.
            # base64 keeps the JSON transport clean for binary payloads.
            try:
                raw = base64.b64decode(body.get("b64") or "", validate=True)
            except Exception:
                return self.json({"detail": "bad base64"}, 400)
            with lock_for_path(target):
                backup_existing(target)
                target.write_bytes(raw)
            return self.json({"ok": True})
        if path.endswith("/docx-extra") and is_docx(target):
            with lock_for_path(target):
                backup_existing(target)
                docx_write_extra(target, body.get("region") or "", body.get("content") or "")
            return self.json({"ok": True})
        if path.endswith("/rename-file") and target.exists() and target != root:
            new = (body.get("new_name") or "").strip()
            if not new or "/" in new or new in (".", ".."):
                return self.json({"detail": "bad name"}, 400)
            dst = (target.parent / new).resolve()
            if not self._inside(root, dst):
                return self.json({"detail": "Path escapes workdir"}, 400)
            with lock_for_path(target):
                target.rename(dst)
            return self.json({"ok": True})
        if path.endswith("/delete-file") and target.exists() and target != root:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
            return self.json({"ok": True})
        return self.not_found()
