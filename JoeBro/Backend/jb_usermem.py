"""The `user` scope of Shared Memory, as the home of JoeBro's own memory.

Before: three stores (jb_memory's `memory` table = the Brain, the phone agent's muse markdown
files, and Shared Memory for coding agents). Now there is one: `shared_facts` with project
`user`. The old surfaces are thin aliases over it:

- /api/memory*, manage_memory, recall_memories      -> user facts with source `jb_memory`
- /api/muse*, muse_* tools (files = topics)          -> user facts with source `muse`
- the personal agent's brief                         -> the whole user scope (+ recall across projects on request)

`migrate()` is one-time and idempotent: facts keep the old row id (`mem_...`) or get a stable id
derived from file+text (`mu_...`), insert never overwrites, and the old table and files are left
untouched, so nothing is lost and a re-run adds nothing. Archiving in the new store sticks.
The user scope is never part of a coding agent's brief, recall or repo mirror (jb_sharedmem)."""
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import jb_sharedmem as sm

USER = "user"
S_MEM, S_MUSE = "jb_memory", "muse"
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}):\s*")


def _ts(iso, default=None):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "")).timestamp()
    except (ValueError, TypeError):
        return default or time.time()


def _slug(t):
    return re.sub(r"[^a-z0-9-]+", "-", str(t or "").lower()).strip("-")[:40]


