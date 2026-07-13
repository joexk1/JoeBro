"""Anki .apkg (old collection.anki2 format, schema 11) <-> plain Q/A text.

Everything here is stdlib: an .apkg is a zip holding a SQLite database
(collection.anki2: col/notes/cards tables, note fields \x1f-separated) plus a
`media` JSON manifest. The old format is what every Anki version can import.

The text form is what the model writes and the editor edits:

    Q: What is the capital of France?
    A: Paris

    Q: A question
       can continue on more lines
    A: So can the answer
    S: learned

`Q:` starts a card, `A:` its answer; further lines continue the open field.
`S: learned` marks a card the user has learned (kept as a review card in
Anki terms, so real Anki imports the progress too). Blank lines separate.
"""
import hashlib
import html
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

# Fixed model id: deterministic ids (model, note guids) mean re-importing an
# edited deck into real Anki UPDATES the existing notes instead of duplicating.
MODEL_ID = 1607392319495

_MARK_RE = re.compile(r"^\s*(Q|A|S)\s*:\s?(.*)$", re.IGNORECASE)


def parse_cards(text):
    """Q/A text -> [{'front','back','learned'}]. Lenient: lines before the
    first Q: are ignored, continuation lines join the open field."""
    cards, front, back, learned, field = [], [], [], False, None

    def flush():
        nonlocal front, back, learned, field
        f, b = "\n".join(front).strip(), "\n".join(back).strip()
        if f or b:
            cards.append({"front": f, "back": b, "learned": learned})
        front, back, learned, field = [], [], False, None

    for line in (text or "").splitlines():
        m = _MARK_RE.match(line)
        if m:
            tag, val = m.group(1).upper(), m.group(2)
            if tag == "Q":
                flush()
                front, field = [val], "q"
            elif tag == "A":
                back, field = [val], "a"
            else:
                learned = val.strip().lower() == "learned"
                field = None
        elif field == "q":
            front.append(line)
        elif field == "a":
            back.append(line)
    flush()
    return cards


