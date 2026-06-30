#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import textwrap
import threading
import time
import email
import email.header
import email.policy
import email.utils
import imaplib
import mimetypes
import smtplib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_NAME = "JoeBro"
DEFAULT_PORT = 8765

# Deep-research worker registry. Handler is per-request, so worker bookkeeping
# lives at module scope: _RESEARCH_STARTED is the set of ids that already have a
# worker (so a panel reload / re-POST never spawns a second one or re-runs a
# finished item); _RESEARCH_CANCEL holds ids the worker must stop on.
_RESEARCH_GUARD = threading.Lock()
_RESEARCH_STARTED: set = set()
_RESEARCH_CANCEL: set = set()


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# === JoeBro macOS Use: dependency-free computer use via osascript + screencapture
# (both ship with macOS) - no external dependencies.
def _applescript_str(text):
    """Quote a Python string as an AppleScript string literal."""
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


_MACOS_KEY_CODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53,
                    "esc": 53, "delete": 51, "backspace": 51, "up": 126, "down": 125,
                    "left": 123, "right": 124}
_MACOS_MODS = {"cmd": "command down", "command": "command down", "ctrl": "control down",
               "control": "control down", "opt": "option down", "option": "option down",
               "alt": "option down", "shift": "shift down"}

MACOS_USE_TOOL = {"type": "function", "function": {
    "name": "macos_use",
    "description": ("Control this Mac (computer use). FIRST call action 'snapshot' to see the "
                   "frontmost app and its windows, menus and buttons, then act. Actions: snapshot "
                   "(see the screen), open_app, click (a button/element by name), type (text), key "
                   "(e.g. 'cmd+s', 'return', 'tab'), menu (menu_path like 'File>Save'), screenshot."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["snapshot", "open_app", "click", "type", "key", "menu", "screenshot"]},
        "app": {"type": "string", "description": "app name for open_app"},
        "element": {"type": "string", "description": "name of the button/element to click"},
        "text": {"type": "string", "description": "text to type"},
        "key": {"type": "string", "description": "shortcut to press, e.g. 'cmd+s'"},
        "menu_path": {"type": "string", "description": "menu path for action 'menu', e.g. 'File>Save'"}},
        "required": ["action"]}}}


def macos_use_argv(action, params):
    """Build the dependency-free computer-use command (argv) for an action.
    Returns an argv list, or raises ValueError for a bad/unknown action."""
    action = (action or "").strip().lower()
    params = params or {}
    if action == "snapshot":
        scr = ('tell application "System Events"\n'
               'set fa to name of first application process whose frontmost is true\n'
               'set out to "Frontmost app: " & fa & linefeed\n'
               'tell process fa\n'
               'try\nset out to out & "Windows: " & (name of windows as string) & linefeed\nend try\n'
               'try\nset out to out & "Menus: " & (name of menu bar items of menu bar 1 as string) & linefeed\nend try\n'
               'try\nset out to out & "Buttons: " & (name of buttons of front window as string) & linefeed\nend try\n'
               'end tell\nreturn out\nend tell')
        return ["osascript", "-e", scr]
    if action == "open_app":
        app = (params.get("app") or params.get("target") or "").strip()
        if not app:
            raise ValueError("open_app needs an 'app' name")
        return ["osascript", "-e", "tell application " + _applescript_str(app) + " to activate"]
    if action == "type":
        return ["osascript", "-e", 'tell application "System Events" to keystroke ' + _applescript_str(params.get("text") or "")]
    if action == "key":
        combo = (params.get("key") or "").strip().lower()
        parts = [p for p in re.split(r"[+\-\s]+", combo) if p]
        if not parts:
            raise ValueError("key needs a shortcut, e.g. 'cmd+s'")
        keyname = parts[-1]
        mods = [_MACOS_MODS[p] for p in parts[:-1] if p in _MACOS_MODS]
        using = (" using {" + ", ".join(mods) + "}") if mods else ""
        if keyname in _MACOS_KEY_CODES:
            tail = "key code " + str(_MACOS_KEY_CODES[keyname]) + using
        else:
            tail = "keystroke " + _applescript_str(keyname) + using
        return ["osascript", "-e", 'tell application "System Events" to ' + tail]
    if action == "click":
        el = (params.get("element") or params.get("target") or "").strip()
        if not el:
            raise ValueError("click needs an 'element' name")
        scr = ('tell application "System Events"\n'
               'set fa to name of first application process whose frontmost is true\n'
               'tell process fa\n'
               'click (first UI element of front window whose name is ' + _applescript_str(el) + ')\n'
               'end tell\nend tell')
        return ["osascript", "-e", scr]
    if action == "menu":
        path = (params.get("menu_path") or "").strip()
        items = [p.strip() for p in re.split(r"[>/]", path) if p.strip()]
        if len(items) < 2:
            raise ValueError("menu needs a path like 'File>Save'")
        top, sub = items[0], items[1]
        scr = ('tell application "System Events"\n'
               'set fa to name of first application process whose frontmost is true\n'
               'tell process fa\n'
               'click menu item ' + _applescript_str(sub) + ' of menu ' + _applescript_str(top) +
               ' of menu bar item ' + _applescript_str(top) + ' of menu bar 1\n'
               'end tell\nend tell')
        return ["osascript", "-e", scr]
    if action == "screenshot":
        return ["screencapture", "-x", "/tmp/joebro_screenshot_" + str(int(time.time())) + ".png"]
    raise ValueError("unknown macos_use action: " + action)




def app_support_dir():
    root = os.environ.get("JOEBRO_DATA_DIR")
    if root:
        return Path(root)
    return Path.home() / "Library" / "Application Support" / APP_NAME


def atomic_write_text(path: Path, text: str):
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# Per-path write locks. The server is one process with many threads
# (ThreadingHTTPServer), so a threading.Lock per file path serializes the
# read-modify-write that edit/update_document do — without it two concurrent
# agents can both read the original and the second write silently drops the
# first's edit. (fcntl.flock would be for separate processes; not our case.)
_PATH_LOCKS: dict = {}
_PATH_LOCKS_GUARD = threading.Lock()

