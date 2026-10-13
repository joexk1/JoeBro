"""Workdir browsing, uploads, and the folder picker."""
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

    def workdir_get(self, path, q):
        sid = path.split("/")[3]
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        wd = (session or {}).get("workdir") or ""
        # The bare /workdir endpoint reports the bound path. The app chooses
        # the folder; agent permission mode decides whether tools stay inside it.
        if path.endswith("/workdir"):
            return self.json({"workdir": wd or None})
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

    def browse_host(self, q):
        """GET /api/fs/browse?path= : directory names under the user's home, for the
        phone's "bind a folder" picker. Read-only, no dotfiles, and it will not
        follow a symlink out of the home folder."""
        home = Path.home().resolve()
        raw = (q.get("path") or "").strip()
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

    def workdir_post(self, path):
        sid = path.split("/")[3]
        if path.endswith("/workdir"):
            body = self._json_body()
            folder = body.get("workdir")
            if getattr(self.server, "is_lan", False) and folder:     # phone: home only, no app data
                try:
                    folder = phone_folder_check(folder)
                except ValueError as exc:
                    return self.json({"detail": str(exc)}, 403)
            self.store.exec("update sessions set workdir=? where id=?", (folder, sid))
            return self.json({"ok": True})
        body = self._json_body()
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root = Path((session or {}).get("workdir") or "")
        sub = body.get("sub", "")
        target = (root / sub).resolve()
        if not self._inside(root, target):
            return self.json({"detail": "Path escapes workdir"}, 400)
        if path.endswith("/open-file") and target.exists() and target.is_file():
            data = read_doc_text(target)
            return self.json({"id": "file_" + os.urandom(6).hex(), "title": target.name, "language": target.suffix[1:] or "text", "current_content": data, "version_count": 1})
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