def cards_to_text(cards):
    # ponytail: continuation lines are written raw — a field line that itself
    # starts with "Q:/A:/S:" would garble the round-trip; escape if it ever bites.
    blocks = []
    for c in cards:
        block = "Q: " + (c.get("front") or "") + "\nA: " + (c.get("back") or "")
        if c.get("learned"):
            block += "\nS: learned"
        blocks.append(block)
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _field_to_text(fld):
    """Anki fields are HTML — flatten to plain text."""
    t = re.sub(r"(?i)<br\s*/?>", "\n", fld)
    t = re.sub(r"(?i)</div>\s*<div>", "\n", t)
    t = re.sub(r"(?i)</?(div|p)[^>]*>", "\n", t)
    t = re.sub(r"\[sound:[^]]*\]", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def _text_to_field(text):
    return html.escape(text, quote=False).replace("\n", "<br>")


def apkg_to_text(data):
    """.apkg bytes -> Q/A text. A note is learned when all its cards are in
    review (type/queue 2) — true for our own decks and for real Anki ones."""
    with zipfile.ZipFile(BytesIO(data)) as z:
        names = z.namelist()
        # Newer exports ship both; collection.anki21 holds the real data and
        # .anki2 is a "please upgrade" stub.
        member = "collection.anki21" if "collection.anki21" in names else "collection.anki2"
        raw = z.read(member)
    with tempfile.NamedTemporaryFile(suffix=".anki2") as tf:
        tf.write(raw)
        tf.flush()
        con = sqlite3.connect(tf.name)
        try:
            notes = con.execute("select id, flds from notes order by id").fetchall()
            state = {}
            for nid, ctype, queue in con.execute("select nid, type, queue from cards"):
                done = ctype == 2 or queue == 2
                state[nid] = state.get(nid, True) and done
        finally:
            con.close()
    cards = []
    for nid, flds in notes:
        parts = flds.split("\x1f")
        cards.append({"front": _field_to_text(parts[0] if parts else ""),
                      "back": _field_to_text(parts[1] if len(parts) > 1 else ""),
                      "learned": state.get(nid, False)})
    return cards_to_text(cards)


def text_to_apkg(text, deck_name="Flashcards"):
    """Q/A text -> .apkg bytes (schema-11 collection.anki2 + media manifest)."""
    cards = parse_cards(text)
    now = int(time.time())
    now_ms = now * 1000
    mid = str(MODEL_ID)

    model = {
        "id": MODEL_ID, "name": "Basic", "type": 0, "mod": now, "usn": -1,
        "sortf": 0, "did": 1, "vers": [], "tags": [],
        "tmpls": [{"name": "Card 1", "ord": 0, "did": None, "bqfmt": "", "bafmt": "",
                   "qfmt": "{{Front}}",
                   "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}"}],
        "flds": [{"name": "Front", "ord": 0, "sticky": False, "rtl": False,
                  "font": "Arial", "size": 20, "media": []},
                 {"name": "Back", "ord": 1, "sticky": False, "rtl": False,
                  "font": "Arial", "size": 20, "media": []}],
        "css": ".card { font-family: arial; font-size: 20px; text-align: center; "
               "color: black; background-color: white; }",
        "latexPre": "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n"
                    "\\usepackage[utf8]{inputenc}\n\\usepackage{amssymb,amsmath}\n"
                    "\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n\\begin{document}\n",
        "latexPost": "\\end{document}",
        "req": [[0, "all", [0]]],
    }
    deck = {"id": 1, "name": deck_name or "Flashcards", "mod": now, "usn": -1,
            "lrnToday": [0, 0], "revToday": [0, 0], "newToday": [0, 0],
            "timeToday": [0, 0], "collapsed": False, "desc": "", "dyn": 0,
            "conf": 1, "extendNew": 10, "extendRev": 50}
    dconf = {"id": 1, "name": "Default", "replayq": True, "timer": 0, "maxTaken": 60,
             "usn": -1, "mod": 0, "autoplay": True,
             "new": {"perDay": 20, "delays": [1, 10], "separate": True,
                     "ints": [1, 4, 7], "initialFactor": 2500, "bury": True, "order": 1},
             "rev": {"perDay": 100, "fuzz": 0.05, "ivlFct": 1, "maxIvl": 36500,
                     "ease4": 1.3, "bury": True, "minSpace": 1},
             "lapse": {"leechFails": 8, "minInt": 1, "delays": [10],
                       "leechAction": 0, "mult": 0}}
    conf = {"nextPos": 1, "estTimes": True, "activeDecks": [1], "sortType": "noteFld",
            "timeLim": 0, "sortBackwards": False, "addToCur": True, "curDeck": 1,
            "newBury": True, "newSpread": 0, "dueCounts": True,
            "curModel": mid, "collapseTime": 1200}

    with tempfile.NamedTemporaryFile(suffix=".anki2") as tf:
        con = sqlite3.connect(tf.name)
        try:
            con.executescript("""
                create table col (id integer primary key, crt integer, mod integer,
                    scm integer, ver integer, dty integer, usn integer, ls integer,
                    conf text, models text, decks text, dconf text, tags text);
                create table notes (id integer primary key, guid text, mid integer,
                    mod integer, usn integer, tags text, flds text, sfld integer,
                    csum integer, flags integer, data text);
                create table cards (id integer primary key, nid integer, did integer,
                    ord integer, mod integer, usn integer, type integer, queue integer,
                    due integer, ivl integer, factor integer, reps integer,
                    lapses integer, left integer, odue integer, odid integer,
                    flags integer, data text);
                create table revlog (id integer primary key, cid integer, usn integer,
                    ease integer, ivl integer, lastIvl integer, factor integer,
                    time integer, type integer);
                create table graves (usn integer, oid integer, type integer);
                create index ix_notes_usn on notes (usn);
                create index ix_cards_usn on cards (usn);
                create index ix_revlog_usn on revlog (usn);
                create index ix_cards_nid on cards (nid);
                create index ix_cards_sched on cards (did, queue, due);
                create index ix_revlog_cid on revlog (cid);
                create index ix_notes_csum on notes (csum);
            """)
            con.execute(
                "insert into col values (1,?,?,?,11,0,0,0,?,?,?,?,'{}')",
                (now - now % 86400, now_ms, now_ms, json.dumps(conf),
                 json.dumps({mid: model}), json.dumps({"1": deck}),
                 json.dumps({"1": dconf})))
            for i, c in enumerate(cards):
                fld_front = _text_to_field(c["front"])
                fld_back = _text_to_field(c["back"])
                sha = hashlib.sha1(fld_front.encode("utf-8")).hexdigest()
                nid = now_ms + i
                con.execute(
                    "insert into notes values (?,?,?,?,-1,'',?,?,?,0,'')",
                    (nid, sha[:10], MODEL_ID, now,
                     fld_front + "\x1f" + fld_back, c["front"], int(sha[:8], 16)))
                if c["learned"]:
                    ctype, queue, due, ivl, factor, reps = 2, 2, 1, 1, 2500, 1
                else:
                    ctype, queue, due, ivl, factor, reps = 0, 0, i + 1, 0, 0, 0
                con.execute(
                    "insert into cards values (?,?,1,0,?,-1,?,?,?,?,?,?,0,0,0,0,0,'')",
                    (nid, nid, now, ctype, queue, due, ivl, factor, reps))
            con.commit()
        finally:
            con.close()
        db = Path(tf.name).read_bytes()

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("collection.anki2", db)
        z.writestr("media", "{}")
    return buf.getvalue()
