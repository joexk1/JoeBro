"""One registry for everything that runs on its own.

Three things in this backend do long work without a human watching: a deep
research run, a spawned background agent, and a scheduled task. They each kept
their own half-record (research had progress but no session, tasks had only a
`last_run` timestamp, spawned agents had a result nobody could read in full), so
no client could show "what is my assistant doing right now" without three
different queries and a guess.

This makes them one kind of thing. Every run opens a row in `agents`, reports
progress while it works, and closes with a status. The `agents` table IS the
registry: `kind` says which sort of run it is, `ref_id` points back at the
owning row (a research id, a task id) so the detail view can fetch the long
output from wherever it already lives instead of copying it.

Statuses are `queued`, `running`, `done`, `failed`, `cancelled`. Older rows used
`error`/`stopped`; those are normalised on read and by the boot backfill.

ponytail: no worker pool or job queue. These are threads writing rows in one
process, which is what the rest of this backend already is.
"""
from jb_core import *  # noqa: F401,F403

# Order is the order both Agents tabs render their subdivisions in.
KINDS = ("chat", "research", "task", "briefing", "subagent")   # "briefing" is reserved; nothing opens one yet
LIVE = ("queued", "running")

# Cancellation is cooperative: the flag is set here, the worker notices. Both
# sets are process-local — a restart clears them, which is correct, because a
# restart also kills the workers they refer to.
_CANCEL_RUNS = set()        # agent-run ids
_CANCEL_SESSIONS = set()    # session ids (the chat Stop button)
_CANCEL_GUARD = threading.Lock()

_LEGACY_STATUS = {"error": "failed", "stopped": "cancelled", "completed": "done"}


def request_cancel(run_id="", session_id=""):
    with _CANCEL_GUARD:
        if run_id:
            _CANCEL_RUNS.add(run_id)
        if session_id:
            _CANCEL_SESSIONS.add(session_id)


def clear_cancel(run_id="", session_id=""):
    with _CANCEL_GUARD:
        _CANCEL_RUNS.discard(run_id)
        _CANCEL_SESSIONS.discard(session_id)


def is_cancelled(run_id="", session_id=""):
    with _CANCEL_GUARD:
        return (run_id and run_id in _CANCEL_RUNS) or (session_id and session_id in _CANCEL_SESSIONS)


