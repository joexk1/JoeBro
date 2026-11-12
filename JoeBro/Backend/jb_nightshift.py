"""Night Shift: queue coding tasks from the phone or Mac; they run unattended in the
task's time window, each in its own git worktree and branch, with the agents' own
CLIs (Claude Code, Codex, OpenCode: the user's subscriptions, no JoeBro model call).
The morning gets review cards (briefing + actionable push): approve keeps or merges
the branch locally, reject discards the worktree, a follow-up re-queues the task.
Nothing is ever pushed, and the destructive git steps run only on an explicit decision.

The same dispatcher starts or resumes an interactive coding session right now
(kind `session`: tmux by default, the Agent Control terminal when preferred), which
is what the personal agent's `start_coding_session` tool uses."""
import json
import re
import os
import threading
import time
import types
from pathlib import Path

import jb_nightworker as worker
import jb_sharedmem


def data_dir(store=None):
    from jb_core import data_dir as _dd     # late: jb_core imports this module's tool schemas
    return _dd(store)

STATUSES = ("queued", "running", "review", "approved", "rejected", "failed", "cancelled", "launched")
EDITABLE = ("title", "repo", "prompt", "agent", "model", "host", "window_start", "window_end", "run_after",
            "max_minutes", "budget_usd", "test_command", "on_approve")
DEFAULTS = {"enabled": True, "window_start": "23:00", "window_end": "07:00", "on_approve": "keep", "parallel": 1,
            "default_agent": "claude", "max_minutes": 60, "budget_usd": 5.0}
MAX_MINUTES, MAX_BUDGET = 240, 50.0
_RUNNING = {}            # task id -> threading.Event (set = stop the agent)
_LOCK = threading.Lock()

RECENT_SESSIONS_TOOL = {"type": "function", "function": {
    "name": "recent_coding_sessions",
    "description": ("The user's recent coding sessions (Claude Code, Codex, OpenCode) per project: repo, agent, model, computer, "
                    "branch, a one-line summary of where it stopped, and the id to resume it. Use before start_coding_session "
                    "when the user says things like 'carry on with what I was doing yesterday'."),
    "parameters": {"type": "object", "properties": {"project": {"type": "string", "description": "project name, key or repo path; omit for all"}}}}}
START_SESSION_TOOL = {"type": "function", "function": {
    "name": "start_coding_session",
    "description": ("Start a coding agent on a repo right now, or resume the most recent session there, with a prompt. "
                    "It runs in a terminal (tmux) on the computer that has the repo and shows up in the Agents list."),
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string", "description": "project name or key from recent_coding_sessions"},
        "repo": {"type": "string", "description": "or: absolute repo path"},
        "agent": {"type": "string", "enum": ["claude", "codex", "opencode"]},
        "prompt": {"type": "string"},
        "resume": {"type": "boolean", "description": "true = resume the latest session in that project (native resume)"}},
        "required": ["prompt"]}}}


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else ""


def in_window(start, end, now=None):
    """Whether local time `now` is inside HH:MM..HH:MM (wraps midnight). Empty = always."""
    if not start or not end:
        return True
    t = time.localtime(now or time.time())
    cur = "%02d:%02d" % (t.tm_hour, t.tm_min)
    return (start <= cur < end) if start <= end else (cur >= start or cur < end)


def _hhmm(v):
    v = str(v or "").strip()
    if v and not (len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60):
        raise ValueError("times are HH:MM")
    return v