# Claude-Code-style command-permission prompts. When "ask before running
# commands" is on (full mode), the agent loop blocks on one of these events
# until the app POSTs the user's allow/deny/always decision.
_PERMISSION_REGISTRY: dict = {}        # request_id -> {"event": Event, "decision": str}
_PERMISSION_GUARD = threading.Lock()
_SESSION_ALLOW: dict = {}              # session_id -> set of tools "always allowed this session"

# Cache of the assembled model picker list — each endpoint needs a live network
# fetch, so we don't redo it on every /api/models call.
_MODELS_CACHE: dict = {"ts": 0.0, "items": None}
_MODELS_CACHE_LOCK = threading.Lock()
_MODELS_TTL = 300                      # seconds


def lock_for_path(path) -> threading.Lock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = _PATH_LOCKS[key] = threading.Lock()
        return lk


def backup_existing(path: Path):
    """Copy an existing file aside before it's overwritten, so a bad AI edit is
    recoverable. Best-effort; kept in Application Support (outside the bound
    folder) so it stays clean and agents can't reach the backups. Last 5/file."""
    try:
        if not path.is_file():
            return
        bdir = app_support_dir() / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
        # microseconds so rapid (sub-second) AI edits don't overwrite each other
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        shutil.copy2(path, bdir / f"{path.name}.{tag}.{stamp}.bak")
        olds = sorted(bdir.glob(f"{path.name}.{tag}.*.bak"))
        for old in olds[:-5]:
            old.unlink()
    except Exception:
        pass  # never let a backup failure block the actual write


# ---------------------------------------------------------------------------
# .doc / .docx support for the AI's file tools. Word files are real Word files,
# never Markdown. The AI reads them as plain text (so it can summarize / answer
# questions) and edits them in place — find/replace patches the document.xml
# text nodes so all formatting, colours, and images are preserved. The user's
# own rich editing happens natively in the Swift app (NSAttributedString).
# ponytail: stdlib zipfile + ElementTree, no python-docx dependency.

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def is_docx(path) -> bool:
    return str(path).lower().endswith(".docx")


