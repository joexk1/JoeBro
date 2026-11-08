"""Document tools: chunked create_document (append), safe find/replace, paged reads,
Word text extraction and chat attachments. Real SQLite store in a temp dir."""
import json, tempfile, unittest, zipfile
from pathlib import Path

from jb_core import Store, apply_find_replace, read_doc_context, docx_to_text, text_to_docx
from jb_tools import ToolsMixin


class H(ToolsMixin):
    MAX_TOOL_OUTPUT_CHARS = 24000
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
        self.root = Path(tempfile.mkdtemp())
    def run(self, name, **fields):
        body = "\n".join(f"{k}: {v}" for k, v in fields.items())
        return self.execute_file_tool(self.root, name, body)


class CreateAppend(unittest.TestCase):
    def test_parts_accumulate_and_replace_by_default(self):
        h = H()
        h.run("create_document", path="long.md", content="Part one.\n")
        h.run("create_document", path="long.md", content="Part two.\n", append="true")
        h.run("create_document", path="long.md", content="Part three.", append="true")
        self.assertEqual((h.root / "long.md").read_text(), "Part one.\n\nPart two.\n\nPart three.")
        h.run("create_document", path="long.md", content="Fresh.")
        self.assertEqual((h.root / "long.md").read_text(), "Fresh.")

    def test_append_to_missing_file_creates_it(self):
        h = H()
        h.run("create_document", path="new.md", content="Hello", append="true")
        self.assertEqual((h.root / "new.md").read_text(), "Hello")

    def test_docx_append_keeps_earlier_text(self):
        h = H()
        h.run("create_document", path="a.docx", content="First paragraph")
        h.run("create_document", path="a.docx", content="\nSecond paragraph", append="true")
        text = docx_to_text(h.root / "a.docx")
        self.assertIn("First paragraph", text)
        self.assertIn("Second paragraph", text)

    def test_empty_call_writes_nothing(self):
        h = H()
        with self.assertRaises(ValueError):
            h.run("create_document", append="true")
        self.assertEqual(list(h.root.iterdir()), [])

    def test_row_files_join_with_a_single_newline(self):
        self.assertEqual(ToolsMixin._join_chunk("a,1", "b,2", ".csv"), "a,1\nb,2")
        self.assertEqual(ToolsMixin._join_chunk("a\n", "b", ".md"), "a\nb")

    def test_cut_off_arguments_are_detected(self):
        self.assertTrue(ToolsMixin._tool_call_truncated({"arguments": '{"path": "a.md", "content": "abc'}))
        self.assertFalse(ToolsMixin._tool_call_truncated({"arguments": '{"path": "a.md"}'}))
        self.assertFalse(ToolsMixin._tool_call_truncated({"arguments": ""}))


class FindReplace(unittest.TestCase):
    def test_ambiguous_match_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_find_replace("a\nb\na\n", "a", "z")
        self.assertIn("2 places", str(ctx.exception))

    def test_replace_all_and_occurrence(self):
        self.assertEqual(apply_find_replace("a b a", "a", "z", replace_all=True)[0], "z b z")
        self.assertEqual(apply_find_replace("a b a", "a", "z", occurrence=2)[0], "a b z")

    def test_edit_document_reports_the_refusal(self):
        h = H()
        (h.root / "n.md").write_text("x\nx\n")
        with self.assertRaises(ValueError):
            h.run("edit_document", path="n.md", find="x", replace="y")
        self.assertEqual((h.root / "n.md").read_text(), "x\nx\n")
        self.assertIn("2 occurrence", h.run("edit_document", path="n.md", find="x", replace="y", replace_all="true"))


class PagedReads(unittest.TestCase):
    def test_long_text_comes_back_in_windows_with_a_resume_hint(self):
        h = H()
        (h.root / "big.txt").write_text("\n".join(f"line {i} " + "x" * 80 for i in range(1, 1000)))
        first = h.run("read_file", path="big.txt")
        self.assertIn("--- shown: lines 1-", first)
        self.assertIn('lines: "', first)
        window = h.run("read_file", path="big.txt", lines="10-12")
        self.assertTrue(window.startswith("line 10 "))
        self.assertIn("lines 10-12 of 999", window)

    def test_short_text_is_returned_whole(self):
        h = H()
        (h.root / "s.txt").write_text("one\ntwo\n")
        self.assertEqual(h.run("read_file", path="s.txt"), "one\ntwo\n")

    def test_bad_range_is_explained(self):
        h = H()
        (h.root / "s.txt").write_text("one\ntwo\n")
        with self.assertRaises(ValueError):
            h.run("read_file", path="s.txt", lines="five")


class Word(unittest.TestCase):
    def test_tables_and_notes_are_read(self):
        h = H()
        p = h.root / "t.docx"
        text_to_docx(p, "Title\nBody")
        with zipfile.ZipFile(p) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        doc = parts["word/document.xml"].decode()
        table = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>'
                 '<w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        parts["word/document.xml"] = doc.replace("</w:body>", table + "</w:body>").encode()
        with zipfile.ZipFile(p, "w") as z:
            for n, b in parts.items():
                z.writestr(n, b)
        out = read_doc_context(p)
        self.assertIn("Title", out)
        self.assertIn("A1\tB1", out)

    def test_damaged_docx_says_so(self):
        p = Path(tempfile.mkdtemp()) / "bad.docx"
        p.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            docx_to_text(p)


if __name__ == "__main__":
    unittest.main()