class NightShiftMixin:
    def _night_table(self):
        self.store.exec("""CREATE TABLE IF NOT EXISTS night_tasks(id TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'task',
            title TEXT NOT NULL, repo TEXT NOT NULL, prompt TEXT NOT NULL, agent TEXT NOT NULL DEFAULT 'any', model TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '', window_start TEXT NOT NULL DEFAULT '', window_end TEXT NOT NULL DEFAULT '', run_after TEXT NOT NULL DEFAULT '',
            max_minutes INTEGER NOT NULL DEFAULT 60, budget_usd REAL NOT NULL DEFAULT 0, test_command TEXT NOT NULL DEFAULT '',
            on_approve TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued', run_now INTEGER NOT NULL DEFAULT 0,
            resume_id TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, runner TEXT NOT NULL DEFAULT '', agent_id TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '', base_branch TEXT NOT NULL DEFAULT '', base_commit TEXT NOT NULL DEFAULT '', head_commit TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, started REAL, finished REAL,
            error TEXT NOT NULL DEFAULT '', followups TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '{}', decision TEXT NOT NULL DEFAULT '{}')""")
        if not getattr(self.store, "_night_cols", False):     # node-runner columns (a node reports its diff; decisions park for it)
            for col in ("pending_action TEXT NOT NULL DEFAULT ''", "diff_text TEXT NOT NULL DEFAULT ''", "diff_truncated INTEGER NOT NULL DEFAULT 0"):
                try:
                    self.store.exec("ALTER TABLE night_tasks ADD COLUMN " + col)
                except Exception:
                    pass
            try:
                self.store._night_cols = True
            except Exception:
                pass

    # ------------------------------------------------------------------ config
    def night_config(self):
        cfg = dict(DEFAULTS)
        for k in DEFAULTS:
            v = self.pref("night_shift_" + k)
            if v is not None:
                cfg[k] = v
        return cfg

    def night_set_config(self, body):
        for k in DEFAULTS:
            if k not in body:
                continue
            v = body[k]
            if k in ("window_start", "window_end"):
                v = _hhmm(v)
            elif k == "on_approve" and v not in ("keep", "merge"):
                raise ValueError("on_approve is keep or merge")
            elif k == "default_agent" and v not in worker.COMMANDS:
                raise ValueError("default_agent is claude, codex or opencode")
            elif k == "parallel":
                v = max(1, min(int(v), 4))
            elif k == "max_minutes":
                v = max(1, min(int(v), MAX_MINUTES))
            elif k == "budget_usd":
                v = max(0.0, min(float(v), MAX_BUDGET))
            elif k == "enabled":
                v = bool(v)
            self.set_pref("night_shift_" + k, v)
        return self.night_config()

    def _night_commands(self):
        c = self.pref("night_shift_commands")
        return c if isinstance(c, dict) else {}

    # ------------------------------------------------------------------ tasks
    def night_task(self, tid):
        self._night_table()
        r = self.store.one("SELECT * FROM night_tasks WHERE id=?", (str(tid),))
        if not r:
            raise LookupError("no such Night Shift task")
        return r

    def night_out(self, r):
        out = {k: r[k] for k in ("id", "kind", "title", "position", "repo", "prompt", "agent", "model", "host", "window_start", "window_end",
                                 "run_after", "max_minutes", "budget_usd", "test_command", "status", "attempts", "runner", "agent_id",
                                 "branch", "base_branch", "base_commit", "head_commit", "worktree", "error", "resume_id")}
        out.update(repo_name=os.path.basename(r["repo"].rstrip("/")), on_approve=r["on_approve"] or self.night_config()["on_approve"],
                   run_now=bool(r["run_now"]), created_at=_iso(r["created"]), updated_at=_iso(r["updated"]), started_at=_iso(r["started"]),
                   finished_at=_iso(r["finished"]), followups=json.loads(r["followups"] or "[]"),
                   result=json.loads(r["result"] or "{}") or None, decision=json.loads(r["decision"] or "{}") or None, pending_action=r.get("pending_action") or "")
        try:
            out["project"] = jb_sharedmem.resolve_project(self.store, path=r["repo"])
        except Exception:
            out["project"] = r["repo"]
        return out

    def _night_fields(self, body, cfg, current=None):
        f = {}
        for k in EDITABLE:
            if k in body and body[k] is not None:
                f[k] = body[k]
        if "repo" in f:
            f["repo"] = os.path.abspath(os.path.expanduser(str(f["repo"]).strip()))
        if "prompt" in f:
            f["prompt"] = str(f["prompt"]).strip()[:8000]
        if "agent" in f and f["agent"] not in ("any",) + tuple(worker.COMMANDS):
            raise ValueError("agent is claude, codex, opencode or any")
        for k in ("window_start", "window_end"):
            if k in f:
                f[k] = _hhmm(f[k])
        if "on_approve" in f and f["on_approve"] not in ("", "keep", "merge"):
            raise ValueError("on_approve is keep or merge")
        if "max_minutes" in f:
            f["max_minutes"] = max(1, min(int(f["max_minutes"]), MAX_MINUTES))
        if "budget_usd" in f:
            f["budget_usd"] = max(0.0, min(float(f["budget_usd"]), MAX_BUDGET))
        for k in ("title", "model", "host", "run_after", "test_command"):
            if k in f:
                f[k] = str(f[k]).strip()[:300]
        return f

    def night_add(self, body, kind="task", run_now=False, resume_id=""):
        self._night_table()
        cfg = self.night_config()
        f = self._night_fields(body, cfg)
        if not f.get("repo") or not f.get("prompt"):
            raise ValueError("repo and prompt are required")
        f.setdefault("title", f["prompt"].splitlines()[0][:70])
        f.setdefault("max_minutes", cfg["max_minutes"])
        f.setdefault("budget_usd", cfg["budget_usd"])       # every task has a cap
        tid = "ns_" + os.urandom(5).hex()
        now = time.time()
        pos = (self.store.one("SELECT COALESCE(MAX(position),0)+1 AS p FROM night_tasks") or {}).get("p", 1)
        cols = list(f) + ["id", "position", "kind", "run_now", "resume_id", "created", "updated"]
        vals = list(f.values()) + [tid, pos, kind, int(run_now), resume_id, now, now]
        self.store.exec("INSERT INTO night_tasks(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)
        return self.night_task(tid)

    def night_edit(self, tid, body):
        r = self.night_task(tid)
        if r["status"] != "queued":
            raise ValueError("only queued tasks can be edited")
        f = self._night_fields(body, self.night_config())
        if f:
            self.store.exec("UPDATE night_tasks SET %s, updated=? WHERE id=?" % ",".join(k + "=?" for k in f), (*f.values(), time.time(), tid))
        return self.night_task(tid)

    def night_reorder(self, ids):
        self._night_table()
        if not isinstance(ids, list):
            raise ValueError("ids must be a list")
        for i, tid in enumerate(ids):
            self.store.exec("UPDATE night_tasks SET position=? WHERE id=? AND status='queued'", (i + 1, str(tid)))
        rest = self.store.rows("SELECT id FROM night_tasks WHERE status='queued' AND id NOT IN (%s) ORDER BY position" % ",".join("?" * len(ids)), ids)
        for i, r in enumerate(rest):
            self.store.exec("UPDATE night_tasks SET position=? WHERE id=?", (len(ids) + i + 1, r["id"]))
        return self.night_list("queued")

    def night_list(self, status=""):
        self._night_table()
        rows = self.store.rows("SELECT * FROM night_tasks %s ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'review' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END, "
                               "CASE WHEN status='queued' THEN position ELSE -updated END LIMIT 200" % ("WHERE status=?" if status else ""),
                               (status,) if status else ())
        return [self.night_out(r) for r in rows]

    def _night_set(self, tid, **f):
        for k in ("result", "decision", "followups"):
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k], ensure_ascii=False)
        self.store.exec("UPDATE night_tasks SET %s, updated=? WHERE id=?" % ",".join(k + "=?" for k in f), (*f.values(), time.time(), tid))

    def night_cancel(self, tid):
        r = self.night_task(tid)
        if r["status"] == "queued":
            self._night_set(tid, status="cancelled")
        elif r["status"] == "running":
            ev = _RUNNING.get(tid)
            if ev:
                ev.set()        # the run thread stops the agent and files the task as failed, worktree kept
            else:
                self._night_set(tid, status="failed", error="cancelled")
        else:
            raise ValueError("that task is not queued or running")
        return self.night_task(tid)

    def night_run_now(self, tid):
        r = self.night_task(tid)
        if r["status"] != "queued":
            raise ValueError("only queued tasks can be started")
        self._night_set(tid, run_now=1)
        self.night_tick()
        return self.night_task(tid)

    # ------------------------------------------------------------------ scheduler
    def _night_due(self, r, cfg, now):
        if r["run_now"]:
            return True
        if not cfg["enabled"]:
            return False
        if r["run_after"] and _iso(now) < r["run_after"]:
            return False
        return in_window(r["window_start"] or cfg["window_start"], r["window_end"] or cfg["window_end"], now)

    def night_tick(self, now=None):
        """Called from the coding loop (and after 'run now'): start due tasks, up to the parallel limit."""
        self._night_table()
        now = now or time.time()
        cfg = self.night_config()
        started = []
        with _LOCK:
            for r in self.store.rows("SELECT * FROM night_tasks WHERE status='running'"):
                if r["id"] not in _RUNNING and r["runner"] == "local":
                    self._night_set(r["id"], status="failed", finished=now,
                                    error="interrupted (the backend restarted during the run); the worktree is kept")
            for r in self.store.rows("SELECT * FROM night_tasks WHERE status='running' AND runner LIKE 'node:%'"):
                if now - r["updated"] > (r["max_minutes"] + 30) * 60:        # the node claimed it and never reported
                    self._night_set(r["id"], status="failed", finished=now, error="node went away (no report within the time cap + 30 minutes)")
            free = int(cfg["parallel"]) - len(_RUNNING)
            for r in self.store.rows("SELECT * FROM night_tasks WHERE status='queued' ORDER BY run_now DESC, position"):
                if r["kind"] == "task" and free <= 0:
                    continue
                if not self._night_due(r, cfg, now) or not os.path.isdir(r["repo"]):
                    continue        # a repo that is not on this machine waits for a node that has it
                stop = threading.Event()
                if r["kind"] == "task":
                    _RUNNING[r["id"]] = stop
                    free -= 1
                self._night_set(r["id"], status="running", runner="local", started=now, attempts=r["attempts"] + 1, error="", run_now=0)
                h = type(self).__new__(type(self))
                h.server = types.SimpleNamespace(store=self.store)
                target = h._night_run if r["kind"] == "task" else h._night_launch_session
                t = threading.Thread(target=target, args=(r["id"], stop), daemon=True)
                t.start()
                started.append((r["id"], t))
        return started

    def _night_agent(self, r, cfg):
        commands = self._night_commands()
        if r["agent"] != "any":
            return r["agent"]
        order = [cfg["default_agent"]] + [a for a in worker.COMMANDS if a != cfg["default_agent"]]
        return next((a for a in order if worker.installed(a, commands)), cfg["default_agent"])

    def _night_prompt(self, r, branch):
        text = r["prompt"]
        followups = json.loads(r["followups"] or "[]")
        if followups:
            prev = (json.loads(r["result"] or "{}").get("summary") or "")[:600]
            text += "\n\nYou already worked on this (branch %s). Your earlier summary: %s\nFollow-up from the user: %s" % (
                branch, prev or "(none)", followups[-1]["text"])
        return text + (
            "\n\n--- JoeBro Night Shift ---\n"
            "You are running unattended in a dedicated git worktree on branch %s. Work only in this folder and commit your work "
            "locally. Never push and never touch other branches.%s\n"
            "End your reply with:\nSUMMARY: 2-4 sentences on what you changed and what is left.\n"
            "LEARNED: one durable fact about this repo per line (a convention, gotcha, or how to run/test), at most 3; skip if none.\n"
            "Optional: save a screenshot of a visible change to .joebro/night/screenshot.png."
            % (branch, (" Check your work with: %s" % r["test_command"]) if r["test_command"] else ""))

    def _night_dir(self, tid):
        return str(Path(data_dir(self.store)) / "night-shift" / tid)

    def _night_run(self, tid, stop):
        try:
            r = self.night_task(tid)
            cfg = self.night_config()
            agent = self._night_agent(r, cfg)
            try:
                prep = worker.prepare(r["repo"], tid, r["title"], str(Path(data_dir(self.store)) / "night-shift" / "worktrees"),
                                      r["branch"], r["worktree"])
                if not prep.get("reused"):
                    self._night_set(tid, branch=prep["branch"], worktree=prep["worktree"], base_branch=prep["base_branch"],
                                    base_commit=prep["base_commit"], agent=agent)
                r = self.night_task(tid)
                if not worker.installed(agent, self._night_commands()):
                    raise ValueError("%s is not installed on this computer" % worker.NAMES.get(agent, agent))
                argv = worker.agent_argv(agent, self._night_prompt(r, r["branch"]), r["model"], r["budget_usd"], self._night_commands())
                os.makedirs(self._night_dir(tid), exist_ok=True)
                run = worker.run_agent(argv, r["worktree"], r["max_minutes"], dict(worker.no_push_env(), JOEBRO_NIGHT_TASK=tid), stop,
                                       log_path=os.path.join(self._night_dir(tid), "agent.log"))
                parsed = worker.parse_output(run["output"])
                col = worker.collect(r["worktree"], r["base_commit"], r["title"], r["test_command"], shot_dir=self._night_dir(tid))
            except (ValueError, OSError) as exc:
                self._night_set(tid, status="failed", finished=time.time(), error=str(exc)[:300])
                return
            result = {"summary": parsed["summary"], "learned": parsed["learned"], "cost_usd": parsed["cost_usd"],
                      "commits": col["commits"], "diff": col["diff"], "tests": col["tests"], "screenshot": col["screenshot"],
                      "duration_s": run["duration_s"], "agent_exit": run["exit"], "timed_out": run["timed_out"]}
            error = ("cancelled" if run["stopped"] else "stopped at the %d minute cap" % r["max_minutes"] if run["timed_out"]
                     else "the agent made no changes" if not col["commits"] else "")
            status = "review" if col["commits"] and not run["stopped"] else "failed"
            self._night_set(tid, status=status, finished=time.time(), result=result, head_commit=col["head_commit"], error=error,
                            resume_id=parsed["session_id"] or r["resume_id"])
            for fact in parsed["learned"]:          # what was learned goes into Shared Memory
                try:
                    jb_sharedmem.remember(self.store, jb_sharedmem.resolve_project(self.store, path=r["repo"]), fact, "", "learned",
                                          prov={"agent": agent, "model": r["model"], "session": tid, "source": "night_shift"})
                except ValueError:
                    pass
            if status == "review":
                self._night_notify(self.night_task(tid))
        except Exception as exc:
            try:
                self._night_set(tid, status="failed", finished=time.time(), error="internal error: " + str(exc)[:200])
            except Exception:
                pass
        finally:
            _RUNNING.pop(tid, None)

    def _night_launch_session(self, tid, stop=None):
        """kind=session: an interactive agent, started or resumed right now in the repo itself."""
        try:
            r = self.night_task(tid)
            agent = self._night_agent(r, self.night_config())
            argv = worker.session_argv(agent, r["prompt"], r["model"], r["resume_id"], self._night_commands())
            if self.pref("coding_launch_terminal") == "kitty":
                out = self.coding_launch(r["host"] or "mac", agent, r["repo"], r["prompt"])
            else:
                out = worker.tmux_launch("jb-" + tid[3:], r["repo"], argv, {"JOEBRO_NIGHT_TASK": tid})
            self._night_set(tid, status="launched", agent=agent, finished=time.time(), result={"launch": out, "summary": "Session started."})
        except Exception as exc:
            self._night_set(tid, status="failed", finished=time.time(), error=str(exc)[:300])

    # ------------------------------------------------------------------ review
    def night_card(self, t):
        res = t.get("result") or {}
        diff = res.get("diff") or {"files": 0, "insertions": 0, "deletions": 0, "file_list": []}
        tests = res.get("tests")
        base = "/api/night-shift/%s" % t["id"]
        mode = t["on_approve"]
        metrics = [{"label": "Files", "value": str(diff["files"])}, {"label": "Added", "value": "+%d" % diff["insertions"]},
                   {"label": "Removed", "value": "-%d" % diff["deletions"]}]
        if tests:
            metrics.append({"label": "Tests", "value": "passed" if tests["passed"] else "failed"})
        bits = [t["repo_name"], worker.NAMES.get(t["agent"], t["agent"])] + ([t["model"]] if t["model"] else [])
        if res.get("duration_s"):
            bits.append("%d min" % max(1, round(res["duration_s"] / 60)))
        act = lambda i, label, **kw: dict({"id": i, "label": label, "method": "POST", "path": base + "/decide", "body": {"decision": i}}, **kw)
        return {"id": "night:" + t["id"], "kind": "review", "presentation": "outcome", "task_id": t["id"], "title": t["title"],
                "subtitle": " · ".join(bits), "state": t["status"], "summary": res.get("summary") or t["error"], "branch": t["branch"],
                "metrics": metrics,
                "rows": [{"title": f["path"][:180], "detail": "+%d -%d" % (f["added"], f["removed"]), "state": ""} for f in diff["file_list"][:12]],
                "tests": tests, "screenshot_url": (base + "/screenshot") if res.get("screenshot") else None, "diff_url": base + "/diff",
                "actions": [act("approve", "Approve (merge)" if mode == "merge" else "Approve (keep branch)"),
                            act("reject", "Reject", destructive=True), act("followup", "Ask follow-up", needs_text=True)]}

    def night_review_cards(self):
        self._night_table()
        rows = self.store.rows("SELECT * FROM night_tasks WHERE status='review' OR (status='failed' AND worktree<>'' AND kind='task') ORDER BY finished DESC LIMIT 20")
        return [self.night_card(self.night_out(r)) for r in rows]

    def briefing_night_facts(self):
        cards = [c for c in self.night_review_cards() if c["state"] == "review"]
        if not cards:
            return "NIGHT SHIFT: nothing waiting for review."
        return "NIGHT SHIFT (finished overnight, waiting for his review):\n" + "\n".join(
            "- %s (%s): %s. %s" % (c["title"], c["subtitle"], ", ".join("%s %s" % (m["label"], m["value"]) for m in c["metrics"]),
                                   (c["summary"] or "")[:200]) for c in cards[:6])

    def _night_notify(self, r):
        t = self.night_out(r)
        res = t["result"] or {}
        d = res.get("diff") or {}
        tests = res.get("tests")
        title = "Night Shift: " + t["title"][:80]
        body = "%d files, +%d -%d%s" % (d.get("files", 0), d.get("insertions", 0), d.get("deletions", 0),
                                         (" · tests " + ("passed" if tests["passed"] else "failed")) if tests else "")
        nid = "inb_" + os.urandom(6).hex()
        try:
            self.store.exec("insert into inbox(id,kind,title,body,source,read,created_at) values(?,?,?,?,?,0,?)",
                            (nid, "night_review", title, body, t["id"], _iso(time.time())))
        except Exception:
            return False            # never push a review the Notifications list cannot show
        try:
            from jb_push import send_push
            send_push(self.store, title, body, category="NIGHT_REVIEW",
                      data={"kind": "night_review", "task": t["id"], "inbox_id": nid, "repo": t["repo_name"],
                            "tests": ("passed" if tests["passed"] else "failed") if tests else "none"})
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ node runner (a linked computer that has the repo)
    def night_claim(self, tid, body):
        """A node takes a queued task. Atomic: only one claimer wins. Returns what the node needs to run it."""
        node = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("node") or ""))[:40]
        if not node:
            raise ValueError("node is required")
        r = self.night_task(tid)
        if r["host"] and r["host"] != node:
            raise PermissionError("that task is pinned to " + r["host"])
        cfg = self.night_config()
        # Sessions start at once. A task is held back by the master switch or its own schedule (run_after / its own window);
        # the default night window is the node's own timer (it is asked to run overnight), not a second gate here.
        if r["kind"] == "task" and not r["run_now"] and (
                not cfg["enabled"] or (r["run_after"] and _iso(time.time()) < r["run_after"])
                or ((r["window_start"] or r["window_end"]) and not in_window(r["window_start"] or cfg["window_start"], r["window_end"] or cfg["window_end"]))):
            raise PermissionError("not due yet")
        with self.store.lock, self.store.db() as db:
            cur = db.execute("UPDATE night_tasks SET status='running', runner=?, started=?, attempts=attempts+1, error='', run_now=0, "
                             "pending_action='', updated=? WHERE id=? AND status='queued'", ("node:" + node, time.time(), time.time(), tid))
            won = cur.rowcount == 1
            db.commit()
        if not won:
            raise PermissionError("not queued")
        r = self.night_task(tid)
        branch = r["branch"] or "%s%s-%s" % (worker.BRANCH_PREFIX, tid, worker.slug(r["title"]))
        return {"ok": True, "task": self.night_out(r), "prompt": self._night_prompt(r, branch), "commands": self._night_commands(),
                "default_agent": self.night_config()["default_agent"]}

    def night_report(self, tid, body):
        """A node files its run (like _night_run does at the end) or the outcome of a parked decision."""
        r = self.night_task(tid)
        node = str(body.get("node") or "")
        if not r["runner"].startswith("node:") or (node and r["runner"] != "node:" + node):
            raise PermissionError("that task is not running on this node")
        if body.get("decision"):
            d = body["decision"] if isinstance(body["decision"], dict) else {}
            dec = {"decision": str(d.get("decision") or ""), "mode": str(d.get("mode") or ""), "merged": bool(d.get("merged")),
                   "detail": str(d.get("detail") or "")[:400], "at": _iso(time.time())}
            if dec["decision"] not in ("approve", "reject"):
                raise ValueError("decision is approve or reject")
            self._night_set(tid, status="approved" if dec["decision"] == "approve" else "rejected", decision=dec, worktree="", pending_action="")
            try:
                self.store.exec("update inbox set read=1 where kind='night_review' and source=?", (tid,))
            except Exception:
                pass
            return {"ok": True, "task": self.night_out(self.night_task(tid))}
        status = str(body.get("status") or "")
        if status not in ("review", "failed", "launched"):
            raise ValueError("status is review, failed or launched")
        fields = {k: str(body[k])[:500] for k in ("branch", "worktree", "base_branch", "base_commit", "head_commit", "resume_id", "agent")
                  if isinstance(body.get(k), str)}
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        self._night_set(tid, status=status, finished=time.time(), error=str(body.get("error") or "")[:300], result=result,
                        diff_text=str(body.get("diff_text") or "")[:400000], diff_truncated=int(bool(body.get("diff_truncated"))), **fields)
        if body.get("screenshot_b64"):
            import base64
            try:
                png = base64.b64decode(str(body["screenshot_b64"]), validate=True)
            except ValueError:
                png = b""
            if 0 < len(png) <= 8 * 1024 * 1024:
                os.makedirs(self._night_dir(tid), exist_ok=True)
                with open(os.path.join(self._night_dir(tid), "screenshot.png"), "wb") as f:
                    f.write(png)
        for fact in result.get("learned") or []:          # what was learned goes into Shared Memory
            try:
                jb_sharedmem.remember(self.store, jb_sharedmem.resolve_project(self.store, path=r["repo"]), str(fact), "", "learned",
                                      prov={"agent": fields.get("agent") or r["agent"], "model": r["model"], "session": tid, "source": "night_shift"})
            except (ValueError, OSError):
                pass
        if status == "review":
            self._night_notify(self.night_task(tid))
        return {"ok": True, "task": self.night_out(self.night_task(tid))}

    def night_decide(self, tid, body):
        """The explicit user decision. The only place a destructive git command is run."""
        r = self.night_task(tid)
        decision = str(body.get("decision") or "")
        if r["kind"] != "task" or r["status"] not in ("review", "failed"):
            raise ValueError("that task is not waiting for a decision")
        if r["runner"].startswith("node:") and decision in ("approve", "reject"):
            # the repo and worktree live on the node: park the action, the node runs it on its next pass and reports a decision
            mode = body.get("mode") or r["on_approve"] or self.night_config()["on_approve"]
            if decision == "approve" and (mode not in ("keep", "merge") or r["status"] != "review"):
                raise ValueError("mode is keep or merge, and only a task in review can be approved")
            self._night_set(tid, pending_action=("approve:" + mode) if decision == "approve" else "reject")
            return {"ok": True, "task": self.night_out(self.night_task(tid)), "parked": True,
                    "detail": "Waiting for %s to carry it out on its next pass." % r["runner"][5:]}
        if decision == "followup":
            text = str(body.get("text") or "").strip()[:4000]
            if not text:
                raise ValueError("a follow-up needs text")
            followups = json.loads(r["followups"] or "[]") + [{"text": text, "at": _iso(time.time())}]
            self._night_set(tid, status="queued", followups=followups, run_now=int(bool(body.get("now"))), error="")
            if body.get("now"):
                self.night_tick()
            return {"ok": True, "task": self.night_out(self.night_task(tid)), "detail": "Queued again with your follow-up."}
        if decision == "approve":
            mode = body.get("mode") or r["on_approve"] or self.night_config()["on_approve"]
            if mode not in ("keep", "merge"):
                raise ValueError("mode is keep or merge")
            if r["status"] != "review":
                raise ValueError("a failed task can be rejected or given a follow-up, not approved")
            out = worker.approve(r["repo"], r["branch"], r["worktree"], r["base_branch"], mode, r["title"])
            status = "approved"
        elif decision == "reject":
            out = worker.reject(r["repo"], r["branch"], r["worktree"]) if r["branch"] else {"detail": "Nothing to discard."}
            status, mode = "rejected", ""
        else:
            raise ValueError("decision is approve, reject or followup")
        dec = {"decision": decision, "mode": mode, "merged": bool(out.get("merged")), "detail": out["detail"], "at": _iso(time.time())}
        self._night_set(tid, status=status, decision=dec, worktree="")
        try:
            self.store.exec("update inbox set read=1 where kind='night_review' and source=?", (tid,))
        except Exception:
            pass
        return {"ok": True, "task": self.night_out(self.night_task(tid)), "detail": out["detail"]}

    # ------------------------------------------------------------------ personal-agent tools
    def _night_find_project(self, name):
        name = str(name or "").strip()
        projects = [p for p in jb_sharedmem.list_projects(self.store) if p["scope"] == "project"]
        hit = [p for p in projects if name and (p["key"] == name or p["name"].lower() == name.lower() or name in p["paths"])]
        return (hit or [p for p in projects if name and name.lower() in p["key"].lower()] or [None])[0]

    def recent_coding_sessions_tool(self, body):
        args = self.parse_tool_args(body) if isinstance(body, str) else (body or {})
        p = self._night_find_project(args.get("project")) if args.get("project") else None
        if args.get("project") and not p:
            return "No project called %r in memory yet." % args["project"]
        rows = jb_sharedmem.recent_sessions(self.store, p["key"] if p else None, 10)
        if not rows:
            return "No coding sessions recorded yet."
        return "Recent coding sessions:\n" + "\n".join(
            "- %s · %s%s on %s · %s · branch %s · %s · resume id %s" % (
                s["at"][:16], s["agent"], ("/" + s["model"]) if s["model"] else "", s["host"], s["project_name"], s["branch"] or "?",
                s["summary"] or "(no summary)", s["resume_id"] or "-") for s in rows)

    def start_coding_session_tool(self, body):
        args = self.parse_tool_args(body) if isinstance(body, str) else (body or {})
        repo, agent, resume_id = str(args.get("repo") or "").strip(), args.get("agent") or "", ""
        p = self._night_find_project(args.get("project") or repo)
        sessions = jb_sharedmem.recent_sessions(self.store, p["key"], 10) if p else []
        if agent:
            sessions = [s for s in sessions if s["agent"] == agent]
        if isinstance(args.get("resume"), str) and args["resume"].strip() not in ("", "true", "false"):
            # exact session: a recorded session id, or a coding-agent id (code:host:source:session)
            rid = args["resume"].strip()
            hit = next((x for x in jb_sharedmem.recent_sessions(self.store, None, 50) if x["id"] == rid or x["resume_id"] == rid), None)
            if not hit:
                self._coding_table()
                row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (rid,)) if rid.startswith("code:") else None
                if row and row["cwd"]:
                    hit = {"repo": row["cwd"], "agent": row["source"], "resume_id": row["session_id"]}
            if not hit:
                return "No recorded session with that id to resume."
            repo, agent, resume_id = hit["repo"], hit["agent"], hit["resume_id"]
        elif args.get("resume"):
            if not sessions:
                return "No earlier session to resume there. Call again with resume=false to start a fresh one."
            last = sessions[0]
            repo, agent, resume_id = last["repo"], last["agent"], last["resume_id"]
        repo = repo or (sessions[0]["repo"] if sessions else (p["paths"][-1] if p and p["paths"] else ""))
        if not repo:
            return "I don't know where that project lives. Give me the repo path."
        try:
            r = self.night_add({"repo": repo, "prompt": args.get("prompt") or ("Continue where you left off." if resume_id else ""), "agent": agent or "any",
                                "title": (args.get("title") or "Session: " + str(args.get("prompt") or ""))[:60]}, kind="session", run_now=True, resume_id=resume_id)
            for _, t in self.night_tick():
                t.join(25)
            t = self.night_out(self.night_task(r["id"]))
        except (ValueError, LookupError) as exc:
            return "Could not start it: %s" % exc
        if t["status"] == "launched":
            launch = (t["result"] or {}).get("launch") or {}
            return "%s %s in %s%s. It will appear in the Agents list." % (
                "Resumed" if resume_id else "Started", worker.NAMES.get(t["agent"], t["agent"]), t["repo"],
                (" (%s)" % launch["attach"]) if launch.get("attach") else "")
        if t["status"] == "queued":
            return "Queued: that repo is not on this computer, so it waits for a linked computer that has it."
        return "Could not start it: %s" % (t["error"] or t["status"])