def docx_to_text(path: Path) -> str:
    """Plain text of a .docx (one line per paragraph), for AI context. No
    Markdown, no formatting markers — just the words."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return ""
    body = root.find(_W + "body")
    lines = []
    for p in (list(body) if body is not None else []):
        if p.tag != _W + "p":
            continue
        lines.append("".join(t.text or "" for t in p.iter(_W + "t")))
    return "\n".join(lines).strip() + "\n"


def docx_extras(path: Path) -> dict:
    """Header / footer / footnote text from a .docx. NSAttributedString only
    imports the main body, so the editor fetches these separately to show them
    around the page. Plain text per region (one line per paragraph)."""
    out = {"header": "", "footer": "", "footnotes": ""}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()

            def text_of(name):
                try:
                    root = ET.fromstring(z.read(name))
                except (KeyError, ET.ParseError):
                    return ""
                paras = ["".join(t.text or "" for t in p.iter(_W + "t"))
                         for p in root.iter(_W + "p")]
                return "\n".join(x for x in paras if x.strip())

            heads = sorted(n for n in names if re.match(r"word/header\d*\.xml$", n))
            foots = sorted(n for n in names if re.match(r"word/footer\d*\.xml$", n))
            notes = [n for n in names if n == "word/footnotes.xml"]
            out["header"] = "\n".join(text_of(n) for n in heads).strip()
            out["footer"] = "\n".join(text_of(n) for n in foots).strip()
            if notes:
                try:
                    root = ET.fromstring(z.read(notes[0]))
                    rows = []
                    for fn in root.iter(_W + "footnote"):
                        fid = fn.attrib.get(_W + "id") or fn.attrib.get("id") or ""
                        if fid.startswith("-") or fid == "0":
                            continue
                        txt = "\n".join(
                            "".join(t.text or "" for t in p.iter(_W + "t"))
                            for p in fn.iter(_W + "p")
                        ).strip()
                        if txt:
                            rows.append(f"{fid}. {txt}")
                    out["footnotes"] = "\n".join(rows).strip()
                except (KeyError, ET.ParseError):
                    out["footnotes"] = ""
    except (zipfile.BadZipFile, OSError):
        pass
    return out


def docx_write_extra(path: Path, region: str, text: str):
    """Write the header or footer text of a .docx (user/agent edits). Rewrites
    an existing header/footer part, or creates and wires one up if absent.
    ponytail: sets the text; intricate per-run header formatting isn't kept."""
    if region == "footnotes":
        return docx_write_footnotes(path, text)
    if region not in ("header", "footer"):
        raise ValueError("region must be 'header', 'footer', or 'footnotes'")
    tag, ctype = ("hdr", "header") if region == "header" else ("ftr", "footer")
    paras = "".join(
        '<w:p><w:r><w:t xml:space="preserve">' + _xml_escape(line) + "</w:t></w:r></w:p>"
        for line in (text.splitlines() or [""]))
    part_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:%s xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        % tag + paras + "</w:%s>" % tag)

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}

    existing = sorted(n for n in names if re.match(r"word/%s\d*\.xml$" % ctype, n))
    if existing:
        contents[existing[0]] = part_xml.encode("utf-8")
    else:
        # New part: write it, declare its content type, add a relationship, and
        # reference it from the body's sectPr so Word actually shows it.
        part_name = "word/%s1.xml" % ctype
        contents[part_name] = part_xml.encode("utf-8")

        ct = contents["[Content_Types].xml"].decode("utf-8")
        override = ('<Override PartName="/word/%s1.xml" ContentType='
                    '"application/vnd.openxmlformats-officedocument.wordprocessingml.%s+xml"/>'
                    % (ctype, ctype))
        ct = ct.replace("</Types>", override + "</Types>")
        contents["[Content_Types].xml"] = ct.encode("utf-8")

        rels_name = "word/_rels/document.xml.rels"
        rels = contents.get(rels_name, (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
        )).decode("utf-8")
        rid = "rId%s" % ctype
        rel = ('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
               'officeDocument/2006/relationships/%s" Target="%s1.xml"/>'
               % (rid, ctype, ctype))
        rels = rels.replace("</Relationships>", rel + "</Relationships>")
        contents[rels_name] = rels.encode("utf-8")

        doc = contents["word/document.xml"].decode("utf-8")
        if "xmlns:r=" not in doc:
            doc = doc.replace(
                "<w:document ",
                '<w:document xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships" ', 1)
        ref = '<w:%sReference w:type="default" r:id="%s"/>' % (ctype, rid)
        if re.search(r"<w:sectPr[ >]", doc):
            # Insert the reference right after the opening <w:sectPr ...> tag.
            doc = re.sub(r"(<w:sectPr\b[^>]*>)", r"\1" + ref, doc, count=1)
        else:
            doc = doc.replace("</w:body>", "<w:sectPr>" + ref + "</w:sectPr></w:body>", 1)
        contents["word/document.xml"] = doc.encode("utf-8")
        names = list(contents.keys())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in contents.items():
                z.writestr(n, data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def docx_write_footnotes(path: Path, text: str):
    """Persist simple numbered footnotes in word/footnotes.xml. Body markers are
    inserted by the native editor as superscript text; this keeps the note list
    with the document and lets JoeBro render it in the footnotes band."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    notes = []
    for idx, line in enumerate(lines, start=1):
        body = re.sub(r"^\d+\.\s*", "", line).strip()
        notes.append(
            '<w:footnote w:id="%d"><w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p></w:footnote>'
            % (idx, _xml_escape(body))
        )
    footnotes_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
        + "".join(notes) + "</w:footnotes>"
    )

    with zipfile.ZipFile(path) as z:
        contents = {n: z.read(n) for n in z.namelist()}

    contents["word/footnotes.xml"] = footnotes_xml.encode("utf-8")

    ct = contents.get("[Content_Types].xml", b"").decode("utf-8", errors="replace")
    override = ('<Override PartName="/word/footnotes.xml" ContentType='
                '"application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>')
    if "/word/footnotes.xml" not in ct:
        ct = ct.replace("</Types>", override + "</Types>")
        contents["[Content_Types].xml"] = ct.encode("utf-8")

    rels_name = "word/_rels/document.xml.rels"
    rels = contents.get(rels_name, (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
    )).decode("utf-8", errors="replace")
    if "relationships/footnotes" not in rels:
        used = [int(x) for x in re.findall(r'Id="rId(\d+)"', rels)]
        rid = "rId%d" % ((max(used) + 1) if used else 2)
        rel = ('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
               'officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
               % rid)
        rels = rels.replace("</Relationships>", rel + "</Relationships>")
        contents[rels_name] = rels.encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in contents.items():
                z.writestr(n, data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def docx_replace_in_place(path: Path, find: str, replace: str) -> bool:
    """Find/replace inside a .docx while keeping every other part (styles,
    colours, images) byte-for-byte. Replaces within the concatenated run text
    of each paragraph. Returns True if anything changed.
    ponytail: matches within a single paragraph; cross-paragraph spans aren't
    handled — fine for the usual phrase/sentence edit."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    root = ET.fromstring(contents["word/document.xml"])
    body = root.find(_W + "body")
    changed = False
    for p in (list(body) if body is not None else []):
        if p.tag != _W + "p":
            continue
        ts = list(p.iter(_W + "t"))
        joined = "".join(t.text or "" for t in ts)
        if find not in joined:
            continue
        new = joined.replace(find, replace)
        # Put all the new text into the first run; blank the rest. Crude but
        # keeps the paragraph's run formatting from the first run.
        if ts:
            ts[0].text = new
            ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            for t in ts[1:]:
                t.text = ""
            changed = True
    if not changed:
        return False
    contents["word/document.xml"] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                z.writestr(n, contents[n])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_to_docx(path: Path, text: str):
    """Write plain text as a minimal .docx (one paragraph per line). Used when
    the AI creates a brand-new Word file or fully rewrites one."""
    paras = "".join(
        '<w:p><w:r><w:t xml:space="preserve">' + _xml_escape(line) + "</w:t></w:r></w:p>"
        for line in (text.splitlines() or [""]))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + paras + "</w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>")
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("word/document.xml", document)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_doc_text(path: Path) -> str:
    """Document text for the AI — plain text for .docx, CSV for .xlsx, raw otherwise."""
    if is_docx(path):
        return docx_to_text(path)
    if str(path).lower().endswith(".xlsx"):
        from jb_xlsx import xlsx_to_csv
        return xlsx_to_csv(path.read_bytes())
    return path.read_text(encoding="utf-8", errors="replace")


def write_doc_text(path: Path, text: str):
    """Persist full content from the AI — .docx, .xlsx (text is CSV), or atomic text write."""
    if is_docx(path):
        text_to_docx(path, text)
    elif str(path).lower().endswith(".xlsx"):
        from jb_xlsx import csv_to_xlsx
        atomic_write_bytes(path, csv_to_xlsx(text))
    else:
        atomic_write_text(path, text)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"}

