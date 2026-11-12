"""Phone-agent tasks: the regular `tasks` table with scope='personal'.

Same table, same scheduler (_task_scheduler), same runner (execute_task), same
registry rows as every other task. What differs is only:
  * the phone agent (session PHONE_SESSION_ID) is the only chat with tools for
    them (personal_task_*), and the REST/agents/chat lists hide them unless asked;
  * a run is silent: its final reply is a message for the user, or the word NOTHING;
  * a non-empty, changed (sha256) reply is appended to the phone chat as an
    assistant notice and pushed through the normal notify() path.

Nothing is seeded by the app itself. A user gets a task only by making one, or
by tapping a "Starter task" button, which creates ONE ordinary task from STARTERS
below. An extension may seed its own once (hook `ptask_seeds`) and may run a
task without a model (hook `ptask_run`).

A task whose prompt has `WATCH: <url>` lines is a page watch: no model, it
replies only when a page's text changed since the last run.
"""
import difflib
import hashlib
import traceback

from jb_core import *  # noqa: F401,F403
import jb_ext

PTASK_PREFIX = "ptask_"
_NOTHING = re.compile(r"^\W*(nothing|no update|nothing to report)\b", re.I)

RUN_SUFFIX = (
    "\n\n---\nThis is a user-requested personal background task. Do the work using real tools, "
    "then write a COMPLETE useful message for the user's personal chat. This is not a notification-sized "
    "output: include everything important and report unavailable sources honestly. For a morning briefing, "
    "cover the user's requested sources in a concise readable briefing, not a fragment. "
    "Never include credentials or passwords from source material. You may start with TITLE: <heading>. "
    "Only when there is genuinely no new information, reply exactly NOTHING. Do not call notify_me; "
    "the completed chat message gets a short lock-screen preview automatically.")


# id -> (title, prompt, schedule, time). Written to be safe on a fresh install:
# every source is optional and a missing one is skipped without comment.
STARTERS = {
    "morning": (
        "Morning briefing",
        "Morning briefing for the user. Cover whichever of these you can reach: the weather for their "
        "current location (get_weather, if a location exists), today's calendar events, today's pending "
        "reminders (manage_reminders), and unread email that looks important (skip newsletters and "
        "promotions) if email is connected, and, if coding agents are linked (coding_agents), a short "
        "'your agents' line: which are waiting on the user, which finished overnight and which are still "
        "working. Skip any source that is not connected and do not mention "
        "that it is missing. Lead with what matters soonest. If nothing is available at all, reply NOTHING.",
        "daily", "08:00"),
    "evening": (
        "Evening wrap-up",
        "Evening wrap-up for the user: tomorrow's calendar events and any reminders due in the next 48 "
        "hours (manage_reminders). Two or three short lines. Skip any source that is not connected and "
        "do not mention it. If tomorrow is empty and nothing is due, reply NOTHING.",
        "daily", "21:30"),
}


