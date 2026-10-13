"""The personal-assistant layer: the working plan, and reminders.

Two small things that change what the agent can be.

**The plan** is the to-do list for a chat. The agent writes its steps down,
ticks them off, and gets the list back in the system prompt every round — so a
long job survives compaction, a model switch and its own forgetfulness. It is
also the honest answer to "where are you up to?".

**Reminders** are one-shot: "remind me at six to email him". Scheduled tasks
already cover *recurring* work; this covers the thing you say out loud on a
phone, and it comes back to you as a notification (the Inbox always, push or
Telegram when those are set up).
"""
from jb_core import *  # noqa: F401,F403
from jb_prompt import JOEBRO_DEFAULT_SOUL

_STATUS_MARK = {"done": "[x]", "doing": "[~]", "todo": "[ ]", "blocked": "[!]"}


def _on(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class PersonalMixin:
    _plan_session = ""      # set per-turn by run_agent; the plan belongs to a chat

    # ---- the phone agent's own session -----------------------------------
    # One reserved chat (PHONE_SESSION_ID) that always exists once the phone has
    # opened it, always runs as an agent + orchestrator, and always follows the
    # app's default model. Clients cannot rename, re-model or delete it.

    def phone_payload(self, create=False):
        """The phone agent's profile. A GET never creates it; the POST does."""
        sid = PHONE_SESSION_ID
        model = self.pref("default_model") or ""
        endpoint_id = self.pref("default_endpoint_id") or ""
        name = (self.pref("phone_agent_name") or "JoeBro").strip()[:40] or "JoeBro"
        pmode = "full" if _on(self.pref("mobile_allow_full")) else "sandbox"
        if create:
            self.store.exec(
                "insert or ignore into sessions"
                "(id,name,model,endpoint_url,endpoint_id,mode,permission_mode,created_at) "
                "values(?,?,?,?,?,?,?,?)",
                (sid, name, model, "", endpoint_id, "agent", pmode, now_iso()))
            # The persona always follows the shared default, even if a client
            # submitted another model or changed the row.
            self.store.exec(
                "update sessions set name=?, model=?, endpoint_url='', endpoint_id=?, "
                "mode='agent', permission_mode=?, archived=0 where id=?",
                (name, model, endpoint_id, pmode, sid))
        session = self.store.one("select * from sessions where id=?", (sid,))
        soul = self.pref("phone_soul")
        visible = self.session_json(session) if session else None
        if visible:
            visible.update({"name": name, "model": model, "mode": "agent", "archived": False})
        return {"session": visible, "model": model, "endpoint_id": endpoint_id,
                "permission_mode": pmode,
                "soul": soul if isinstance(soul, str) else JOEBRO_DEFAULT_SOUL}

    def ensure_phone_session(self):
        self.phone_payload(create=True)

    def phone_turn(self, form):
        """Apply the phone agent's capability policy to every turn it takes.

        The personal fork forced Full Access here. Production holds the phone at
        sandbox unless the user switched on `mobile_allow_full` on the Mac, so a
        lost phone is not a shell on the Mac. The agent is an orchestrator: it
        delegates hands-on work to chats, it does not touch files itself."""
        if form.get("session") != PHONE_SESSION_ID:
            return form
        self.ensure_phone_session()
        form.update({"model": self.pref("default_model") or "",
                     "endpoint_id": self.pref("default_endpoint_id") or "",
                     "endpoint_url": "", "mode": "agent", "orchestrator": "true",
                     "permission_mode": "full" if _on(self.pref("mobile_allow_full")) else "sandbox",
                     "allow_web_search": "true", "ask_permission": "true"})
        # Edit approval follows the user's "Require approval" choice (default on).
        if self.pref("require_doc_approval") is not False:
            form["ask_doc_edit"] = "true"
        form.pop("allow_bash", None)
        return form

    def phone_busy(self):
        """True while the phone agent is mid-reply (clear/compact must wait)."""
        from jb_chat import _ACTIVE_TURNS
        return PHONE_SESSION_ID in _ACTIVE_TURNS

    # ---- the working plan ------------------------------------------------

    def plan_get(self, sid):
        row = self.store.one("select steps from plans where session_id=?", (sid or "",))
        try:
            steps = json.loads(row["steps"]) if row and row.get("steps") else []
        except (ValueError, TypeError):
            steps = []
        return [s for s in steps if isinstance(s, dict) and s.get("text")]

    def plan_save(self, sid, steps):
        self.store.exec(
            "insert or replace into plans(session_id,steps,updated_at) values(?,?,?)",
            (sid or "", json.dumps(steps[:40], ensure_ascii=False), now_iso()))
        return steps

    def plan_block(self, sid):
        """The plan as it appears in the system prompt. Empty when there's nothing
        outstanding — a finished plan should stop taking up room."""
        steps = self.plan_get(sid)
        if not steps or all(s.get("status") == "done" for s in steps):
            return ""
        lines = [f"{_STATUS_MARK.get(s.get('status') or 'todo', '[ ]')} {i + 1}. {s['text']}"
                 for i, s in enumerate(steps)]
        left = sum(1 for s in steps if s.get("status") != "done")
        return ("YOUR PLAN for this job (carried across rounds — keep it current with manage_plan):\n"
                + "\n".join(lines)
                + f"\n{left} step(s) left. Mark each done as you finish it; if the plan is now wrong, "
                  "rewrite it rather than working around it.")

    def manage_plan_tool(self, body):
        args = self.parse_tool_args(body)
        sid = (args.get("session") or self._plan_session or "").strip()
        action = (args.get("action") or "get").strip().lower()
        steps = self.plan_get(sid)

        def _out(note=""):
            payload = {"ok": True, "steps": [dict(s, n=i + 1) for i, s in enumerate(steps)],
                       "remaining": sum(1 for s in steps if s.get("status") != "done")}
            if note:
                payload["note"] = note
            return json.dumps(payload, ensure_ascii=False)

        if action == "get":
            return _out()
        if action in ("set", "replace"):
            raw = args.get("steps") or args.get("plan") or []
            if isinstance(raw, str):
                raw = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip()
                       for ln in raw.splitlines() if ln.strip()]
            steps = [{"text": str(t).strip(), "status": "todo"} for t in raw if str(t).strip()]
            if not steps:
                raise ValueError("manage_plan set needs steps (a list, or one per line)")
            self.plan_save(sid, steps)
            return _out("plan set — work it top to bottom and mark each done")
        if action == "add":
            text = (args.get("text") or args.get("step") or "").strip()
            if not text:
                raise ValueError("manage_plan add needs text")
            steps.append({"text": text, "status": "todo"})
            self.plan_save(sid, steps)
            return _out()
        if action in ("done", "doing", "blocked", "todo"):
            which = args.get("step") or args.get("n") or args.get("text") or ""
            idx = None
            if str(which).strip().isdigit():
                idx = int(str(which).strip()) - 1
            else:
                needle = str(which).strip().lower()
                idx = next((i for i, s in enumerate(steps) if needle and needle in s["text"].lower()), None)
                if idx is None and action == "done":
                    # "done" with nothing named means the next unfinished step.
                    idx = next((i for i, s in enumerate(steps) if s.get("status") != "done"), None)
            if idx is None or not (0 <= idx < len(steps)):
                raise ValueError(f"no such step '{which}' — call manage_plan get to see the numbers")
            steps[idx]["status"] = action
            self.plan_save(sid, steps)
            return _out(f"step {idx + 1} → {action}")
        if action == "clear":
            self.store.exec("delete from plans where session_id=?", (sid,))
            return json.dumps({"ok": True, "steps": [], "note": "plan cleared"})
        raise ValueError(f"unknown manage_plan action '{action}' (get, set, add, done, doing, blocked, clear)")

    # ---- reminders -------------------------------------------------------

    def remind_me_tool(self, body):
        args = self.parse_tool_args(body)
        text = (args.get("text") or args.get("message") or args.get("what") or "").strip()
        if not text:
            raise ValueError("remind_me needs the text of the reminder")
        due = self._reminder_due(args)
        rid = "rem_" + os.urandom(6).hex()
        self.store.exec(
            "insert into reminders(id,text,due_at,status,created_at) values(?,?,?,'pending',?)",
            (rid, text, due.isoformat(), now_iso()))
        return json.dumps({"ok": True, "id": rid, "text": text,
                           "due_at": due.isoformat(),
                           "when": due.strftime("%A %d %B at %H:%M")}, ensure_ascii=False)

    @staticmethod
    def _reminder_due(args):
        """When to fire. `at` is an absolute ISO-8601 the model resolved; `in_minutes`
        is the relative shortcut. Anything in the past is a mistake, not a reminder."""
        now = datetime.now().astimezone()
        raw = str(args.get("at") or args.get("when") or "").strip()
        mins = args.get("in_minutes") or args.get("minutes")
        if raw:
            try:
                due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"couldn't read '{raw}' as a date/time — pass ISO-8601, e.g. "
                                 f"{now.strftime('%Y-%m-%dT18:00:00')}")
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
        elif mins is not None:
            due = now + timedelta(minutes=max(1, int(mins)))
        else:
            raise ValueError("remind_me needs `at` (ISO-8601) or `in_minutes`")
        if due <= now - timedelta(minutes=1):
            raise ValueError(f"{due.isoformat()} is in the past — did you mean tomorrow?")
        return due

    def manage_reminders_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").strip().lower()
        if action == "list":
            rows = self.store.rows(
                "select * from reminders where status='pending' order by due_at limit 30")
            return json.dumps({"ok": True, "reminders": [
                {"id": r["id"], "text": r["text"], "due_at": r["due_at"]} for r in rows]},
                ensure_ascii=False)
        if action in ("cancel", "delete", "done"):
            rid = (args.get("id") or "").strip()
            if not rid:
                needle = (args.get("text") or "").strip().lower()
                row = next((r for r in self.store.rows("select * from reminders where status='pending'")
                            if needle and needle in (r["text"] or "").lower()), None)
                rid = (row or {}).get("id") or ""
            if not rid:
                raise ValueError("which reminder? pass id (from list) or matching text")
            self.store.exec("update reminders set status='cancelled' where id=?", (rid,))
            return json.dumps({"ok": True, "cancelled": rid})
        raise ValueError(f"unknown manage_reminders action '{action}' (list, cancel)")

    def fire_due_reminders(self):
        """Deliver everything now due. Called on a timer; safe to call often."""
        now = datetime.now().astimezone()
        fired = 0
        for r in self.store.rows("select * from reminders where status='pending' order by due_at"):
            try:
                due = datetime.fromisoformat(r["due_at"])
            except (ValueError, TypeError):
                self.store.exec("update reminders set status='error' where id=?", (r["id"],))
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
            if due > now:
                break                      # ordered by due_at: the rest are later
            # Mark fired BEFORE delivering: a crash mid-send costs one reminder,
            # a crash after delivering would otherwise repeat it every 30 seconds.
            self.store.exec("update reminders set status='fired', fired_at=? where id=?",
                            (now_iso(), r["id"]))
            self.notify(r["text"], title="Reminder", kind="reminder", source=r["id"])
            fired += 1
        return fired


def reminder_loop(server):
    """Daemon: fire due reminders. Half-minute granularity is plenty for 'at 6pm'."""
    import types as _types
    from joebro_backend import Handler
    while True:
        try:
            h = Handler.__new__(Handler)
            h.server = _types.SimpleNamespace(store=server.store)
            h.fire_due_reminders()
        except Exception:
            pass
        time.sleep(30)