def _schema(store):
    sm._tables(store)
    try:
        store.exec("ALTER TABLE shared_facts ADD COLUMN extra TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    store.exec("CREATE TABLE IF NOT EXISTS user_files(name TEXT PRIMARY KEY)")


def ensure(store):
    if getattr(store, "_um_ready", False):
        return
    _schema(store)
    try:
        migrate(store)
        store._um_ready = True
    except Exception:
        pass            # retried on the next call; the old stores are untouched either way


def _put(store, fid, text, topic, kind, pinned, created, updated, source, session="", extra="", strength=sm.START_STRENGTH):
    """Insert-if-absent with a chosen id and times. Returns True when added."""
    before = store.one("SELECT 1 AS x FROM shared_facts WHERE id=?", (fid,))
    if before:
        return False
    store.exec("""INSERT INTO shared_facts(id,project,topic,kind,text,pinned,strength,created,updated,last_used,agent,session,source,extra)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (fid, USER, _slug(topic), str(kind or "fact")[:20], text, int(bool(pinned)), strength,
                                                created, updated, updated, source, str(session)[:100], source, extra))
    return True


def migrate(store, mdir=None):
    """Copy the Brain table and the muse files into the user scope. Returns counts of facts added."""
    _schema(store)
    added = {S_MEM: 0, S_MUSE: 0}
    try:
        mem = store.rows("SELECT * FROM memory")
    except Exception:
        mem = []
    for r in mem:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        created, updated = _ts(r.get("created_at")), _ts(r.get("updated_at") or r.get("created_at"))
        ent = (r.get("entities") or "").strip()
        ok = _put(store, str(r["id"]), text, r.get("topic") or r.get("category") or "general", r.get("kind") or "world",
                  r.get("pinned"), created, updated, S_MEM, r["id"], json.dumps({"entities": ent}) if ent else "",
                  strength=min(1.0, 0.5 + 0.1 * int(r.get("use_count") or 0)))
        if ok and r.get("use_count"):
            store.exec("UPDATE shared_facts SET recalls=? WHERE id=?", (int(r["use_count"]), str(r["id"])))
        added[S_MEM] += ok
    if mdir is None:
        from jb_core import data_dir
        mdir = Path(data_dir(store)) / "muse"
    for p in sorted(Path(mdir).glob("*.md")) if Path(mdir).is_dir() else []:
        name = p.stem
        store.exec("INSERT OR IGNORE INTO user_files(name) VALUES(?)", (name,))
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines()):
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            t = t[2:].strip() if t.startswith("- ") else t
            m = _DATE.match(t)
            when = _ts(m.group(1) + "T12:00:00") if m else p.stat().st_mtime
            t = _DATE.sub("", t).strip()
            if t:
                fid = "mu_" + hashlib.sha1((name + "\0" + t).encode()).hexdigest()[:12]
                added[S_MUSE] += _put(store, fid, t, name, "fact", False, when + i * 1e-3, when + i * 1e-3, S_MUSE, name)
    return added


# ---------------------------------------------------------------- Brain alias (jb_memory shape)
def mem_json(r):
    ex = json.loads(r.get("extra") or "{}") if r.get("extra") else {}
    return {"id": r["id"], "text": r["text"], "category": r["topic"] or "general", "kind": r["kind"] or "world", "topic": r["topic"],
            "entities": ex.get("entities", ""), "pinned": bool(r["pinned"]), "use_count": r["recalls"],
            "last_used": sm._iso(r["last_used"]) if r["recalls"] else "", "created_at": sm._iso(r["created"]),
            "updated_at": sm._iso(r["updated"])}


def mem_rows(store):
    ensure(store)
    rows = store.rows("SELECT * FROM shared_facts WHERE project=? AND status='active' AND source<>? "
                      "ORDER BY pinned DESC, created DESC", (USER, S_MUSE))
    return [mem_json(r) for r in rows]


def mem_get(store, mid):
    ensure(store)
    r = store.one("SELECT * FROM shared_facts WHERE id=? AND project=?", (mid, USER))
    return mem_json(r) if r else None


def mem_write(store, mid, text, topic, kind, entities, pinned):
    """Insert or update one Brain memory under `mid`."""
    ensure(store)
    now = time.time()
    extra = json.dumps({"entities": entities}) if entities else ""
    if store.one("SELECT 1 AS x FROM shared_facts WHERE id=? AND project=?", (mid, USER)):
        store.exec("UPDATE shared_facts SET text=?,topic=?,kind=?,pinned=?,extra=?,updated=?,status='active',valid_to=NULL WHERE id=?",
                   (text, _slug(topic), kind, int(bool(pinned)), extra, now, mid))
    else:
        _put(store, mid, text, topic, kind, pinned, now, now, S_MEM, mid, extra)


def mem_delete(store, mid):
    ensure(store)
    if store.one("SELECT 1 AS x FROM shared_facts WHERE id=? AND project=?", (mid, USER)):
        sm.archive(store, mid, "deleted in the Brain tab", "jb_memory")


def mem_pin(store, mid, pinned=None):
    ensure(store)
    r = store.one("SELECT pinned FROM shared_facts WHERE id=? AND project=?", (mid, USER))
    if r:
        store.exec("UPDATE shared_facts SET pinned=? WHERE id=?", (int(not r["pinned"]) if pinned is None else int(pinned), mid))


def mem_touch(store, ids):
    for i in ids:
        r = store.one("SELECT * FROM shared_facts WHERE id=?", (i,))
        if r:
            sm.reinforce(store, r)


# ---------------------------------------------------------------- muse alias (markdown file shape)
def muse_names(store):
    ensure(store)
    names = {r["name"] for r in store.rows("SELECT name FROM user_files")}
    names |= {r["topic"] for r in store.rows("SELECT DISTINCT topic FROM shared_facts WHERE project=? AND source=? AND status='active'",
                                             (USER, S_MUSE)) if r["topic"]}
    return sorted(names)


def _muse_facts(store, name):
    return store.rows("SELECT * FROM shared_facts WHERE project=? AND source=? AND topic=? AND status='active' ORDER BY created, rowid",
                      (USER, S_MUSE, name))


def muse_read(store, name):
    ensure(store)
    if name not in muse_names(store):
        return None
    out = ["# " + name.replace("-", " ").title()]
    out += ["- %s: %s" % (time.strftime("%Y-%m-%d", time.localtime(r["created"])), r["text"]) for r in _muse_facts(store, name)]
    return "\n".join(out) + "\n"


def muse_info(store, name):
    rows = _muse_facts(store, name)
    ts = max([r["updated"] for r in rows], default=time.time())
    return {"lines": len(rows), "bytes": sum(len(r["text"]) + 14 for r in rows), "updated": datetime.fromtimestamp(ts).isoformat(timespec="seconds")}


def muse_write(store, name, content):
    """Replace a muse file's lines. Unchanged lines stay; a changed line replaces its old one in place;
    removed lines are archived (history stays in the store); new lines are appended."""
    ensure(store)
    store.exec("INSERT OR IGNORE INTO user_files(name) VALUES(?)", (name,))
    new = []
    for line in str(content or "").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        t = t[2:].strip() if t.startswith("- ") else t
        m = _DATE.match(t)
        t = _DATE.sub("", t).strip()
        if t:
            new.append((t, _ts(m.group(1) + "T12:00:00") if m else None))
    old = _muse_facts(store, name)
    left_new = list(new)
    unmatched_old = []
    for r in old:
        hit = next((n for n in left_new if n[0] == r["text"]), None)
        if hit:
            left_new.remove(hit)
        else:
            unmatched_old.append(r)
    now = time.time()
    for r, n in zip(unmatched_old, left_new):          # replaced in place: same position in the file
        fid = "mu_" + os.urandom(5).hex()
        _put(store, fid, n[0], name, "fact", r["pinned"], r["created"] + 1e-6, now, S_MUSE, name)
        sm.archive(store, r["id"], "edited in the memory file", S_MUSE, by=fid)
    for r in unmatched_old[len(left_new):]:
        sm.archive(store, r["id"], "removed from the memory file", S_MUSE)
    for i, n in enumerate(left_new[len(unmatched_old):]):
        _put(store, "mu_" + os.urandom(5).hex(), n[0], name, "fact", False, n[1] or now + i * 1e-3, now, S_MUSE, name)


def muse_delete(store, name):
    ensure(store)
    if name not in muse_names(store):
        return False
    for r in _muse_facts(store, name):
        sm.archive(store, r["id"], "memory file deleted", S_MUSE)
    store.exec("DELETE FROM user_files WHERE name=?", (name,))
    return True


def recall_everywhere(store, query, limit=8):
    """The personal agent, on request: facts from every scope (user, global, all projects)."""
    keys = [p["key"] for p in sm.list_projects(store)]
    return sm.search(store, None, query, limit, keys=keys)