TOOL_BLOCK_RE = re.compile(r"```([A-Za-z_][A-Za-z0-9_]*)\n(.*?)```", re.DOTALL)
# Models emit tool calls in several XML shapes; normalize_xml_tools rewrites
# all of them into the fenced form. Direct <create_document><path>..</path>..,
# <invoke name="tool"><parameter name="k">v</parameter></invoke>, and DSML.
XML_TOOL_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>(.*?)</\1>", re.DOTALL)
XML_FIELD_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>(.*?)</\1>", re.DOTALL)
XML_INVOKE_RE = re.compile(r'<invoke\s+name=["\'](\w+)["\']>\s*(.*?)</invoke>', re.DOTALL | re.IGNORECASE)
XML_PARAM_RE = re.compile(r'<parameter\s+name=["\'](\w+)["\']>(.*?)</parameter>', re.DOTALL | re.IGNORECASE)
# Common model aliases -> our tool names (subset of the Pi's _TOOL_NAME_MAP).
TOOL_ALIASES = {
    "shell": "bash", "terminal": "bash", "command": "bash", "run": "bash", "execute": "bash",
    "code": "python", "search": "web_search", "websearch": "web_search",
    "edit": "edit_document", "create": "create_document", "document": "edit_document",
    "update_document": "edit_document",
    "suggest": "suggest_document", "memory": "manage_memory", "tasks": "manage_tasks",
    "skills": "manage_skills", "skill": "manage_skills", "research": "manage_research",
}
OBSOLETE_AI_TOOLS = {
    "manage_documents",
    "chat_with_model", "create_session", "list_sessions", "send_to_session", "pipeline",
    "manage_session", "ask_teacher", "list_models",
    "download_model", "serve_model", "list_served_models", "stop_served_model",
    "list_downloads", "cancel_download", "search_hf_models", "list_cached_models",
    "list_serve_presets", "serve_preset", "adopt_served_model", "list_cookbook_servers",
    "app_api", "api_call",
}
EMAIL_AI_TOOLS = {
    "list_email_accounts", "list_emails", "read_email", "send_email", "reply_to_email",
    "bulk_email", "delete_email", "archive_email", "mark_email_read",
}
PRODUCTION_AI_TOOLS = {
    "bash", "python", "web_search",
    "list_files", "read_file", "read_pdf",
    "create_document", "edit_document", "suggest_document",
    "trigger_research", "manage_research", "manage_memory", "manage_tasks", "manage_skills",
    "manage_chats", "create_event",
} | EMAIL_AI_TOOLS
WRITE_SIDE_EFFECT_TOOLS = {
    "bash", "python",
    "create_document", "edit_document",
    "manage_memory", "manage_tasks", "manage_skills", "trigger_research", "manage_research",
    "manage_chats", "create_event",
    "send_email", "reply_to_email", "bulk_email", "delete_email", "archive_email", "mark_email_read",
}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}

# Native function-calling schemas. DeepSeek (and most API models) emit reliable
# STRUCTURED tool calls when given these, instead of guessing a text format and
# dumping raw markup / ```markdown blocks into the chat.
FUNCTION_TOOL_SCHEMAS = [
    _fn("list_files", "List the files and folders in the bound folder, one level at a time. Pass `path` to look INSIDE a sub-folder (folders are shown with a trailing /). Use before read_file/read_pdf, and drill into sub-folders by listing them with their path.",
        {"path": {"type": "string", "description": "relative sub-folder to list, e.g. 'assets' or 'assets/PRODUCTIONSCROTS' (omit for the top level)"}}, []),
    _fn("read_file", "Read a file from the bound folder. Text files come back as text; IMAGE files (png, jpg, gif, webp, heic) are shown to you visually so you can actually look at them. Read-only, works in the lowest agent permission mode.",
        {"path": {"type": "string", "description": "relative path inside the bound folder"}}, ["path"]),
    _fn("read_pdf", "Extract text from a PDF in the bound folder. This is read-only and works in the lowest agent permission mode; use it to summarize or answer questions about local PDFs.",
        {"path": {"type": "string", "description": "relative PDF path inside the bound folder"}}, ["path"]),
    _fn("create_document", "Create a brand-new file in the bound folder. Works for any editable type, including .docx (Word).",
        {"path": {"type": "string", "description": "relative path, e.g. notes.md or report.docx"},
         "content": {"type": "string", "description": "full file content"}}, ["path", "content"]),
    _fn("edit_document", "Edit an existing file. EITHER make a small in-place change with exact find/replace (quote the existing text verbatim), OR rewrite the whole file by passing `content` with the COMPLETE new text (use this to append to / change the open document). For a .docx header/footer set region.",
        {"path": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"},
         "content": {"type": "string", "description": "the COMPLETE new file content — pass this INSTEAD of find/replace to rewrite the whole file"},
         "region": {"type": "string", "enum": ["body", "header", "footer"], "description": ".docx only: which part to edit (default body)"}}, ["path"]),
    _fn("suggest_document", "Suggest an edit to a file without applying it.",
        {"path": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}, "reason": {"type": "string"}}, ["path", "find", "replace"]),
    _fn("web_search", "Search the web for quick facts / current info.", {"query": {"type": "string"}}, ["query"]),
    _fn("trigger_research", "Start a DEEP RESEARCH report on a topic (multi-source, synthesized). Use this — NOT web_search — when the user asks to 'research' / 'do research on' / 'write a report on' something.",
        {"query": {"type": "string", "description": "the research topic"}}, ["query"]),
    _fn("manage_research", "List, view, archive or delete deep-research reports.",
        {"action": {"type": "string", "enum": ["list", "view", "archive", "delete"]}, "id": {"type": "string"}, "query": {"type": "string"}}, ["action"]),
    _fn("manage_memory", "Store/recall long-term memory about the user.",
        {"action": {"type": "string", "enum": ["add", "search", "list", "edit", "delete"]}, "text": {"type": "string"}, "category": {"type": "string"}, "memory_id": {"type": "string"}}, ["action"]),
    _fn("manage_tasks", "Create/list/edit/delete the user's scheduled tasks. Tasks run as a background agent on their schedule.",
        {"action": {"type": "string", "enum": ["add", "list", "edit", "delete"]}, "title": {"type": "string"}, "prompt": {"type": "string"},
         "schedule": {"type": "string", "enum": ["daily", "weekly", "monthly"]}, "time": {"type": "string", "description": "HH:MM 24h"},
         "weekday": {"type": "integer", "description": "for weekly tasks, which day it fires: 0=Mon, 1=Tue … 6=Sun"},
         "permission_mode": {"type": "string", "enum": ["sandbox", "readonly", "full"], "description": "file access when the task runs: sandbox=bound folder only, readonly=read anywhere, full=read/write anywhere"},
         "id": {"type": "string"}}, ["action"]),
    _fn("manage_skills", "Create/list/edit/delete reusable skills. When adding, write a DETAILED skill, not a one-liner: a clear description, a when_to_use trigger, and a step-by-step procedure.",
        {"action": {"type": "string", "enum": ["add", "list", "edit", "delete"]}, "name": {"type": "string"},
         "description": {"type": "string", "description": "one-line summary of what the skill does"},
         "when_to_use": {"type": "string", "description": "the trigger — what kind of request should invoke this skill"},
         "content": {"type": "string", "description": "the procedure: concrete markdown/numbered steps to follow"},
         "status": {"type": "string", "enum": ["active", "draft", "disabled"]}, "id": {"type": "string"}}, ["action"]),
    _fn("list_email_accounts", "List the user's connected email accounts.", {}, []),
    _fn("list_emails", "List recent emails. Use this to read/pull/summarize the inbox — NEVER use bash/curl for email.",
        {"folder": {"type": "string"}, "limit": {"type": "integer"}, "query": {"type": "string"}}, []),
    _fn("read_email", "Read the full body of one email by uid.",
        {"uid": {"type": "string"}, "folder": {"type": "string"}}, ["uid"]),
    _fn("send_email", "Send an email.",
        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, ["to", "subject", "body"]),
    _fn("archive_email", "Archive an email by uid.", {"uid": {"type": "string"}, "folder": {"type": "string"}}, ["uid"]),
    _fn("delete_email", "Delete an email by uid.", {"uid": {"type": "string"}, "folder": {"type": "string"}}, ["uid"]),
    _fn("mark_email_read", "Mark an email read/unread.", {"uid": {"type": "string"}, "read": {"type": "boolean"}}, ["uid"]),
    _fn("create_event", "Add an event to the user's calendar. YOU resolve the natural-language date/time into ISO-8601 datetimes using today's date given in the system prompt (e.g. 'wednesday 2-5pm' -> the next Wednesday's date with start 14:00 and end 17:00).",
        {"summary": {"type": "string", "description": "the event title"},
         "start": {"type": "string", "description": "ISO-8601 start datetime, e.g. 2026-06-17T14:00:00"},
         "end": {"type": "string", "description": "ISO-8601 end datetime, e.g. 2026-06-17T17:00:00"},
         "all_day": {"type": "boolean", "description": "true for an all-day event (optional)"}}, ["summary", "start", "end"]),
    _fn("manage_chats", "Manage and drive the user's chats — you are the orchestrator over all of them. action=list (every chat), search (find chats/messages containing text), read (recent history of one chat), create (start a new chat), bind_folder (bind a chat to a folder on disk), set_mode (chat/agent), set_permission (a chat's file access: sandbox/readonly/full), send (post a message into a chat and get that chat's agent reply — this is how you DELEGATE coding/file work). Identify a chat with `chat` = its id or name.",
        {"action": {"type": "string", "enum": ["list", "search", "read", "create", "bind_folder", "set_mode", "set_permission", "send"]},
         "chat": {"type": "string", "description": "target chat id or name (read/send/set_mode/set_permission/bind_folder)"},
         "query": {"type": "string", "description": "text to search for across chats (search)"},
         "message": {"type": "string", "description": "message to post into the chat (send)"},
         "mode": {"type": "string", "enum": ["chat", "agent"], "description": "new mode (set_mode); also the starting mode for create"},
         "permission": {"type": "string", "enum": ["sandbox", "readonly", "full"], "description": "file-access level (set_permission); also for create"},
         "folder": {"type": "string", "description": "absolute folder path to bind (bind_folder); also for create"},
         "name": {"type": "string", "description": "name for the new chat (create)"},
         "limit": {"type": "integer"}}, ["action"]),
    _fn("bash", "Run a shell command (only with terminal access + Full Access). NOT for email or web.", {"command": {"type": "string"}}, ["command"]),
    _fn("python", "Run a Python snippet (only with terminal access + Full Access).", {"code": {"type": "string"}}, ["code"]),
]

