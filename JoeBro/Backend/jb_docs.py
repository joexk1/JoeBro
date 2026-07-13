"""Document library CRUD, export, PDF rendering."""
from jb_core import *  # noqa: F401,F403


class DocsMixin:

    def documents_library(self, q):
        search = (q.get("search") or "").strip()
        limit = int(q.get("limit") or 50)
        if search:
            rows = self.store.rows(
                "select * from documents where archived=0 and title like ? order by datetime(updated_at) desc limit ?",
                (f"%{search}%", limit),
            )
        else:
            rows = self.store.rows(
                "select * from documents where archived=0 order by datetime(updated_at) desc limit ?",
                (limit,),
            )
        return self.json({"documents": [self.document_summary_json(r) for r in rows], "total": len(rows)})

    def get_document(self, doc_id):
        row = self.store.one("select * from documents where id=?", (doc_id,))
        if not row or row.get("archived"):
            return self.json({"detail": "Document not found"}, 404)
        return self.json(self.document_detail_json(row))

    def create_document(self, body):
        doc_id = "doc_" + os.urandom(8).hex()
        title = body.get("title") or "Untitled"
        language = body.get("language") or Path(title).suffix.lstrip(".") or "markdown"
        content = body.get("content") or ""
        ts = now_iso()
        self.store.exec(
            """insert into documents(id,title,language,current_content,version_count,file_path,archived,created_at,updated_at)
               values(?,?,?,?,?,?,?,?,?)""",
            (doc_id, title, language, content, 1, body.get("file_path") or "", 0, ts, ts),
        )
        row = self.store.one("select * from documents where id=?", (doc_id,))
        return self.json(self.document_detail_json(row))

    def update_document(self, doc_id, body):
        row = self.store.one("select * from documents where id=?", (doc_id,))
        if not row or row.get("archived"):
            return self.json({"detail": "Document not found"}, 404)
        content = body.get("content")
        if content is None:
            content = row.get("current_content") or ""
        title = body.get("title") or row.get("title") or "Untitled"
        language = body.get("language") or row.get("language") or "markdown"
        file_path = row.get("file_path") or ""
        if file_path:
            try:
                target = Path(file_path)
                with lock_for_path(target):
                    backup_existing(target)
                    write_doc_text(target, content)
            except OSError as exc:
                return self.json({"detail": f"Disk write failed: {exc}"}, 500)
        self.store.exec(
            "update documents set title=?, language=?, current_content=?, updated_at=? where id=?",
            (title, language, content, now_iso(), doc_id),
        )
        row = self.store.one("select * from documents where id=?", (doc_id,))
        return self.json(self.document_detail_json(row))

    def export_document(self, doc_id):
        row = self.store.one("select * from documents where id=?", (doc_id,))
        if not row or row.get("archived"):
            return self.json({"detail": "Document not found"}, 404)
        data = self.simple_pdf(row.get("title") or "Document", row.get("current_content") or "")
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f"attachment; filename=\"{(row.get('title') or 'document').replace(chr(34), '')}.pdf\"")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def pdf_escape(text):
        return (text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    @classmethod
    def simple_pdf(cls, title, content):
        lines = [title, ""] + (content or "").splitlines()
        max_chars = 92
        wrapped = []
        for line in lines:
            if not line:
                wrapped.append("")
                continue
            while len(line) > max_chars:
                wrapped.append(line[:max_chars])
                line = line[max_chars:]
            wrapped.append(line)
        wrapped = wrapped[:54]
        stream_lines = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
        for i, line in enumerate(wrapped):
            if i:
                stream_lines.append("T*")
            stream_lines.append(f"({cls.pdf_escape(line)}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("utf-8")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for idx, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{idx} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii")
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode("ascii")
        out += f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
        return bytes(out)

    @staticmethod
    def document_summary_json(r):
        return {
            "id": r["id"],
            "title": r.get("title") or "Untitled",
            "language": r.get("language") or "markdown",
            "updated_at": r.get("updated_at"),
            "version_count": int(r.get("version_count") or 1),
            "archived": bool(r.get("archived")),
        }

    @classmethod
    def document_detail_json(cls, r):
        out = cls.document_summary_json(r)
        out["current_content"] = r.get("current_content") or ""
        return out
