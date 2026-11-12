"""Shared Memory: one JoeBro-owned memory that every linked coding agent reads and
writes (MCP tools in `joebro-agents mcp`) and the apps browse (/api/shared-memory).

It replaces the per-agent memory files (CLAUDE.md, AGENTS.md, Claude Code memory):
those are imported once, deduplicated, and from then on each agent gets a brief of
at most BRIEF_TOKENS tokens at session start; everything else is pull-only (recall).

Rules:
- Scopes: `project` (key = normalised git remote, else repo path), `global`, and
  `user` (facts about the person, for the personal agent). The `user` scope is
  never part of a coding agent's brief, recall or repo mirror.
- Facts are superseded, never overwritten, and archived, never deleted.
- Strength grows on recall and decays with a half-life; writes are deduplicated or
  superseded at write time; each project has a size cap (weakest get archived).
- No model calls anywhere: decay, dedupe and consolidation are arithmetic, token-set
  similarity and SQLite. Stdlib only.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path



def data_dir(store=None):
    from jb_core import data_dir as _dd     # late: jb_core imports this module's tool schemas
    return _dd(store)

BRIEF_TOKENS = 400          # hard cap for the session-start brief
RECALL_LIMIT = 5
HALF_LIFE_DAYS = 30.0
START_STRENGTH = 0.5
RECALL_GAIN = 0.25          # s += (1 - s) * gain
DEDUPE_SIM = 0.8            # same fact again: reinforce the existing one
SUPERSEDE_SIM = 0.5         # same subject, new wording: the old one is superseded
MERGE_SIM = 0.4             # nightly: weak notes this similar are merged
WEAK = 0.3
ARCHIVE_BELOW = 0.12
PROJECT_CAP = 150           # active facts per project
MIN_FACT, MAX_FACT = 12, 400
KINDS = ("fact", "decision", "convention", "gotcha", "howto", "preference", "handoff", "learned")
RESERVED = ("global", "user")
MARK_START, MARK_END = "<!-- joebro:memory:start -->", "<!-- joebro:memory:end -->"

_STOP = set("the a an and or of to in on for with is are was be it this that as at by from use uses used "
            "we you our your not no do does should must always never when if then so can will has have".split())
_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|"
                     r"\bxox[abp]-[A-Za-z0-9-]{10,}|(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?[^\s'\"<{$]{8,}", re.I)


def tokens(text):
    """Token estimate (about 4 characters per token). Used for every budget here."""
    return (len(text or "") + 3) // 4


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else ""


def _words(text):
    out = set()
    for w in re.findall(r"[a-z0-9_][a-z0-9_./+-]*", (text or "").lower()):
        w = w.strip("./-")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) > 1 and w not in _STOP:
            out.add(w)
    return out


def similarity(a, b):
    a, b = (a if isinstance(a, set) else _words(a)), (b if isinstance(b, set) else _words(b))
    return len(a & b) / len(a | b) if a and b else 0.0


def _tables(store):
    if getattr(store, "_sm_ready", False):
        return
    store.exec("""CREATE TABLE IF NOT EXISTS shared_facts(id TEXT PRIMARY KEY, project TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'fact', text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', pinned INTEGER NOT NULL DEFAULT 0,
        strength REAL NOT NULL, recalls INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, last_used REAL NOT NULL,
        valid_to REAL, supersedes TEXT NOT NULL DEFAULT '', superseded_by TEXT NOT NULL DEFAULT '', merged_from TEXT NOT NULL DEFAULT '[]',
        agent TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'api')""")
    store.exec("CREATE INDEX IF NOT EXISTS shared_facts_project ON shared_facts(project, status)")
    store.exec("""CREATE TABLE IF NOT EXISTS shared_projects(key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', remote TEXT NOT NULL DEFAULT '',
        paths TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL)""")
    store.exec("""CREATE TABLE IF NOT EXISTS shared_log(id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT NOT NULL, project TEXT NOT NULL,
        action TEXT NOT NULL, at REAL NOT NULL, agent TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""")
    try:
        store._sm_ready = True
    except Exception:
        pass


def _log(store, fact, action, agent="", detail=""):
    store.exec("INSERT INTO shared_log(fact_id,project,action,at,agent,detail) VALUES(?,?,?,?,?,?)",
               (fact["id"], fact["project"], action, time.time(), str(agent or "")[:40], str(detail or "")[:300]))


# ---------------------------------------------------------------------- projects
def norm_remote(url):
    u = re.sub(r"^[a-z+]+://", "", str(url or "").strip(), flags=re.I)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", u)
    return re.sub(r"\.git$", "", u.rstrip("/")).lower()


def _git(path, *args):
    try:
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_project(store, project="", path="", remote="", name=""):
    """The project key for whatever the caller knows, registering the project."""
    _tables(store)
    project, path, remote = str(project or "").strip(), str(path or "").strip(), str(remote or "").strip()
    if project in RESERVED or (not project and not path and not remote):
        return project or "global"
    if project.startswith("project:"):
        project = project[8:]
    if path:
        path = os.path.abspath(os.path.expanduser(path))
    known = store.rows("SELECT * FROM shared_projects")
    key = project
    if not key and path:
        hit = next((p for p in known if path in json.loads(p["paths"] or "[]")), None)
        key = hit["key"] if hit else ""
        if not key and not remote and os.path.isdir(path):
            top = _git(path, "rev-parse", "--show-toplevel")
            # A worktree shares its repo's memory: key on the main checkout.
            common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if common.endswith("/.git"):
                top = common[:-5]
            remote = _git(path, "remote", "get-url", "origin")
            path = top or path
            hit = next((p for p in known if path in json.loads(p["paths"] or "[]")), None)
            key = hit["key"] if hit else ""
    if not key:
        key = norm_remote(remote) if remote else path
    row = next((p for p in known if p["key"] == key), None)
    paths = json.loads(row["paths"]) if row else []
    if path and path not in paths:
        paths = (paths + [path])[-8:]
    nm = name or (row or {}).get("name") or os.path.basename((path or key).rstrip("/")) or key
    store.exec("""INSERT INTO shared_projects(key,name,remote,paths,updated) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
        name=excluded.name, remote=CASE WHEN excluded.remote<>'' THEN excluded.remote ELSE remote END, paths=excluded.paths""",
               (key, nm[:80], remote[:300], json.dumps(paths), time.time()))
    return key


def scope_of(key):
    return key if key in RESERVED else "project"


def project_info(store, key):
    _tables(store)
    row = store.one("SELECT * FROM shared_projects WHERE key=?", (key,)) or {}
    n = {r["status"]: r["n"] for r in store.rows("SELECT status, COUNT(*) AS n FROM shared_facts WHERE project=? GROUP BY status", (key,))}
    return {"key": key, "scope": scope_of(key), "name": row.get("name") or ("You" if key == "user" else "Global" if key == "global" else key),
            "remote": row.get("remote") or "", "paths": json.loads(row.get("paths") or "[]"),
            "active": n.get("active", 0), "archived": n.get("archived", 0), "superseded": n.get("superseded", 0),
            "updated_at": _iso(row.get("updated"))}


def list_projects(store):
    _tables(store)
    keys = [r["key"] for r in store.rows("SELECT key FROM shared_projects ORDER BY updated DESC")]
    keys += [r["project"] for r in store.rows("SELECT DISTINCT project FROM shared_facts") if r["project"] not in keys]
    return [project_info(store, k) for k in ["global", "user"] + [k for k in keys if k not in RESERVED]]


# ---------------------------------------------------------------------- facts
def effective(row, now=None):
    """Strength after decay. Pinned facts do not decay."""
    if row["pinned"]:
        return row["strength"]
    days = max(0.0, ((now or time.time()) - row["last_used"]) / 86400.0)
    return row["strength"] * 0.5 ** (days / HALF_LIFE_DAYS)


def fact_out(store, r, now=None):
    info = store.one("SELECT name FROM shared_projects WHERE key=?", (r["project"],)) or {}
    return {"id": r["id"], "project": r["project"], "project_name": info.get("name") or r["project"], "scope": scope_of(r["project"]),
            "topic": r["topic"], "kind": r["kind"], "text": r["text"], "status": r["status"], "pinned": bool(r["pinned"]),
            "strength": round(effective(r, now), 3), "base_strength": round(r["strength"], 3), "recalls": r["recalls"],
            "created_at": _iso(r["created"]), "updated_at": _iso(r["updated"]), "last_recalled": _iso(r["last_used"]),
            "valid_from": _iso(r["created"]), "valid_to": _iso(r["valid_to"]),
            "supersedes": r["supersedes"], "superseded_by": r["superseded_by"], "merged_from": json.loads(r["merged_from"] or "[]"),
            "provenance": {"agent": r["agent"], "model": r["model"], "session": r["session"], "host": r["host"], "source": r["source"]}}


def _row(store, fid):
    _tables(store)
    r = store.one("SELECT * FROM shared_facts WHERE id=?", (str(fid),))
    if not r:
        raise LookupError("no such memory")
    return r


def _clean(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) < MIN_FACT:
        raise ValueError("too short: store one durable fact (a decision, convention, gotcha, or how to run/test)")
    if len(text) > MAX_FACT:
        raise ValueError("too long (max %d characters): store one short durable fact, not a log or a summary" % MAX_FACT)
    if _SECRET.search(text):
        raise ValueError("that looks like a credential; secrets are never stored in memory")
    return text


def _prov(p):
    p = p or {}
    return [str(p.get(k) or "")[:n] for k, n in (("agent", 40), ("model", 60), ("session", 100), ("host", 40))] + \
        [str(p.get("source") or "api")[:20]]


def _insert(store, key, text, topic, kind, pinned, prov, strength, supersedes=""):
    _tables(store)
    now = time.time()
    fid = "sm_" + os.urandom(5).hex()
    store.exec("""INSERT INTO shared_facts(id,project,topic,kind,text,pinned,strength,created,updated,last_used,supersedes,
        agent,model,session,host,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (fid, key, re.sub(r"[^a-z0-9-]+", "-", str(topic or "").lower()).strip("-")[:40], str(kind or "fact")[:20], text,
                int(bool(pinned)), strength, now, now, now, supersedes, *_prov(prov)))
    return store.one("SELECT * FROM shared_facts WHERE id=?", (fid,))


def _active(store, key):
    return store.rows("SELECT * FROM shared_facts WHERE project=? AND status='active'", (key,))


def reinforce(store, r):
    s = effective(r)
    s = min(1.0, s + (1.0 - s) * RECALL_GAIN)
    store.exec("UPDATE shared_facts SET strength=?, last_used=?, recalls=recalls+1 WHERE id=?", (s, time.time(), r["id"]))


def remember(store, key, text, topic="", kind="fact", pinned=False, prov=None, strength=START_STRENGTH, mirror=True):
    """Store one fact. Returns (row, outcome): added | deduped | superseded."""
    _tables(store)
    text = _clean(text)
    words = _words(text)
    best, sim = None, 0.0
    for r in _active(store, key):
        s = similarity(words, r["text"])
        if s > sim:
            best, sim = r, s
    # Facts that differ in a number or identifier (a port, "module 3" / "module 4") are different facts.
    numeric = best is not None and any(any(c.isdigit() for c in w) for w in words ^ _words(best["text"]))
    if best and ((sim >= DEDUPE_SIM and not numeric) or best["text"].lower() == text.lower()):
        reinforce(store, best)
        _log(store, best, "reinforce", (prov or {}).get("agent"), "same fact written again")
        return _row(store, best["id"]), "deduped"
    bulk = (prov or {}).get("source") == "import"       # an import only dedupes; it never decides one line replaces another
    if best and not bulk and sim >= SUPERSEDE_SIM and (not topic or not best["topic"] or topic == best["topic"]):
        return supersede(store, best["id"], text, prov, "newer wording of the same fact", topic, kind, mirror=mirror), "superseded"
    row = _insert(store, key, text, topic, kind, pinned, prov, strength)
    _log(store, row, "remember", row["agent"])
    enforce_cap(store, key)
    if mirror:
        write_mirror(store, key)
    return row, "added"


def supersede(store, fid, text, prov=None, reason="", topic=None, kind=None, mirror=True):
    old = _row(store, fid)
    text = _clean(text)
    new = _insert(store, old["project"], text, topic or old["topic"], kind or old["kind"], old["pinned"], prov,
                  max(START_STRENGTH, effective(old)), supersedes=old["id"])
    now = time.time()
    store.exec("UPDATE shared_facts SET status='superseded', valid_to=?, superseded_by=?, updated=? WHERE id=?", (now, new["id"], now, old["id"]))
    _log(store, old, "supersede", new["agent"], reason or "replaced by " + new["id"])
    if mirror:
        write_mirror(store, old["project"])
    return new


def archive(store, fid, reason="", agent="", by=""):
    r = _row(store, fid)
    if r["status"] == "active":
        now = time.time()
        store.exec("UPDATE shared_facts SET status='archived', valid_to=?, updated=?, superseded_by=? WHERE id=?", (now, now, by, r["id"]))
        _log(store, r, "archive", agent, reason)
    return _row(store, fid)


def restore(store, fid):
    r = _row(store, fid)
    now = time.time()
    store.exec("UPDATE shared_facts SET status='active', valid_to=NULL, superseded_by='', strength=MAX(strength,?), last_used=?, updated=? WHERE id=?",
               (START_STRENGTH, now, now, r["id"]))
    _log(store, r, "restore")
    return _row(store, fid)


def edit(store, fid, body, prov=None):
    r = _row(store, fid)
    if body.get("text") and re.sub(r"\s+", " ", str(body["text"])).strip() != r["text"]:
        r = supersede(store, fid, body["text"], prov or {"agent": "app", "source": "app"}, "edited", body.get("topic"), body.get("kind"), mirror=False)
    sets, args = [], []
    for k in ("topic", "kind"):
        if isinstance(body.get(k), str):
            sets.append(k + "=?"); args.append(body[k][:40])
    if isinstance(body.get("pinned"), bool):
        sets.append("pinned=?"); args.append(int(body["pinned"]))
        sets.append("strength=?"); args.append(effective(r))      # freeze / resume decay from the current value
        sets.append("last_used=?"); args.append(time.time())
    if sets:
        store.exec("UPDATE shared_facts SET %s, updated=? WHERE id=?" % ", ".join(sets), (*args, time.time(), r["id"]))
        _log(store, r, "edit", "app", ",".join(s.split("=")[0] for s in sets))
    return _row(store, r["id"])


def enforce_cap(store, key, cap=PROJECT_CAP):
    """Size cap per project: the weakest unpinned facts are archived."""
    rows = _active(store, key)
    over = len(rows) - cap
    if over <= 0:
        return 0
    weakest = sorted((r for r in rows if not r["pinned"]), key=effective)[:over]
    for r in weakest:
        archive(store, r["id"], "project memory is full: weakest fact archived", "joebro")
    return len(weakest)


def _scopes(key, include_global=True):
    """Which stores a read on `key` may touch. `user` is only ever read as itself."""
    return [key] + (["global"] if include_global and key not in RESERVED else [])


def search(store, key, query, limit=RECALL_LIMIT, include_archived=False, include_global=True, do_reinforce=False, keys=None):
    _tables(store)
    keys = keys or _scopes(key, include_global)
    statuses = ("active", "archived", "superseded") if include_archived else ("active",)
    rows = store.rows("SELECT * FROM shared_facts WHERE project IN (%s) AND status IN (%s)" % (
        ",".join("?" * len(keys)), ",".join("?" * len(statuses))), (*keys, *statuses))
    qw = _words(query)
    now, scored = time.time(), []
    for r in rows:
        fw = _words(r["text"] + " " + r["topic"])
        hit = sum(1.0 if w in fw else (0.5 if any(w in f or f in w for f in fw if len(f) > 3 and len(w) > 3) else 0.0) for w in qw)
        if qw and hit:
            scored.append((hit / len(qw) * (0.6 + 0.4 * effective(r, now)) * (1.0 if r["status"] == "active" else 0.6), r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, r in scored[:max(1, min(int(limit or RECALL_LIMIT), 20))]:
        if do_reinforce and r["status"] == "active":
            reinforce(store, r)
        out.append(dict(fact_out(store, r, now), score=round(score, 3)))
    return out


def browse(store, key=None, status="active", topic="", kind="", limit=200):
    _tables(store)
    sql, args = "SELECT * FROM shared_facts WHERE 1=1", []
    for col, val in (("project", key), ("topic", topic), ("kind", kind), ("status", status if status != "all" else "")):
        if val:
            sql += " AND %s=?" % col; args.append(val)
    now = time.time()
    rows = sorted(store.rows(sql, args), key=lambda r: (-r["pinned"], -effective(r, now)))
    return [fact_out(store, r, now) for r in rows[:max(1, min(int(limit or 200), 500))]]


def brief(store, key, budget=BRIEF_TOKENS):
    """The session-start brief: strongest facts for this project (plus a little
    global), hard-capped at `budget` tokens. Reading it does not reinforce.
    Returns (markdown, facts, tokens). A project brief never includes `user`."""
    _tables(store)
    budget = max(60, min(int(budget or BRIEF_TOKENS), BRIEF_TOKENS))
    info, now = project_info(store, key), time.time()
    head = ("Shared memory (JoeBro) for %s: top facts. memory_recall finds more; memory_remember saves durable facts "
            "(decisions, conventions, gotchas, how to run/test)." % info["name"])
    if key == "user":
        head = "What you know about the user (JoeBro memory). Use recall for more."
    lines, used, facts = [head], tokens(head), []
    rank = lambda rows: sorted(rows, key=lambda r: (-r["pinned"], -effective(r, now)))
    pool = rank(_active(store, key))
    if key not in RESERVED:
        pool += rank(_active(store, "global"))[:5]
    for r in pool:
        if effective(r, now) < ARCHIVE_BELOW and not r["pinned"]:
            continue
        text = r["text"] if len(r["text"]) <= 200 else r["text"][:199] + "…"
        line = "- " + (("[%s] " % r["topic"]) if r["topic"] else "") + text
        if used + tokens(line) + 1 > budget:
            continue
        lines.append(line); used += tokens(line) + 1; facts.append(fact_out(store, r, now))
    text = "\n".join(lines)
    return text, facts, tokens(text)


# ---------------------------------------------------------------------- consolidation (nightly, no model)
def consolidate(store, key=None, dry_run=False):
    """Merge weak, similar notes into the strongest of the group, archive what has
    decayed away, enforce the size cap. Archives; never deletes."""
    _tables(store)
    out = {"merged": 0, "archived": 0, "capped": 0, "groups": []}
    keys = [key] if key else [r["project"] for r in store.rows("SELECT DISTINCT project FROM shared_facts WHERE status='active'")]
    now = time.time()
    for k in keys:
        weak = sorted((r for r in _active(store, k) if not r["pinned"] and effective(r, now) < WEAK), key=lambda r: -effective(r, now))
        used = set()
        for r in weak:
            if r["id"] in used:
                continue
            group = [o for o in weak if o["id"] != r["id"] and o["id"] not in used and o["topic"] == r["topic"]
                     and similarity(r["text"], o["text"]) >= MERGE_SIM]
            if not group:
                continue
            used.update([r["id"]] + [o["id"] for o in group])
            out["groups"].append({"kept": r["id"], "merged": [o["id"] for o in group], "project": k})
            out["merged"] += len(group)
            if dry_run:
                continue
            strength = min(1.0, effective(r, now) + sum(effective(o, now) for o in group))
            merged = json.loads(r["merged_from"] or "[]") + [o["id"] for o in group]
            store.exec("UPDATE shared_facts SET strength=?, last_used=?, merged_from=?, updated=? WHERE id=?",
                       (strength, now, json.dumps(merged), now, r["id"]))
            for o in group:
                archive(store, o["id"], "merged into " + r["id"], "joebro", by=r["id"])
            _log(store, r, "consolidate", "joebro", "merged %d similar notes" % len(group))
        for r in weak:
            if r["id"] not in used and effective(r, now) < ARCHIVE_BELOW:
                out["archived"] += 1
                if not dry_run:
                    archive(store, r["id"], "faded: not recalled", "joebro")
        if not dry_run:
            out["capped"] += enforce_cap(store, k)
            write_mirror(store, k)
    return out


def nightly(store):
    """Called from the coding loop: consolidate once a day."""
    _tables(store)
    row = store.one("SELECT value FROM prefs WHERE key='shared_memory_consolidated_at'")
    try:
        last = float(json.loads(row["value"])) if row else 0.0
    except (ValueError, TypeError):
        last = 0.0
    if time.time() - last < 20 * 3600 or time.localtime().tm_hour not in (2, 3, 4, 5):
        return None
    store.exec("INSERT OR REPLACE INTO prefs(key,value) VALUES('shared_memory_consolidated_at',?)", (json.dumps(time.time()),))
    return consolidate(store)


# ---------------------------------------------------------------------- import (replace the agents' own memory files)
def split_facts(text):
    """(topic, fact) pairs from a CLAUDE.md / AGENTS.md / memory file. Pure text rules."""
    text = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), "", text or "", flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)        # front matter
    out, topic, para, code, in_code = [], "", [], [], False

    def flush():
        if para:
            chunk = " ".join(para).strip()
            del para[:]
            pieces = [chunk] if len(chunk) <= MAX_FACT else re.split(r"(?<=[.!?])\s+", chunk)
            for piece in pieces:
                piece = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", piece).strip()
                if len(piece) >= MIN_FACT:
                    out.append((topic, piece[:MAX_FACT]))

    for line in text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            if in_code and code and len(code) <= 3:
                cmd = "; ".join(c.strip() for c in code)
                lead = " ".join(para).strip().rstrip(":") or topic.replace("-", " ") or "Command"
                del para[:]
                out.append((topic, ("%s: `%s`" % (lead, cmd))[:MAX_FACT]))
            else:
                flush()
            in_code, code = not in_code, []
            continue
        if in_code:
            if line.strip():
                code.append(line)
            continue
        m = re.match(r"\s{0,3}#{1,6}\s+(.*)", line)
        if m:
            flush()
            topic = re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-")[:40]
        elif not line.strip():
            flush()
        elif re.match(r"\s*(?:[-*+]|\d+[.)])\s+", line):
            flush(); para.append(line.strip())
        else:
            para.append(line.strip())
    flush()
    return out


def slim_pointer(name=""):
    """What CLAUDE.md / AGENTS.md shrink to once memory is the single source of truth."""
    return ("%s\n# Project memory\n"
            "Kept in JoeBro Shared Memory, shared by every coding agent; a short brief is added at session start. "
            "Look things up with `memory_recall`; save durable facts with `memory_remember`. "
            "Readable copy: `.joebro/memory/MEMORY.md`.\n%s\n" % (MARK_START, MARK_END))


def import_files(store, key, files, prov=None):
    """Import agent memory files ({path, text, agent}) into one deduplicated store."""
    per_agent = {}
    stats = {"files": 0, "tokens_before": 0, "candidates": 0, "added": 0, "deduped": 0, "superseded": 0, "skipped": 0}
    for f in files or []:
        text = str(f.get("text") or "")
        if not text.strip():
            continue
        stats["files"] += 1
        stats["tokens_before"] += tokens(text)
        per_agent[str(f.get("agent") or "agent")] = per_agent.get(str(f.get("agent") or "agent"), 0) + tokens(text)
        p = dict(prov or {}, agent=str(f.get("agent") or (prov or {}).get("agent") or "import"), source="import",
                 session=os.path.basename(str(f.get("path") or ""))[:100])
        for topic, fact in split_facts(text):
            stats["candidates"] += 1
            try:
                _, outcome = remember(store, key, fact, topic, "fact", prov=p, mirror=False)
                stats[outcome] += 1
            except ValueError:
                stats["skipped"] += 1
    write_mirror(store, key)
    text, _, used = brief(store, key)
    pointer = slim_pointer()
    stats.update(brief_tokens=used, pointer_tokens=tokens(pointer),
                 tokens_after=used + tokens(pointer), slim=pointer)
    # What each agent loaded per session from its own files, against brief + pointer afterwards.
    stats["per_agent_before"] = per_agent
    stats["tokens_saved"] = {a: n - stats["tokens_after"] for a, n in per_agent.items()}
    return stats


# ---------------------------------------------------------------------- Markdown mirror
def mirror_files(store, key):
    info, now = project_info(store, key), time.time()
    rows = store.rows("SELECT * FROM shared_facts WHERE project=? ORDER BY topic, created", (key,))

    def line(r):
        who = "/".join(x for x in (r["agent"], r["model"]) if x) or r["source"]
        return "- %s  <!-- %s · %.2f · %s · %s -->" % (r["text"], r["id"], effective(r, now), who, _iso(r["created"])[:10])
    live = ["# Memory: %s" % info["name"], "",
            "Generated by JoeBro Shared Memory. Edit facts in JoeBro or through the memory tools; this file is rewritten.", ""]
    topic = None
    for r in sorted((r for r in rows if r["status"] == "active"), key=lambda r: (r["topic"], -r["pinned"], -effective(r, now))):
        if r["topic"] != topic:
            topic = r["topic"]
            live += ["", "## " + (topic or "general"), ""]
        live.append(line(r) + (" 📌" if r["pinned"] else ""))
    old = ["# Archive: %s" % info["name"], "", "Superseded and archived facts. Nothing is deleted.", ""]
    for r in rows:
        if r["status"] != "active":
            old.append(line(r) + " (%s%s)" % (r["status"], (" by " + r["superseded_by"]) if r["superseded_by"] else ""))
    return {"MEMORY.md": "\n".join(live) + "\n", "ARCHIVE.md": "\n".join(old) + "\n"}


def write_mirror(store, key):
    try:
        files = mirror_files(store, key)
        slug = re.sub(r"[^a-z0-9._-]+", "-", key.lower()).strip("-")[-80:] or "global"
        dirs = [Path(data_dir(store)) / "shared-memory" / slug]
        if key not in RESERVED:          # personal and global facts never go into a repo
            pref = store.one("SELECT value FROM prefs WHERE key='shared_memory_repo_mirror'")
            on = bool(pref) and json.loads(pref["value"]) == "on"
            for p in project_info(store, key)["paths"]:
                target = Path(p) / ".joebro" / "memory"
                if os.path.isdir(p) and (on or target.is_dir()):
                    dirs.append(target)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                (d / name).write_text(text)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------- HTTP
def _fill_provenance(store, body):
    """Model/session from the caller's live coding-agent row when the MCP could not know them."""
    p = {k: body.get(k) for k in ("agent", "model", "session", "host", "source")}
    if p.get("agent") and p.get("host") and not (p.get("model") and p.get("session")):
        try:
            r = store.one("SELECT session_id, model FROM coding_agents WHERE host=? AND source=? AND status<>'ended' AND (?='' OR cwd=?) "
                          "ORDER BY last_activity DESC LIMIT 1", (p["host"], p["agent"], body.get("path") or "", body.get("path") or ""))
            if r:
                p["model"], p["session"] = p.get("model") or r["model"], p.get("session") or r["session_id"]
        except Exception:
            pass
    return p


def caller_of(h):
    """Who is asking, decided server-side from how the request arrived, never from a flag in the request.
    "app": the phone listener (token-authenticated), a request carrying the app token (env JOEBRO_APP_TOKEN, header
    X-JoeBro-App), or, when no app token is configured, a native JoeBro client (URLSession user agent).
    Everything else (the joebro-agents MCP server, plugins, node runner, curl, scripts) is "agent": project + global only.
    The personal agent's own tool calls are in-process and never come through here."""
    import hmac
    server = getattr(h, "server", None)
    if getattr(server, "is_lan", False):
        return "app"
    want = os.environ.get("JOEBRO_APP_TOKEN", "")
    if want:
        got = (getattr(h, "headers", None) or {}).get("X-JoeBro-App") or ""
        return "app" if got and hmac.compare_digest(got.encode(), want.encode()) else "agent"
    ua = ((getattr(h, "headers", None) or {}).get("User-Agent") or "")
    return "app" if ("CFNetwork" in ua or ua.startswith("JoeBro")) else "agent"


def _dispatch(store, method, rest, q, b, caller="app"):
    if caller != "app":
        return _dispatch_agent(store, method, rest, q, b)
    return _dispatch_inner(store, method, rest, q, b)


def _dispatch_agent(store, method, rest, q, b):
    """A coding agent / MCP / node: the `user` scope does not exist for it."""
    keyed = lambda src: str(src.get("project") or "").strip() == "user"
    head = rest.strip("/").split("/")[0] if rest.strip("/") else ""
    if keyed(q) or keyed(b):
        raise LookupError("not found")
    if head and head not in ("projects", "brief", "mirror", "sessions", "review", "recall", "consolidate", "import"):
        r = store.one("SELECT project FROM shared_facts WHERE id=?", (head,))
        if r and r["project"] == "user":
            raise LookupError("not found")
    if method == "POST" and head == "recall":
        b = dict(b, include_user=False)
    out = _dispatch_inner(store, method, rest, q, b, hide_user=True)
    return out


def _dispatch_inner(store, method, rest, q, b, hide_user=False):
    key = lambda src: resolve_project(store, src.get("project"), src.get("path"), src.get("remote"), src.get("project_name") or "")
    parts = [p for p in rest.split("/") if p]
    head = parts[0] if parts else ""
    if method == "GET":
        if head == "projects":
            return {"ok": True, "projects": [p for p in list_projects(store) if not (hide_user and p["key"] == "user")]}
        if head == "brief":
            k = key(q)
            text, facts, used = brief(store, k, q.get("budget") or BRIEF_TOKENS)
            return {"ok": True, "project": project_info(store, k), "brief": text, "facts": facts, "tokens": used, "budget": BRIEF_TOKENS}
        if head == "mirror":
            k = key(q)
            return {"ok": True, "project": project_info(store, k), "files": mirror_files(store, k)}
        if head == "sessions":
            k = key(q) if (q.get("project") or q.get("path") or q.get("remote")) else None
            return {"ok": True, "sessions": recent_sessions(store, k, q.get("limit") or 10)}
        if head == "review":
            k = key(q) if (q.get("project") or q.get("path")) else None
            return {"ok": True, "facts": [f for f in browse(store, k) if not f["pinned"] and f["strength"] < 0.2 and not (hide_user and f["scope"] == "user")]}
        if head:
            r = _row(store, head)
            chain, cur = [r], r
            while cur["supersedes"] and len(chain) < 30:
                cur = store.one("SELECT * FROM shared_facts WHERE id=?", (cur["supersedes"],))
                if not cur:
                    break
                chain.insert(0, cur)
            log = store.rows("SELECT at, action, agent, detail FROM shared_log WHERE fact_id=? ORDER BY id", (r["id"],))
            return {"ok": True, "fact": fact_out(store, r), "history": [fact_out(store, c) for c in chain],
                    "log": [dict(l, at=_iso(l["at"])) for l in log]}
        k = key(q) if (q.get("project") or q.get("path") or q.get("remote")) else None
        if q.get("q"):
            keys = [k] if k else [p["key"] for p in list_projects(store) if not (hide_user and p["key"] == "user")]
            facts = search(store, k, q["q"], q.get("limit") or 20, (q.get("status") or "active") != "active", keys=keys)
        else:
            facts = browse(store, k, q.get("status") or "active", q.get("topic") or "", q.get("kind") or "", q.get("limit") or 200)
            if hide_user:
                facts = [f for f in facts if f["scope"] != "user"]
        return {"ok": True, "project": project_info(store, k) if k else None, "facts": facts, "count": len(facts)}
    if method == "DELETE" and head:
        return {"ok": True, "fact": fact_out(store, archive(store, head, "archived in the app", "app"))}
    if method == "PUT" and head:
        return {"ok": True, "fact": fact_out(store, edit(store, head, b, _fill_provenance(store, b) if b.get("agent") else None))}
    if method != "POST":
        raise LookupError("not found")
    if not head:
        row, outcome = remember(store, key(b), b.get("text"), b.get("topic") or "", b.get("kind") or "fact", b.get("pinned") is True,
                                _fill_provenance(store, b))
        return {"ok": True, "fact": fact_out(store, row), "outcome": outcome, "deduped": outcome == "deduped"}
    if head == "recall":
        k = key(b)
        if b.get("all_projects"):       # the personal agent, on request: every scope
            keys = [p["key"] for p in list_projects(store) if not (hide_user and p["key"] == "user")]
        else:
            keys = _scopes(k, b.get("include_global", True))
        facts = search(store, k, b.get("query") or "", b.get("limit") or RECALL_LIMIT, bool(b.get("include_archived")),
                       do_reinforce=True, keys=keys)
        return {"ok": True, "facts": facts, "count": len(facts)}
    if head == "consolidate":
        k = key(b) if (b.get("project") or b.get("path")) else None
        return dict(consolidate(store, k, bool(b.get("dry_run"))), ok=True)
    if head == "import":
        k = key(b)
        if k == "user":
            raise ValueError("agent memory files import into a project or global, not the user scope")
        return dict(import_files(store, k, b.get("files"), _fill_provenance(store, b)), ok=True, project=project_info(store, k))
    action = parts[1] if len(parts) > 1 else ""
    if action == "supersede":
        old = _row(store, head)
        new = supersede(store, head, b.get("text"), _fill_provenance(store, b), b.get("reason") or "", b.get("topic"), b.get("kind"))
        return {"ok": True, "fact": fact_out(store, new), "superseded": fact_out(store, _row(store, old["id"]))}
    if action == "pin":
        return {"ok": True, "fact": fact_out(store, edit(store, head, {"pinned": b.get("pinned", True) is not False}))}
    if action == "archive":
        r = archive(store, head, b.get("reason") or "", b.get("agent") or "app")
        write_mirror(store, r["project"])
        return {"ok": True, "fact": fact_out(store, r)}
    if action == "restore":
        r = restore(store, head)
        write_mirror(store, r["project"])
        return {"ok": True, "fact": fact_out(store, r)}
    raise LookupError("not found")


def route(h, method, path, q):
    """Serve /api/shared-memory/*. True when the request was handled."""
    if path != "/api/shared-memory" and not path.startswith("/api/shared-memory/"):
        return False
    try:
        body = (h._json_body() or {}) if method in ("POST", "PUT") else {}
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        h.json(_dispatch(h.store, method, path[len("/api/shared-memory"):], q, body, caller_of(h)))
    except LookupError as exc:
        h.json({"ok": False, "detail": str(exc)}, 404)
    except (ValueError, TypeError) as exc:
        h.json({"ok": False, "detail": str(exc) or "invalid request"}, 400)
    return True


# ---------------------------------------------------------------------- recent coding sessions
SESSIONS_KEEP = 10      # per project. Records, not facts: never in the brief, never decayed.


def _session_table(store):
    store.exec("""CREATE TABLE IF NOT EXISTS shared_sessions(id TEXT PRIMARY KEY, project TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', resume_id TEXT NOT NULL DEFAULT '', at REAL NOT NULL)""")


def record_session(store, agent_row, summary="", branch=""):
    """One compact record per coding session, written when it goes idle or ends."""
    cwd = agent_row.get("cwd") or ""
    if not cwd:
        return None
    _session_table(store)
    summary = re.sub(r"\s+", " ", str(summary or "")).strip()[:240]
    old = store.one("SELECT * FROM shared_sessions WHERE id=?", (agent_row["id"],))
    if not summary and not old:
        return None
    if _SECRET.search(summary):
        summary = ""
    key = resolve_project(store, path=cwd)
    if not branch and os.path.isdir(cwd):
        branch = _git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    store.exec("""INSERT INTO shared_sessions(id,project,repo,agent,model,host,branch,summary,resume_id,at) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET model=excluded.model, at=excluded.at,
        branch=CASE WHEN excluded.branch<>'' THEN excluded.branch ELSE branch END,
        summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE summary END""",
               (agent_row["id"], key, cwd, agent_row.get("source") or "", agent_row.get("model") or "", agent_row.get("host") or "",
                str(branch or "")[:120], summary, agent_row.get("session_id") or "", time.time()))
    store.exec("DELETE FROM shared_sessions WHERE project=? AND id NOT IN (SELECT id FROM shared_sessions WHERE project=? ORDER BY at DESC LIMIT ?)",
               (key, key, SESSIONS_KEEP))
    return key


def recent_sessions(store, key=None, limit=10):
    _session_table(store)
    _tables(store)
    rows = store.rows("SELECT * FROM shared_sessions %s ORDER BY at DESC LIMIT ?" % ("WHERE project=?" if key else ""),
                      ((key,) if key else ()) + (max(1, min(int(limit or 10), 50)),))
    names = {p["key"]: p["name"] for p in store.rows("SELECT key, name FROM shared_projects")}
    return [{"id": r["id"], "project": r["project"], "project_name": names.get(r["project"]) or r["project"], "repo": r["repo"],
             "agent": r["agent"], "model": r["model"], "host": r["host"], "branch": r["branch"], "summary": r["summary"],
             "resume_id": r["resume_id"], "at": _iso(r["at"])} for r in rows]