# Every reserved/built-in function name. A user's custom API tool is sanitized to
# a safe snake_case name and prefixed if it would collide with any of these, so a
# custom tool can never shadow a built-in one in the dispatch loop.
BUILTIN_TOOL_NAMES = (
    {t["function"]["name"] for t in FUNCTION_TOOL_SCHEMAS}
    | PRODUCTION_AI_TOOLS | OBSOLETE_AI_TOOLS | EMAIL_AI_TOOLS | {"write_file"}
)


def sanitize_tool_name(name, taken=None):
    """Turn a user-supplied tool name into a safe function name: lowercase
    snake_case of alnum+underscore, never empty, never colliding with a built-in
    or an already-taken custom name (prefixed/suffixed if it would)."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if not base:
        base = "custom_tool"
    if base[0].isdigit():
        base = "t_" + base
    reserved = set(BUILTIN_TOOL_NAMES) | set(taken or set())
    candidate = base
    if candidate in reserved:
        candidate = "custom_" + base
    n = 2
    while candidate in reserved:
        candidate = f"custom_{base}_{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) — stdio transport. The user registers MCP servers
# (Tier 2 / "Advanced": any server you can launch with a command + args). When a
# server is enabled JoeBro spawns it, performs the MCP handshake over stdin/stdout
# (newline-delimited JSON-RPC 2.0), discovers its tools (tools/list), and offers
# them to the model in Agent mode. Calling a tool spawns the server again, does
# initialize -> tools/call, reads the result, then terminates the process.
#
# ROBUSTNESS is the whole point of this module: it runs inside a threaded HTTP
# server, so a hung subprocess would block a request thread forever. Therefore
# EVERY interaction is STATELESS (spawn -> use -> kill; no long-lived processes)
# and HARD-BOUNDED by a wall-clock timeout, after which the process is killed and
# reaped. Nothing in here ever raises out to the agent loop / tools_for; failures
# come back as a structured error so a broken server can only ever skip itself.

# Where to look for an MCP launch command when the app's PATH is thin (the
# sandboxed app may not include Homebrew). Mirrors JoeBroApp.swift pythonURL().
MCP_PATH_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                 "/opt/homebrew/sbin", "/usr/sbin"]
MCP_CONNECT_TIMEOUT = 15        # initialize + tools/list
MCP_CALL_TIMEOUT = 30           # initialize + tools/call


def mcp_resolve_command(command):
    """Resolve an MCP launch command to an absolute path. Checks common install
    dirs first (the app PATH may lack /opt/homebrew/bin), then shutil.which, then
    the literal string (which Popen will resolve if it's already absolute)."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    if os.path.isabs(cmd):
        return cmd if os.path.exists(cmd) else None
    for d in MCP_PATH_DIRS:
        cand = os.path.join(d, cmd)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(cmd)
    if found:
        return found
    found = shutil.which(cmd, path=os.pathsep.join(MCP_PATH_DIRS))
    if found:
        return found
    return None


def mcp_env():
    """Subprocess env with an augmented PATH so node/npx/uvx resolve their own
    helpers even when the parent process PATH is thin."""
    env = dict(os.environ)
    extra = os.pathsep.join(MCP_PATH_DIRS)
    env["PATH"] = (env.get("PATH", "") + os.pathsep + extra).strip(os.pathsep)
    return env


def mcp_split_args(args):
    """Args may arrive as a list (JSON) or a single shell-style string."""
    if isinstance(args, list):
        return [str(a) for a in args]
    s = (args or "").strip()
    if not s:
        return []
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


class _MCPError(Exception):
    pass


def _mcp_kill(proc):
    """Terminate then hard-kill an MCP subprocess and reap it. Best-effort and
    never raises — used on every exit path so we never leak a process."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream:
                stream.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _mcp_session(command, args, want_call, deadline):
    """Spawn the server, do the MCP handshake, then either tools/list (want_call
    is None) or tools/call(want_call=(name, arguments)). Returns the parsed JSON
    result dict. Raises _MCPError on any failure. The process is always killed
    and reaped before returning. `deadline` is an absolute time.monotonic()."""
    resolved = mcp_resolve_command(command)
    if not resolved:
        raise _MCPError(f"command not found: {command!r} (checked {', '.join(MCP_PATH_DIRS)} and PATH)")
    argv = [resolved] + mcp_split_args(args)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=mcp_env(), text=True, bufsize=1,
            cwd=str(Path.home()))
    except Exception as exc:
        raise _MCPError(f"could not launch {command!r}: {exc}")

    # A background reader thread drains stdout into a queue so every read is
    # bounded by the deadline — a server that hangs without replying can never
    # block the request thread.
    import queue as _queue
    q: "_queue.Queue" = _queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)   # EOF sentinel

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()

    def _send(obj):
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as exc:
            raise _MCPError(f"server closed its input: {exc}")

    def _read_result(expect_id):
        """Read newline JSON until the response with id==expect_id arrives,
        skipping notifications/other messages. Bounded by the deadline."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _MCPError("timed out waiting for server response")
            try:
                line = q.get(timeout=remaining)
            except _queue.Empty:
                raise _MCPError("timed out waiting for server response")
            if line is None:
                err = ""
                try:
                    err = (proc.stderr.read() or "").strip()[:500]
                except Exception:
                    pass
                raise _MCPError("server exited before responding" + (f": {err}" if err else ""))
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue   # non-JSON log line on stdout — ignore
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == expect_id:
                if "error" in msg and msg["error"]:
                    e = msg["error"]
                    raise _MCPError(str(e.get("message") or e) if isinstance(e, dict) else str(e))
                return msg.get("result") or {}

    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "JoeBro", "version": "1.0"}}})
        _read_result(1)
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if want_call is None:
            return _read_result_for_list(_send, _read_result)
        tool_name, arguments = want_call
        _send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": tool_name, "arguments": arguments or {}}})
        return _read_result(3)
    finally:
        _mcp_kill(proc)


