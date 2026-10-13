"""The fleet: background sub-agents, the proactive pulse, and the inbox.

Three things the chat loop can't do on its own:

* **spawn_agent** runs work in the background. A spawned agent is a real agent
  turn in its own session with its own model and permissions — you fire off
  five and carry on talking. They report back through the inbox.
* **the pulse** is the agent waking itself up. Every N minutes it works through
  a checklist you wrote in plain English and messages you ONLY if something
  actually needs you. This is the difference between a chatbot and an
  assistant.
* **the inbox** is how anything in the backend reaches you: a finished agent,
  the pulse, a scheduled task. It is written to the inbox first, then pushed
  (or sent to Telegram) when those are set up.

ponytail: threads, not a queue/worker service. A handful of concurrent agents
on one Mac is what a semaphore is for; reach for a real queue when you want
them surviving a restart.
"""
from jb_core import *  # noqa: F401,F403
from jb_agents import request_cancel

# Subagent limits. Depth is 1: a subagent is a leaf and can never spawn another
# (it isn't offered the tool, and the spawn path refuses it anyway). At most
# MAX_LIVE_SUBAGENTS may be queued/running at once across the whole backend.
MAX_SUBAGENT_DEPTH = 1
MAX_LIVE_SUBAGENTS = 4
SUBAGENT_SESSION_PREFIX = "subagent_session_"
# Wall-clock ceiling for one subagent. A model call has its own socket timeout but
# the agent loop runs many rounds, so nothing else bounds the total. 20 minutes:
# the longest real subagent so far took 7. Override with JOEBRO_SUBAGENT_TIMEOUT (seconds).
SUBAGENT_TIMEOUT = float(os.environ.get("JOEBRO_SUBAGENT_TIMEOUT") or 1200)
_CLOSE_LOCK = threading.Lock()
INBOX_CAP = 500


def subagent_gate(parent_is_subagent, live_count, max_live=MAX_LIVE_SUBAGENTS):
    """Pure spawn guard. Returns an error string, or None if spawning is allowed."""
    if parent_is_subagent:
        return ("subagents cannot spawn further subagents (depth cap %d) - do the work yourself "
                "or return what you found" % MAX_SUBAGENT_DEPTH)
    if live_count >= max_live:
        return (f"{live_count} subagents are already running (cap {max_live}) - wait for one to "
                "finish (manage_agents list) or do this one yourself")
    return None


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


MAX_PARALLEL_AGENTS = 3
_AGENT_SLOTS = threading.Semaphore(MAX_PARALLEL_AGENTS)
_SUBAGENT_LOCK = threading.Lock()

PULSE_DEFAULT_CHECKLIST = """- Anything in today's or tomorrow's calendar I should be ready for? Only if it's soon and I'd want warning.
- Any email that genuinely needs a reply, or that is time-sensitive? Ignore newsletters and receipts.
- Any scheduled task that failed, or any background agent that finished with something I should see?
- Any deadline I've mentioned that is close and has had no progress.
Skip any source that is not connected. Never mention that a source is missing.
"""