class AgentsMixin:
    # Set by a worker on its own Handler for the duration of a run, so the agent
    # loop in jb_chat can ask "have I been cancelled?" without knowing anything
    # about the registry.
    _agent_run_id = ""

    # ---- writing a run's life ------------------------------------------------

    def agent_open(self, kind, title, session_id="", model="", endpoint_id="",
                   ref_id="", prompt="", progress="", notify=1, status="queued"):
        """Register a run and return its id. Called by the worker that is about
        to do the work, before it starts."""
        if kind not in KINDS:
            raise ValueError(f"unknown agent kind '{kind}' (expected one of {', '.join(KINDS)})")
        aid = {"research": "res_", "task": "tsk_", "chat": "agt_",
               "briefing": "brf_", "subagent": "sub_"}[kind] + os.urandom(6).hex()
        self.store.exec(
            """insert into agents(id,kind,ref_id,title,prompt,status,session_id,model,endpoint_id,
                                  progress,notify,created_at)
               values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, kind, ref_id or "", (title or kind).strip()[:200], prompt or "", status,
             session_id or "", model or "", endpoint_id or "", progress or "", int(notify), now_iso()))
        return aid

    def agent_upsert(self, aid, kind, title, session_id="", model="", endpoint_id="",
                     ref_id="", progress="", notify=0, keep_result=True):
        """Create or RESET a run with a caller-chosen id, for a singleton.

        A chat's per-turn row (agt_t<sid>) is one: exactly one row per session,
        rewritten in place every turn, so there is no history to accumulate or
        prune. The old `result` is kept while the new run is in flight."""
        if kind not in KINDS:
            raise ValueError(f"unknown agent kind '{kind}'")
        existing = self.store.one("select result from agents where id=?", (aid,))
        if existing:
            self.store.exec(
                """update agents set kind=?, title=?, status='queued', session_id=?, model=?,
                       endpoint_id=?, ref_id=?, progress=?, notify=?, error='', started_at='',
                       finished_at='', cancel_requested=0, created_at=?, result=?
                   where id=?""",
                (kind, title, session_id, model, endpoint_id, ref_id, progress, int(notify),
                 now_iso(), (existing.get("result") or "") if keep_result else "", aid))
        else:
            self.store.exec(
                """insert into agents(id,kind,ref_id,title,prompt,status,session_id,model,
                                      endpoint_id,progress,notify,created_at)
                   values(?,?,?,?,'','queued',?,?,?,?,?,?)""",
                (aid, kind, ref_id, title, session_id, model, endpoint_id, progress,
                 int(notify), now_iso()))
        clear_cancel(run_id=aid)
        return aid

    # ---- interactive chat turns ---------------------------------------------
    # A chat turn typed into the Mac or phone app never went through agent_open,
    # so the registry (what /api/agents serves) knew nothing about it: the other
    # device could only see the bare session, never "running". Each agent-mode
    # turn now keeps ONE row per session (id agt_t<sid>), reset per turn. The
    # phone agent's own chat is left out: it is the conversation, not a job.

    def chat_turn_open(self, sid, f, title=""):
        mode = (f.get("mode") or "").lower()
        if not mode and sid:       # no explicit mode on the request: the session's own
            mode = ((self.store.one("select mode from sessions where id=?", (sid,)) or {}).get("mode") or "").lower()
        if mode != "agent" or not sid or sid == PHONE_SESSION_ID or sid.startswith(("bot_tg_", "task_session_", "pulse_")):
            return ""
        try:
            other = self.store.one(
                "select id from agents where session_id=? and status in ('queued','running') and id not like 'agt_t%'",
                (sid,))
            if other:      # a registry run (spawned agent / task) already owns it
                return ""
            sess = self.store.one("select name from sessions where id=?", (sid,)) or {}
            aid = "agt_t" + sid
            self.agent_upsert(aid, "chat", (sess.get("name") or title or "Chat")[:200], session_id=sid,
                              model=f.get("model") or "", endpoint_id=f.get("endpoint_id") or "",
                              progress="Starting")
            self.agent_started(aid)
            return aid
        except Exception:
            return ""

    def chat_turn_close(self, aid, status="done", error=""):
        if aid:
            try:
                self.agent_close(aid, status, error=error)
            except Exception:
                pass

    def agent_started(self, aid):
        self.store.exec("update agents set status='running', started_at=? where id=?", (now_iso(), aid))

    def agent_progress(self, aid, text):
        """Last-activity line. Deliberately one short string, not an event log —
        a client polling a list wants one readable line per agent."""
        if aid:
            self.store.exec("update agents set progress=? where id=?", (str(text or "")[:400], aid))

    def agent_close(self, aid, status, result="", error=""):
        if status in _LEGACY_STATUS:
            status = _LEGACY_STATUS[status]
        self.store.exec(
            "update agents set status=?, result=?, error=?, finished_at=?, progress=? where id=?",
            (status, (result or "").strip(), (error or "")[:1000], now_iso(),
             {"done": "Finished", "failed": "Failed", "cancelled": "Cancelled"}.get(status, ""), aid))
        clear_cancel(run_id=aid)
        try:
            from jb_push import end_if_idle
            from jb_chat import _ACTIVE_TURNS
            end_if_idle(self.store, lambda: bool(_ACTIVE_TURNS))
        except Exception:
            pass

    # ---- cancellation -------------------------------------------------------

    def agent_run_cancelled(self, aid=None, session_id=None):
        """True when this run has been asked to stop. The agent loop calls this
        between rounds; research calls it between phases."""
        aid = aid if aid is not None else self._agent_run_id
        if not aid and not session_id:
            return False
        if is_cancelled(run_id=aid or "", session_id=session_id or ""):
            return True
        if aid:      # survives across the worker's own Handler instances
            row = self.store.one("select cancel_requested from agents where id=?", (aid,))
            if row and row.get("cancel_requested"):
                request_cancel(run_id=aid)     # cache it so later checks are cheap
                return True
        return False

    def agent_clear_cancel(self, session_id="", run_id=""):
        """Drop a stale stop request. A new turn is a fresh intent — without this
        one Stop press would silently kill every later turn in that chat."""
        clear_cancel(run_id=run_id, session_id=session_id)

    def agent_cancel(self, aid):
        """Ask a run to stop, and say honestly what that will actually do.

        Research checks a flag between phases; chat and task runs check between
        model rounds. Neither aborts an HTTP call already in flight, so a stop
        lands within one step rather than instantly. A queued run never starts."""
        row = self.store.one("select * from agents where id=?", (aid,))
        if not row:
            return {"ok": False, "detail": "no such agent"}, 404
        status = self.agent_status(row)
        if status not in LIVE:
            return {"ok": True, "id": aid, "status": status, "cancelled": False,
                    "detail": f"already {status} — nothing to stop"}, 200
        self.store.exec("update agents set cancel_requested=1 where id=?", (aid,))
        request_cancel(run_id=aid, session_id=row.get("session_id") or "")
        kind = row.get("kind") or "chat"
        if kind == "research" and row.get("ref_id"):
            # Research owns a real between-phase cancel already; use it so the
            # research row itself stops claiming to be running.
            try:
                self.cancel_research(row["ref_id"])
            except Exception:
                pass
        for ch in self.store.rows("select id from agents where parent_id=? and status in ('queued','running')", (aid,)):
            self.agent_cancel(ch["id"])        # cancelling a parent stops its subagents
        if status == "queued":
            self.agent_close(aid, "cancelled", error="cancelled before it started")
            return {"ok": True, "id": aid, "status": "cancelled", "cancelled": True,
                    "detail": "was queued, never started"}, 200
        note = {"research": "stops at the next phase boundary",
                "task": "stops after the current model round",
                "chat": "stops after the current model round",
                "subagent": "stops after the current model round"}.get(kind, "stops soon")
        return {"ok": True, "id": aid, "status": "running", "cancelled": True,
                "detail": f"cancel requested — {note}; an in-flight model call finishes first"}, 200

    def delete_session_cascade(self, sid):
        """Delete a chat session AND its agent records (idempotent).

        Agent rows with session_id=sid go, plus their subagents (parent_id chain)
        and those subagents' own sessions/messages: a subagent has no meaning
        without the chat that spawned it. Live runs are cancelled first via
        agent_cancel so no worker loses its row mid-run. Rows with an empty
        session_id are never touched. Returns the agent ids removed."""
        ids, frontier = [], [r["id"] for r in self.store.rows("select id from agents where session_id=?", (sid,))]
        while frontier:
            ids += [i for i in frontier if i not in ids]
            marks = ",".join("?" * len(frontier))
            frontier = [r["id"] for r in self.store.rows(
                f"select id from agents where parent_id in ({marks})", tuple(frontier)) if r["id"] not in ids]
        sessions = {sid}
        for aid in ids:
            row = self.store.one("select session_id from agents where id=?", (aid,))
            if row and row.get("session_id"):
                sessions.add(row["session_id"])
            try:
                self.agent_cancel(aid)      # no-op unless queued/running
            except Exception:
                pass
        for s in sessions:
            self.store.exec("delete from sessions where id=?", (s,))
            self.store.exec("delete from messages where session_id=?", (s,))
            self.store.exec("delete from prefs where key=?", ("compact_" + s,))
            self.store.exec("delete from plans where session_id=?", (s,))
        for aid in ids:
            self.store.exec("delete from agents where id=?", (aid,))
            clear_cancel(run_id=aid)
        clear_cancel(session_id=sid)
        return ids

    def reap_subagent(self, aid):
        """Remove ONE finished subagent: its registry row, its subagent_session_*
        session and messages. Reuses delete_session_cascade so the cleanup rules
        live in one place. Refuses (returns False) unless the row is a subagent in
        a TERMINAL state - a live subagent is never deleted."""
        row = self.store.one("select * from agents where id=?", (aid,))
        if not row or row.get("kind") != "subagent" or self.agent_status(row) in LIVE:
            return False
        self.delete_session_cascade(row.get("session_id") or ("subagent_session_" + aid))
        self.store.exec("delete from agents where id=? and status not in ('queued','running')", (aid,))
        return True

    def reap_subagents_of(self, parent_id, cancel_live=True):
        """The parent is finished/dead: remove its terminal subagents and (cancel_live) ask
        any still-live ones to stop; they reap themselves when they close."""
        n = 0
        for r in self.store.rows("select id from agents where kind='subagent' and parent_id=?", (parent_id,)):
            if self.reap_subagent(r["id"]):
                n += 1
            elif cancel_live:
                try:
                    self.agent_cancel(r["id"])
                except Exception:
                    pass
        return n

    def cleanup_orphan_agents(self):
        """One-off: drop agent rows whose session_id names a session that no
        longer exists. Empty session_id is left alone."""
        rows = self.store.rows(
            "select id from agents where coalesce(session_id,'')!='' "
            "and session_id not in (select id from sessions)")
        for r in rows:
            self.store.exec("delete from agents where id=?", (r["id"],))
        return len(rows)

    def cancel_session_run(self, session_id):
        """The chat Stop button. Cancels whatever registry run owns this session,
        and flags the session so an interactive (non-registry) turn stops too."""
        request_cancel(session_id=session_id)
        row = self.store.one(
            "select * from agents where session_id=? and status in ('queued','running') "
            "order by datetime(created_at) desc limit 1", (session_id,))
        if row:
            return self.agent_cancel(row["id"])
        return {"ok": True, "session": session_id, "cancelled": True,
                "detail": "stop requested — the turn stops after the current model round"}, 200

    # ---- reading ------------------------------------------------------------

    @staticmethod
    def agent_status(r):
        s = (r.get("status") or "queued").strip().lower()
        return _LEGACY_STATUS.get(s, s)

    def agent_record(self, r, full=False):
        """The wire shape. `full` adds the complete result text — for research
        that is the report, which lives in its own table and is not duplicated
        here."""
        status = self.agent_status(r)
        out = {
            "id": r.get("id"),
            "kind": r.get("kind") or "chat",
            "title": r.get("title") or "",
            "status": status,
            "live": status in LIVE,
            "progress": r.get("progress") or "",
            "session_id": r.get("session_id") or "",
            "model": r.get("model") or "",
            "ref_id": r.get("ref_id") or "",
            "parent_id": r.get("parent_id") or "",
            "created_at": r.get("created_at") or "",
            "started_at": r.get("started_at") or "",
            "finished_at": r.get("finished_at") or "",
            "error": r.get("error") or "",
            "cancel_requested": bool(r.get("cancel_requested")),
        }
        result = r.get("result") or ""
        if (r.get("kind") == "research") and r.get("ref_id"):
            ref = self.store.one("select * from research where id=?", (r["ref_id"],))
            if ref:
                result = ref.get("content") or result
                research = self.research_json(ref)
                out["source_count"] = research["source_count"]
                if full:
                    out["sources"] = research["sources"]
        out["result_chars"] = len(result)
        out["result"] = result if full else result[:400]
        out["result_truncated"] = (not full) and len(result) > 400
        return out

    def agent_rows(self, kind="", status="", limit=100):
        sql = "select * from agents"
        where, args = ["coalesce(ref_id,'') not like 'ptask\\_%' escape '\\'"], []   # personal tasks stay private
        if kind:
            where.append("kind=?")
            args.append(kind)
        if status == "live":
            where.append("status in ('queued','running')")
        elif status:
            where.append("status=?")
            args.append(status)
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by datetime(created_at) desc limit ?"
        args.append(int(limit))
        return self.store.rows(sql, tuple(args))

    # Most specific kind wins when two rows describe the same session: a task
    # run already surfaces as a chat (its work happens in task_session_*), and it
    # must appear once, under `task`.
    _KIND_RANK = {"briefing": 0, "task": 1, "subagent": 1, "research": 2, "chat": 3}

    def _dedupe(self, rows):
        best, loose = {}, []
        for r in rows:
            sid = (r.get("session_id") or "").strip()
            if not sid:
                loose.append(r)
                continue
            cur = best.get(sid)
            if cur is None or (self._rank(r), self.agent_status(r) not in LIVE) < (self._rank(cur), self.agent_status(cur) not in LIVE):
                best[sid] = r
        out = list(best.values()) + loose
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return out

    def _rank(self, r):
        return self._KIND_RANK.get(r.get("kind") or "chat", 9)

    def _reconcile_live(self, rows):
        """A live row whose underlying work has finished is a lie the Active tab
        would show forever. Research is the one that can drift, because its own
        table is the thing that actually completes."""
        for r in rows:
            if self.agent_status(r) not in LIVE or r.get("kind") != "research" or not r.get("ref_id"):
                continue
            ref = self.store.one("select status from research where id=?", (r["ref_id"],))
            done = (ref or {}).get("status")
            if done in ("completed", "cancelled", "failed"):
                self.agent_close(r["id"], {"completed": "done", "cancelled": "cancelled",
                                           "failed": "failed"}[done])
                r["status"] = {"completed": "done", "cancelled": "cancelled", "failed": "failed"}[done]
        return rows

    def agents_payload(self, kind="", status="", limit=100):
        rows = self._dedupe(self._reconcile_live(self.agent_rows(kind, status, limit)))
        recs = [self.agent_record(r) for r in rows]
        counts = {k: 0 for k in KINDS}
        for r in recs:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        return {"ok": True, "agents": recs, "count": len(recs),
                "active": sum(1 for r in recs if r["live"]),
                "live": sum(1 for r in recs if r["live"]),   # kept: older clients read this
                "by_kind": counts, "kinds": list(KINDS)}

    # ---- boot reconciliation ------------------------------------------------

    def reconcile_agents_on_boot(self):   # noqa: D401  (see docstring below)
        """Make the registry honest after a restart.

        Two jobs. Any run still marked live is a lie — its thread died with the
        old process, so mark it interrupted rather than leaving a permanent
        spinner in the UI. And adopt research rows that predate the registry so
        the list isn't empty on day one."""
        adopted = 0
        for r in self.store.rows("select * from research order by datetime(created_at) desc limit 100"):
            if self.store.one("select id from agents where kind='research' and ref_id=?", (r["id"],)):
                continue
            prog = ""
            try:
                p = json.loads(r.get("progress") or "{}")
                prog = (p.get("phase") or "") + ((": " + p["message"]) if p.get("message") else "")
            except (ValueError, TypeError):
                pass
            status = {"completed": "done", "cancelled": "cancelled", "failed": "failed",
                      "error": "failed", "running": "failed", "queued": "failed"}.get(
                          (r.get("status") or "").lower(), "done")
            aid = self.agent_open("research", r.get("query") or "Research", ref_id=r["id"],
                                  progress=prog[:400], status=status, notify=0)
            # A research row that says "running" but has no worker is stale.
            self.store.exec("update agents set created_at=?, finished_at=? where id=?",
                            (r.get("created_at") or now_iso(),
                             r.get("updated_at") or "" if status != "running" else "", aid))
            adopted += 1
        stale = self.store.rows(
            "select id, kind from agents where status in ('queued','running')")
        for r in stale:
            self.agent_close(r["id"], "failed",
                             error="interrupted — the backend restarted while this was running")
        # Subagents are covered by the sweep above (all kinds). Then drop subagents
        # nobody can consume any more: parent is a dead agent row, or its chat
        # session no longer exists.
        reaped = 0
        for r in self.store.rows("select id, parent_id from agents where kind='subagent'"):
            pid = r.get("parent_id") or ""
            prow = self.store.one("select status from agents where id=?", (pid,)) if pid else None
            gone = bool(pid) and not prow and not self.store.one("select id from sessions where id=?", (pid,))
            dead = bool(prow) and self.agent_status(prow) in ("failed", "cancelled")
            if (gone or dead) and self.reap_subagent(r["id"]):
                reaped += 1
        return {"adopted_research": adopted, "marked_interrupted": len(stale), "reaped_subagents": reaped}
