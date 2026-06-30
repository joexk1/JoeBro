"""Minimal stdlib xlsx <-> CSV conversion for the spreadsheet grid editor.

No openpyxl/pandas: read just enough OOXML to round-trip cell *values*.
ponytail: first sheet only, values only — no formulas, formatting, charts,
or multi-sheet. That's the chosen scope; widen the sheet-selection and the
cell writer here if multi-sheet/typed cells are ever needed.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

# A plain integer or decimal. Deliberately excludes leading-zero numbers
# ("007" stays text so zip codes survive) and scientific notation / inf / nan.
_NUM_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?\Z")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _col_index(cell_ref):
    """'AB12' -> 27 (zero-based column index)."""
    m = re.match(r"[A-Za-z]+", cell_ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(0).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_letters(i):
    """0 -> 'A', 26 -> 'AA'."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_to_rows(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                if _local(si.tag) == "si":
                    # Concatenate every <t> (handles rich-text runs).
                    shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        if not sheets:
            return []
        root = ET.fromstring(z.read(sheets[0]))
        rows = []
        for el in root.iter():
            if _local(el.tag) != "row":
                continue
            # ponytail: rows taken in document order; sparse row gaps ignored
            # (rare in data grids). Honor the row <r> attr if that ever bites.
            cells = {}
            for c in el:
                if _local(c.tag) != "c":
                    continue
                ctype = c.get("t")
                if ctype == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                else:
                    v = next((x for x in c if _local(x.tag) == "v"), None)
                    raw = (v.text if v is not None else "") or ""
                    if ctype == "s":
                        idx = int(raw or 0)
                        text = shared[idx] if 0 <= idx < len(shared) else ""
                    else:
                        text = raw
                cells[_col_index(c.get("r") or "")] = text
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])
        while rows and not any(rows[-1]):
            rows.pop()
        return rows


def _cell_xml(ref, val):
    s = "" if val is None else str(val)
    if s == "":
        return f'<c r="{ref}"/>'
    if _NUM_RE.match(s):
        return f'<c r="{ref}"><v>{s}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(s)}</t></is></c>'


def rows_to_xlsx(rows):
    body = []
    for ri, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_col_letters(ci)}{ri}", v) for ci, v in enumerate(row))
        body.append(f'<row r="{ri}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def xlsx_to_csv(data):
    buf = io.StringIO()
    csv.writer(buf).writerows(xlsx_to_rows(data))
    return buf.getvalue()


def csv_to_xlsx(text):
    return rows_to_xlsx(list(csv.reader(io.StringIO(text))))


if __name__ == "__main__":
    src = [["Name", "Qty", "Zip"], ["Apple", "12", "007"], ["Pear, green", "3.5", "90210"], ["", "", ""]]
    out = xlsx_to_rows(rows_to_xlsx(src))
    expect = [r for r in src if any(r)]  # trailing empty row trimmed
    assert out == expect, f"round-trip mismatch:\n{out}\n{expect}"
    # CSV path, including a quoted comma and a leading-zero string preserved.
    assert xlsx_to_rows(csv_to_xlsx(xlsx_to_csv(rows_to_xlsx(src)))) == expect
    print("jb_xlsx self-check OK")
