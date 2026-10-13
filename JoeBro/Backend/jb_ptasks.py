"""Phone-agent tasks: the regular `tasks` table with scope='personal'.

Same table, same scheduler (_task_scheduler), same runner (execute_task), same
registry rows as every other task. What differs is only:
  * the phone agent (session `phone`) is the only chat with tools for them
    (personal_task_*), and the REST/agents/chat lists hide them unless asked;
  * a run is silent: its final reply is a notification, or the word NOTHING;
  * a non-empty, changed (sha256) reply is appended to the phone chat as a
    short assistant notice and pushed through the normal notify() path.

Nothing is seeded. A user gets a task only by making one, or by tapping a
"Starter task" button, which creates ONE ordinary task from STARTERS below.
"""
import hashlib
import traceback

from jb_core import *  # noqa: F401,F403

PTASK_PREFIX = "ptask_"
_NOTHING = re.compile(r"^\W*(nothing|no update|nothing to report)\b", re.I)

RUN_SUFFIX = (
    "\n\n---\nYou are running silently in the background; nobody is reading this turn. Your final reply is "
    "posted in the user's chat AND sent word for word as the phone notification, so write ONLY that "
    "message: plain text, under 400 characters, no preamble, no sign-off. You choose the wording. Optionally "
    "start with a line 'TITLE: <short heading>' to set the notification heading (default: the task title). If there is nothing worth reporting right now, reply "
    "with exactly: NOTHING. Do not call notify_me yourself.")

# id -> (title, prompt, schedule, time). Written to be safe on a fresh install:
# every source is optional and a missing one is skipped without comment.
STARTERS = {
    "morning": (
        "Morning briefing",
        "Morning briefing for the user. Cover whichever of these you can reach: the weather for their "
        "current location (get_weather, if a location exists), today's calendar events, today's pending "
        "reminders (manage_reminders), and unread email that looks important (skip newsletters and "
        "promotions) if email is connected. Skip any source that is not connected and do not mention "
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

    # ---- helpers ---------------------------------------------------------

    def ptask_is_personal(self, tid):
        row = self.store.one("select scope from tasks where id=?", (tid or "",))
        return bool(row) and (row.get("scope") or "general") == "personal"

    def tasks_for_scope(self, scope):
        scope = "personal" if str(scope or "").lower() == "personal" else "general"
        rows = self.store.rows("select * from tasks where coalesce(scope,'general')=? order by datetime(created_at) desc", (scope,))
        return [self.task_json(r) for r in rows]

    def ptask_starter(self, starter_id):
        """Create ONE personal task from a starter, only when the user asks for it
        (a button in the Mac or phone app). Raises ValueError for an unknown id."""
        spec = STARTERS.get(str(starter_id or "").strip().lower())
        if not spec:
            raise ValueError("starter must be one of: " + ", ".join(sorted(STARTERS)))
        title, prompt, schedule, at = spec
        return self.upsert_task({"title": title, "prompt": prompt, "schedule": schedule,
                                 "scheduled_time": at, "scope": "personal"})

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
        text = text[:600]
        digest = hashlib.sha256(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()
        if digest == (row.get("last_hash") or ""):
            return {"reported": False, "why": "unchanged since last report"}
        self.store.exec("update tasks set last_hash=? where id=?", (digest, row["id"]))
        title = row.get("title") or "JoeBro"
        m = re.match(r"\s*TITLE:\s*(.+?)\s*\n+(.*)", text, re.S | re.I)  # agent-chosen heading
        if m and m.group(2).strip():
            title, text = m.group(1)[:150], m.group(2).strip()
        self.ensure_phone_session()
        self.add_message(PHONE_SESSION_ID, "assistant", text,
                         {"notice": True, "source": "personal_task", "task_id": row["id"], "task_title": title})
        try:
            delivery = self.notify(text, title=title[:150], kind="personal_task", source=row["id"],
                                   session=PHONE_SESSION_ID)
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
        if ((form or {}).get("session") or "") != PHONE_SESSION_ID:
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