def route(h, method, path, q):
    """Serve /api/night-shift/*. True when the request was handled."""
    if path != "/api/night-shift" and not path.startswith("/api/night-shift/"):
        return False
    parts = [p for p in path[len("/api/night-shift"):].split("/") if p]
    head, action = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
    try:
        b = (h._json_body() or {}) if method in ("POST", "PUT") else {}
        if not isinstance(b, dict):
            raise ValueError("body must be a JSON object")
        one = lambda r: {"ok": True, "task": h.night_out(r)}
        if method == "GET":
            if not head:
                tasks = h.night_list(q.get("status") or "")
                out = {"ok": True, "tasks": tasks, "reviews": h.night_review_cards(), "config": h.night_config(),
                       "running": sum(t["status"] == "running" for t in tasks), "queued": sum(t["status"] == "queued" for t in tasks)}
            elif head == "reviews":
                out = {"ok": True, "cards": h.night_review_cards()}
            elif head == "config":
                out = {"ok": True, "config": h.night_config()}
            elif action == "diff":
                r = h.night_task(head)
                if r["runner"].startswith("node:"):       # the repo is on the node: serve what it reported
                    h.json({"ok": True, "diff": r["diff_text"], "truncated": bool(r["diff_truncated"])})
                    return True
                text, cut = worker.diff_text(r["repo"], r["base_commit"], r["head_commit"]) if r["head_commit"] else ("", False)
                out = {"ok": True, "diff": text, "truncated": cut}
            elif action == "screenshot":
                shot = os.path.join(h._night_dir(h.night_task(head)["id"]), "screenshot.png")
                if not os.path.isfile(shot):
                    raise LookupError("no screenshot")
                h._send(200, ctype="image/png")
                h.wfile.write(open(shot, "rb").read())
                return True
            else:
                t = h.night_out(h.night_task(head))
                out = {"ok": True, "task": t, "card": h.night_card(t) if t["kind"] == "task" and t["status"] not in ("queued", "cancelled") else None}
        elif method == "PUT":
            out = {"ok": True, "config": h.night_set_config(b)} if head == "config" else one(h.night_edit(head, b))
        elif method == "DELETE" and head:
            out = one(h.night_cancel(head))
        elif method == "POST":
            if not head:
                out = one(h.night_add(b))
            elif head == "reorder":
                out = {"ok": True, "tasks": h.night_reorder(b.get("ids"))}
            elif action == "cancel":
                out = one(h.night_cancel(head))
            elif action == "run":
                out = one(h.night_run_now(head))
            elif action == "decide":
                out = h.night_decide(head, b)
            elif action == "node-decide":      # park a decision for the node (same as decide on a node-run task)
                act = str(b.get("action") or "")
                if act not in ("approve:keep", "approve:merge", "reject"):
                    raise ValueError("action is approve:keep, approve:merge or reject")
                if not h.night_task(head)["runner"].startswith("node:"):
                    raise ValueError("only a task a node ran can have a decision parked")
                out = h.night_decide(head, {"decision": act.split(":")[0], "mode": act.partition(":")[2]})
            elif action == "claim":
                out = h.night_claim(head, b)
            elif action == "report":
                out = h.night_report(head, b)
            else:
                raise LookupError("not found")
        else:
            raise LookupError("not found")
        h.json(out)
    except LookupError as exc:
        h.json({"ok": False, "detail": str(exc)}, 404)
    except PermissionError as exc:
        h.json({"ok": False, "error": str(exc), "detail": str(exc)}, 409)
    except (ValueError, TypeError) as exc:
        h.json({"ok": False, "detail": str(exc) or "invalid request"}, 400)
    return True