def _read_result_for_list(_send, _read_result):
    _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return _read_result(2)


def mcp_discover(command, args, timeout=MCP_CONNECT_TIMEOUT):
    """Connect to an MCP server and return its tool list as
    [{"name","description","inputSchema"}]. Raises _MCPError on failure."""
    deadline = time.monotonic() + max(1, timeout)
    result = _mcp_session(command, args, None, deadline)
    tools = result.get("tools") or []
    out = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append({
            "name": str(t.get("name")),
            "description": str(t.get("description") or ""),
            "inputSchema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}},
        })
    return out


def mcp_call_tool(command, args, tool_name, arguments, timeout=MCP_CALL_TIMEOUT):
    """Spawn the server, call one tool, return its text output. Raises _MCPError
    on failure. Concatenates text content blocks; flags isError as an error."""
    deadline = time.monotonic() + max(1, timeout)
    result = _mcp_session(command, args, (tool_name, arguments), deadline)
    blocks = result.get("content") or []
    parts = []
    for b in blocks:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif b.get("text"):
                parts.append(str(b.get("text")))
    text = "\n".join(parts).strip()
    if not text and not blocks:
        text = json.dumps(result, ensure_ascii=False)[:4000]
    is_error = bool(result.get("isError"))
    return text, is_error


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "uploads").mkdir(exist_ok=True)
        (self.root / "previews").mkdir(exist_ok=True)
        self.db_path = self.root / "joebro.sqlite3"
        self.lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        with self.db() as db:
            db.executescript(
                """
                create table if not exists sessions (
                    id text primary key,
                    name text not null,
                    model text,
                    endpoint_url text,
                    endpoint_id text,
                    mode text default 'chat',
                    archived integer default 0,
                    is_important integer default 0,
                    workdir text,
                    created_at text not null
                );
                create table if not exists messages (
                    id integer primary key autoincrement,
                    session_id text not null,
                    role text not null,
                    content text not null,
                    metadata text,
                    created_at text not null
                );
                create table if not exists endpoints (
                    id text primary key,
                    name text not null,
                    base_url text not null,
                    api_key text,
                    is_enabled integer default 1,
                    created_at text not null
                );
                create table if not exists prefs (
                    key text primary key,
                    value text
                );
                create table if not exists memory (
                    id text primary key,
                    text text not null,
                    category text default 'general',
                    pinned integer default 0,
                    created_at text not null
                );
                create table if not exists events (
                    id text primary key,
                    summary text not null,
                    dtstart text not null,
                    dtend text not null,
                    all_day integer default 0,
                    location text default '',
                    description text default '',
                    caldav_href text default '',
                    caldav_etag text default '',
                    created_at text not null
                );
                create table if not exists uploads (
                    id text primary key,
                    name text not null,
                    path text not null,
                    mime text default '',
                    created_at text not null
                );
                create table if not exists email_sources (
                    id text primary key,
                    type text not null,
                    display text not null,
                    imap_host text not null,
                    imap_port integer not null,
                    imap_ssl integer default 1,
                    imap_user text not null,
                    imap_password text default '',
                    smtp_host text default '',
                    smtp_port integer default 465,
                    smtp_ssl integer default 1,
                    smtp_user text default '',
                    smtp_password text default '',
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists documents (
                    id text primary key,
                    title text not null,
                    language text default 'markdown',
                    current_content text default '',
                    version_count integer default 1,
                    file_path text,
                    archived integer default 0,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists tasks (
                    id text primary key,
                    title text not null,
                    prompt text default '',
                    schedule text default '',
                    status text default 'active',
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists skills (
                    id text primary key,
                    name text not null,
                    description text default '',
                    content text default '',
                    status text default 'draft',
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists research (
                    id text primary key,
                    query text not null,
                    status text default 'queued',
                    content text default '',
                    archived integer default 0,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists api_tools (
                    id text primary key,
                    name text not null,
                    func_name text not null,
                    base_url text not null,
                    api_key text default '',
                    method text default 'GET',
                    description text default '',
                    is_enabled integer default 1,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists mcp_servers (
                    id text primary key,
                    name text not null,
                    command text not null,
                    args text default '',
                    enabled integer default 1,
                    tools_json text default '[]',
                    error text default '',
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists plugins (
                    id text primary key,
                    name text not null,
                    kind text default 'foreground',
                    repo_path text default '',
                    description text default '',
                    is_enabled integer default 1,
                    source text default 'user',
                    permission_mode text default 'sandbox',
                    created_at text not null,
                    updated_at text not null
                );
                """
            )
            # plugins.permission_mode = the file-access level the plugin's tools run at.
            try:
                db.execute("alter table plugins add column permission_mode text default 'sandbox'")
            except sqlite3.OperationalError:
                pass
            # Bundled dependency-free computer-use plugin (osascript / macOS
            # Accessibility). Runs at full access by default.
            _macos_use_blurb = ("Dependency-free macOS computer use: the agent can open apps, read "
                                "the on-screen UI, click buttons and menus, type, and take screenshots "
                                "via macOS Accessibility.")
            db.execute("delete from plugins where id='plugin_osaurus_macos_use'")
            db.execute(
                "insert or ignore into plugins"
                "(id,name,kind,repo_path,description,is_enabled,source,permission_mode,created_at,updated_at) "
                "values('plugin_macos_use','JoeBro macOS Use','foreground','',?,1,'bundled','full',?,?)",
                (_macos_use_blurb, now_iso(), now_iso()),
            )
            # Migrate any earlier bundled row to drop the old external link + blurb.
            db.execute("update plugins set repo_path='', description=? where id='plugin_macos_use'",
                       (_macos_use_blurb,))
            db.execute("delete from endpoints where lower(name) like '%fake%' or lower(name) like '%qa%' or lower(base_url) like '%fake%'")
            db.execute("delete from prefs where key in ('default_endpoint_id', 'default_model_id') and value like '%fake%'")
            for col in ("caldav_href", "caldav_etag"):
                try:
                    db.execute(f"alter table events add column {col} text default ''")
                except sqlite3.OperationalError:
                    pass
            try:
                db.execute("alter table tasks add column scheduled_time text default ''")
            except sqlite3.OperationalError:
                pass
            # folder: optional project-folder name a chat is grouped under in the
            # sidebar (null/empty = ungrouped). Nullable, so existing DBs migrate
            # without a crash.
            try:
                db.execute("alter table sessions add column folder text")
            except sqlite3.OperationalError:
                pass
            # sort_order: user-defined sidebar order for drag-to-reorder. Backfilled
            # from created_at so existing chats keep their newest-first order.
            try:
                db.execute("alter table sessions add column sort_order real")
                db.execute("update sessions set sort_order = -julianday(created_at) where sort_order is null")
            except sqlite3.OperationalError:
                pass
            # progress: JSON {phase, message} the research worker advances through
            # ("Searching the web" -> "Reading sources" -> "Writing report" ->
            # "Complete") so the status endpoint reports honest progress.
            try:
                db.execute("alter table research add column progress text default ''")
            except sqlite3.OperationalError:
                pass
            for stmt in ("alter table skills add column confidence integer default 60",
                         "alter table skills add column when_to_use text default ''",
                         "alter table research add column images text default ''",
                         "alter table memory add column use_count integer default 0",
                         "alter table memory add column last_used text default ''",
                         "alter table tasks add column last_run text default ''",
                         "alter table tasks add column permission_mode text default 'sandbox'",
                         "alter table tasks add column repeat_day integer default 0",
                         "alter table sessions add column permission_mode text default 'sandbox'"):
                try:
                    db.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            db.commit()
            self._seed_default_tasks(db)

    def _seed_default_tasks(self, db):
        """Seed example tasks + skills ONCE (tracked by a pref flag, not by table
        emptiness — so they don't reappear after you delete them, and existing
        installs that never got them still get seeded). The skill audit is
        enabled (it powers the self-improving loop); the rest are paused/example."""
        now = now_iso()
        skills = [
            ("Daily standup",
             "Produce a crisp daily standup update.",
             "When I ask for a \"standup\", \"daily update\", or \"what did I do today\".",
             "# Daily Standup\n\n"
             "Turn what I tell you (or what's in the current chat / bound folder) into a clean standup update.\n\n"
             "## Output format\n"
             "Reply with exactly three short sections, one or two bullets each:\n\n"
             "- **Yesterday / done:** what was completed\n"
             "- **Today / next:** what I'm working on next\n"
             "- **Blockers:** anything stuck (write \"None\" if clear)\n\n"
             "## Rules\n"
             "- Keep each bullet to one line.\n"
             "- Be concrete (names, tickets, files), not vague.\n"
             "- No preamble or sign-off — just the three sections."),
            ("Polish my writing",
             "Tighten and clarify text without changing meaning.",
             "When I ask you to \"polish\", \"tighten\", \"proofread\", or \"clean up\" some writing.",
             "# Polish My Writing\n\n"
             "Improve a piece of text I give you (or the selected/open document) while preserving my meaning and voice.\n\n"
             "## What to do\n"
             "1. Fix grammar, spelling, and punctuation.\n"
             "2. Cut filler and tighten wordy phrasing.\n"
             "3. Improve flow and clarity; keep my tone and intent.\n"
             "4. Preserve any Markdown structure, code, and links.\n\n"
             "## Rules\n"
             "- Do **not** change the meaning, add new claims, or over-formalise.\n"
             "- Return only the revised text (no commentary) unless I ask for an explanation."),
        ]
        # Upgrade earlier example skills: give one-liner bodies their full
        # procedure (only when still the short seed), and backfill when_to_use
        # if it's empty (the column was added after the first seed). Never
        # clobber a procedure the user has already edited.
        for name, desc, when_to_use, procedure in skills:
            db.execute("update skills set content=?, description=? where name=? and length(content) < 140",
                       (procedure, desc, name))
            db.execute("update skills set when_to_use=? where name=? and (when_to_use is null or when_to_use='')",
                       (when_to_use, name))
        memory_audit_prompt = (
            "Audit my memories using manage_memory (action: list). Delete every memory that is stale — "
            "use_count is 0 and last_used is empty (or older than two weeks) — using manage_memory "
            "(action: delete, id: <id>). Keep anything pinned. Then briefly report what you removed.")
        # One-time upgrade for existing installs (these run regardless of the
        # seed flag): move a still-daily "Skill audit" to weekly, and insert the
        # "Memory audit" task if it isn't already present.
        db.execute("update tasks set schedule='weekly' where title='Skill audit' and schedule='daily'")
        if not db.execute("select 1 from tasks where title='Memory audit'").fetchone():
            db.execute(
                "insert into tasks(id,title,prompt,schedule,scheduled_time,status,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?)",
                ("task_" + os.urandom(6).hex(), "Memory audit", memory_audit_prompt,
                 "weekly", "07:00", "active", now, now))
        if db.execute("select 1 from prefs where key='seeded_defaults_v1'").fetchone():
            db.commit()
            return   # already seeded once — only the upgrades above run for them
        for name, desc, when_to_use, procedure in skills:
            db.execute(
                "insert into skills(id,name,description,content,when_to_use,status,confidence,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                ("skill_" + os.urandom(6).hex(), name, desc, procedure, when_to_use, "active", 60, now, now))
        defaults = [
            ("Skill audit",
             "Review my skills using manage_skills (action: list). Delete every skill whose confidence "
             "is below 30 using manage_skills (action: delete, id: <id>). Then briefly report what you removed.",
             "weekly", "07:00", "active"),
            ("Morning email summary",
             "Summarise every email I've received since 10pm last night: group by sender, give one line each, "
             "and call out anything that needs a reply or action.",
             "daily", "07:00", "paused"),
        ]
        for title, prompt, sched, t, status in defaults:
            db.execute(
                "insert into tasks(id,title,prompt,schedule,scheduled_time,status,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?)",
                ("task_" + os.urandom(6).hex(), title, prompt, sched, t, status, now, now),
            )
        db.execute("insert or replace into prefs(key,value) values('seeded_defaults_v1', ?)", (json.dumps(True),))
        db.commit()

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def rows(self, sql, args=()):
        with self.lock, self.db() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        with self.lock, self.db() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def exec(self, sql, args=()):
        with self.lock, self.db() as db:
            cur = db.execute(sql, args)
            db.commit()
            return cur.lastrowid

__all__ = [
    "APP_NAME",
    "BUILTIN_TOOL_NAMES",
    "BaseHTTPRequestHandler",
    "DEFAULT_PORT",
    "EMAIL_AI_TOOLS",
    "ET",
    "FUNCTION_TOOL_SCHEMAS",
    "IMAGE_EXTS",
    "MACOS_USE_TOOL",
    "MCP_CALL_TIMEOUT",
    "MCP_CONNECT_TIMEOUT",
    "MCP_PATH_DIRS",
    "MIMEMultipart",
    "MIMEText",
    "OBSOLETE_AI_TOOLS",
    "PRODUCTION_AI_TOOLS",
    "Path",
    "Store",
    "TOOL_ALIASES",
    "TOOL_BLOCK_RE",
    "ThreadingHTTPServer",
    "WRITE_SIDE_EFFECT_TOOLS",
    "XML_FIELD_RE",
    "XML_INVOKE_RE",
    "XML_PARAM_RE",
    "XML_TOOL_RE",
    "_MACOS_KEY_CODES",
    "_MACOS_MODS",
    "_MCPError",
    "_MODELS_CACHE",
    "_MODELS_CACHE_LOCK",
    "_MODELS_TTL",
    "_PATH_LOCKS",
    "_PATH_LOCKS_GUARD",
    "_PERMISSION_GUARD",
    "_PERMISSION_REGISTRY",
    "_RESEARCH_CANCEL",
    "_RESEARCH_GUARD",
    "_RESEARCH_STARTED",
    "_SESSION_ALLOW",
    "_W",
    "_applescript_str",
    "_fn",
    "_mcp_kill",
    "_mcp_session",
    "_read_result_for_list",
    "_xml_escape",
    "app_support_dir",
    "argparse",
    "atomic_write_text", "atomic_write_bytes",
    "backup_existing",
    "base64",
    "datetime",
    "docx_extras",
    "docx_replace_in_place",
    "docx_to_text",
    "docx_write_extra",
    "docx_write_footnotes",
    "email",
    "hashlib",
    "imaplib",
    "is_docx",
    "json",
    "lock_for_path",
    "macos_use_argv",
    "mcp_call_tool",
    "mcp_discover",
    "mcp_env",
    "mcp_resolve_command",
    "mcp_split_args",
    "mimetypes",
    "now_iso",
    "os",
    "re",
    "read_doc_text",
    "sanitize_tool_name",
    "shlex",
    "shutil",
    "smtplib",
    "socket",
    "sqlite3",
    "subprocess",
    "tempfile",
    "text_to_docx",
    "textwrap",
    "threading",
    "time",
    "timedelta",
    "timezone",
    "urllib",
    "write_doc_text",
    "zipfile",
]