class PersonalTasksMixin:
    PTASK_TOOL_NAMES = ("personal_task_create", "personal_task_list", "personal_task_update",
                        "personal_task_delete", "personal_task_run_now")

    # ---- seed ------------------------------------------------------------

    def ptask_seed(self):
        """Seed an extension's own tasks, once (pref flag, so deleted seeds stay
        deleted). Hook `ptask_seeds` returns a list of
        (id, title, prompt, schedule, time, weekday, status). Core seeds nothing."""
        seeds = [s for group in jb_ext.call("ptask_seeds") for s in group]
        if not seeds or self.store.one("select 1 from prefs where key='seeded_ptasks_v1'"):
            return 0
        now = now_iso()
        for tid, title, prompt, sched, t, wd, status in seeds:
            self.store.exec(
                "insert or ignore into tasks(id,title,prompt,schedule,scheduled_time,status,repeat_day,scope,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,'personal',?,?)", (tid, title, prompt, sched, t, status, wd, now, now))
        self.store.exec("insert or replace into prefs(key,value) values('seeded_ptasks_v1', ?)", (json.dumps(True),))
        return len(seeds)

    # ---- helpers ---------------------------------------------------------

    def ptask_is_personal(self, tid):
        row = self.store.one("select scope from tasks where id=?", (tid or "",))
        return bool(row) and (row.get("scope") or "general") == "personal"

    def tasks_for_scope(self, scope):
        scope = "personal" if str(scope or "").lower() == "personal" else "general"
        rows = self.store.rows("select * from tasks where coalesce(scope,'general')=? order by datetime(created_at) desc", (scope,))
        out = []
        for row in rows:
            item = self.task_json(row)
            latest = self.store.one("SELECT status,error,finished_at,substr(result,1,1500) result FROM agents WHERE ref_id=? AND kind='task' ORDER BY created_at DESC LIMIT 1", (row['id'],))
            if latest: item["last_execution"] = latest
            out.append(item)
        return out

    def ptask_starter(self, starter_id):
        """Create ONE personal task from a starter, only when the user asks for it
        (a button in the Mac or phone app). Raises ValueError for an unknown id."""
        spec = STARTERS.get(str(starter_id or "").strip().lower())
        if not spec:
            raise ValueError("starter must be one of: " + ", ".join(sorted(STARTERS)))
        title, prompt, schedule, at = spec
        return self.upsert_task({"title": title, "prompt": prompt, "schedule": schedule,
                                 "scheduled_time": at, "scope": "personal"})

    # ---- deterministic page watch (no model) ----------------------------------

    _WATCH_RE = re.compile(r"(?m)^WATCH:\s*(https?://\S+)")
    _WATCH_HOT = re.compile(r"2027|2026/27|2027/28|fee|deadline|clos|open|apply|application", re.I)

    def ptask_watch(self, row):
        """Tasks whose prompt has `WATCH: <url>` lines: fetch each page (browser
        headers, Jina fallback), keep its text + sha256 in prefs, and reply only
        when a page's text changed since the last run (first run = silent
        baseline). All pages unreadable -> one 'check failed' reply."""
        urls = self._WATCH_RE.findall(row.get("prompt") or "")
        key = "watch_" + row["id"]
        saved = self.pref(key) or {}
        out, failed, changed = [], [], []
        for url in urls:
            try:
                text = "\n".join(l for l in (re.sub(r"\s+", " ", x).strip() for x in self._reach_read(url).splitlines()) if l)
                if len(text) < 300:
                    raise ValueError("too little text")
            except Exception as exc:
                failed.append(f"{url} ({str(exc)[:60]})")
                continue
            h = hashlib.sha256(text.encode()).hexdigest()
            old = saved.get(url)
            saved[url] = {"hash": h, "text": text[:200000], "at": now_iso()}
            if old and old.get("hash") != h:
                a, b = (old.get("text") or "").splitlines(), text.splitlines()
                add = [l[2:] for l in difflib.ndiff(a, b) if l.startswith("+ ")]
                rem = [l[2:] for l in difflib.ndiff(a, b) if l.startswith("- ")]
                add.sort(key=lambda l: not self._WATCH_HOT.search(l))
                if add or rem:
                  changed.append((url, add[:3], len(add), len(rem)))
        self.set_pref(key, saved)
        if urls and len(failed) == len(urls):
            return "TITLE: Page watch - check failed\nCould not read any watched page: " + "; ".join(failed)[:350]
        for url, add, na, nr in changed:
            out.append(f"{urllib.parse.urlparse(url).path.strip('/') or url}: +{na}/-{nr} lines. " + " | ".join(l[:110] for l in add))
        if not out:
            return "NOTHING"
        return "TITLE: " + (row.get("title") or "Page watch")[:60] + " - changed\n" + "\n".join(out)[:500]

    def ptask_deterministic(self, row):
        """A reply for a task that needs no model, or None to run it as an agent.
        Page watches are built in; an extension can claim others (hook `ptask_run`,
        fn(handler, row) -> reply text or None)."""
        if (row.get("scope") or "general") != "personal":
            return None
        reply = jb_ext.first("ptask_run", self, row)
        if reply is not None:
            return reply
        if self._WATCH_RE.search(row.get("prompt") or ""):
            return self.ptask_watch(row)
        return None

    # ---- the run's outcome -----------------------------------------------

    def ptask_prompt(self, prompt):
        return (prompt or "Run this task.") + RUN_SUFFIX

    def ptask_report(self, row, reply):
        """Silent unless the reply is real AND different from last time."""
        if str(self.pref("personal_automation_paused")).lower() in ("true", "1", "yes", "on"):
            return {"reported": False, "why": "phone automation paused"}
        text = (reply or "").strip()
        if not text or _NOTHING.match(text):
            return {"reported": False, "why": "nothing to report"}
        digest = hashlib.sha256(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()
        if digest == (row.get("last_hash") or ""):
            return {"reported": False, "why": "unchanged since last report"}
        title = row.get("title") or "JoeBro"
        m = re.match(r"\s*TITLE:\s*(.+?)\s*\n+(.*)", text, re.S | re.I)  # agent-chosen heading
        if m and m.group(2).strip():
            title, text = m.group(1)[:150], m.group(2).strip()
        meta = {"notice": True, "source": "personal_task", "task_id": row["id"], "task_title": title}
        if getattr(self, "_task_visualizations", None): meta["visualizations"] = self._task_visualizations
        self.ensure_phone_session()
        message_id = self.add_message(PHONE_SESSION_ID, "assistant", text, meta)
        self.store.exec("update tasks set last_hash=? where id=?", (digest, row["id"]))
        try:
            delivery = self.notify(text, title=title[:150], kind="personal_task", source=row["id"],
                                   session=PHONE_SESSION_ID, message_id=message_id)
        except Exception as exc:
            delivery = {"ok": False, "detail": str(exc)[:200]}
        return {"reported": True, "delivery": delivery}

    # ---- tools (phone agent only; schemas live in jb_core) ------------------

    def _ptask_bg(self, tid):
        try:
            self.execute_task(tid)
        except Exception:
            traceback.print_exc()

    def ptask_tool(self, name, body, form):
        from jb_updates import personal_context
        if not personal_context(self, form):
            raise ValueError(name + " is only available to the phone agent")
        a = self.parse_tool_args(body)
        a.pop("permission_mode", None)             # never model-settable (see manage_tasks)
        if "enabled" in a:
            a["status"] = "active" if a.pop("enabled") not in (False, "false", "False", 0) else "paused"
        tid = a.get("id") or a.get("task_id") or ""
        if name == "personal_task_list":
            return json.dumps({"ok": True, "tasks": self.tasks_for_scope("personal")}, ensure_ascii=False)
        if name == "personal_task_create":
            a.pop("id", None)
            a["scope"] = "personal"
            return json.dumps(self.upsert_task(a), ensure_ascii=False)
        if not self.ptask_is_personal(tid):
            return json.dumps({"ok": False, "detail": "no phone-agent task with that id"})
        if name == "personal_task_update":
            return json.dumps(self.upsert_task(a, tid), ensure_ascii=False)
        if name == "personal_task_delete":
            self.store.exec("delete from tasks where id=?", (tid,))
            self.store.exec("delete from messages where session_id=?", ("task_session_" + tid,))
            self.store.exec("delete from sessions where id=?", ("task_session_" + tid,))
            return json.dumps({"ok": True, "deleted": tid})
        if name == "personal_task_run_now":
            h = type(self).__new__(type(self))
            h.server = self.server
            threading.Thread(target=h._ptask_bg, args=(tid,), daemon=True).start()
            return json.dumps({"ok": True, "started": tid})
        raise ValueError("unknown phone-agent task tool " + name)
