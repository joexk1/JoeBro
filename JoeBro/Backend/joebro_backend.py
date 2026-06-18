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


def app_support_dir():
    root = os.environ.get("JOEBRO_DATA_DIR")
    if root:
        return Path(root)
    return Path.home() / "Library" / "Application Support" / APP_NAME


def atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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
    """Document text for the AI — extracted plain text for .docx, raw otherwise."""
    if is_docx(path):
        return docx_to_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def write_doc_text(path: Path, text: str):
    """Persist full content from the AI — plain-text .docx, atomic text write otherwise."""
    if is_docx(path):
        text_to_docx(path, text)
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
    "create_event",
} | EMAIL_AI_TOOLS
WRITE_SIDE_EFFECT_TOOLS = {
    "bash", "python",
    "create_document", "edit_document",
    "manage_memory", "manage_tasks", "manage_skills", "trigger_research", "manage_research",
    "create_event",
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
    _fn("manage_tasks", "Create/list/edit/delete the user's scheduled tasks.",
        {"action": {"type": "string", "enum": ["add", "list", "edit", "delete"]}, "title": {"type": "string"}, "prompt": {"type": "string"}, "schedule": {"type": "string", "enum": ["daily", "weekly", "monthly"]}, "time": {"type": "string", "description": "HH:MM 24h"}, "id": {"type": "string"}}, ["action"]),
    _fn("manage_skills", "Create/list/edit/delete reusable skills.",
        {"action": {"type": "string", "enum": ["add", "list", "edit", "delete"]}, "name": {"type": "string"}, "content": {"type": "string"}, "id": {"type": "string"}}, ["action"]),
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
    _fn("bash", "Run a shell command (only with terminal access + Full Access). NOT for email or web.", {"command": {"type": "string"}}, ["command"]),
    _fn("python", "Run a Python snippet (only with terminal access + Full Access).", {"code": {"type": "string"}}, ["code"]),
]


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
                """
            )
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
                         "alter table tasks add column permission_mode text default 'sandbox'"):
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


class Handler(BaseHTTPRequestHandler):
    server_version = "JoeBroLocal/1.0"

    @property
    def store(self) -> Store:
        return self.server.store

    def log_message(self, fmt, *args):
        if os.environ.get("JOEBRO_BACKEND_LOGS") == "1":
            super().log_message(fmt, *args)

    def _path(self):
        return urllib.parse.urlparse(self.path)

    def _query(self):
        return dict(urllib.parse.parse_qsl(self._path().query))

    def _body_bytes(self):
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._body_bytes()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _form_body(self):
        raw = self._body_bytes().decode("utf-8")
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[-1] for k, v in parsed.items()}

    def _send(self, status=200, ctype="application/json"):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Content-Type", ctype)
        self.end_headers()

    def json(self, obj, status=200):
        self._send(status)
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def not_found(self):
        self.json({"detail": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        path = self._path().path
        q = self._query()
        if path in ("/api/ping", "/api/health"):
            return self.json({"ok": True, "local": True})
        if path == "/api/sessions":
            rows = self.store.rows(
                "select id,name,model,mode,archived,is_important,created_at from sessions order by datetime(created_at) desc"
            )
            return self.json([self.session_json(r) for r in rows])
        if path.startswith("/api/history/"):
            sid = path.rsplit("/", 1)[-1]
            session = self.store.one("select * from sessions where id=?", (sid,))
            msgs = self.store.rows(
                "select id,role,content,metadata from messages where session_id=? order by id", (sid,)
            )
            return self.json({
                "history": [
                    {"role": m["role"], "content": m["content"], "metadata": self.loads(m["metadata"]) | {"_db_id": m["id"]}}
                    for m in msgs
                ],
                "model": (session or {}).get("model"),
                "name": (session or {}).get("name"),
            })
        if path == "/api/models":
            refresh = str(q.get("refresh") or "").lower() in ("1", "true", "yes")
            return self.json({"models": [], "items": self.model_items(refresh=refresh)})
        if path == "/api/model-endpoints":
            eps = self.store.rows("select id,name,base_url,is_enabled from endpoints order by created_at")
            return self.json([
                {"id": e["id"], "name": e["name"], "base_url": e["base_url"], "model_type": "local", "is_enabled": bool(e["is_enabled"])}
                for e in eps
            ])
        if path.startswith("/api/model-endpoints/") and path.endswith("/models"):
            ep_id = path.split("/")[-2]
            return self.json(self.endpoint_models(ep_id))
        if path == "/api/prefs":
            rows = self.store.rows("select key,value from prefs")
            return self.json({r["key"]: self.loads(r["value"]) for r in rows})
        if path == "/api/email/status":
            sources = self.email_sources()
            first = sources[0] if sources else {}
            return self.json({
                "configured": bool(sources),
                "type": first.get("type") or "",
                "display": first.get("display") or "",
                "sources": [self.email_source_json(a) for a in sources],
            })
        if path == "/api/calendar/status":
            cfg = self.pref("calendar_connection") or {}
            return self.json({
                "configured": bool(cfg.get("type")),
                "type": cfg.get("type") or "",
                "display": cfg.get("display") or "",
                "macos_authorized": bool(cfg.get("macos_authorized")),
            })
        if path == "/api/memory":
            rows = self.store.rows("select * from memory order by pinned desc, datetime(created_at) desc")
            return self.json({"memory": [self.memory_json(r) for r in rows]})
        if path == "/api/calendar/events":
            return self.calendar_events(q)
        if path == "/api/documents/library":
            return self.documents_library(q)
        if path.startswith("/api/document/") and path.endswith("/export-pdf"):
            return self.export_document(path.split("/")[3])
        if path.startswith("/api/document/"):
            return self.get_document(path.rsplit("/", 1)[-1])
        if path.startswith("/api/upload/"):
            return self.serve_upload(path.rsplit("/", 1)[-1])
        if path.startswith("/api/session/") and "/workdir" in path:
            return self.workdir_get(path, q)
        if path == "/api/email/list":
            return self.email_list(q)
        if path == "/api/email/folders":
            return self.email_folders()
        if path.startswith("/api/email/read/"):
            return self.email_read(path.rsplit("/", 1)[-1])
        if path == "/api/research/active":
            rows = self.store.rows("select id,query from research where archived=0 and status in ('queued','running') order by datetime(created_at) desc")
            return self.json({"active": [{"session_id": r["id"], "id": r["id"], "query": r["query"]} for r in rows]})
        if path == "/api/research/library":
            rows = self.store.rows("select * from research where archived=0 order by datetime(created_at) desc")
            return self.json({"research": [self.research_json(r) for r in rows]})
        if path.startswith("/api/research/status/"):
            row = self.store.one("select * from research where id=?", (path.rsplit("/", 1)[-1],))
            if not row:
                return self.json({"status": "not_found", "progress": {}}, 404)
            status = row.get("status") or "running"
            # Honest progress: report whatever phase the worker actually reached,
            # not a fixed duration it can't promise.
            prog = self.loads(row.get("progress")) or {}
            if not prog.get("phase"):
                prog = {"phase": ("Complete" if status == "completed" else
                                  "Cancelled" if status == "cancelled" else "Searching the web"),
                        "message": row.get("query") or ""}
            return self.json({"status": status, "progress": prog})
        if path.startswith("/api/research/detail/"):
            row = self.store.one("select * from research where id=?", (path.rsplit("/", 1)[-1],))
            if not row:
                return self.json({"detail": "not found"}, 404)
            d = self.research_json(row)
            # Inline images go into the RENDERED report only — the .md download
            # (raw content) stays image-free.
            d["report"] = self._report_with_images(row.get("content") or "", self.loads(row.get("images")) or [])
            return self.json(d)
        if path == "/api/notes":
            return self.json({"notes": []})
        if path == "/api/tasks":
            rows = self.store.rows("select * from tasks order by datetime(created_at) desc")
            return self.json({"tasks": [self.task_json(r) for r in rows]})
        if path == "/api/skills":
            rows = self.store.rows("select * from skills order by datetime(created_at) desc")
            return self.json({"skills": [self.skill_json(r) for r in rows]})
        return self.not_found()

    def do_POST(self):
        path = self._path().path
        if path == "/api/session":
            f = self._form_body()
            sid = "s_" + os.urandom(8).hex()
            self.store.exec(
                "insert into sessions(id,name,model,endpoint_url,endpoint_id,mode,created_at) values(?,?,?,?,?,?,?)",
                (sid, f.get("name") or "New Chat", f.get("model") or "", f.get("endpoint_url") or "",
                 f.get("endpoint_id") or "", "chat", now_iso()),
            )
            return self.json(self.session_json(self.store.one("select * from sessions where id=?", (sid,))))
        if path.startswith("/api/permission/"):
            rid = path.rsplit("/", 1)[-1]
            decision = (self._json_body().get("decision") or "deny").lower()
            with _PERMISSION_GUARD:
                entry = _PERMISSION_REGISTRY.get(rid)
                if entry:
                    entry["decision"] = decision
                    entry["event"].set()
            return self.json({"ok": True})
        if path == "/api/chat_stream":
            return self.chat_stream()
        if path == "/api/chat":
            # Background AI jobs (skill/task generators) post message+session
            # only — the session row carries which model to use.
            f = self._form_body()
            sess = self.store.one("select * from sessions where id=?", (f.get("session") or "",)) or {}
            f.setdefault("model", sess.get("model") or "")
            f.setdefault("endpoint_id", sess.get("endpoint_id") or "")
            return self.json({"response": self.chat_reply_with_tools(f)})
        if path.endswith("/message") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            body = self._json_body()
            self.add_message(sid, body.get("role", "assistant"), body.get("content", ""), body.get("metadata") or {})
            return self.json({"ok": True})
        if path.endswith("/important") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            val = 1 if self._form_body().get("important") == "true" else 0
            self.store.exec("update sessions set is_important=? where id=?", (val, sid))
            return self.json({"ok": True})
        if path.endswith("/archive") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            self.store.exec("update sessions set archived=1 where id=?", (sid,))
            return self.json({"ok": True})
        if path.endswith("/compact") and path.startswith("/api/session/"):
            return self.json(self.manual_compact(path.split("/")[3]))
        if path.endswith("/delete-messages") and path.startswith("/api/session/"):
            body = self._json_body()
            for mid in body.get("msg_ids", []):
                self.store.exec("delete from messages where id=?", (str(mid),))
            return self.json({"ok": True})
        if path.endswith("/mark-stopped"):
            return self.json({"ok": True})
        if path == "/api/model-endpoints":
            f = self._form_body()
            ep_id = "ep_" + os.urandom(5).hex()
            self.store.exec(
                "insert into endpoints(id,name,base_url,api_key,created_at) values(?,?,?,?,?)",
                (ep_id, f.get("name") or "Local endpoint", f.get("base_url") or "", f.get("api_key") or "", now_iso()),
            )
            return self.json({"ok": True, "id": ep_id})
        if path == "/api/email/connect-imap":
            body = self._json_body()
            self.upsert_email_account("imap", body)
            return self.json({"ok": True, "configured": True, "type": "imap"})
        if path == "/api/email/disconnect":
            account_id = (self._query().get("id") or "").strip()
            if account_id:
                self.store.exec("delete from email_sources where id=?", (account_id,))
            else:
                self.store.exec("delete from email_sources")
            return self.json({"ok": True, "configured": False})
        if path == "/api/calendar/connect-caldav":
            body = self._json_body()
            cfg = {
                "type": "caldav",
                "display": body.get("principal") or body.get("url") or "CalDAV source",
                "url": body.get("url") or "",
                "principal": body.get("principal") or "",
                "password": body.get("password") or "",
                "calendar_names": body.get("calendar_names") or [],
                "updated_at": now_iso(),
            }
            try:
                self.validate_caldav(cfg)
            except Exception as exc:
                return self.json({"detail": f"CalDAV connection failed: {exc}"}, 400)
            self.set_pref("calendar_connection", cfg)
            return self.json({"ok": True, "configured": True, "type": "caldav"})
        if path == "/api/calendar/connect-macos":
            body = self._json_body()
            cfg = {
                "type": "macos",
                "display": "macOS Calendar",
                "macos_authorized": bool(body.get("authorized")),
                "updated_at": now_iso(),
            }
            self.set_pref("calendar_connection", cfg)
            return self.json({"ok": True, "configured": True, "type": "macos"})
        if path == "/api/calendar/disconnect":
            self.set_pref("calendar_connection", {})
            return self.json({"ok": True, "configured": False})
        if path == "/api/memory/add":
            body = self._json_body()
            mid = "mem_" + os.urandom(6).hex()
            self.store.exec(
                "insert into memory(id,text,category,created_at) values(?,?,?,?)",
                (mid, body.get("text") or "", body.get("category") or "general", now_iso()),
            )
            return self.json({"ok": True, "id": mid})
        if path == "/api/document":
            return self.create_document(self._json_body())
        if path.endswith("/pin") and path.startswith("/api/memory/"):
            mid = path.split("/")[3]
            row = self.store.one("select pinned from memory where id=?", (mid,))
            self.store.exec("update memory set pinned=? where id=?", (0 if row and row["pinned"] else 1, mid))
            return self.json({"ok": True})
        if path == "/api/calendar/events":
            return self.create_event(self._json_body())
        if path == "/api/calendar/quick-parse":
            return self.json({"ok": True, "event": self.parse_natural_calendar_event(self._json_body().get("text", "New event"))})
        if path == "/api/upload":
            return self.handle_upload("files")
        if path == "/api/email/compose-upload":
            return self.handle_upload("file", for_email=True)
        if path == "/api/email/send":
            return self.email_send(self._json_body())
        if path.startswith("/api/email/mark-read/"):
            return self.email_set_read(path.rsplit("/", 1)[-1], True)
        if path.startswith("/api/email/mark-unread/"):
            return self.email_set_read(path.rsplit("/", 1)[-1], False)
        if path.startswith("/api/email/archive/"):
            return self.email_archive(path.rsplit("/", 1)[-1])
        if path.startswith("/api/session/") and "/workdir" in path:
            return self.workdir_post(path)
        if path == "/api/stt/transcribe":
            return self.json({"text": ""})
        if path == "/api/ai-check":
            return self.ai_check(self._json_body())
        if path == "/api/research/start":
            body = self._json_body()
            q = body.get("query") or body.get("topic") or "Untitled research"
            rid = self.create_research(q, body.get("endpoint_id"), body.get("model"))
            return self.json({"ok": True, "session_id": rid, "id": rid,
                              "active": [{"session_id": rid, "id": rid, "query": q}]})
        if path.startswith("/api/research/cancel/"):
            return self.cancel_research(path.rsplit("/", 1)[-1])
        if path.startswith("/api/research/"):
            return self.json({"ok": True})
        if path.startswith("/api/email/"):
            return self.json({"success": True, "summary": "", "reply": ""})
        if path.startswith("/api/tasks/") and path.rsplit("/", 1)[-1] in ("pause", "resume"):
            parts = path.split("/")
            tid = parts[3] if len(parts) > 3 else ""
            action = parts[4] if len(parts) > 4 else ""
            status = "paused" if action == "pause" else "active"
            self.store.exec("update tasks set status=?, updated_at=? where id=?", (status, now_iso(), tid))
            row = self.store.one("select * from tasks where id=?", (tid,))
            return self.json({"ok": bool(row), "task": self.task_json(row) if row else None})
        if path.startswith("/api/tasks/") and path.endswith("/run"):
            tid = path.split("/")[3] if len(path.split("/")) > 3 else ""
            if not self.store.one("select id from tasks where id=?", (tid,)):
                return self.json({"ok": False, "detail": "Task not found"}, 404)
            result = self.execute_task(tid)
            return self.json({"ok": True, "result": result or "Couldn't run — add a model endpoint in Settings first."})
        if path == "/api/tasks" or path == "/api/tasks/add":
            return self.json(self.upsert_task(self._json_body()))
        if path == "/api/skills" or path == "/api/skills/add":
            return self.json(self.upsert_skill(self._json_body()))
        return self.not_found()

    def do_PUT(self):
        path = self._path().path
        if path.startswith("/api/session/") and path.endswith("/workdir"):
            return self.workdir_post(path)
        if path.startswith("/api/session/"):
            sid = path.split("/")[3]
            f = self._form_body()
            fields = []
            vals = []
            for k in ("name", "model", "endpoint_url", "endpoint_id", "mode"):
                if k in f:
                    fields.append(f"{k}=?")
                    vals.append(f[k])
            if fields:
                vals.append(sid)
                self.store.exec(f"update sessions set {','.join(fields)} where id=?", vals)
            return self.json({"ok": True})
        if path.startswith("/api/prefs/"):
            key = path.rsplit("/", 1)[-1]
            val = self._json_body().get("value")
            self.store.exec("insert or replace into prefs(key,value) values(?,?)", (key, json.dumps(val)))
            return self.json({"ok": True})
        if path.startswith("/api/memory/"):
            mid = path.rsplit("/", 1)[-1]
            f = self._form_body()
            self.store.exec("update memory set text=?, category=? where id=?", (f.get("text", ""), f.get("category", "general"), mid))
            return self.json({"ok": True})
        if path.startswith("/api/tasks/"):
            return self.json(self.upsert_task(self._json_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/skills/"):
            return self.json(self.upsert_skill(self._json_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/research/") and path.endswith("/archive"):
            rid = path.split("/")[3]
            self.store.exec("update research set archived=1, updated_at=? where id=?", (now_iso(), rid))
            return self.json({"ok": True})
        if path.startswith("/api/calendar/events/"):
            uid = path.rsplit("/", 1)[-1]
            return self.update_event(uid, self._json_body())
        if path.startswith("/api/document/"):
            return self.update_document(path.rsplit("/", 1)[-1], self._json_body())
        if path.startswith("/api/model-endpoints/") and path.endswith("/models"):
            ep_id = path.split("/")[-2]
            hidden = self._json_body().get("hidden") or []
            self.set_pref("hidden_models_" + ep_id, hidden)
            return self.json({"ok": True})
        return self.not_found()

    do_PATCH = do_PUT

    def do_DELETE(self):
        path = self._path().path
        if path.startswith("/api/session/"):
            self.store.exec("delete from sessions where id=?", (path.split("/")[3],))
            return self.json({"ok": True})
        if path.startswith("/api/model-endpoints/"):
            self.store.exec("delete from endpoints where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/memory/"):
            self.store.exec("delete from memory where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/tasks/"):
            self.store.exec("delete from tasks where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/skills/"):
            self.store.exec("delete from skills where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/research/"):
            self.store.exec("delete from research where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/calendar/events/"):
            return self.delete_event(path.rsplit("/", 1)[-1])
        if path.startswith("/api/document/"):
            self.store.exec("update documents set archived=1, updated_at=? where id=?", (now_iso(), path.rsplit("/", 1)[-1]))
            return self.json({"ok": True})
        if path.startswith("/api/email/delete/"):
            return self.email_delete(path.rsplit("/", 1)[-1])
        return self.not_found()

    def chat_stream(self):
        f = self._form_body()
        sid = f.get("session")
        message = f.get("message", "")
        sess = self.store.one("select * from sessions where id=?", (sid or "",)) or {}
        f.setdefault("model", sess.get("model") or "")
        f.setdefault("endpoint_id", sess.get("endpoint_id") or "")
        f.setdefault("endpoint_url", sess.get("endpoint_url") or "")
        if f.get("model") or f.get("endpoint_id") or f.get("endpoint_url"):
            self.store.exec(
                "update sessions set model=?, endpoint_id=?, endpoint_url=? where id=?",
                (
                    f.get("model") or sess.get("model") or "",
                    f.get("endpoint_id") or sess.get("endpoint_id") or "",
                    f.get("endpoint_url") or sess.get("endpoint_url") or "",
                    sid,
                ),
            )
        self.add_message(sid, "user", message, {})
        # Open the SSE stream BEFORE running the agent so tool rows and reasoning
        # arrive at the client AS THEY HAPPEN, not after the whole loop finishes.
        self._send(200, "text/event-stream")
        started = time.time()

        # If the user switches chats / closes the tab mid-run, the client drops
        # the SSE socket. Swallow the write error and keep computing server-side
        # so the agent still finishes and the reply is persisted (loaded when
        # they come back) — otherwise a backgrounded turn silently loses its answer.
        client_alive = {"ok": True}

        def sse(obj):
            if not client_alive["ok"]:
                return
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_alive["ok"] = False

        def emit(obj):
            # Live events from run_agent. A doc payload is streamed into the
            # editor; everything else (tool_start/tool_output/thinking delta)
            # passes straight through with the existing SSE shapes.
            doc = obj.get("_doc")
            if doc is not None:
                if obj.get("_tool") == "create_document":
                    self._stream_document(sse, doc)
                else:
                    sse({"type": "doc_update", "doc_id": doc.get("doc_id", ""),
                         "title": doc.get("title", ""), "language": doc.get("language", "markdown"),
                         "content": doc.get("content", ""), "version": 1})
            else:
                sse(obj)

        native = self.native_agent_tool_result(sid, message, f)
        thinking = ""
        tool_events = []
        streamed_live = False   # run_agent streams content+thinking deltas itself
        if native:
            if len(native) == 3:
                reply, tool_events, thinking = native
            else:
                reply, tool_events = native
            # Native path produced events up-front — replay them live.
            for ev in tool_events:
                emit({"type": "tool_start", "tool": ev.get("tool"), "command": ev.get("command", "")})
                emit({"type": "tool_output", "tool": ev.get("tool"), "command": ev.get("command", ""),
                      "output": ev.get("output", ""), "exit_code": ev.get("exit_code", 0)})
            if thinking:
                for chunk in self._chunks(thinking, 24):
                    sse({"delta": chunk, "thinking": True})
        else:
            # Multi-round agent loop — emits tool rows AND the final answer's
            # content + thinking deltas live (so it streams, not dumps).
            reply, thinking, agent_events = self.run_agent(f, emit=emit)
            tool_events.extend(agent_events)
            streamed_live = True
        # Catch any text-format tool calls in the FINAL reply + strip markup.
        # These weren't seen during the loop, so stream their rows now. In CHAT
        # mode there are no tools, so we only strip stray tool markup — we never
        # execute it (running it would let chat mode silently edit files).
        run_text_tools = (f.get("mode") or "").lower() == "agent"
        reply, tool_results = self.execute_ai_file_tools(sid, reply, f, run=run_text_tools)
        for ev in tool_results:
            emit({"type": "tool_start", "tool": ev.get("tool"), "command": ev.get("command", "")})
            emit({"type": "tool_output", "tool": ev.get("tool"), "command": ev.get("command", ""),
                  "output": ev.get("output", ""), "exit_code": ev.get("exit_code", 0)})
            doc = ev.get("doc")
            if doc and ev.get("exit_code") == 0:
                emit({"_doc": doc, "_tool": ev.get("tool")})
        tool_events.extend(tool_results)
        saved_reply = reply

        # Stream the visible reply tokens — UNLESS run_agent already streamed the
        # content live (word-splitting then would double it).
        if not streamed_live:
            for token in saved_reply.split(" "):
                sse({"delta": token + " "})
        sse({"type": "metrics", "data": {"total_time": round(time.time() - started, 2)}})
        if client_alive["ok"]:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_alive["ok"] = False

        # Persist the assistant message AFTER streaming (with tool_events meta).
        metadata = {"model": f.get("model") or "Local fallback"}
        if thinking:
            metadata["thinking"] = thinking
        if tool_events:
            # Persist tool rows without the bulky `doc` content blob.
            metadata["tool_events"] = [{k: v for k, v in e.items() if k != "doc"} for e in tool_events]
        self.add_message(sid, "assistant", saved_reply, metadata)

        # Self-improving: quietly learn a memory / skill from this turn (off the
        # request path so it never delays the reply). Skipped for task sessions.
        if not sid.startswith("task_session_"):
            threading.Thread(target=self._auto_learn,
                             args=(sid, message, saved_reply, f.get("endpoint_id"), f.get("model")),
                             daemon=True).start()

    @staticmethod
    def _chunks(text, n):
        for i in range(0, len(text), n):
            yield text[i:i + n]

    def _stream_document(self, sse, doc):
        """Open a freshly-created doc in the editor and type its content in,
        matching the Pi's doc_stream_open -> deltas -> doc_update sequence."""
        content = doc.get("content") or ""
        title = doc.get("title", "")
        language = doc.get("language", "markdown")
        sse({"type": "doc_stream_open", "title": title, "language": language})
        step = max(40, len(content) // 60)  # cap the animation at ~60 frames
        acc = ""
        for chunk in self._chunks(content, step):
            acc += chunk
            sse({"type": "doc_stream_delta", "content": acc})
            time.sleep(0.02)
        sse({"type": "doc_update", "doc_id": doc.get("doc_id", ""), "title": title,
             "language": language, "content": content, "version": 1})

    # no agent round cap — the loop runs until the model stops calling tools

    def _agent_system(self, f):
        # COMPACT prompt — we send native function schemas, so the model must
        # NOT be told a text tool format (verbose format instructions make weak
        # models echo the tool-call boilerplate INTO the file content).
        now = datetime.now().astimezone()
        system = (
            "You are the assistant inside the native JoeBro app. You can use the provided function "
            "tools across MULTIPLE steps: call a tool, read its result, then call another, until the "
            "task is done — then give a short prose summary. For email use the email tools (never bash/curl); "
            "for current info or 'research X' use web_search or trigger_research; use list_files/read_file/read_pdf "
            "to inspect bound-folder files, including local PDFs; to add to / change a file "
            "call edit_document (find/replace for a small change, or pass `content` with the COMPLETE new "
            "text to rewrite it; create_document only for a new file). "
            "To add a calendar event call create_event with ISO-8601 start/end datetimes that YOU resolve "
            "from the user's words. Never put tool syntax or raw file contents in your text reply.\n"
            f"Today is {now.strftime('%A, %Y-%m-%d')} and the current local time is {now.strftime('%H:%M')} "
            f"({now.strftime('%z') or 'local'}). Use this to resolve relative dates like 'tomorrow', "
            "'wednesday', or 'next week' into absolute ISO-8601 datetimes.\n"
            "CRITICAL: only claim you did something if the matching tool actually ran and returned success. "
            "To create, edit, or save a file you MUST call create_document or edit_document and see it succeed. "
            "Saying 'done', 'created', 'saved', or 'added' WITHOUT a successful tool call in this turn is forbidden — "
            "if you have not called the tool yet, call it NOW instead of claiming completion. "
            "If a tool isn't available to you, or returns an error / non-zero exit, tell the user plainly that "
            "it didn't happen and why — NEVER pretend a command ran or a file changed when it didn't."
        )
        mode = (f.get("permission_mode") or "").lower()
        if mode == "readonly":
            system += " You are in READ-ONLY mode: you may search and read, but must not create, edit, send, or delete anything."
        if str(f.get("allow_bash") or "").lower() not in ("1", "true", "yes") or mode != "full":
            system += (" The terminal is OFF: you have NO ability to run shell or Python commands or copy/move/delete "
                       "files. If the user asks for that, say the terminal toggle (Full Access) must be enabled — do not claim you did it.")
        doc = self.open_doc_context(f)
        if doc:
            system += (
                f"\n\nThe user has this document OPEN: path `{doc['path']}`. When they ask to add to / "
                f"append to / change \"the doc\", call edit_document for that path with `content` set to the "
                f"entire updated file (current text plus their additions) — do not create a new file. "
                f"Current content of `{doc['path']}`:\n\"\"\"\n{doc['content']}\n\"\"\""
            )
        return system

    def _chat_system(self, f):
        """System prompt for CHAT mode — pure conversation, NO tools. Spells out
        that the assistant cannot take actions so it stops claiming it edited the
        doc / sent mail / ran something, and stops emitting tool-call syntax."""
        now = datetime.now().astimezone()
        system = (
            "You are the assistant inside the native JoeBro app, in CHAT mode. This is a plain "
            "conversation: you have NO tools and CANNOT take any action on the user's machine. "
            "You cannot edit, create, or save files, cannot send or read email, cannot search the "
            "web, run commands, or change the calendar. NEVER claim or imply you did any of these — "
            "do not say you 'edited', 'updated', 'saved', 'created', 'sent', or 'added' anything, and "
            "never output tool-call syntax, XML tags, or function calls (they will not run). "
            "If the user wants you to actually DO something — edit the open document, manage email, "
            "search the web, run a task — tell them to turn on Agent mode (the hammer toggle next to "
            "the message box) and you'll carry it out there.\n"
            f"Today is {now.strftime('%A, %Y-%m-%d')} and the local time is {now.strftime('%H:%M')} "
            f"({now.strftime('%z') or 'local'})."
        )
        doc = self.open_doc_context(f)
        if doc:
            system += (
                f"\n\nThe user has this document OPEN: `{doc['path']}`. You may read, quote, and "
                f"discuss it, and you can describe or draft changes in your reply as text — but you "
                f"cannot modify the file itself in chat mode. Current content:\n"
                f"\"\"\"\n{doc['content']}\n\"\"\""
            )
        return system

    @staticmethod
    def tools_for(f, has_folder):
        """The tool schemas to offer this turn. Don't advertise tools that can't
        actually run in the current context, or the model wastes a round calling
        them and then apologises (e.g. create_document in a plain chat with no
        bound folder)."""
        # Chat mode is pure conversation — no tools at all. Tools (search, email,
        # files, research, …) are an AGENT-mode capability.
        if (f.get("mode") or "").lower() != "agent":
            return []
        readonly = (f.get("permission_mode") or "") == "readonly"
        full = (f.get("permission_mode") or "") == "full"
        terminal_on = str(f.get("allow_bash") or "").lower() in ("1", "true", "yes") and full
        drop = set()
        if readonly:
            drop |= WRITE_SIDE_EFFECT_TOOLS            # read-only: no side effects at all
        if not has_folder and not full:
            # File writes need a bound folder (or Full Access to a scratch dir).
            drop |= {"list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"}
        if not terminal_on:
            # Never advertise the shell when the terminal toggle is off — the
            # model would call it, get refused, then lie that it "ran" the command.
            drop |= {"bash", "python"}
        return [t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] not in drop]

    def _post_chat(self, f, messages, tools=None):
        """One raw chat-completions call with tool schemas. Returns the model's
        message dict, or an error string."""
        ep = self.store.one("select * from endpoints where id=?", (f.get("endpoint_id") or "",))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        schemas = tools if tools is not None else FUNCTION_TOOL_SCHEMAS
        payload = {"model": f.get("model") or "", "messages": messages,
                   "stream": False, "max_tokens": int(f.get("max_tokens") or 8192)}
        if schemas:   # omit entirely in chat mode — an empty `tools: []` upsets some APIs
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        # Groq/Cerebras sit behind Cloudflare, which 403s requests that have no
        # browser User-Agent (the model-list probe already does this; the chat
        # call did not, so those providers' models silently failed).
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        req.add_header("HTTP-Referer", "https://joebro.app")   # OpenRouter app attribution
        req.add_header("X-Title", "JoeBro")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("choices", [{}])[0].get("message", {}) or {}
        except urllib.error.HTTPError as exc:
            # urllib's str() is just "HTTP Error 403: Forbidden"; the provider
            # puts the real reason (rate limit, bad model, data policy) in the body.
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else (body.get("error") or body.get("detail"))
            except Exception:
                detail = ""
            return f"Endpoint error: HTTP {exc.code} {exc.reason}" + (f" — {detail}" if detail else "")
        except Exception as exc:
            return f"Endpoint error: {exc}"

    def _stream_chat(self, f, messages, tools, on_delta):
        """Like _post_chat but STREAMS. Calls on_delta({"delta": text}) for
        content and on_delta({"delta": text, "thinking": True}) for reasoning as
        tokens arrive, so the final answer (and its thinking) shows live instead
        of appearing all at once after the round finishes. Returns the assembled
        message dict {content, reasoning_content, tool_calls}, or an error string."""
        ep = self.store.one("select * from endpoints where id=?", (f.get("endpoint_id") or "",))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": f.get("model") or "", "messages": messages,
                   "stream": True, "max_tokens": int(f.get("max_tokens") or 8192)}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        req.add_header("HTTP-Referer", "https://joebro.app")
        req.add_header("X-Title", "JoeBro")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        content, reasoning, calls = [], [], {}
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        choice = (json.loads(data).get("choices") or [{}])[0]
                    except Exception:
                        continue
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning.append(rc); on_delta({"delta": rc, "thinking": True})
                    c = delta.get("content")
                    if c:
                        content.append(c); on_delta({"delta": c})
                    for tc in (delta.get("tool_calls") or []):
                        slot = calls.setdefault(tc.get("index", 0),
                                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"): slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"): slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"): slot["function"]["arguments"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else (body.get("error") or body.get("detail"))
            except Exception:
                detail = ""
            return f"Endpoint error: HTTP {exc.code} {exc.reason}" + (f" — {detail}" if detail else "")
        except Exception as exc:
            return f"Endpoint error: {exc}"
        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg

    def remote_reply(self, f):
        """Single-round helper (used by Tasks/Skills generation). Returns
        (content, reasoning, tool_calls) where tool_calls is [(name, body)]."""
        if not (f.get("endpoint_id") or "") or f.get("endpoint_id") == "local":
            return None, "", []
        msg = self._post_chat(f, [{"role": "system", "content": self._agent_system(f)}] + self._history_messages(f))
        if msg is None:
            return None, "", []
        if isinstance(msg, str):
            return msg, "", []
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            name = (fn.get("name") or "").strip().lower()
            if name not in PRODUCTION_AI_TOOLS:
                continue
            try:
                params = json.loads(fn.get("arguments") or "{}")
            except Exception:
                params = {}
            tool_calls.append((name, self._tool_body_from_params(name, params)))
        return msg.get("content") or "", reasoning, tool_calls

    def run_agent(self, f, emit=None):
        """Multi-round tool loop: call the model, run whatever tools it asks for,
        feed the results back, repeat until it stops calling tools (or the round
        cap). Returns (final_text, thinking, events). This is what lets the agent
        chain steps (list emails -> read -> summarize; create file -> edit it).

        If `emit(obj)` is given, it is called LIVE as work happens — reasoning
        deltas, tool_start/tool_output rows, and doc events — so chat_stream can
        forward them to the client the moment each tool runs (instead of only
        after the whole loop finishes). Returned events are still the full list
        for persistence; the caller must NOT re-stream tool rows it already saw."""
        emit = emit or (lambda obj: None)
        sid = f.get("session") or ""
        if not (f.get("endpoint_id") or "") or f.get("endpoint_id") == "local":
            return self.local_reply(f.get("message", "")), "", []
        readonly = (f.get("permission_mode") or "") == "readonly"
        allow_outside = (f.get("permission_mode") or "") == "full"
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root_text = ((session or {}).get("workdir") or "").strip()
        root = Path(root_text).expanduser().resolve() if root_text else None

        # Chat mode is pure conversation (no tools); agent mode gets the full
        # tool-using prompt. Using the agent prompt in chat mode made weak models
        # claim they'd "edited the doc" or emit tool syntax that never ran.
        agent = (f.get("mode") or "").lower() == "agent"
        system = self._agent_system(f) if agent else self._chat_system(f)
        skills_ctx = self._use_relevant_skills(f.get("message", ""))   # inject + bump confidence
        if skills_ctx:
            system += "\n\n" + skills_ctx
        mem_ctx = self._use_relevant_memories(f.get("message", ""))    # inject + bump use_count
        if mem_ctx:
            system += "\n\n" + mem_ctx
        messages = [{"role": "system", "content": system}] + self._history_messages(f)
        tools = self.tools_for(f, has_folder=root is not None)
        events, thinking_parts, final_text = [], [], ""
        # Optional hard cap on tool calls per turn (Settings). 0 = unlimited.
        max_tool_calls = max(0, int(f.get("max_tool_calls") or 0))
        tool_calls_made = 0
        rounds, call_sigs, stuck = 0, {}, False
        while True:
            rounds += 1
            # No artificial low cap, but never hang forever: if the model repeats
            # the SAME tool call (a read/list/edit loop), hits the user's tool-use
            # limit, or blows past a generous backstop, drop tools so it MUST give
            # a final answer this round.
            hit_limit = max_tool_calls and tool_calls_made >= max_tool_calls
            use_tools = None if (stuck or hit_limit or rounds > 60) else tools
            # Stream every round: content + reasoning deltas reach the client
            # live (the final answer no longer appears all at once after a wait).
            msg = self._stream_chat(f, messages, use_tools, emit)
            # If the call errored and we'd attached images, the model/endpoint
            # likely can't take them — drop the images and try once more so the
            # whole run doesn't die on one image.
            if isinstance(msg, str) and self._strip_image_blocks(messages):
                msg = self._stream_chat(f, messages, use_tools, emit)
            if msg is None:
                final_text = self.local_reply(f.get("message", "")); emit({"delta": final_text}); break
            if isinstance(msg, str):
                final_text = msg; emit({"delta": final_text}); break
            r = msg.get("reasoning_content") or msg.get("reasoning")
            if r:
                thinking_parts.append(r)   # already streamed live by _stream_chat
            raw_calls = msg.get("tool_calls") or []
            if not raw_calls or use_tools is None:
                final_text = msg.get("content") or ""   # already streamed live
                if not final_text and use_tools is None:
                    final_text = "I started repeating myself there, so I stopped. Tell me how you'd like to proceed."
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": raw_calls})
            for tc in raw_calls:
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip().lower()
                try:
                    params = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    params = {}
                body = self._tool_body_from_params(name, params)
                sig = name + "|" + (body or "")[:300]
                call_sigs[sig] = call_sigs.get(sig, 0) + 1
                if call_sigs[sig] >= 3:
                    stuck = True   # same call 3× — wrap up on the next round
                command = self.tool_command_summary(name, body)
                # Claude-Code-style approval for commands in full mode.
                if name in ("bash", "python") and self._needs_permission(f, sid, name):
                    decision = self._await_permission(emit, name, command)
                    if decision == "always":
                        with _PERMISSION_GUARD:
                            _SESSION_ALLOW.setdefault(sid, set()).add(name)
                    elif decision != "allow":
                        emit({"type": "tool_start", "tool": name, "command": command})
                        emit({"type": "tool_output", "tool": name, "command": command,
                              "output": "Command denied by the user.", "exit_code": 1})
                        events.append({"tool": name, "command": command, "output": "Command denied by the user.", "exit_code": 1})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                         "content": "Command denied by the user."})
                        continue
                # Announce the tool the moment it starts so the row appears live.
                emit({"type": "tool_start", "tool": name, "command": command})
                tool_calls_made += 1
                event = self._run_one_tool(root, name, body, f, allow_outside, readonly)
                if event is None:
                    event = {"tool": name, "command": command,
                             "output": f"The {name} tool is not available in this mode.", "exit_code": 1}
                events.append(event)
                emit({"type": "tool_output", "tool": event.get("tool"), "command": event.get("command", ""),
                      "output": event.get("output", ""), "exit_code": event.get("exit_code", 0)})
                doc = event.get("doc")
                if doc and event.get("exit_code") == 0:
                    emit({"_doc": doc, "_tool": event.get("tool")})
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                 "content": (event.get("output") or "")})
                # If the agent read an image, attach it so the model can SEE it.
                if name == "read_file" and event.get("exit_code") == 0:
                    vis = self._image_vision_message(f, root, body, allow_outside)
                    if vis:
                        messages.append(vis)
                        self._prune_context_images(messages)
        if not final_text:
            final_text = "I stopped without a final answer. Ask me to continue."
        return final_text, "\n\n".join(thinking_parts), events

    # The agent never holds more than this many images in context at once. Older
    # ones are summarised to text, so a long image task is sent in chunks and no
    # single request balloons (which is what got rejected before).
    MAX_CONTEXT_IMAGES = 4

    def _image_vision_message(self, f, root, body, allow_outside):
        """Build a vision user-message for an image the agent just read, so the
        model actually sees it — full quality. Works for local files and remote
        (bridge) hosts. Request size is bounded by _prune_context_images, not by
        compressing the image."""
        rel = (self.parse_tool_fields(body).get("path") or "").strip()
        ext = os.path.splitext(rel)[1].lower()
        if not rel or ext not in IMAGE_EXTS:
            return None
        mime = mimetypes.guess_type(rel)[0] or "image/png"
        b64 = None
        bridge = self._device_bridge(self._session_host(f)) if hasattr(self, "_device_bridge") else None
        if bridge:
            url, token = bridge
            r = bridge_call(url, token, "/v1/exec", {"command": "base64 < " + shlex.quote(self._remote_path(f, rel))})
            if r and r.get("exit_code") == 0:
                b64 = (r.get("stdout") or "").replace("\n", "").strip()
        else:
            try:
                target = self.resolve_tool_path(root, rel, allow_outside)
                if target.is_file():
                    b64 = base64.b64encode(target.read_bytes()).decode("ascii")
            except Exception:
                b64 = None
        if not b64:
            return None
        return {"role": "user", "content": [
            {"type": "text", "text": f"Here is the image {rel} you opened:"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}

    def _prune_context_images(self, messages):
        """Keep only the most recent MAX_CONTEXT_IMAGES image messages with their
        image data; older image messages collapse to a short text note (the model
        has already seen and commented on them). This chunks the visual load."""
        keep = self.MAX_CONTEXT_IMAGES
        idxs = [i for i, m in enumerate(messages)
                if isinstance(m.get("content"), list)
                and any(isinstance(b, dict) and b.get("type") == "image_url" for b in m["content"])]
        for i in idxs[:-keep] if keep > 0 else idxs:
            c = messages[i]["content"]
            text = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            messages[i]["content"] = (text + " [viewed earlier]").strip()

    @staticmethod
    def _strip_image_blocks(messages):
        """Replace image content blocks with a text note (in place) so the run can
        continue on a model/endpoint that can't accept inlined images. True if changed."""
        changed = False
        for m in messages:
            c = m.get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image_url" for b in c):
                text = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                m["content"] = (text + " [an image was here; this model can't view images]").strip()
                changed = True
        return changed

    @staticmethod
    def _needs_permission(f, sid, tool):
        if str(f.get("ask_permission") or "").lower() not in ("1", "true", "yes"):
            return False
        if (f.get("permission_mode") or "") != "full":
            return False
        with _PERMISSION_GUARD:
            return tool not in _SESSION_ALLOW.get(sid, set())

    def _await_permission(self, emit, tool, command):
        """Ask the app to approve a command; block until it answers (or 3 min)."""
        rid = os.urandom(8).hex()
        ev = threading.Event()
        with _PERMISSION_GUARD:
            _PERMISSION_REGISTRY[rid] = {"event": ev, "decision": "deny"}
        emit({"type": "permission_request", "request_id": rid, "tool": tool, "command": command})
        ev.wait(timeout=180)
        with _PERMISSION_GUARD:
            return _PERMISSION_REGISTRY.pop(rid, {}).get("decision", "deny")

    _AICHECK_CHUNK = 9000   # ZeroGPT truncates around ~10k; stay under
    _AICHECK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    @classmethod
    def _aicheck_chunks(cls, text):
        """Split long text on paragraph boundaries (hard-slice giant paragraphs)
        so each chunk reads coherently for ZeroGPT."""
        text = text.strip()
        if len(text) <= cls._AICHECK_CHUNK:
            return [text]
        size, out, cur = cls._AICHECK_CHUNK, [], ""
        for para in text.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            if len(cur) + len(para) + 2 <= size:
                cur = (cur + "\n\n" + para) if cur else para
                continue
            if cur:
                out.append(cur); cur = ""
            if len(para) <= size:
                cur = para
            else:
                for i in range(0, len(para), size):
                    out.append(para[i:i + size])
        if cur:
            out.append(cur)
        return out

    def _zerogpt_score(self, chunk):
        req = urllib.request.Request(
            "https://api.zerogpt.com/api/detect/detectText",
            data=json.dumps({"input_text": chunk}).encode("utf-8"), method="POST")
        for k, v in {"Content-Type": "application/json", "Origin": "https://www.zerogpt.com",
                     "Referer": "https://www.zerogpt.com/", "User-Agent": self._AICHECK_UA}.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not body.get("success"):
            raise RuntimeError(body.get("message") or "ZeroGPT reported failure")
        data = body.get("data") or {}
        return {"ai_words": int(data.get("aiWords") or 0),
                "total_words": int(data.get("textWords") or 0),
                "flagged_sentences": list(data.get("h") or [])}

    def ai_check(self, body):
        """AI-text detection via ZeroGPT's public endpoint. Long inputs are
        chunked, each chunk scored, and the result is a word-weighted average
        with every flagged sentence surfaced. Ported from the Pi."""
        text = (body.get("text") or "").strip()
        if not text:
            return self.json({"detail": "text is required"}, 400)
        if len(text.split()) < 20:
            return self.json({"detail": "Input too short — paste at least ~20 words for a useful estimate."}, 400)
        results = []
        chunks = self._aicheck_chunks(text)
        for i, c in enumerate(chunks):
            try:
                results.append(self._zerogpt_score(c))
            except Exception as exc:
                return self.json({"detail": f"AI-check service unreachable: {exc}"}, 502)
            if len(chunks) > 1 and i < len(chunks) - 1:
                time.sleep(0.4)   # polite spacing so the per-IP throttle doesn't reject the batch
        total_words = sum(r["total_words"] for r in results) or 1
        ai_words = sum(r["ai_words"] for r in results)
        ai_percent = ai_words / total_words * 100.0
        flagged = []
        for r in results:
            flagged.extend(r["flagged_sentences"])
        return self.json({"ai_percent": round(ai_percent, 1), "human_percent": round(100.0 - ai_percent, 1),
                          "ai_words": ai_words, "total_words": total_words, "chunks": len(chunks),
                          "flagged_sentences": flagged, "provider": "zerogpt"})

    def _task_endpoint(self):
        """Pick an endpoint/model to run a scheduled task: the saved default,
        else the most recent chat's, else any configured endpoint."""
        ep, model = self.pref("default_endpoint_id") or "", self.pref("default_model_id") or ""
        if not ep:
            s = self.store.one("select endpoint_id, model from sessions where endpoint_id != '' order by datetime(created_at) desc limit 1")
            if s:
                ep, model = s.get("endpoint_id") or "", s.get("model") or ""
        if not ep:
            e = self.store.one("select id from endpoints order by datetime(created_at) limit 1")
            ep = e["id"] if e else ""
        return ep, model

    def execute_task(self, task_id):
        """Run a saved task through the agent loop (so it can actually use tools —
        e.g. the skill audit calls manage_skills, the email summary reads mail).
        Output lands in a persistent '[TASK] <name>' chat. Returns the reply."""
        row = self.store.one("select * from tasks where id=?", (task_id,))
        if not row:
            return None
        ep, model = self._task_endpoint()
        if not ep:
            return "No model endpoint configured — add one in Settings."
        tsid = "task_session_" + task_id
        if not self.store.one("select id from sessions where id=?", (tsid,)):
            self.store.exec(
                "insert into sessions(id,name,model,endpoint_url,endpoint_id,workdir,created_at) values(?,?,?,?,?,?,?)",
                (tsid, "[TASK] " + (row.get("title") or "Task"), model, "", ep, "", now_iso()))
        prompt = row.get("prompt") or row.get("title") or "Run this task."
        self.add_message(tsid, "user", prompt, {})
        # Tasks always run as an agent (so they can use tools); the file-access
        # mode is the task's own setting, defaulting to the safe sandbox.
        pmode = (row.get("permission_mode") or "sandbox").strip() or "sandbox"
        f = {"session": tsid, "message": prompt, "endpoint_id": ep, "model": model,
             "permission_mode": pmode, "mode": "agent", "max_tokens": "4000"}
        reply, thinking, events = self.run_agent(f)
        meta = {"model": model}
        if thinking:
            meta["thinking"] = thinking
        if events:
            meta["tool_events"] = [{k: v for k, v in e.items() if k != "doc"} for e in events]
        self.add_message(tsid, "assistant", reply, meta)
        self.store.exec("update tasks set last_run=?, updated_at=? where id=?", (now_iso(), now_iso(), task_id))
        return reply

    def _use_relevant_memories(self, message):
        """Inject memories relevant to this request into the agent's context and
        bump their use_count/last_used. This both makes memory actually useful in
        chats AND gives the weekly memory audit a real 'frequency of use' signal:
        memories that keep matching survive; ones never recalled get purged."""
        rows = self.store.rows("select * from memory")
        if not rows:
            return ""
        words = [w for w in re.findall(r"[a-z0-9]+", (message or "").lower()) if len(w) > 3]
        if not words:
            return ""
        matched = [r for r in rows
                   if any(w in (r.get("text") or "").lower() for w in words)][:6]
        if not matched:
            return ""
        for r in matched:
            self.store.exec("update memory set use_count=?, last_used=? where id=?",
                            ((r.get("use_count") or 0) + 1, now_iso(), r["id"]))
        lines = "\n".join(f"- {r.get('text')}" for r in matched)
        return "What you know about the user (memory):\n" + lines

    def _use_relevant_skills(self, message):
        """Find saved skills relevant to this request, inject them, and RAISE
        their confidence (frequency of use). Skills that keep getting used climb
        and survive the audit; ones that never match stay low and get pruned —
        the self-improving loop."""
        rows = self.store.rows("select * from skills where status != 'archived'")
        if not rows:
            return ""
        words = [w for w in re.findall(r"[a-z0-9]+", (message or "").lower()) if len(w) > 3]
        if not words:
            return ""
        scored = []
        for r in rows:
            hay = " ".join([r.get("name") or "", r.get("description") or "", r.get("content") or ""]).lower()
            hits = sum(1 for w in set(words) if w in hay)
            if hits:
                scored.append((hits, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        matched = [r for _, r in scored[:3]]
        if not matched:
            return ""
        for hits, r in scored[:3]:
            # Confidence rises with FREQUENCY (each use) and EFFICACY (stronger,
            # multi-word matches = more on-point applications count for more).
            bump = min(25, 8 + 4 * hits)
            new_conf = min(100, (r.get("confidence") if r.get("confidence") is not None else 60) + bump)
            self.store.exec("update skills set confidence=?, updated_at=? where id=?", (new_conf, now_iso(), r["id"]))
        lines = "\n".join(f"- {r.get('name')}: {(r.get('content') or '').strip()[:300]}" for r in matched)
        return "Relevant saved skills you can apply to this request:\n" + lines

    def _auto_learn(self, sid, user_msg, reply, endpoint_id, model):
        """After a turn, quietly extract a durable user memory and/or a reusable
        skill and save them. Auto-skills start at low confidence so they must
        prove useful (get used) or get pruned by the skill audit. Best-effort."""
        if not endpoint_id or len((user_msg or "").strip()) < 25:
            return
        try:
            out = self._complete(
                endpoint_id, model,
                "You extract durable learnings from one chat turn. Respond with ONLY JSON: "
                '{"memory": <one stable fact about the USER worth remembering long-term, or null>, '
                '"skill": {"name": <short>, "content": <1-2 sentence reusable instruction>} or null}. '
                "Memory = lasting preferences/identity/recurring context, never one-off task details. "
                "Skill = a repeatable workflow the user will likely want again (content = ONE short sentence). "
                "Use null when nothing qualifies. Keep it brief so the JSON is complete.",
                f"User: {user_msg}\n\nAssistant: {(reply or '')[:1500]}", max_tokens=600)
            if not out:
                return
            m = re.search(r"\{.*\}", out, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            mem = data.get("memory")
            if isinstance(mem, str) and mem.strip().lower() not in ("", "null", "none"):
                existing = [(e.get("text") or "").lower() for e in self.store.rows("select text from memory")]
                if mem.strip().lower() not in existing:
                    self.store.exec("insert into memory(id,text,category,created_at) values(?,?,?,?)",
                                    ("mem_" + os.urandom(6).hex(), mem.strip(), "auto", now_iso()))
            sk = data.get("skill")
            if isinstance(sk, dict) and (sk.get("name") or "").strip():
                names = [(e.get("name") or "").lower() for e in self.store.rows("select name from skills")]
                if (sk.get("name") or "").strip().lower() not in names:
                    self.store.exec(
                        "insert into skills(id,name,description,content,status,confidence,created_at,updated_at)"
                        " values(?,?,?,?,?,?,?,?)",
                        ("skill_" + os.urandom(6).hex(), sk.get("name").strip(), "", (sk.get("content") or "").strip(),
                         "draft", 20, now_iso(), now_iso()))
        except Exception:
            pass

    # Compaction tuning. We don't have a real tokenizer in the stdlib, so we
    # budget by characters (~4 chars/token). When history exceeds the budget we
    # summarize the older turns into one dense system note and keep the recent
    # turns verbatim — far better recall than the old `msgs[-30:]` front-chop.
    COMPACT_KEEP_RECENT = 12       # most recent turns always sent verbatim
    COMPACT_CHAR_BUDGET = 48000    # ~12k tokens of history before we compact

    COMPACT_SYSTEM_PROMPT = (
        "You are summarizing a conversation to preserve context after compaction. "
        "Produce a dense, structured summary that lets the conversation continue "
        "seamlessly. Use these sections:\n\n"
        "### User Goal\nOne sentence on what the user is trying to accomplish.\n\n"
        "### What Was Done\n- Completed actions, decisions, and key outputs. "
        "Include specific file paths, names, URLs, and values. Note errors and how they were resolved.\n\n"
        "### Current State\nWhat the task/code state is right now and what was last discussed.\n\n"
        "### Pending / Next Steps\n- What remains, open questions, blockers.\n\n"
        "### Key Context\n- Constraints, preferences, decisions that must not be forgotten; "
        "specific values (models, ports, paths, versions).\n\n"
        "Keep it under 1000 tokens. Every token should carry information. No pleasantries.")

    def _history_messages(self, f):
        """The session's conversation as OpenAI messages, so the model has
        memory of prior turns. The current user turn is already the last stored
        row. Older turns are compacted into a summary when the history grows."""
        sid = f.get("session") or ""
        rows = self.store.rows(
            "select role, content from messages where session_id=? order by id", (sid,)) if sid else []
        msgs = []
        prefix = []   # a manually-compacted summary, carried verbatim into context
        for r in rows:
            role = r.get("role")
            content = (r.get("content") or "").strip()
            if role == "system" and content.startswith("[Conversation summary"):
                prefix = [{"role": "system", "content": content}]
                continue
            if role not in ("user", "assistant") or not content:
                continue
            msgs.append({"role": role, "content": content})
        if not msgs:
            return prefix + [{"role": "user", "content": f.get("message", "")}]

        total = sum(len(m["content"]) for m in msgs)
        if len(msgs) <= self.COMPACT_KEEP_RECENT or total <= self.COMPACT_CHAR_BUDGET:
            return prefix + msgs[-30:]

        older = msgs[:-self.COMPACT_KEEP_RECENT]
        recent = msgs[-self.COMPACT_KEEP_RECENT:]
        summary = self._compact_history(f, sid, older)
        if summary:
            return prefix + [{"role": "system",
                     "content": "[Conversation summary — earlier turns were compacted]\n" + summary}] + recent
        return prefix + msgs[-30:]   # summary unavailable — fall back to naive trim

    def _compact_history(self, f, sid, older):
        """Summarize `older` turns into one dense note via the session's own
        endpoint. Cached per session in prefs and only refreshed once the older
        window has grown by a few turns, so we don't pay to re-summarize every
        turn. Incremental: a prior summary is folded into the next one."""
        cache = self.pref("compact_" + sid) or {}
        cached_n = int(cache.get("n") or 0)
        cached_summary = cache.get("summary") or ""
        # Reuse the cached summary until at least 6 new turns have aged out.
        if cached_summary and len(older) - cached_n < 6:
            return cached_summary

        convo = "\n".join(f"{m['role'].upper()}: {m['content'][:2000]}" for m in older)
        user = convo
        if cached_summary:
            user = ("Existing summary of earlier turns:\n" + cached_summary +
                    "\n\n---\nNewer turns to fold in:\n" + convo)
        summary = self._complete(
            f.get("endpoint_id") or "", f.get("model") or "",
            self.COMPACT_SYSTEM_PROMPT, user, max_tokens=1024)
        if not summary:
            return cached_summary or ""
        self.set_pref("compact_" + sid, {"n": len(older), "summary": summary})
        return summary

    def manual_compact(self, sid):
        """Force-compact a session on demand (the 'Compact Chat' button):
        summarize all but the recent tail into one stored system note and
        replace those older rows. Destructive, unlike the on-the-fly summary
        used during chat. The note is carried back into context by
        _history_messages and shown as a pill by the app."""
        sess = self.store.one("select * from sessions where id=?", (sid,))
        if not sess:
            return {"ok": False, "error": "no such session"}
        rows = self.store.rows(
            "select id, role, content from messages where session_id=? order by id", (sid,))
        convo = [r for r in rows
                 if r.get("role") in ("user", "assistant") and (r.get("content") or "").strip()]
        if len(convo) <= self.COMPACT_KEEP_RECENT:
            return {"ok": True, "compacted": False}   # not enough history to bother
        older = convo[:-self.COMPACT_KEEP_RECENT]
        text = "\n".join(f"{r['role'].upper()}: {(r['content'] or '')[:2000]}" for r in older)
        # fold any prior manual summary into the new one so nothing is lost
        prior = next((r for r in rows if r.get("role") == "system"
                      and (r.get("content") or "").startswith("[Conversation summary")), None)
        if prior:
            text = "Existing summary:\n" + prior["content"] + "\n\n---\nNewer turns:\n" + text
        summary = self._complete(
            sess.get("endpoint_id") or "", sess.get("model") or "",
            self.COMPACT_SYSTEM_PROMPT, text, max_tokens=1024)
        if not summary:
            return {"ok": False, "error": "couldn't summarize — model unavailable"}
        drop_ids = [r["id"] for r in older] + ([prior["id"]] if prior else [])
        keep_id = min(drop_ids)   # reuse the lowest id so the note sorts first
        q = ",".join("?" * len(drop_ids))
        self.store.exec(f"delete from messages where id in ({q})", tuple(drop_ids))
        self.store.exec(
            "insert into messages(id, session_id, role, content, metadata, created_at) values(?,?,?,?,?,?)",
            (keep_id, sid, "system",
             "[Conversation summary — earlier turns were compacted]\n" + summary,
             json.dumps({"compacted": True}), now_iso()))
        self.store.exec("delete from prefs where key=?", ("compact_" + sid,))   # stale cache
        return {"ok": True, "compacted": True, "removed": len(older)}

    def open_doc_context(self, f):
        """The path + current text of the document open in the app, so the model
        edits it instead of creating a new file. Truncated to bound spend."""
        doc_id = (f.get("active_doc_id") or "").strip()
        if not doc_id or doc_id == "_streaming_":
            return None
        session = self.store.one("select workdir from sessions where id=?", (f.get("session") or "",))
        root_text = ((session or {}).get("workdir") or "").strip()
        # The app opens bound-folder files natively with client-local ids
        # ("local-<relpath>") that aren't in the documents table — resolve them
        # straight from disk so the AI knows the open file.
        if doc_id.startswith("local-") and root_text:
            sub = doc_id[len("local-"):]
            try:
                target = self.resolve_in_workdir(Path(root_text).expanduser().resolve(), sub)
                if target.is_file():
                    return {"path": sub, "content": read_doc_text(target)}
            except Exception:
                pass
            return None
        row = self.store.one("select file_path, current_content from documents where id=?", (doc_id,))
        if not row:
            return None
        file_path = row.get("file_path") or ""
        rel = file_path
        if root_text and file_path:
            try:
                rel = str(Path(file_path).resolve().relative_to(Path(root_text).expanduser().resolve()))
            except Exception:
                rel = Path(file_path).name
        return {"path": rel or Path(file_path).name, "content": (row.get("current_content") or "")}

    def chat_reply_with_tools(self, f):
        sid = f.get("session") or ""
        message = f.get("message", "")
        self.add_message(sid, "user", message, {})
        reply, _reasoning, structured = self.remote_reply(f)
        reply = reply or ("" if structured else self.local_reply(message))
        struct_results = self.execute_structured_tools(sid, structured, f)
        reply, tool_results = self.execute_ai_file_tools(sid, reply, f)
        tool_results = struct_results + tool_results
        if tool_results:
            reply = reply + "\n\n" + "\n".join(e.get("output", "") for e in tool_results)
        self.add_message(sid, "assistant", reply, {"model": f.get("model") or "Local fallback"})
        return reply

    def execute_ai_file_tools(self, sid, reply, form=None, run=True):
        if not sid or not reply:
            return reply, []
        form = form or {}
        reply = self.normalize_xml_tools(reply)
        readonly = (form.get("permission_mode") or "") == "readonly"
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root_text = ((session or {}).get("workdir") or "").strip()
        root = Path(root_text).expanduser().resolve() if root_text else None
        allow_outside = (form.get("permission_mode") or "") == "full"
        events = []
        # Strip every recognized tool block from the displayed text — executed
        # tools are shown as tool rows, not raw markup. (The Pi does the same.)
        recognized = PRODUCTION_AI_TOOLS | OBSOLETE_AI_TOOLS | {"write_file"}
        def strip_recognized(match):
            return "" if match.group(1).strip().lower() in recognized else match.group(0)
        cleaned = re.sub(r"\n{3,}", "\n\n", TOOL_BLOCK_RE.sub(strip_recognized, reply)).strip()
        if not run:
            return cleaned, []
        for tool, body in TOOL_BLOCK_RE.findall(reply):
            name = tool.strip().lower()
            if name == "write_file" or name in OBSOLETE_AI_TOOLS:
                continue
            event = self._run_one_tool(root, name, body, form, allow_outside, readonly)
            if event:
                events.append(event)
        return cleaned, events

    def execute_structured_tools(self, sid, blocks, form=None):
        """Run the model's STRUCTURED function calls (name, body) — same
        execution + doc-event path as text-parsed tool blocks."""
        if not sid or not blocks:
            return []
        form = form or {}
        readonly = (form.get("permission_mode") or "") == "readonly"
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root_text = ((session or {}).get("workdir") or "").strip()
        root = Path(root_text).expanduser().resolve() if root_text else None
        allow_outside = (form.get("permission_mode") or "") == "full"
        events = []
        for name, body in blocks:
            event = self._run_one_tool(root, name, body, form, allow_outside, readonly)
            if event:
                events.append(event)
        return events

    def _run_one_tool(self, root, name, body, form, allow_outside, readonly):
        """Execute a single tool (name, body) and return its event, or None if
        the tool is disabled/unknown. Shared by the text and structured paths."""
        name = (name or "").strip().lower()
        if name == "write_file" or name in OBSOLETE_AI_TOOLS:
            return None
        if readonly and name in WRITE_SIDE_EFFECT_TOOLS:
            return None
        if name not in PRODUCTION_AI_TOOLS:
            return None
        try:
            output = self.execute_production_tool(root, name, body, form, allow_outside=allow_outside)
            event = {"tool": name, "command": self.tool_command_summary(name, body),
                     "output": output, "exit_code": 0}
            if name in ("create_document", "edit_document", "update_document"):
                event["doc"] = self._doc_event_payload(root, name, body, allow_outside)
            return event
        except Exception as exc:
            return {"tool": name, "command": self.tool_command_summary(name, body),
                    "output": str(exc), "exit_code": 1}

    def _doc_event_payload(self, root, name, body, allow_outside):
        """The {doc_id,title,language,content} for a document the AI just wrote,
        so the app can open it / refresh the open tab. Reads the file from disk
        (natively-opened files have no documents row) and matches the app's
        "local-<relpath>" id scheme so the open tab updates in place."""
        try:
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "").strip()
            if not rel or root is None:
                return None
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                return None
            content = read_doc_text(target)
            try:
                sub = str(target.relative_to(root))
            except Exception:
                sub = target.name
            # Bound-folder files are opened in the editor as "local-<sub>", so the
            # doc event MUST use that id (not a documents-table row id) or the open
            # tab won't match — the edit then won't show live or trigger approval.
            doc_id = "local-" + sub
            return {"doc_id": doc_id, "title": target.name,
                    "language": target.suffix.lstrip(".") or "markdown",
                    "content": content}
        except Exception:
            return None

    def tool_command_summary(self, name, body):
        fields = self.parse_tool_fields(body)
        target = fields.get("path") or fields.get("title") or ""
        if target.strip():
            return target.strip()
        if body.strip():
            return body.strip().splitlines()[0][:160]
        return name

    def read_pdf_text(self, target: Path):
        """Extract text from a local PDF without shell/Pi dependencies."""
        pieces = []
        errors = []
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(target))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            for i, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    pieces.append(f"--- Page {i} ---\n{text.strip()}")
        except Exception as exc:
            errors.append(f"pypdf: {exc}")
        if not pieces:
            try:
                from Foundation import NSURL
                from Quartz import PDFDocument
                doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(target)))
                count = int(doc.pageCount()) if doc else 0
                for i in range(count):
                    page = doc.pageAtIndex_(i)
                    text = str(page.string() or "") if page else ""
                    if text.strip():
                        pieces.append(f"--- Page {i + 1} ---\n{text.strip()}")
            except Exception as exc:
                errors.append(f"Quartz: {exc}")
        if not pieces:
            try:
                proc = subprocess.run(
                    ["/usr/bin/mdls", "-raw", "-name", "kMDItemTextContent", str(target)],
                    text=True, capture_output=True, timeout=12)
                text = (proc.stdout or "").strip()
                if text and text != "(null)":
                    pieces.append(text)
            except Exception as exc:
                errors.append(f"Spotlight: {exc}")
        text = "\n\n".join(pieces).strip()
        if not text:
            raise ValueError("Could not extract text from PDF" + (": " + "; ".join(errors) if errors else ""))
        return text   # no char cap — compaction handles context if it grows

    def execute_file_tool(self, root, name, body, allow_outside=False):
        root = Path(root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("session workdir is not available")
        if name == "list_files":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip() or ".").strip()
            base = self.resolve_tool_path(root, rel, allow_outside)
            if base.is_file():
                return str(base.relative_to(root) if base.is_relative_to(root) else base)
            if not base.exists() or not base.is_dir():
                raise ValueError(f"folder not found: {rel}")
            # One level only (dirs first, marked with a trailing /), so the agent
            # gets a clean, navigable listing and drills in with `path`.
            rows = []
            for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                rows.append(p.name + ("/" if p.is_dir() else ""))
                if len(rows) >= 400:
                    rows.append("… (truncated — list a sub-folder to narrow down)")
                    break
            header = "" if rel in (".", "") else rel.rstrip("/") + "/\n"
            return header + ("\n".join(rows) or "(empty folder)")
        if name == "read_file":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip()).strip()
            if not rel:
                raise ValueError("read_file requires path")
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                raise ValueError(f"file not found: {rel}")
            if target.suffix.lower() == ".pdf":
                return self.read_pdf_text(target)
            if target.suffix.lower() in IMAGE_EXTS:
                # The actual image is attached to the conversation by run_agent so
                # the model can see it; this is just the tool's text acknowledgement.
                return f"[image {rel} is shown to you below — look at it directly]"
            text = read_doc_text(target)
            return text   # no char cap — compaction handles context if it grows
        if name == "read_pdf":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip()).strip()
            if not rel:
                raise ValueError("read_pdf requires path")
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                raise ValueError(f"PDF not found: {rel}")
            if target.suffix.lower() != ".pdf":
                raise ValueError(f"not a PDF: {rel}")
            return self.read_pdf_text(target)
        if name == "create_document":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "Untitled.md").strip()
            content = fields.get("content") or ""
            target = self.resolve_tool_path(root, rel, allow_outside)
            with lock_for_path(target):
                backup_existing(target)
                write_doc_text(target, content)
            language = fields.get("language") or target.suffix.lstrip(".") or "markdown"
            ts = now_iso()
            row = self.store.one("select id from documents where file_path=?", (str(target),))
            if row:
                self.store.exec(
                    "update documents set title=?, language=?, current_content=?, updated_at=? where id=?",
                    (target.name, language, content, ts, row["id"]),
                )
            else:
                self.store.exec(
                    """insert into documents(id,title,language,current_content,version_count,file_path,archived,created_at,updated_at)
                       values(?,?,?,?,?,?,?,?,?)""",
                    ("doc_" + os.urandom(8).hex(), target.name, language, content, 1, str(target), 0, ts, ts),
                )
            return f"File written: {target}"
        if name == "edit_document":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "").strip()
            if not rel:
                raise ValueError("edit_document requires path")
            target = self.resolve_tool_path(root, rel, allow_outside)
            region = (fields.get("region") or "body").strip().lower()
            # Lock the whole read-modify-write so a concurrent edit can't be lost.
            with lock_for_path(target):
                if is_docx(target) and region in ("header", "footer"):
                    cur = docx_extras(target).get(region, "")
                    if "content" in fields:
                        new = fields["content"]
                    else:
                        find = fields.get("find")
                        if find is None:
                            raise ValueError("edit_document requires find/replace or content")
                        if find not in cur:
                            raise ValueError(f"could not find text in {target.name} {region}")
                        new = cur.replace(find, fields.get("replace", ""), 1)
                    backup_existing(target)
                    docx_write_extra(target, region, new)
                    return f"File edited ({region}): {target}"
                # .docx find/replace patches the file in place so every bit of
                # formatting, colour and imagery survives the edit.
                if is_docx(target) and "content" not in fields:
                    find = fields.get("find")
                    if find is None:
                        raise ValueError("edit_document requires find/replace or content")
                    backup_existing(target)
                    if not docx_replace_in_place(target, find, fields.get("replace", "")):
                        raise ValueError(f"could not find text in {target.name}")
                    updated = read_doc_text(target)
                else:
                    text = read_doc_text(target)
                    if "content" in fields:
                        updated = fields["content"]
                    else:
                        find = fields.get("find")
                        replace = fields.get("replace", "")
                        if find is None:
                            raise ValueError("edit_document requires find/replace or content")
                        if find not in text:
                            raise ValueError(f"could not find text in {target.name}")
                        updated = text.replace(find, replace, 1)
                    backup_existing(target)
                    write_doc_text(target, updated)
            row = self.store.one("select id from documents where file_path=?", (str(target),))
            if row:
                self.store.exec(
                    "update documents set current_content=?, updated_at=? where id=?",
                    (updated, now_iso(), row["id"]),
            )
            return f"File edited: {target}"
        raise ValueError(f"unsupported tool {name}")

    def require_full_terminal(self, form, tool):
        if str(form.get("allow_bash") or "").lower() != "true":
            raise ValueError(f"{tool} requires the terminal toggle to be enabled.")
        if (form.get("permission_mode") or "") != "full":
            raise ValueError(f"{tool} requires Full access. Bound-folder mode is not a real shell sandbox.")

    def bash_tool(self, root, body, form, allow_outside=False):
        self.require_full_terminal(form, "bash")
        cwd = str(root) if root and root.exists() else str(Path.home())
        proc = subprocess.run(
            ["/bin/bash", "-lc", body],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=60,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(out.strip() or f"bash exited {proc.returncode}")
        return out.strip() or "(no output)"

    def python_tool(self, root, body, form, allow_outside=False):
        self.require_full_terminal(form, "python")
        cwd = str(root) if root and root.exists() else str(Path.home())
        proc = subprocess.run(
            ["python3", "-I", "-c", body],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=60,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(out.strip() or f"python exited {proc.returncode}")
        return out.strip() or "(no output)"

    def web_search_tool(self, body):
        args = self.parse_tool_args(body)
        query = (args.get("query") or body.splitlines()[0] if body.strip() else "").strip()
        if not query:
            raise ValueError("web_search requires query")
        results = self.search_web(query, count=5)
        rows = []
        for r in results:
            rows.append(f"- {r['title']}\n  {r['url']}")
            if r.get("snippet"):
                rows.append(f"  {r['snippet']}")
        return "\n".join(rows) if rows else "No results found."

    def search_web(self, query, count=5):
        self.ensure_searxng()   # prefer the local searxng (better results) ...
        for base in self.searxng_urls():
            try:
                results = self.searxng_search(base, query, count)
                if results:
                    return results
            except Exception:
                continue
        return self.duckduckgo_search(query, count)   # ... DuckDuckGo fallback

    @staticmethod
    def _searxng_home():
        return Path.home() / ".joebro-searxng"

    @classmethod
    def find_searxng(cls):
        """(venv_python, src_dir) for the bundled/local searxng, or (None, None)."""
        home = cls._searxng_home()
        py, src = home / "venv" / "bin" / "python", home / "src"
        if py.is_file() and (src / "searx" / "webapp.py").is_file():
            return str(py), str(src)
        return None, None

    def ensure_searxng(self):
        """Boot the local searxng meta-search if it isn't already up. Best-effort:
        if it's not installed, web search just falls through to DuckDuckGo."""
        if self.port_open("127.0.0.1", 8888):
            return True
        py, src = self.find_searxng()
        if not py:
            return False
        settings = self._searxng_home() / "settings.yml"
        if not settings.exists():
            try:
                settings.write_text(
                    "use_default_settings: true\n"
                    "server:\n"
                    f"  secret_key: \"{os.urandom(16).hex()}\"\n"
                    "  bind_address: \"127.0.0.1\"\n"
                    "  port: 8888\n"
                    "  limiter: false\n"
                    "  public_instance: false\n"
                    "search:\n"
                    "  formats:\n    - html\n    - json\n")
            except Exception:
                return False
        env = dict(os.environ)
        env["SEARXNG_SETTINGS_PATH"] = str(settings)
        logs = self.store.root / "searxng"
        logs.mkdir(exist_ok=True)
        try:
            subprocess.Popen([py, "-m", "searx.webapp"], cwd=src, env=env,
                             stdout=open(logs / "searxng.log", "ab"), stderr=subprocess.STDOUT)
        except Exception:
            return False
        deadline = time.time() + 12   # searxng takes a few seconds to come up
        while time.time() < deadline:
            if self.port_open("127.0.0.1", 8888):
                return True
            time.sleep(0.3)
        return False

    def searxng_urls(self):
        # Production is Pi-independent: only an explicitly-configured searxng
        # (env/pref) or a LOCAL one is used. Web/image search otherwise falls
        # back to Pi-free sources (DuckDuckGo / Openverse). No remote hosts here.
        raw = [
            os.environ.get("JOEBRO_SEARXNG_URL", ""),
            os.environ.get("SEARXNG_INSTANCE", ""),
            os.environ.get("SEARXNG_URL", ""),
            str(self.pref("searxng_url") or ""),
            "http://127.0.0.1:8888",
            "http://127.0.0.1:8080",
            "http://localhost:8888",
            "http://localhost:8080",
        ]
        urls = []
        for url in raw:
            url = (url or "").strip().rstrip("/")
            if url and url not in urls and not url.startswith("http://searxng:"):
                urls.append(url)
        return urls

    def search_images(self, query, count=2):
        """Best-effort image URLs for a research topic (searxng images category,
        else Openverse). Images are a nice-to-have."""
        self.ensure_searxng()
        for base in self.searxng_urls():
            try:
                params = {"q": query, "format": "json", "language": "en", "categories": "images"}
                url = base + "/search?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                out = []
                for item in data.get("results", []):
                    src = (item.get("img_src") or item.get("thumbnail_src") or item.get("url") or "").strip()
                    if src.startswith("http") and src not in out:
                        out.append(src)
                    if len(out) >= count:
                        break
                if out:
                    return out
            except Exception:
                continue
        return self._openverse_images(query, count)   # Pi-free fallback

    def _openverse_images(self, query, count):
        """Pi-free image source: Openverse (openly-licensed images, no API key)."""
        try:
            url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
                {"q": query, "page_size": max(count, 3), "mature": "false"})
            req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            out = []
            for item in data.get("results", []):
                src = (item.get("url") or item.get("thumbnail") or "").strip()
                if src.startswith("http") and src not in out:
                    out.append(src)
                if len(out) >= count:
                    break
            return out
        except Exception:
            return []

    def searxng_search(self, base, query, count):
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "categories": "general",
        }
        engines = os.environ.get("JOEBRO_SEARXNG_GENERAL_ENGINES") or os.environ.get("SEARXNG_GENERAL_ENGINES")
        if engines:
            params["engines"] = engines
        url = base + "/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = []
        for item in data.get("results", [])[:count]:
            href = (item.get("url") or "").strip()
            title = re.sub(r"\s+", " ", (item.get("title") or "").strip())
            if not href or not title:
                continue
            results.append({
                "title": title,
                "url": href,
                "snippet": re.sub(r"\s+", " ", (item.get("content") or item.get("snippet") or "").strip()),
            })
        return results

    def duckduckgo_search(self, query, count):
        url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        results = []
        pattern = r"""<a[^>]+href=["']([^"']+)["'][^>]+class=["'](?:result__a|result-link)["'][^>]*>(.*?)</a>"""
        for m in re.finditer(pattern, html, re.S):
            href = m.group(1).replace("&amp;", "&")
            if "uddg=" in href:
                href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
            if href.startswith("//"):
                href = "https:" + href
            href = urllib.parse.unquote(href)
            title = re.sub(r"<.*?>", "", m.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if title:
                results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= count:
                break
        return results

    def suggest_document_tool(self, root, body, allow_outside=False):
        fields = self.parse_tool_fields(body)
        rel = (fields.get("path") or fields.get("title") or "").strip()
        if not rel:
            raise ValueError("suggest_document requires path")
        target = self.resolve_tool_path(root, rel, allow_outside)
        text = read_doc_text(target)
        find = fields.get("find", "")
        replace = fields.get("replace", "")
        reason = fields.get("reason", "")
        if find and find not in text:
            raise ValueError(f"could not find text in {target.name}")
        return json.dumps({
            "path": str(target),
            "suggestions": [{
                "find": find,
                "replace": replace,
                "reason": reason or "Suggested improvement",
            }],
            "count": 1 if find or replace else 0,
        }, ensure_ascii=False)

    def manage_memory_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action == "add":
            mid = "mem_" + os.urandom(6).hex()
            self.store.exec(
                "insert into memory(id,text,category,created_at) values(?,?,?,?)",
                (mid, args.get("text") or "", args.get("category") or "general", now_iso()),
            )
            return f"Added memory {mid}"
        if action == "edit":
            mid = args.get("id") or args.get("memory_id")
            if not mid:
                raise ValueError("memory id required")
            self.store.exec("update memory set text=?, category=? where id=?", (args.get("text") or "", args.get("category") or "general", mid))
            return f"Edited memory {mid}"
        if action == "delete":
            mid = args.get("id") or args.get("memory_id")
            if not mid:
                raise ValueError("memory id required")
            self.store.exec("delete from memory where id=?", (mid,))
            return f"Deleted memory {mid}"
        rows = self.store.rows("select * from memory order by pinned desc, datetime(created_at) desc")
        if action == "search" and args.get("text"):
            q = args["text"].lower()
            # Match on any significant word, not the whole phrase as a substring,
            # so "mode preference" still recalls "I prefer dark mode".
            words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2]
            if words:
                rows = [r for r in rows
                        if q in (r.get("text") or "").lower()
                        or any(w in (r.get("text") or "").lower() for w in words)]
            for r in rows:   # recalled = used
                self.store.exec("update memory set use_count=?, last_used=? where id=?",
                                ((r.get("use_count") or 0) + 1, now_iso(), r["id"]))
        return json.dumps([self.memory_json(r) for r in rows], ensure_ascii=False)

    def manage_tasks_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action in ("add", "create"):
            return json.dumps(self.upsert_task(args), ensure_ascii=False)
        if action in ("edit", "update"):
            tid = args.get("id") or args.get("task_id")
            return json.dumps(self.upsert_task(args, tid), ensure_ascii=False)
        if action == "delete":
            tid = args.get("id") or args.get("task_id")
            if not tid:
                raise ValueError("task id required")
            self.store.exec("delete from tasks where id=?", (tid,))
            return f"Deleted task {tid}"
        rows = self.store.rows("select * from tasks order by datetime(created_at) desc")
        return json.dumps([self.task_json(r) for r in rows], ensure_ascii=False)

    def manage_skills_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action in ("add", "create"):
            return json.dumps(self.upsert_skill(args), ensure_ascii=False)
        if action in ("edit", "update"):
            sid = args.get("id") or args.get("skill_id")
            return json.dumps(self.upsert_skill(args, sid), ensure_ascii=False)
        if action == "delete":
            sid = args.get("id") or args.get("skill_id")
            if not sid:
                raise ValueError("skill id required")
            self.store.exec("delete from skills where id=?", (sid,))
            return f"Deleted skill {sid}"
        rows = self.store.rows("select * from skills order by datetime(created_at) desc")
        return json.dumps([self.skill_json(r) for r in rows], ensure_ascii=False)

    def trigger_research_tool(self, body, form=None):
        form = form or {}
        args = self.parse_tool_args(body)
        query = args.get("topic") or args.get("query") or body.strip()
        rid = self.create_research(query or "Untitled research", form.get("endpoint_id"), form.get("model"))
        return json.dumps({"id": rid, "query": query, "status": "running",
                           "note": f"Deep research started. Track it in the Deep Research panel: [{query}](#research-{rid})"},
                          ensure_ascii=False)

    def manage_research_tool(self, body, form=None):
        form = form or {}
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action in ("add", "create", "start"):
            rid = self.create_research(args.get("query") or args.get("topic") or "Untitled research",
                                       form.get("endpoint_id"), form.get("model"))
            return json.dumps({"id": rid, "status": "running"}, ensure_ascii=False)
        if action in ("view", "get"):
            rid = args.get("id") or args.get("research_id")
            row = self.store.one("select * from research where id=?", (rid,))
            if not row:
                raise ValueError("research item not found")
            return json.dumps(self.research_json(row), ensure_ascii=False)
        if action in ("delete", "archive"):
            rid = args.get("id") or args.get("research_id")
            if not rid:
                raise ValueError("research id required")
            if action == "archive":
                self.store.exec("update research set archived=1, updated_at=? where id=?", (now_iso(), rid))
                return f"Archived research {rid}"
            self.store.exec("delete from research where id=?", (rid,))
            return f"Deleted research {rid}"
        rows = self.store.rows("select * from research where archived=0 order by datetime(created_at) desc")
        return json.dumps([self.research_json(r) for r in rows], ensure_ascii=False)

    def email_tool(self, name, body):
        args = self.parse_tool_args(body)
        if name == "list_email_accounts":
            return json.dumps([self.email_source_json(r) for r in self.email_sources()], ensure_ascii=False)
        if name == "list_emails":
            return self.email_list_payload(args)
        if name == "read_email":
            uid = args.get("uid") or body.strip()
            return self.email_read_payload(uid, args.get("folder") or "INBOX")
        if name == "send_email":
            return self.email_send_payload(args)
        if name == "reply_to_email":
            uid = args.get("uid")
            if not uid:
                raise ValueError("uid required")
            msg = self.email_read_data(uid, args.get("folder") or "INBOX")
            return self.email_send_payload({
                "to": msg.get("from_address") or msg.get("from") or "",
                "subject": "Re: " + (msg.get("subject") or ""),
                "body": args.get("body") or "",
            })
        if name in ("archive_email", "delete_email", "mark_email_read"):
            uid = args.get("uid")
            if not uid:
                raise ValueError("uid required")
            if name == "archive_email":
                return self.email_flag_payload(uid, "\\Deleted", add=False, archive=True, folder=args.get("folder") or "INBOX")
            if name == "delete_email":
                return self.email_flag_payload(uid, "\\Deleted", add=True, expunge=True, folder=args.get("folder") or "INBOX")
            return self.email_flag_payload(uid, "\\Seen", add=bool(args.get("read", True)), folder=args.get("folder") or "INBOX")
        if name == "bulk_email":
            action = args.get("action")
            uids = args.get("uids") or []
            outs = []
            for uid in uids:
                if action == "delete":
                    outs.append(self.email_flag_payload(uid, "\\Deleted", add=True, expunge=True, folder=args.get("folder") or "INBOX"))
                elif action == "archive":
                    outs.append(self.email_flag_payload(uid, "\\Deleted", add=False, archive=True, folder=args.get("folder") or "INBOX"))
                elif action in ("mark_read", "mark_unread"):
                    outs.append(self.email_flag_payload(uid, "\\Seen", add=action == "mark_read", folder=args.get("folder") or "INBOX"))
            return "\n".join(outs) or "No email UIDs supplied."
        raise ValueError(f"unsupported email tool {name}")

    def execute_production_tool(self, root, name, body, form, allow_outside=False):
        if name in ("list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"):
            if root is None:
                # No bound folder: only Full Access may use a scratch workspace
                # (sandbox/read-only correctly get "no folder").
                if not allow_outside:
                    raise ValueError("No folder is bound to this chat. Bind a folder, or use Full Access.")
                root = (app_support_dir() / "workspace")
                root.mkdir(parents=True, exist_ok=True)
            if name in ("list_files", "read_file", "read_pdf"):
                return self.execute_file_tool(root, name, body, allow_outside=allow_outside)
            if name == "suggest_document":
                return self.suggest_document_tool(root, body, allow_outside=allow_outside)
            return self.execute_file_tool(root, name, body, allow_outside=allow_outside)
        if name == "bash":
            return self.bash_tool(root, body, form, allow_outside=allow_outside)
        if name == "python":
            return self.python_tool(root, body, form, allow_outside=allow_outside)
        if name == "web_search":
            return self.web_search_tool(body)
        if name == "manage_memory":
            return self.manage_memory_tool(body)
        if name == "manage_tasks":
            return self.manage_tasks_tool(body)
        if name == "manage_skills":
            return self.manage_skills_tool(body)
        if name == "trigger_research":
            return self.trigger_research_tool(body, form)
        if name == "manage_research":
            return self.manage_research_tool(body, form)
        if name == "create_event":
            return self.create_event_tool(body)
        if name in EMAIL_AI_TOOLS:
            return self.email_tool(name, body)
        raise ValueError(f"unsupported tool {name}")

    def create_event_tool(self, body):
        """Create a calendar event from the AI's structured args (it parses the
        natural-language date/time itself into ISO-8601). For a macOS calendar,
        create_event_result raises (native app owns EventKit writes), so we store
        the event in the local `events` table at the AI-resolved time and return
        success — the app's Calendar panel reads that table."""
        args = self.parse_tool_args(body)
        summary = (args.get("summary") or args.get("title") or "New event").strip() or "New event"
        start = (args.get("start") or args.get("dtstart") or "").strip()
        end = (args.get("end") or args.get("dtend") or "").strip()
        all_day = bool(args.get("all_day"))
        if not start:
            raise ValueError("create_event requires an ISO-8601 start datetime")
        if not end:
            end = start
        event = {"summary": summary, "dtstart": start, "dtend": end, "all_day": all_day,
                 "location": args.get("location") or "", "description": args.get("description") or ""}
        cfg = self.pref("calendar_connection") or {}
        if cfg.get("type") == "macos":
            # Native app owns EventKit; still persist locally so the panel shows
            # it at the correct time, and hand the data back for EventKit later.
            uid = "ev_" + os.urandom(6).hex()
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (uid, summary, start, end, 1 if all_day else 0, event["location"], event["description"], "", "", now_iso()),
            )
            return json.dumps({"ok": True, "uid": uid, "event": event}, ensure_ascii=False)
        result = self.create_event_result(event)
        return json.dumps({"ok": True, "uid": result.get("uid", ""), "event": event}, ensure_ascii=False)

    @staticmethod
    def _tool_body_from_params(name, params, fallback=""):
        """Serialize a {param: value} dict into the body text each executor
        expects. Mirrors the Pi's function_call_to_tool_block shaping."""
        if name == "bash":
            return params.get("command") or params.get("cmd") or fallback
        if name == "python":
            return params.get("code") or params.get("script") or fallback
        if name == "web_search":
            return params.get("query") or params.get("q") or fallback
        if name in ("list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"):
            # `key: value` inline (even multi-line) — parse_tool_fields re-splits
            # the body, so an inline first line avoids a spurious leading newline.
            keys = ("path",) if name in ("list_files", "read_file", "read_pdf") else (
                "path", "title", "language", "region", "find", "replace", "reason", "content")
            lines = [f"{key}: {(params[key] or '').strip(chr(10))}"
                     for key in keys
                     if key in params]
            return "\n".join(lines) if lines else fallback
        # manage_*, email, research: executors accept JSON via parse_tool_args.
        if params:
            return json.dumps(params)
        return fallback

    @staticmethod
    def normalize_xml_tools(reply):
        """Rewrite every XML-style tool call the models emit — DeepSeek DSML,
        <tool_call>/<function_call><invoke name=..><parameter ..> wrappers, and
        direct <create_document><path>..</path>.. tags — into the fenced form the
        existing parser/executor handle. Ported from the Pi's tool_parsing."""
        text = reply or ""
        known = PRODUCTION_AI_TOOLS | OBSOLETE_AI_TOOLS | {"write_file"}

        # 1. DeepSeek DSML markup -> standard <invoke>/<parameter>.
        if "DSML" in text:
            pipes = r"[｜|]+"
            text = re.sub(rf"<\s*{pipes}\s*DSML\s*{pipes}\s*tool_calls\s*>", "<tool_call>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*tool_calls\s*>", "</tool_call>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*{pipes}\s*DSML\s*{pipes}\s*invoke\s+name=", "<invoke name=", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*invoke\s*>", "</invoke>", text, flags=re.IGNORECASE)
            text = re.sub(rf'<\s*{pipes}\s*DSML\s*{pipes}\s*parameter\s+name=(["\'][^"\']+["\'])[^>]*>',
                          r"<parameter name=\1>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*parameter\s*>", "</parameter>", text, flags=re.IGNORECASE)

        # 2. <invoke name="tool"><parameter name="k">v</parameter></invoke>
        def invoke_repl(m):
            name = TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
            if name not in known:
                return m.group(0)
            params = {pm.group(1): pm.group(2).strip()
                      for pm in XML_PARAM_RE.finditer(m.group(2))}
            body = Handler._tool_body_from_params(name, params)
            return f"```{name}\n{body}\n```"
        text = XML_INVOKE_RE.sub(invoke_repl, text)
        # Drop now-empty <tool_call>/<function_call> wrapper tags.
        text = re.sub(r"</?(?:[\w]+:)?(?:tool_call|function_call)>", "", text, flags=re.IGNORECASE)

        # 3. Direct <create_document><path>..</path><content>..</content></...>
        def direct_repl(m):
            name = TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
            if name not in known:
                return m.group(0)
            inner = m.group(2)
            params = {fm.group(1).lower(): fm.group(2).strip("\n")
                      for fm in XML_FIELD_RE.finditer(inner)}
            body = Handler._tool_body_from_params(name, params, fallback=inner.strip())
            return f"```{name}\n{body}\n```"
        text = XML_TOOL_RE.sub(direct_repl, text)
        return text

    @staticmethod
    def parse_tool_fields(body):
        fields = {}
        current = None
        buf = []
        block = False  # current field used a YAML block scalar (content: |-)
        key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_ -]*):\s*(.*)$")

        def flush():
            text = "\n".join(buf)
            # Block scalars come indented under the key — dedent to recover it.
            return (textwrap.dedent(text) if block else text).rstrip("\n")

        for line in body.splitlines():
            m = key_re.match(line)
            if m and m.group(1).strip().lower().replace(" ", "_") in {
                "path", "title", "language", "region", "content", "find", "replace", "reason"
            }:
                if current is not None:
                    fields[current] = flush()
                current = m.group(1).strip().lower().replace(" ", "_")
                val = m.group(2)
                # YAML block scalar indicator (|, |-, >, >- …): real content is
                # on the following indented lines — models love this for content.
                if val.strip() in ("|", "|-", "|+", ">", ">-", ">+"):
                    block = True
                    buf = []
                else:
                    block = False
                    # `content:` alone must not seed an empty first line — that
                    # prepends a spurious newline to the file.
                    buf = [val] if val else []
            elif current is not None:
                buf.append(line)
        if current is not None:
            fields[current] = flush()
        return fields

    @staticmethod
    def parse_tool_args(body):
        text = body or ""
        try:
            parsed = json.loads(text) if text.strip() else {}
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        fields = Handler.parse_tool_fields(text)
        if fields:
            return fields
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return {}
        if len(lines) == 1:
            return {"query": lines[0], "text": lines[0], "topic": lines[0]}
        return {"action": lines[0].lower(), "text": "\n".join(lines[1:])}

    @staticmethod
    def resolve_in_workdir(root, rel):
        if not rel:
            raise ValueError("path is required")
        target = (root / rel).resolve()
        if target != root and not str(target).startswith(str(root) + "/"):
            raise ValueError("path escapes session workdir")
        return target

    @staticmethod
    def resolve_tool_path(root, rel, allow_outside=False):
        if allow_outside:
            path = Path(rel).expanduser()
            if path.is_absolute():
                return path.resolve()
        return Handler.resolve_in_workdir(root, rel)

    def local_reply(self, message):
        if message.strip():
            return "JoeBro is running privately on this Mac. Add an OpenAI-compatible local endpoint in Settings to generate model replies. You said: " + message
        return "JoeBro is running privately on this Mac."

    def native_agent_tool_result(self, sid, message, form):
        # Calendar is now a real AI tool (create_event) the model calls in the
        # agent loop with ISO-8601 datetimes it parses itself — no hardcoded
        # regex interception. Nothing else is intercepted here.
        return None

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
                    atomic_write_text(target, content)
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

    @staticmethod
    def document_detail_json(r):
        out = Handler.document_summary_json(r)
        out["current_content"] = r.get("current_content") or ""
        return out

    @staticmethod
    def port_open(host, port):
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    def ensure_hydroxide_bridge(self):
        # Production ships manual IMAP/SMTP only — no Proton/hydroxide bridge.
        # Kept as a no-op so the (now unreachable) bridge code paths stay valid.
        return None

    def email_sources(self):
        return self.store.rows("select * from email_sources order by datetime(created_at) asc")

    @staticmethod
    def email_source_json(row):
        return {"id": row["id"], "display": row["display"], "type": row["type"]}


    def upsert_email_account(self, kind, body):
        is_bridge = kind in {"bridge", "hydroxide"}
        display = body.get("email") or body.get("imap_user") or body.get("host") or "Email account"
        account_id = body.get("id") or "mail_" + os.urandom(6).hex()
        now = now_iso()
        self.store.exec(
            """
            insert or replace into email_sources(
                id,type,display,imap_host,imap_port,imap_ssl,imap_user,imap_password,
                smtp_host,smtp_port,smtp_ssl,smtp_user,smtp_password,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                account_id, kind, display,
                body.get("imap_host") or body.get("host") or "",
                int(body.get("imap_port") or (1143 if is_bridge else 993)),
                1 if bool(body.get("imap_ssl", not is_bridge)) else 0,
                body.get("imap_user") or body.get("email") or "",
                body.get("imap_password") or "",
                body.get("smtp_host") or (body.get("imap_host") if is_bridge else ""),
                int(body.get("smtp_port") or (1025 if is_bridge else 465)),
                1 if bool(body.get("smtp_ssl", not is_bridge)) else 0,
                body.get("smtp_user") or body.get("email") or "",
                body.get("smtp_password") or body.get("imap_password") or "",
                now, now,
            ),
        )
        return account_id

    def split_mail_uid(self, uid):
        raw = urllib.parse.unquote(uid)
        if "::" in raw:
            account_id, msg_uid = raw.split("::", 1)
            account = self.store.one("select * from email_sources where id=?", (account_id,))
            return account, msg_uid
        sources = self.email_sources()
        return (sources[0] if sources else None), raw

    def imap_connect(self, account):
        # Bridge accounts (ProtonMail/hydroxide) need the local bridge running;
        # ensure it for every IMAP op (list, mark-read, archive, …), not just connect.
        if account.get("type") in {"bridge", "hydroxide"} or str(account.get("imap_host")) == "127.0.0.1":
            self.ensure_hydroxide_bridge()
        cls = imaplib.IMAP4_SSL if int(account.get("imap_ssl") or 0) else imaplib.IMAP4
        conn = cls(account["imap_host"], int(account["imap_port"]))
        if account.get("imap_user") or account.get("imap_password"):
            conn.login(account.get("imap_user") or "", account.get("imap_password") or "")
        return conn

    @staticmethod
    def decode_header_value(value):
        if not value:
            return ""
        parts = email.header.decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)

    def message_addresses(self, value):
        parsed = email.utils.parseaddr(self.decode_header_value(value))
        return self.decode_header_value(parsed[0]), parsed[1]

    @staticmethod
    def composite_mail_uid(account, uid):
        return urllib.parse.quote(f"{account['id']}::{uid}", safe=":")

    def email_folders(self):
        folders = {"INBOX"}
        configured = False
        for account in self.email_sources():
            configured = True
            try:
                conn = self.imap_connect(account)
                status, rows = conn.list()
                if status == "OK":
                    for raw in rows or []:
                        text = raw.decode("utf-8", errors="replace")
                        name = text.rsplit(' "/" ', 1)[-1].strip('"') if ' "/" ' in text else text.rsplit(" ", 1)[-1].strip('"')
                        if name:
                            folders.add(name)
                conn.logout()
            except Exception:
                continue
        return self.json({"folders": sorted(folders), "configured": configured})

    def email_list(self, q):
        sources = self.email_sources()
        limit = max(0, int(q.get("limit") or 0))
        folder = q.get("folder") or "INBOX"
        filt = q.get("filter") or "all"
        criterion = "ALL"
        if filt == "unread":
            criterion = "UNSEEN"
        elif filt in ("favorites", "flagged"):
            criterion = "FLAGGED"
        out = []
        errors = []
        total_matched = 0   # all messages matching the filter, before the page limit
        for account in sources:
            try:
                conn = self.imap_connect(account)
                status, _ = conn.select(folder, readonly=True)
                if status != "OK" and folder != "INBOX":
                    status, _ = conn.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError("Mailbox unavailable")
                status, data = conn.uid("search", None, criterion)
                if status != "OK":
                    raise RuntimeError("Search failed")
                uids = (data[0] or b"").split()
                total_matched += len(uids)
                selected = list(reversed(uids if limit == 0 else uids[-limit:]))
                for raw_uid in selected:
                    status, rows = conn.uid("fetch", raw_uid, "(BODY.PEEK[HEADER] FLAGS RFC822.SIZE)")
                    if status != "OK" or not rows:
                        continue
                    header_bytes = b""
                    flags = ""
                    size = 0
                    # FLAGS / RFC822.SIZE can land in the tuple's metadata OR in a
                    # trailing standalone bytes element (after the body literal),
                    # depending on the server. Accumulate ALL of it before parsing,
                    # otherwise read messages look unread (no \Seen captured).
                    for item in rows:
                        if isinstance(item, tuple):
                            flags += item[0].decode("utf-8", errors="replace")
                            header_bytes += item[1] or b""
                        elif isinstance(item, (bytes, bytearray)):
                            flags += item.decode("utf-8", errors="replace")
                    if "RFC822.SIZE" in flags:
                        try:
                            size = int(flags.split("RFC822.SIZE", 1)[1].split()[0].strip(")"))
                        except Exception:
                            size = 0
                    msg = email.message_from_bytes(header_bytes, policy=email.policy.default)
                    from_name, from_addr = self.message_addresses(msg.get("From", ""))
                    flags_lc = flags.lower()
                    out.append({
                        "uid": self.composite_mail_uid(account, raw_uid.decode()),
                        "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                        "from_name": from_name,
                        "from_address": from_addr,
                        "date": msg.get("Date", ""),
                        "is_read": "\\seen" in flags_lc,
                        "is_flagged": "\\flagged" in flags_lc,
                        "has_attachments": False,
                        "cached_summary": "",
                        "size": size,
                    })
                conn.close()
                conn.logout()
            except Exception as exc:
                errors.append(f"{account.get('display') or 'Mailbox'}: {exc}")
        def sort_key(row):
            try:
                dt = email.utils.parsedate_to_datetime(row.get("date") or "")
                return dt.timestamp() if dt else 0
            except Exception:
                return 0
        out.sort(key=sort_key, reverse=True)
        return self.json({"emails": out, "total": total_matched, "configured": bool(sources), "error": "; ".join(errors) if errors and not out else None})

    def email_read(self, uid):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            status, rows = conn.uid("fetch", msg_uid, "(RFC822)")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            raw = b"".join(item[1] for item in rows if isinstance(item, tuple))
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Seen")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            from_name, from_addr = self.message_addresses(msg.get("From", ""))
            body, body_html, attachments = self.extract_email_body(msg)
            conn.logout()
            return self.json({
                "uid": self.composite_mail_uid(account, msg_uid),
                "message_id": msg.get("Message-ID", ""),
                "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                "from_name": from_name,
                "from_address": from_addr,
                "to": msg.get("To", ""),
                "cc": msg.get("Cc", ""),
                "date": msg.get("Date", ""),
                "references": msg.get("References", ""),
                "body": body,
                "body_html": body_html,
                "attachments": attachments,
            })
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def extract_email_body(self, msg):
        text = ""
        html = ""
        attachments = []
        parts = msg.walk() if msg.is_multipart() else [msg]
        for i, part in enumerate(parts):
            cdisp = (part.get_content_disposition() or "").lower()
            ctype = part.get_content_type()
            if cdisp == "attachment":
                attachments.append({
                    "filename": part.get_filename() or f"attachment-{i}",
                    "size": len(part.get_payload(decode=True) or b""),
                    "index": i,
                    "content_type": ctype,
                })
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/plain" and not text:
                text = content
            elif ctype == "text/html" and not html:
                html = content
        return text or html, html, attachments

    def email_set_read(self, uid, read):
        return self.email_store_flag(uid, "\\Seen", add=read)

    def email_archive(self, uid):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            typ, _ = conn.create("Archive")
            conn.uid("COPY", msg_uid, "Archive")
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            conn.logout()
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_delete(self, uid):
        return self.email_store_flag(uid, "\\Deleted", add=True, expunge=True)

    def email_store_flag(self, uid, flag, add=True, expunge=False):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            conn.uid("STORE", msg_uid, "+FLAGS" if add else "-FLAGS", flag)
            if expunge:
                conn.expunge()
            conn.logout()
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_send(self, body):
        sources = self.email_sources()
        if not sources:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        account = sources[0]
        try:
            if account.get("type") in ("bridge", "hydroxide"):
                err = self.ensure_hydroxide_bridge()
                if err:
                    raise RuntimeError(err)
            msg = MIMEMultipart()
            msg["From"] = account.get("smtp_user") or account.get("imap_user") or account.get("display")
            msg["To"] = body.get("to") or ""
            if body.get("cc"):
                msg["Cc"] = body.get("cc")
            msg["Subject"] = body.get("subject") or ""
            if body.get("in_reply_to"):
                msg["In-Reply-To"] = body.get("in_reply_to")
            if body.get("references"):
                msg["References"] = body.get("references")
            msg.attach(MIMEText(body.get("body") or "", "plain", "utf-8"))
            host = account.get("smtp_host") or account.get("imap_host")
            port = int(account.get("smtp_port") or 465)
            use_ssl = bool(int(account.get("smtp_ssl") or 0))
            if use_ssl:
                smtp = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                # Plain connection upgraded via STARTTLS before AUTH (covers
                # submission port 587 and the local hydroxide bridge on :1025).
                smtp = smtplib.SMTP(host, port, timeout=30)
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            user = account.get("smtp_user") or account.get("imap_user") or ""
            password = account.get("smtp_password") or account.get("imap_password") or ""
            if user or password:
                smtp.login(user, password)
            addr_fields = [v for v in (msg.get("To", ""), msg.get("Cc", "")) if v]
            recipients = [a for _, a in email.utils.getaddresses(addr_fields) if a]
            refused = smtp.sendmail(msg["From"], recipients, msg.as_string())
            smtp.quit()
            if refused:
                raise RuntimeError("SMTP refused recipients: " + json.dumps(refused))
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_list_payload(self, args):
        sources = self.email_sources()
        if not sources:
            return json.dumps({"emails": [], "total": 0, "configured": False}, ensure_ascii=False)
        limit = max(1, int(args.get("max_results") or args.get("limit") or 20))
        folder = args.get("folder") or "INBOX"
        filt = args.get("filter") or "all"
        criterion = "UNSEEN" if args.get("unread_only") or filt == "unread" else "ALL"
        out = []
        for account in sources:
            conn = self.imap_connect(account)
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    raise RuntimeError("Mailbox unavailable")
                status, data = conn.uid("search", None, criterion)
                if status != "OK":
                    raise RuntimeError("Search failed")
                for raw_uid in list(reversed((data[0] or b"").split()))[:limit]:
                    status, rows = conn.uid("fetch", raw_uid, "(BODY.PEEK[HEADER] FLAGS)")
                    if status != "OK":
                        continue
                    header = b"".join(item[1] for item in rows if isinstance(item, tuple))
                    msg = email.message_from_bytes(header, policy=email.policy.default)
                    from_name, from_addr = self.message_addresses(msg.get("From", ""))
                    out.append({
                        "uid": self.composite_mail_uid(account, raw_uid.decode()),
                        "account": account.get("display"),
                        "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                        "from_name": from_name,
                        "from_address": from_addr,
                        "date": msg.get("Date", ""),
                    })
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        return json.dumps({"emails": out, "total": len(out), "configured": True}, ensure_ascii=False)

    def email_read_data(self, uid, folder="INBOX"):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        conn = self.imap_connect(account)
        try:
            status, _ = conn.select(folder)
            if status != "OK":
                raise RuntimeError("Mailbox unavailable")
            status, rows = conn.uid("fetch", msg_uid, "(RFC822)")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            raw = b"".join(item[1] for item in rows if isinstance(item, tuple))
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Seen")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            from_name, from_addr = self.message_addresses(msg.get("From", ""))
            body, body_html, attachments = self.extract_email_body(msg)
            return {
                "uid": self.composite_mail_uid(account, msg_uid),
                "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                "from_name": from_name,
                "from_address": from_addr,
                "to": msg.get("To", ""),
                "cc": msg.get("Cc", ""),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""),
                "body": body,
                "body_html": body_html,
                "attachments": attachments,
            }
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def email_read_payload(self, uid, folder="INBOX"):
        if not uid:
            raise ValueError("uid required")
        return json.dumps(self.email_read_data(uid, folder), ensure_ascii=False)

    def email_send_payload(self, body):
        sources = self.email_sources()
        if not sources:
            raise ValueError("No mailbox connected")
        account = sources[0]
        # For the ProtonMail/hydroxide bridge the SMTP server on :1025 must be
        # running before we can send. Starting it is idempotent and cheap.
        if account.get("type") in ("bridge", "hydroxide"):
            err = self.ensure_hydroxide_bridge()
            if err:
                raise RuntimeError(err)
        msg = MIMEMultipart()
        msg["From"] = account.get("smtp_user") or account.get("imap_user") or account.get("display")
        msg["To"] = body.get("to") or ""
        if body.get("cc"):
            msg["Cc"] = body.get("cc")
        if body.get("bcc"):
            msg["Bcc"] = body.get("bcc")
        msg["Subject"] = body.get("subject") or ""
        msg.attach(MIMEText(body.get("body") or "", "plain", "utf-8"))
        host = account.get("smtp_host") or account.get("imap_host")
        port = int(account.get("smtp_port") or 465)
        use_ssl = bool(int(account.get("smtp_ssl") or 0))
        if use_ssl:
            # Implicit TLS (e.g. port 465).
            smtp = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            # Plain connection that must be upgraded with STARTTLS before AUTH.
            # This covers submission port 587 AND the local hydroxide bridge on
            # :1025, which also requires STARTTLS+AUTH.
            smtp = smtplib.SMTP(host, port, timeout=30)
        try:
            if not use_ssl:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            user = account.get("smtp_user") or account.get("imap_user") or ""
            password = account.get("smtp_password") or account.get("imap_password") or ""
            if user or password:
                smtp.login(user, password)
            # NB: pass only non-empty header values — getaddresses() on Python
            # 3.9 returns a bogus empty pair when the list contains "" entries,
            # which silently drops all recipients.
            addr_fields = [v for v in (msg.get("To", ""), msg.get("Cc", ""), msg.get("Bcc", "")) if v]
            recipients = [a for _, a in email.utils.getaddresses(addr_fields) if a]
            if not recipients:
                raise ValueError("No recipients")
            # sendmail raises SMTPException on failure; refused recipients come
            # back in the returned dict — treat any refusals as a hard error so
            # the tool reports exit_code=1 instead of a silent success.
            refused = smtp.sendmail(msg["From"], recipients, msg.as_string())
            if refused:
                raise RuntimeError("SMTP refused recipients: " + json.dumps(refused))
            return json.dumps({"success": True, "sent": len(recipients)}, ensure_ascii=False)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    def email_flag_payload(self, uid, flag, add=True, expunge=False, archive=False, folder="INBOX"):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        conn = self.imap_connect(account)
        try:
            conn.select(folder or "INBOX")
            if archive:
                conn.create("Archive")
                conn.uid("COPY", msg_uid, "Archive")
                conn.uid("STORE", msg_uid, "+FLAGS", "\\Deleted")
                conn.expunge()
            else:
                conn.uid("STORE", msg_uid, "+FLAGS" if add else "-FLAGS", flag)
                if expunge:
                    conn.expunge()
            return json.dumps({"success": True}, ensure_ascii=False)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def add_message(self, sid, role, content, metadata):
        if not sid:
            return
        self.store.exec(
            "insert into messages(session_id,role,content,metadata,created_at) values(?,?,?,?,?)",
            (sid, role, content or "", json.dumps(metadata or {}), now_iso()),
        )

    def pref(self, key):
        row = self.store.one("select value from prefs where key=?", (key,))
        return self.loads(row["value"]) if row else None

    def set_pref(self, key, value):
        self.store.exec("insert or replace into prefs(key,value) values(?,?)", (key, json.dumps(value)))

    def _models_sig(self):
        """Cheap (DB-only) fingerprint of the endpoint config + hidden-model prefs.
        When it changes the model cache auto-invalidates — no need to bust it from
        every endpoint add/edit/delete/hide site."""
        parts = []
        for e in self.store.rows("select id,base_url,api_key,is_enabled from endpoints order by id"):
            parts.append(f"{e['id']}:{e['base_url']}:{e['is_enabled']}:{bool(e.get('api_key'))}")
            parts.append(",".join(sorted(self.pref("hidden_models_" + e["id"]) or [])))
        return "|".join(parts)

    def model_items(self, refresh=False):
        # Cached: model lists barely change but each endpoint needs a live network
        # call (one slow/sleeping endpoint can take ~16s), so recomputing on every
        # /api/models made the picker stall. Serve a cache keyed on the config
        # signature; any endpoint/visibility change refreshes it automatically.
        # Stale-while-revalidate: once warm we always answer instantly and refresh
        # in the background, so only the very first call after a restart can block.
        now = time.time()
        sig = self._models_sig()
        # All DB access happens HERE on the request thread — sqlite isn't safe to
        # touch from the background/worker threads below.
        specs = self._endpoint_specs()
        if not refresh:
            with _MODELS_CACHE_LOCK:
                c = _MODELS_CACHE
                same = c["items"] is not None and c.get("sig") == sig
                fresh = same and now - c["ts"] < _MODELS_TTL
                cached = c["items"] if same else None
            if fresh:
                return cached
            if cached is not None:
                # Same config, just stale — hand back the stale list now and
                # refresh off the request path (network only, no DB).
                threading.Thread(target=self._build_model_items, args=(specs, sig),
                                 daemon=True).start()
                return cached
        return self._build_model_items(specs, sig)

    def _endpoint_specs(self):
        """(id, name, base_url, api_key, hidden-set) per enabled endpoint — the
        DB-side data the model list needs, gathered on the request thread."""
        return [(ep["id"], ep["name"], ep["base_url"], ep.get("api_key") or "",
                 set(self.pref("hidden_models_" + ep["id"]) or []))
                for ep in self.store.rows("select * from endpoints where is_enabled=1 order by created_at")]

    def _build_model_items(self, specs, sig):
        """Network-only: fetch each endpoint's models IN PARALLEL and cache the
        assembled picker list. No DB access, so it's safe to run in a thread."""
        global _MODELS_CACHE
        import concurrent.futures
        fetched = {}
        if specs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(specs))) as ex:
                futs = {ex.submit(self._fetch_models, base, key): epid
                        for (epid, _name, base, key, _h) in specs}
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        fetched[futs[fut]] = fut.result()
                    except Exception:
                        fetched[futs[fut]] = []
        items = []
        for (epid, name, base, key, hidden) in specs:
            # Exclude models the user deselected (hidden) — the picker only
            # shows what's selected; the settings list uses the per-endpoint
            # /models route which still returns everything with is_hidden.
            models = [mid for mid in fetched.get(epid, []) if mid not in hidden]
            if not models:
                continue
            items.append({
                "url": base,
                "endpoint_id": epid,
                "endpoint_name": name,
                "category": "local",
                "offline": False,
                "models": models,
                "models_display": models,
            })
        with _MODELS_CACHE_LOCK:
            _MODELS_CACHE = {"ts": time.time(), "items": items, "sig": sig}
        return items

    def _fetch_models(self, base, api_key):
        """Network-only model-id list for one endpoint (no DB — thread-safe)."""
        base = (base or "").rstrip("/")
        if base.endswith("/v1"):
            candidates = [base + "/models", base[:-3] + "/api/tags"]
        else:
            candidates = [base + "/models", base + "/v1/models", base + "/api/tags"]
        is_anthropic = "anthropic.com" in base
        data = None
        for url in candidates:
            req = urllib.request.Request(url)
            # A real User-Agent — Groq/Cerebras sit behind Cloudflare, which
            # 1010-blocks urllib's default UA.
            req.add_header("User-Agent",
                           "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
            if api_key:
                if is_anthropic:
                    # Anthropic doesn't accept Bearer on /v1/models.
                    req.add_header("x-api-key", api_key)
                    req.add_header("anthropic-version", "2023-06-01")
                else:
                    req.add_header("Authorization", "Bearer " + api_key)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception:
                continue
        if not data:
            return []
        rows = data.get("data") or data.get("models") or []
        out = []
        for row in rows:
            mid = (row.get("id") or row.get("name") or row.get("model")) if isinstance(row, dict) else str(row)
            if mid:
                out.append(mid)
        return out

    def endpoint_models(self, ep_id):
        ep = self.store.one("select * from endpoints where id=?", (ep_id,))
        if not ep:
            return []
        hidden = set(self.pref("hidden_models_" + ep_id) or [])
        return [{"id": mid, "display": mid, "is_hidden": mid in hidden}
                for mid in self._fetch_models(ep["base_url"], ep.get("api_key") or "")]

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

    def workdir_post(self, path):
        sid = path.split("/")[3]
        if path.endswith("/workdir"):
            body = self._json_body()
            self.store.exec("update sessions set workdir=? where id=?", (body.get("workdir"), sid))
            return self.json({"ok": True})
        body = self._json_body()
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root = Path((session or {}).get("workdir") or "")
        sub = body.get("sub", "")
        target = (root / sub).resolve()
        if not self._inside(root, target):
            return self.json({"detail": "Path escapes workdir"}, 400)
        if path.endswith("/open-file") and target.exists() and target.is_file():
            data = target.read_text(errors="replace")
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

    def calendar_events(self, q):
        cfg = self.pref("calendar_connection") or {}
        if cfg.get("type") == "caldav":
            try:
                rows = self.caldav_fetch_events(cfg)
                self.replace_cached_caldav_events(rows)
                return self.json({"events": [self.event_json(r) for r in rows]})
            except Exception as exc:
                return self.json({"detail": f"CalDAV sync failed: {exc}"}, 502)
        rows = self.store.rows("select * from events order by dtstart")
        return self.json({"events": [self.event_json(r) for r in rows]})

    def caldav_url(self, cfg, href=""):
        base = (cfg.get("url") or "").strip()
        if not base:
            raise ValueError("CalDAV URL is missing")
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if not base.endswith("/"):
            base += "/"
        return urllib.parse.urljoin(base, href.lstrip("/"))

    def caldav_request(self, cfg, method, url=None, data=None, headers=None):
        target = url or self.caldav_url(cfg)
        req = urllib.request.Request(target, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        principal = cfg.get("principal") or ""
        password = cfg.get("password") or ""
        if principal or password:
            token = base64.b64encode(f"{principal}:{password}".encode("utf-8")).decode("ascii")
            req.add_header("Authorization", "Basic " + token)
        req.add_header("User-Agent", "JoeBro/1.0")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {target} failed with HTTP {exc.code}: {body[:240]}")

    def validate_caldav(self, cfg):
        body = b'''<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'''
        self.caldav_request(cfg, "PROPFIND", data=body, headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})

    @staticmethod
    def ics_escape(value):
        return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

    @staticmethod
    def ics_unescape(value):
        return (value or "").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

    @staticmethod
    def ics_datetime(value, all_day=False):
        raw = value or now_iso()
        if all_day:
            return raw[:10].replace("-", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def iso_from_ics(value):
        v = (value or "").strip()
        if re.fullmatch(r"\d{8}", v):
            return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
        if v.endswith("Z") and re.fullmatch(r"\d{8}T\d{6}Z", v):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        if re.fullmatch(r"\d{8}T\d{6}", v):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%S")
            return dt.isoformat()
        return v

    @staticmethod
    def fold_ics_line(line):
        out = []
        raw = line.encode("utf-8")
        while len(raw) > 75:
            cut = 75
            while cut > 1 and (raw[cut] & 0xC0) == 0x80:
                cut -= 1
            out.append(raw[:cut].decode("utf-8", errors="ignore"))
            raw = b" " + raw[cut:]
        out.append(raw.decode("utf-8", errors="ignore"))
        return "\r\n".join(out)

    def event_ics(self, uid, body):
        all_day = bool(body.get("all_day"))
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//JoeBro//Local Calendar//EN",
            "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            ("DTSTART;VALUE=DATE:" if all_day else "DTSTART:") + self.ics_datetime(body.get("dtstart"), all_day),
            ("DTEND;VALUE=DATE:" if all_day else "DTEND:") + self.ics_datetime(body.get("dtend"), all_day),
            "SUMMARY:" + self.ics_escape(body.get("summary") or "New event"),
        ]
        if body.get("location"):
            lines.append("LOCATION:" + self.ics_escape(body.get("location")))
        if body.get("description"):
            lines.append("DESCRIPTION:" + self.ics_escape(body.get("description")))
        lines += ["END:VEVENT", "END:VCALENDAR"]
        return "\r\n".join(self.fold_ics_line(line) for line in lines) + "\r\n"

    @staticmethod
    def unfold_ics(text):
        lines = []
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.startswith((" ", "\t")) and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        return lines

    def parse_ics_event(self, text, href="", etag=""):
        fields = {}
        params = {}
        in_event = False
        for line in self.unfold_ics(text):
            if line == "BEGIN:VEVENT":
                in_event = True
                continue
            if line == "END:VEVENT":
                break
            if not in_event or ":" not in line:
                continue
            left, value = line.split(":", 1)
            parts = left.split(";")
            key = parts[0].upper()
            fields[key] = value
            params[key] = parts[1:]
        uid = fields.get("UID") or "caldav_" + os.urandom(6).hex()
        dtstart = self.iso_from_ics(fields.get("DTSTART") or "")
        dtend = self.iso_from_ics(fields.get("DTEND") or fields.get("DTSTART") or "")
        all_day = any("VALUE=DATE" in p.upper() for p in params.get("DTSTART", [])) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", dtstart or "") is not None
        return {
            "id": uid,
            "summary": self.ics_unescape(fields.get("SUMMARY") or "Untitled event"),
            "dtstart": dtstart or now_iso(),
            "dtend": dtend or dtstart or now_iso(),
            "all_day": 1 if all_day else 0,
            "location": self.ics_unescape(fields.get("LOCATION") or ""),
            "description": self.ics_unescape(fields.get("DESCRIPTION") or ""),
            "caldav_href": href,
            "caldav_etag": etag,
            "created_at": now_iso(),
        }

    def caldav_fetch_events(self, cfg):
        body = b'''<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>'''
        _status, _headers, data = self.caldav_request(
            cfg, "REPORT", data=body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        root = ET.fromstring(data)
        ns = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
        rows = []
        for resp in root.findall(".//d:response", ns):
            href = (resp.findtext("d:href", default="", namespaces=ns) or "").strip()
            etag = (resp.findtext(".//d:getetag", default="", namespaces=ns) or "").strip()
            caldata = resp.findtext(".//c:calendar-data", default="", namespaces=ns) or ""
            if "BEGIN:VEVENT" in caldata:
                rows.append(self.parse_ics_event(caldata, href=href, etag=etag))
        return rows

    def replace_cached_caldav_events(self, rows):
        self.store.exec("delete from events where caldav_href != ''")
        for r in rows:
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["summary"], r["dtstart"], r["dtend"], r["all_day"], r["location"],
                 r["description"], r["caldav_href"], r["caldav_etag"], r["created_at"]),
            )

    def caldav_put_event(self, cfg, uid, body, href=""):
        target = self.caldav_url(cfg, href or (urllib.parse.quote(uid, safe="") + ".ics"))
        data = self.event_ics(uid, body).encode("utf-8")
        status, headers, _ = self.caldav_request(
            cfg, "PUT", url=target, data=data,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        return target, headers.get("ETag") or headers.get("Etag") or ""

    def parse_natural_calendar_event(self, text):
        raw = (text or "New event").strip()
        lower = raw.lower()
        base = datetime.now().astimezone()
        day = base.replace(hour=0, minute=0, second=0, microsecond=0)
        if "tomorrow" in lower:
            day += timedelta(days=1)
        else:
            weekdays = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
            }
            for name, target in weekdays.items():
                if name in lower:
                    delta = (target - day.weekday()) % 7 or 7
                    day += timedelta(days=delta)
                    break

        def _hm(num, mins, suffix):
            h = int(num); mi = int(mins or 0); suf = (suffix or "").lower()
            if suf.startswith("p") and h < 12:
                h += 12
            if suf.startswith("a") and h == 12:
                h = 0
            return max(0, min(h, 23)), max(0, min(mi, 59))

        hour, minute, end_hm = 9, 0, None
        # A time RANGE first: "14:00-17:00", "2-5pm", "9 to 10:30am".
        rng = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|—|to|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower, re.I)
        if rng:
            hour, minute = _hm(rng.group(1), rng.group(2), rng.group(3) or rng.group(6))
            end_hm = _hm(rng.group(4), rng.group(5), rng.group(6))
        else:
            # Single time: "at 14", "at 2:30pm", bare "14:00", or "2pm".
            m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b|\b(\d{1,2}):(\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b", lower, re.I)
            if m:
                if m.group(1):
                    hour, minute = _hm(m.group(1), m.group(2), m.group(3))
                elif m.group(4):
                    hour, minute = _hm(m.group(4), m.group(5), m.group(6))
                elif m.group(7):
                    hour, minute = _hm(m.group(7), None, m.group(8))
        start = day.replace(hour=hour, minute=minute)
        end = start.replace(hour=end_hm[0], minute=end_hm[1]) if end_hm else start + timedelta(hours=1)
        if end <= start:
            end = start + timedelta(hours=1)

        summary = re.sub(r"(?i)^\s*(add|schedule|create|book)\s+", "", raw)
        summary = re.sub(r"(?i)\s+(to|on)\s+(my\s+)?calendar.*$", "", summary)
        summary = re.sub(r"(?i)\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.*$", "", summary)
        summary = re.sub(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:-|–|—|to|until)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?", "", summary, flags=re.I)
        summary = re.sub(r"(?i)\s+at\s+\d{1,2}(:\d{2})?\s*(am|pm|a|p)?\b.*$", "", summary)
        summary = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", "", summary, flags=re.I).strip()
        if not summary:
            summary = "New event"
        return {
            "summary": summary,
            "dtstart": start.isoformat(),
            "dtend": end.isoformat(),
            "all_day": False,
            "location": "",
            "description": "",
        }

    def create_event_result(self, body):
        cfg = self.pref("calendar_connection") or {}
        if not cfg.get("type"):
            raise RuntimeError("No calendar is connected. Open Settings > General to connect one.")
        if cfg.get("type") == "macos":
            raise RuntimeError("macOS Calendar writes are handled by the native app so EventKit permissions are used.")
        uid = body.get("uid") or "ev_" + os.urandom(6).hex()
        href = ""
        etag = ""
        if cfg.get("type") == "caldav":
            href, etag = self.caldav_put_event(cfg, uid, body)
        self.store.exec(
            """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
               values(?,?,?,?,?,?,?,?,?,?)""",
            (uid, body.get("summary") or "New event", body.get("dtstart") or now_iso(), body.get("dtend") or now_iso(),
             1 if body.get("all_day") else 0, body.get("location") or "", body.get("description") or "", href, etag, now_iso()),
        )
        return {"ok": True, "uid": uid}

    def create_event(self, body):
        try:
            return self.json(self.create_event_result(body))
        except Exception as exc:
            return self.json({"detail": str(exc)}, 500)

    def update_event(self, uid, body):
        row = self.store.one("select * from events where id=?", (uid,))
        if not row:
            return self.json({"detail": "Event not found"}, 404)
        cfg = self.pref("calendar_connection") or {}
        href = row.get("caldav_href") or ""
        etag = row.get("caldav_etag") or ""
        if cfg.get("type") == "caldav":
            href, etag = self.caldav_put_event(cfg, uid, body, href=href)
        self.store.exec(
            "update events set summary=?,dtstart=?,dtend=?,all_day=?,location=?,description=?,caldav_href=?,caldav_etag=? where id=?",
            (body.get("summary") or "Event", body.get("dtstart") or now_iso(), body.get("dtend") or now_iso(),
             1 if body.get("all_day") else 0, body.get("location") or "", body.get("description") or "", href, etag, uid),
        )
        return self.json({"ok": True})

    def delete_event(self, uid):
        row = self.store.one("select * from events where id=?", (uid,))
        cfg = self.pref("calendar_connection") or {}
        if row and cfg.get("type") == "caldav" and row.get("caldav_href"):
            self.caldav_request(cfg, "DELETE", url=self.caldav_url(cfg, row["caldav_href"]))
        self.store.exec("delete from events where id=?", (uid,))
        return self.json({"ok": True})

    def upsert_task(self, body, task_id=None):
        tid = task_id or body.get("id") or body.get("task_id") or "task_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from tasks where id=?", (tid,))
        title = body.get("title") or body.get("name") or (existing or {}).get("title") or "Untitled task"
        prompt = body.get("prompt") or body.get("description") or (existing or {}).get("prompt") or ""
        schedule = body.get("schedule") or body.get("rrule") or (existing or {}).get("schedule") or ""
        scheduled_time = body.get("time") or body.get("scheduled_time") or (existing or {}).get("scheduled_time") or ""
        status = body.get("status") or (existing or {}).get("status") or "active"
        pmode = (body.get("permission_mode") or (existing or {}).get("permission_mode") or "sandbox").strip() or "sandbox"
        if pmode not in ("sandbox", "readonly", "full"):
            pmode = "sandbox"
        if existing:
            self.store.exec(
                "update tasks set title=?,prompt=?,schedule=?,scheduled_time=?,status=?,permission_mode=?,updated_at=? where id=?",
                (title, prompt, schedule, scheduled_time, status, pmode, now, tid),
            )
        else:
            self.store.exec(
                "insert into tasks(id,title,prompt,schedule,scheduled_time,status,permission_mode,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)",
                (tid, title, prompt, schedule, scheduled_time, status, pmode, now, now),
            )
        return {"ok": True, "task": self.task_json(self.store.one("select * from tasks where id=?", (tid,)))}

    def upsert_skill(self, body, skill_id=None):
        sid = skill_id or body.get("id") or body.get("skill_id") or "skill_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from skills where id=?", (sid,))
        name = body.get("name") or body.get("title") or (existing or {}).get("name") or "Untitled skill"
        description = body.get("description") or (existing or {}).get("description") or ""
        content = body.get("procedure") or body.get("content") or body.get("text") or (existing or {}).get("content") or ""
        if isinstance(content, (list, dict)):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        when_to_use = body.get("when_to_use") or (existing or {}).get("when_to_use") or ""
        status = body.get("status") or (existing or {}).get("status") or "draft"
        # Manually-created skills start established (60); auto-created ones pass
        # a low confidence so they must prove useful (via use) or get audited out.
        conf = body.get("confidence")
        conf = int(conf) if conf is not None else ((existing or {}).get("confidence") if existing else 60)
        if conf is None:
            conf = 60
        if existing:
            self.store.exec(
                "update skills set name=?,description=?,content=?,when_to_use=?,status=?,confidence=?,updated_at=? where id=?",
                (name, description, content, when_to_use, status, conf, now, sid),
            )
        else:
            self.store.exec(
                "insert into skills(id,name,description,content,when_to_use,status,confidence,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)",
                (sid, name, description, content, when_to_use, status, conf, now, now),
            )
        return {"ok": True, "skill": self.skill_json(self.store.one("select * from skills where id=?", (sid,)))}

    def create_research(self, query, endpoint_id=None, model=None):
        rid = "research_" + os.urandom(6).hex()
        now = now_iso()
        progress = json.dumps({"phase": "Queued", "message": query})
        self.store.exec(
            "insert into research(id,query,status,content,progress,archived,created_at,updated_at) values(?,?,?,?,?,?,?,?)",
            (rid, query, "running", f"# {query}\n\nResearching…", progress, 0, now, now),
        )
        self.start_research_worker(rid, query, endpoint_id, model)
        return rid

    def start_research_worker(self, rid, query, endpoint_id, model):
        """Spawn the worker for `rid` exactly once. A panel reload / repeat POST
        never starts a second worker, and a finished/cancelled item is never
        re-run (the worker itself bails on a non-running status)."""
        with _RESEARCH_GUARD:
            if rid in _RESEARCH_STARTED:
                return
            _RESEARCH_STARTED.add(rid)
            _RESEARCH_CANCEL.discard(rid)
        threading.Thread(target=self._run_research, args=(rid, query, endpoint_id, model), daemon=True).start()

    def cancel_research(self, rid):
        """Mark a research item cancelled. The worker checks the flag between
        phases and stops; the row's status becomes 'cancelled' so it never
        re-runs or claims to be running."""
        row = self.store.one("select status from research where id=?", (rid,))
        if not row:
            return self.json({"ok": False, "detail": "not found"}, 404)
        with _RESEARCH_GUARD:
            _RESEARCH_CANCEL.add(rid)
        self.store.exec(
            "update research set status='cancelled', progress=?, updated_at=? where id=?",
            (json.dumps({"phase": "Cancelled", "message": ""}), now_iso(), rid))
        return self.json({"ok": True, "status": "cancelled"})

    def _research_cancelled(self, rid):
        with _RESEARCH_GUARD:
            if rid in _RESEARCH_CANCEL:
                return True
        row = self.store.one("select status from research where id=?", (rid,))
        return bool(row) and row.get("status") == "cancelled"

    @staticmethod
    def _report_with_images(content, images):
        """Place up to 2 images inline: one right under the # title, a second
        under the 2nd ## section. Returns content unchanged if no images."""
        images = [u for u in (images or []) if u][:2]
        if not images:
            return content
        out, placed_top, h2 = [], False, 0
        for line in content.split("\n"):
            out.append(line)
            if not placed_top and line.startswith("# "):
                out += ["", f"![]({images[0]})"]
                placed_top = True
            elif len(images) > 1 and line.startswith("## "):
                h2 += 1
                if h2 == 2:
                    out += ["", f"![]({images[1]})"]
        if not placed_top:   # no top-level heading — prepend
            out = [f"![]({images[0]})", ""] + out
        return "\n".join(out)

    def _set_research_progress(self, rid, phase, message=""):
        self.store.exec("update research set progress=?, updated_at=? where id=?",
                        (json.dumps({"phase": phase, "message": message}), now_iso(), rid))

    def _run_research(self, rid, query, endpoint_id, model):
        try:
            if self._research_cancelled(rid):
                return
            self._set_research_progress(rid, "Searching the web", query)
            results = self.search_web(query, count=12)
            if self._research_cancelled(rid):
                return
            self._set_research_progress(rid, "Reading sources", f"{len(results)} sources")
            images = self.search_images(query, count=2)   # best-effort, rendered inline (not in the .md)
            if images:
                self.store.exec("update research set images=? where id=?", (json.dumps(images), rid))
            sources_md = "\n".join(
                f"- [{r['title']}]({r['url']})" + (f"\n  {r['snippet']}" if r.get("snippet") else "")
                for r in results
            ) or "- No sources found."
            report = None
            if endpoint_id and not self._research_cancelled(rid):
                self._set_research_progress(rid, "Writing report", query)
                src_text = "\n\n".join(
                    f"[{i}] {r['title']}\n{r['url']}\n{r.get('snippet', '')}"
                    for i, r in enumerate(results, 1)
                ) or "(no sources retrieved)"
                report = self._complete(
                    endpoint_id, model,
                    "You are a meticulous senior research analyst writing an in-depth briefing for an "
                    "expert reader. Produce a LONG, comprehensive, well-structured Markdown report — aim "
                    "for substantial depth, not a summary. Required structure:\n"
                    "1. A top-level title (single # heading) restating the topic.\n"
                    "2. An '## Executive Summary' of 1-2 dense paragraphs covering the key findings.\n"
                    "3. At least FOUR to SIX themed analysis sections, each a '## heading', that go deep: "
                    "explain mechanisms, compare viewpoints, cite concrete specifics (names, numbers, dates, "
                    "examples). Reference sources inline by bracket number (e.g. [3]) for specific facts.\n"
                    "4. A '## Caveats & Limitations' section.\n"
                    "5. A '## Conclusion' with a clear bottom line.\n"
                    "STYLE: make it SCANNABLE, not a wall of text. Lead each section with one short framing "
                    "sentence, then use BULLET POINTS (and '### subheadings') for the substance — most of the "
                    "detail should be bullets, with bold lead-ins where useful. Keep paragraphs to 1-3 sentences. "
                    "Be thorough in coverage but tight in prose. Use ONLY the supplied sources; never fabricate.",
                    f"Research topic: {query}\n\nNumbered web sources:\n{src_text}\n\n"
                    "Write the complete, in-depth, bullet-driven report now.",
                    max_tokens=8000,
                )
            if self._research_cancelled(rid):
                return
            content = (report.strip() + "\n\n## Sources\n" + sources_md) if report else (
                f"# {query}\n\n## Sources\n{sources_md}")
            self.store.exec("update research set status='completed', content=?, progress=?, updated_at=? where id=?",
                            (content, json.dumps({"phase": "Complete", "message": ""}), now_iso(), rid))
        except Exception as exc:
            if self._research_cancelled(rid):
                return
            self.store.exec("update research set status='completed', content=?, progress=?, updated_at=? where id=?",
                            (f"# {query}\n\nResearch failed: {exc}",
                             json.dumps({"phase": "Complete", "message": "failed"}), now_iso(), rid))
        finally:
            with _RESEARCH_GUARD:
                _RESEARCH_STARTED.discard(rid)
                _RESEARCH_CANCEL.discard(rid)

    def _complete(self, endpoint_id, model, system, user, max_tokens=1500):
        """One-shot text completion against an endpoint (no tools). Used for
        background work like research synthesis."""
        ep = self.store.one("select * from endpoints where id=?", (endpoint_id,))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": model or "", "stream": False, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return (data.get("choices", [{}])[0].get("message", {}) or {}).get("content")
        except Exception:
            return None

    @staticmethod
    def loads(s):
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {}

    @staticmethod
    def session_json(r):
        return {
            "id": r["id"],
            "name": r["name"],
            "model": r.get("model") or "",
            "mode": r.get("mode") or "chat",
            "archived": bool(r.get("archived")),
            "is_important": bool(r.get("is_important")),
            "created_at": r.get("created_at"),
        }

    @staticmethod
    def memory_json(r):
        return {
            "id": r["id"],
            "text": r["text"],
            "category": r.get("category") or "general",
            "pinned": bool(r.get("pinned")),
            "use_count": r.get("use_count") or 0,
            "last_used": r.get("last_used") or "",
            "created_at": r.get("created_at"),
        }

    @staticmethod
    def event_json(r):
        return {
            "uid": r["id"],
            "id": r["id"],
            "summary": r["summary"],
            "dtstart": r["dtstart"],
            "dtend": r["dtend"],
            "all_day": bool(r["all_day"]),
            "location": r["location"],
            "description": r["description"],
        }

    @staticmethod
    def task_json(r):
        title = r.get("title") or "Untitled task"
        return {
            "id": r["id"],
            "name": title,
            "title": title,
            "prompt": r.get("prompt") or "",
            "schedule": r.get("schedule") or "",
            "scheduled_time": r.get("scheduled_time") or "",
            "status": r.get("status") or "active",
            "permission_mode": r.get("permission_mode") or "sandbox",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    @staticmethod
    def skill_json(r):
        return {
            "id": r["id"],
            "name": r.get("name") or "Untitled skill",
            "description": r.get("description") or "",
            "content": r.get("content") or "",
            "procedure": r.get("content") or "",          # the editor's "Procedure (markdown)"
            "when_to_use": r.get("when_to_use") or "",
            "category": r.get("category") or "general",
            "status": r.get("status") or "draft",
            "confidence": r.get("confidence") if r.get("confidence") is not None else 60,
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    @staticmethod
    def research_json(r):
        return {
            "id": r["id"],
            "query": r.get("query") or "",
            "status": r.get("status") or "queued",
            "content": r.get("content") or "",
            "progress": Handler.loads(r.get("progress")) or {},
            "archived": bool(r.get("archived")),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("JOEBRO_PORT", DEFAULT_PORT)))
    parser.add_argument("--data-dir", default="")
    args = parser.parse_args()
    root = Path(args.data_dir) if args.data_dir else app_support_dir()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = Store(root)
    threading.Thread(target=_task_scheduler, args=(server,), daemon=True).start()
    # Boot the local searxng meta-search alongside the app (best-effort).
    def _boot_searxng():
        h = Handler.__new__(Handler); h.server = server
        try:
            h.ensure_searxng()
        except Exception:
            pass
    threading.Thread(target=_boot_searxng, daemon=True).start()
    # Warm the model picker cache at boot so the first /api/models is instant
    # instead of waiting on every endpoint's live fetch.
    def _warm_models():
        h = Handler.__new__(Handler); h.server = server
        try:
            h.model_items(refresh=True)
        except Exception:
            pass
    threading.Thread(target=_warm_models, daemon=True).start()
    print(f"JoeBro backend listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


def _task_scheduler(server):
    """Fire active scheduled tasks once a day at their scheduled_time (HH:MM).
    Lightweight: one check a minute, dedup via the task's last_run date."""
    import types as _types
    while True:
        try:
            now = datetime.now().astimezone()
            hhmm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
            for row in server.store.rows("select * from tasks where status='active'"):
                st = (row.get("scheduled_time") or "").strip()[:5]
                sched = (row.get("schedule") or "daily").strip().lower()
                # ponytail: weekly = Mondays, monthly = 1st; dedup still by date
                if sched == "weekly" and now.weekday() != 0:
                    continue
                if sched == "monthly" and now.day != 1:
                    continue
                if st and st == hhmm and (row.get("last_run") or "")[:10] != today:
                    h = Handler.__new__(Handler)
                    h.server = _types.SimpleNamespace(store=server.store)
                    try:
                        h.execute_task(row["id"])
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(50)


if __name__ == "__main__":
    main()