class FleetMixin:
    # ---- inbox / delivery ------------------------------------------------

    def notify(self, body, title="", kind="note", source="", channel="auto", session=None):
        """Deliver a message to the user: inbox row, then push, then Telegram.

        The inbox is the record (the apps read it); push and Telegram are the
        delivery. A failed delivery never loses the message, because the row is
        written first. With no push key and no bot, the message just stays here."""
        body = (body or "").strip()
        if channel not in ("auto", "mobile", "telegram", "inbox"):
            raise ValueError("Notification channel must be auto, mobile, telegram or inbox")
        if not body:
            return {"ok": False, "detail": "empty message"}
        nid = "inb_" + os.urandom(6).hex()
        self.store.exec(
            "insert into inbox(id,kind,title,body,source,read,created_at) values(?,?,?,?,?,0,?)",
            (nid, kind, title or "", body, source or "", now_iso()))
        # The inbox is a record, not an archive: keep the newest INBOX_CAP rows.
        self.store.exec(
            "delete from inbox where id not in "
            "(select id from inbox order by datetime(created_at) desc, rowid desc limit ?)", (INBOX_CAP,))
        delivery = {"channel": "inbox", "delivered": False, "detail": "Saved to the JoeBro inbox"}
        if channel in ("auto", "mobile"):
            try:
                from jb_push import send_push
                delivery = send_push(self.store, (title or "JoeBro")[:150], body[:3000], session=session)
            except Exception:
                delivery = {"channel": "mobile", "delivered": False, "detail": "Mobile push unavailable"}
        if channel == "telegram" or (channel == "auto" and not delivery.get("delivered")):
            mobile_detail = delivery.get("detail")
            delivery = {"channel": "telegram", "delivered": self.push_telegram(body, title)}
            if not delivery["delivered"]:
                delivery["detail"] = mobile_detail or "Telegram push unavailable; saved to inbox"
        return {"ok": True, "id": nid, "delivered": bool(delivery.get("delivered")), "delivery": delivery}

    def push_telegram(self, body, title=""):
        """Best-effort push to the Telegram bot's chat. Returns whether it went."""
        try:
            token = (self.pref("bot_token") or "").strip()
            if not token or not self.pref("bot_enabled"):
                return False
            chat_id = (self.pref("bot_push_chat_id") or "").strip()
            if not chat_id:
                # Fall back to the first allowlisted id — the usual single-user case.
                ids = [s.strip() for s in str(self.pref("bot_allowed_ids") or "").replace(";", ",").split(",")]
                chat_id = next((i for i in ids if i), "")
            if not chat_id:
                return False
            from jb_telegram import _send
            _send(token, chat_id, (f"**{title}**\n" if title else "") + body, html=True)
            return True
        except Exception:
            return False

    def inbox_rows(self, unread_only=False, limit=50):
        q = "select * from inbox" + (" where read=0" if unread_only else "")
        return self.store.rows(q + " order by datetime(created_at) desc limit ?", (int(limit),))

    def mark_inbox_read(self, nid=None):
        if nid:
            self.store.exec("update inbox set read=1 where id=?", (nid,))
        else:
            self.store.exec("update inbox set read=1 where read=0")
        return {"ok": True}

    # ---- what the phone is allowed to hand an agent -----------------------

    def _from_phone(self):
        return ((getattr(self, "_turn_form", None) or {}).get("session") or "") == PHONE_SESSION_ID

    def _phone_pmode(self, wanted):
        """A permission level for a spawned agent. From the phone chat, `full`
        needs the Mac-side switch (pref mobile_allow_full); without it the agent
        is held at sandbox, so a lost phone is not a shell on the Mac."""
        pmode = str(wanted or "sandbox").strip().lower()
        if pmode not in ("readonly", "sandbox", "full"):
            pmode = "sandbox"
        if pmode == "full" and self._from_phone() and not _truthy(self.pref("mobile_allow_full")):
            pmode = "sandbox"
        return pmode

    def _phone_folder(self, folder):
        """The folder a phone-spawned agent may be bound to: inside the user's
        home, never a credentials or app-data folder. Other callers pass through."""
        if folder and self._from_phone():
            from jb_files import phone_folder_check
            return phone_folder_check(folder)
        return folder

    def phone_post(self, text, source, title="", **meta):
        """Say something in the phone agent's chat as a plain assistant message.
        The notice flag lets the app style it; the user answers it like any other
        message. Creates the session if the phone has never opened it."""
        self.ensure_phone_session()
        self.add_message(PHONE_SESSION_ID, "assistant", text,
                         dict({"notice": True, "source": source, "task_title": title}, **meta))

    # ---- the fleet -------------------------------------------------------

    def spawn_agents(self, args):
        """Create and start background agents. Returns as soon as they're queued —
        the whole point is that the caller keeps talking."""
        prompts = args.get("prompts")
        if isinstance(prompts, str):
            prompts = [p for p in (prompts.split("\n") if "\n" in prompts else [prompts]) if p.strip()]
        if not prompts:
            one = (args.get("prompt") or args.get("task") or "").strip()
            prompts = [one] if one else []
        prompts = [str(p).strip() for p in prompts if str(p).strip()]
        if not prompts:
            raise ValueError("spawn_agent needs a prompt (or a list of prompts to run in parallel)")
        if len(prompts) > 8:
            raise ValueError(f"{len(prompts)} agents at once is too many — 8 is the cap")
        title = (args.get("title") or "").strip()
        model = (args.get("model") or "").strip() or self.pref("default_model") or ""
        ep = (args.get("endpoint_id") or "").strip() or self.pref("default_endpoint_id") or ""
        workdir = self._phone_folder((args.get("folder") or args.get("workdir") or "").strip())
        pmode = self._phone_pmode(args.get("permission_mode"))
        notify = 0 if str(args.get("notify", "")).lower() in ("0", "false", "no") else 1

        made = []
        for i, prompt in enumerate(prompts):
            aid = "agt_" + os.urandom(6).hex()
            name = title if len(prompts) == 1 and title else (title or prompt)[:60]
            if len(prompts) > 1:
                name = f"{(title or 'Agent')} {i + 1}/{len(prompts)}"
            self.store.exec(
                """insert into agents(id,kind,ref_id,title,prompt,status,session_id,model,endpoint_id,
                                      workdir,permission_mode,notify,progress,created_at)
                   values(?,'chat','',?,?,'queued',?,?,?,?,?,?,'Queued',?)""",
                (aid, name, prompt, "agent_session_" + aid, model, ep, workdir, pmode, notify, now_iso()))
            made.append({"id": aid, "title": name})
            threading.Thread(target=self._run_agent_job, args=(aid,), daemon=True).start()
        return made

    # ---- subagents -------------------------------------------------------

    def subagent_auto(self):
        return _truthy(self.pref("subagent_model_auto"))

    def _subagent_models(self, limit=60):
        """[(endpoint_id, model)] currently available, best-effort."""
        out = []
        try:
            for it in self.model_items() or []:
                for m in it.get("models") or []:
                    out.append((it.get("endpoint_id") or "", m))
        except Exception:
            pass
        return out[:limit]

    def subagent_schema(self, schema):
        """The spawn_subagent schema for THIS turn. Auto mode: the model may pick
        `model` and is told what exists. Manual: no `model` param at all - the
        configured subagent_model is always used."""
        import copy
        s = copy.deepcopy(schema)
        props = s["function"]["parameters"]["properties"]
        if self.subagent_auto():
            names = [m for _e, m in self._subagent_models()]
            if names:
                props["model"]["description"] = (
                    "Pick the model for this task - cheaper/faster for simple work, stronger for hard "
                    "reasoning or code. Available: " + ", ".join(names) + ". Omit for the default.")
        else:
            props.pop("model", None)
        return s

    def resolve_subagent_model(self, requested=""):
        """(model, endpoint_id) per the prefs. Auto + valid request -> the request."""
        avail = self._subagent_models(limit=10000)
        requested = (requested or "").strip()
        if self.subagent_auto() and requested:
            for ep, m in avail:
                if m == requested:
                    return m, ep
        model = (self.pref("subagent_model") or "").strip()
        ep = (self.pref("subagent_endpoint_id") or "").strip()
        if not model:
            return (self.pref("default_model") or "", self.pref("default_endpoint_id") or "")
        if not ep:
            ep = next((e for e, m in avail if m == model), "") or self.pref("default_endpoint_id") or ""
        return model, ep

    def spawn_subagent(self, args, parent_id=None, parent_is_subagent=None):
        prompt = (args.get("prompt") or args.get("task") or "").strip()
        if not prompt:
            raise ValueError("spawn_subagent needs a prompt")
        form = getattr(self, "_turn_form", None) or {}
        if parent_id is None:
            parent_id = self._agent_run_id or getattr(self, "_plan_session", "") or ""
        if parent_is_subagent is None:
            prow = self.store.one("select kind from agents where id=?", (parent_id,)) if parent_id else None
            parent_is_subagent = (_truthy(form.get("subagent")) or bool(prow and prow.get("kind") == "subagent")
                                  or str(parent_id).startswith(SUBAGENT_SESSION_PREFIX)
                                  or str(getattr(self, "_plan_session", "")).startswith(SUBAGENT_SESSION_PREFIX))
        with _SUBAGENT_LOCK:                      # check-and-insert must be atomic
            live = self.store.one("select count(*) n from agents where kind='subagent' "
                                  "and status in ('queued','running')")["n"]
            err = subagent_gate(parent_is_subagent, live)
            if err:
                raise ValueError(err)
            model, ep = self.resolve_subagent_model(args.get("model"))
            pmode = self._phone_pmode(args.get("permission_mode"))
            aid = "sub_" + os.urandom(6).hex()
            title = (args.get("title") or prompt).strip()[:60]
            self.store.exec(
                """insert into agents(id,kind,ref_id,parent_id,title,prompt,status,session_id,model,endpoint_id,
                                      workdir,permission_mode,notify,progress,created_at)
                   values(?,'subagent','',?,?,?,'queued',?,?,?,?,?,?,'Queued',?)""",
                (aid, parent_id or "", title, prompt, SUBAGENT_SESSION_PREFIX + aid, model, ep,
                 self._phone_folder((args.get("folder") or args.get("workdir") or "").strip()), pmode,
                 1 if _truthy(args.get("notify")) else 0, now_iso()))
        threading.Thread(target=self._run_agent_job, args=(aid,), daemon=True).start()
        return {"id": aid, "title": title, "model": model, "parent_id": parent_id or ""}

    def spawn_subagent_tool(self, body):
        made = self.spawn_subagent(self.parse_tool_args(body))
        return json.dumps({"ok": True, "spawned": 1, "agent": made,
                           "note": "subagent running independently - check it with manage_agents "
                                   "(status/result); it cannot spawn further subagents"}, ensure_ascii=False)

    def _close_if_live(self, aid, status, result="", error=""):
        """Terminal write that never overwrites an earlier terminal status (the
        watchdog and the worker may both try). Returns True if this call closed it."""
        with _CLOSE_LOCK:
            row = self.store.one("select status from agents where id=?", (aid,))
            if not row or self.agent_status(row) not in ("queued", "running"):
                return False
            self.agent_close(aid, status, result=result, error=error)
            return True

    def _subagent_done(self, aid):
        """A subagent just reached a terminal status. If its parent is an agent row
        that is already finished, nobody will read it: remove it."""
        row = self.store.one("select parent_id from agents where id=?", (aid,)) or {}
        prow = self.store.one("select status from agents where id=?", (row.get("parent_id") or "",))
        if prow and self.agent_status(prow) not in ("queued", "running"):
            self.reap_subagent(aid)

    def _run_agent_job(self, aid):
        """Never leaves a subagent live: see _run_agent_job_inner. try/finally here
        is the guarantee; the watchdog covers a hang."""
        is_sub = (self.store.one("select kind from agents where id=?", (aid,)) or {}).get("kind") == "subagent"
        timer = None
        if is_sub:
            def _expire():
                if self._close_if_live(aid, "failed", error=f"timed out after {int(SUBAGENT_TIMEOUT)}s"):
                    self.store.exec("update agents set cancel_requested=1 where id=?", (aid,))
                    request_cancel(run_id=aid)     # worker stops at its next round
                    self._subagent_done(aid)
            timer = threading.Timer(SUBAGENT_TIMEOUT, _expire)
            timer.daemon = True
            timer.start()
        try:
            self._run_agent_job_inner(aid)
        except BaseException as exc:                # noqa: B902 - nothing may skip the finaliser
            self._close_if_live(aid, "failed", error=("worker crashed: %r" % (exc,))[:500])
        finally:
            if timer:
                timer.cancel()
            row = self.store.one("select status from agents where id=?", (aid,))
            if row and self.agent_status(row) in ("queued", "running"):
                self._close_if_live(aid, "failed", error="worker exited without a terminal status")
            try:
                if is_sub:
                    self._subagent_done(aid)
                else:
                    st = self.agent_status(self.store.one("select status from agents where id=?", (aid,)) or {})
                    self.reap_subagents_of(aid, cancel_live=st in ("failed", "cancelled"))
            except Exception:
                pass

    def _run_agent_job_inner(self, aid):
        """One background agent, start to finish. Runs on its own Handler so it
        never touches the request-scoped state of whoever spawned it."""
        import types as _types
        from joebro_backend import Handler
        h = Handler.__new__(Handler)
        h.server = _types.SimpleNamespace(store=self.store)
        is_sub = (h.store.one("select kind from agents where id=?", (aid,)) or {}).get("kind") == "subagent"
        import contextlib
        with (contextlib.nullcontext() if is_sub else _AGENT_SLOTS):   # subagents: capped at spawn time
            row = h.store.one("select * from agents where id=?", (aid,))
            if not row or h.agent_status(row) not in ("queued", "running"):
                return                          # cancelled or already handled while queued
            if h.agent_run_cancelled(aid):
                h._close_if_live(aid, "cancelled", error="cancelled before it started")
                return
            sid = row.get("session_id") or ("agent_session_" + aid)
            h._agent_run_id = aid               # lets run_agent check for cancellation
            h.agent_started(aid)
            h.agent_progress(aid, "Working")
            try:
                if not h.store.one("select id from sessions where id=?", (sid,)):
                    h.store.exec(
                        "insert into sessions(id,name,model,endpoint_url,endpoint_id,workdir,mode,created_at)"
                        " values(?,?,?,?,?,?,?,?)",
                        (sid, "[AGENT] " + (row.get("title") or "Agent"), row.get("model") or "", "",
                         row.get("endpoint_id") or "", row.get("workdir") or "", "agent", now_iso()))
                prompt = row.get("prompt") or ""
                h.add_message(sid, "user", prompt, {})
                f = {"session": sid, "message": prompt, "model": row.get("model") or "",
                     "endpoint_id": row.get("endpoint_id") or "",
                     "permission_mode": row.get("permission_mode") or "sandbox",
                     "max_tokens": "4000", "allow_web_search": "true"}
                if row.get("workdir"):
                    f["workdir"] = row["workdir"]
                if is_sub:
                    f["subagent"] = "true"      # leaf: tools_for withholds the spawn tools
                f["mode"] = "agent"            # forced last, same guarantee as tasks
                reply, thinking, events = h.run_agent(f)
                meta = {"model": row.get("model") or ""}
                if events:
                    meta["tool_events"] = [{k: v for k, v in e.items() if k != "doc"} for e in events]
                cur = h.store.one("select status from agents where id=?", (aid,))
                if not cur or h.agent_status(cur) not in ("queued", "running"):
                    return                      # timed out / reaped meanwhile: don't resurrect its session
                h.add_message(sid, "assistant", reply, meta)
                if h.agent_run_cancelled(aid):
                    h._close_if_live(aid, "cancelled", result=(reply or "").strip(),
                                     error="stopped on request; partial output kept")
                    return
                if not h._close_if_live(aid, "done", result=(reply or "").strip()):
                    return
                if row.get("notify"):
                    h.notify((reply or "").strip()[:2000] or "(no output)",
                             title="Done: " + (row.get("title") or "Agent"), kind="agent", source=aid)
            except Exception as exc:
                h._close_if_live(aid, "failed", error=str(exc)[:500])
                if row.get("notify"):
                    h.notify(f"Agent failed: {exc}"[:500],
                             title="Failed: " + (row.get("title") or "Agent"), kind="agent", source=aid)

    def agent_brief(self, r):
        """Compact shape for the AI tool. The HTTP API uses agent_record."""
        rec = self.agent_record(r)
        return {k: rec[k] for k in ("id", "kind", "title", "status", "progress",
                                    "session_id", "parent_id", "created_at", "finished_at",
                                    "result", "error")}

    def manage_agents_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").strip().lower()
        aid = (args.get("id") or "").strip()
        if action == "list":
            rows = self.store.rows("select * from agents order by datetime(created_at) desc limit 30")
            return json.dumps({"ok": True, "agents": [self.agent_brief(r) for r in rows],
                               "running": sum(1 for r in rows if r.get("status") == "running")},
                              ensure_ascii=False)
        if action in ("status", "result"):
            if not aid:
                raise ValueError("manage_agents needs an id for that action — call list first")
            row = self.store.one("select * from agents where id=?", (aid,))
            if not row:
                raise ValueError(f"no agent {aid}")
            out = self.agent_brief(row)
            if action == "result":
                out["result"] = row.get("result") or ""      # full text, not the preview
                # The parent has now been handed the full result: a finished subagent
                # is spent, so remove it (row + its session). reap refuses live ones.
                if row.get("kind") == "subagent" and self.reap_subagent(aid):
                    out["removed"] = True
                    out["note"] = "subagent finished and its result delivered; its registry row was removed"
            return json.dumps({"ok": True, "agent": out}, ensure_ascii=False)
        if action == "stop":
            if not aid:
                raise ValueError("manage_agents stop needs an id")
            out, _code = self.agent_cancel(aid)
            return json.dumps(out, ensure_ascii=False)
        if action == "clear":
            for r in self.store.rows("select id from agents where kind='subagent' and status in ('done','error','stopped','failed','cancelled')"):
                self.reap_subagent(r["id"])
            self.store.exec("delete from agents where status in ('done','error','stopped','failed','cancelled')")
            return json.dumps({"ok": True, "cleared": True})
        raise ValueError(f"unknown manage_agents action '{action}' (list, status, result, stop, clear)")

    def spawn_agent_tool(self, body):
        if _truthy((getattr(self, "_turn_form", None) or {}).get("subagent")):
            raise ValueError("subagents cannot spawn agents")
        made = self.spawn_agents(self.parse_tool_args(body))
        return json.dumps({"ok": True, "spawned": len(made), "agents": made,
                           "note": "running in the background — they report back on their own; "
                                   "use manage_agents to check on them"}, ensure_ascii=False)

    def notify_me_tool(self, body):
        args = self.parse_tool_args(body)
        msg = (args.get("message") or args.get("text") or args.get("body") or "").strip()
        if not msg:
            raise ValueError("notify_me needs a message")
        session = (getattr(self, "_turn_form", None) or {}).get("session")
        title = (args.get("title") or "").strip()
        if session == PHONE_SESSION_ID:
            # phone agent: the chat line IS the notification, word for word
            self.add_message(session, "assistant", msg, {"notice": True, "source": "notify_me", "task_title": title})
        out = self.notify(msg, title=title, kind="note", source="tool",
                          channel=args.get("channel") or "auto", session=session)
        return json.dumps(out, ensure_ascii=False)

    # ---- the pulse -------------------------------------------------------

    def pulse_config(self):
        return {
            "enabled": bool(self.pref("pulse_enabled")),
            "interval_minutes": int(self.pref("pulse_interval_minutes") or 60),
            "checklist": self.pref("pulse_checklist") or PULSE_DEFAULT_CHECKLIST,
            "quiet_hours": self.pref("pulse_quiet_hours") or "23:00-07:30",
            "model": self.pref("pulse_model") or self.pref("default_model") or "",
            "endpoint_id": self.pref("pulse_endpoint_id") or self.pref("default_endpoint_id") or "",
            "last_run": self.pref("pulse_last_run") or "",
            "last_spoke": self.pref("pulse_last_spoke") or "",
        }

    @staticmethod
    def _in_quiet_hours(spec, now=None):
        """'23:00-07:30' — true inside the window, wrapping past midnight."""
        try:
            start, end = [s.strip() for s in str(spec or "").split("-", 1)]
            now = (now or datetime.now().astimezone()).strftime("%H:%M")
            if start == end:
                return False
            return (start <= now < end) if start < end else (now >= start or now < end)
        except Exception:
            return False

    def pulse_due(self, now=None):
        cfg = self.pulse_config()
        if not cfg["enabled"]:
            return False
        if self._in_quiet_hours(cfg["quiet_hours"], now):
            return False
        last = cfg["last_run"]
        if not last:
            return True
        try:
            gap = (now or datetime.now().astimezone()) - datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            return gap.total_seconds() >= max(5, cfg["interval_minutes"]) * 60
        except Exception:
            return True

    def _pulse_recent_reports(self, limit=6):
        rows = self.store.rows(
            "select body, created_at from inbox where kind in ('pulse','heartbeat') order by datetime(created_at) desc limit ?",
            (int(limit),))
        return "\n".join(f"- [{(r.get('created_at') or '')[:16]}] {(r.get('body') or '')[:200]}" for r in rows)

    def manage_pulse_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "get").strip().lower()
        if action == "get":
            return json.dumps({"ok": True, "pulse": self.pulse_config()}, ensure_ascii=False)
        if action in ("pause", "off", "disable"):
            self.set_pref("pulse_enabled", False)
            return json.dumps({"ok": True, "enabled": False})
        if action in ("resume", "on", "enable", "start"):
            self.set_pref("pulse_enabled", True)
            return json.dumps({"ok": True, "enabled": True, "pulse": self.pulse_config()}, ensure_ascii=False)
        if action == "run":
            return json.dumps(self.run_pulse(force=True), ensure_ascii=False)
        if action == "set":
            if args.get("checklist"):
                self.set_pref("pulse_checklist", str(args["checklist"]))
            if args.get("add"):
                cur = self.pref("pulse_checklist") or PULSE_DEFAULT_CHECKLIST
                self.set_pref("pulse_checklist", cur.rstrip() + "\n- " + str(args["add"]).lstrip("- ").strip() + "\n")
            if args.get("interval_minutes"):
                mins = int(args["interval_minutes"])
                if mins < 5:
                    raise ValueError("a pulse more often than every 5 minutes is a pulse you'll mute")
                self.set_pref("pulse_interval_minutes", mins)
            if args.get("quiet_hours"):
                self.set_pref("pulse_quiet_hours", str(args["quiet_hours"]))
            if args.get("model"):
                self.set_pref("pulse_model", str(args["model"]))
            # Editing the checklist never switches the pulse on: that is the
            # user's call (resume, or Settings > Mobile on the Mac).
            out = {"ok": True, "pulse": self.pulse_config()}
            if not out["pulse"]["enabled"]:
                out["note"] = "saved, but the pulse is off; it runs only after the user turns it on (action resume)"
            return json.dumps(out, ensure_ascii=False)
        raise ValueError(f"unknown manage_pulse action '{action}' (get, set, run, pause, resume)")


def pulse_loop(server):
    """Daemon: once a minute, run the heartbeat and the pulse if they are due.
    Both are off until the user turns them on, so on a fresh install this only
    wakes up, reads two prefs and goes back to sleep."""
    import types as _types
    from joebro_backend import Handler
    time.sleep(20)                      # let the backend finish booting
    while True:
        try:
            h = Handler.__new__(Handler)
            h.server = _types.SimpleNamespace(store=server.store)
            try:
                h.run_heartbeat()
            except Exception:
                pass  # a heartbeat failure must not prevent the pulse
            h.run_pulse()           # gated inside: off, quiet hours, backoff, daily cap
        except Exception:
            pass
        time.sleep(60)
