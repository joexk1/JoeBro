"""The briefing: a scheduled job that runs itself and leaves a result to load.

No client prompts this. A loop checks every few minutes whether it is due,
opens a run in the agent registry (kind=task) like anything else, and writes a
briefing a client can poll for.

The important design choice: **the facts are gathered in code, not by the
model.** Every section below is read out of the database or IMAP first, and the
model is given only that text and asked to write it up. It cannot invent a
meeting, because it never queries the calendar — and when a source is missing or
stale, the fact-gatherer says so in the text the model must work from, with an
instruction to pass that on rather than paper over it.

Strictly read-only: it lists mail with IMAP `readonly`/`BODY.PEEK` (so nothing
is marked seen), reads the calendar mirror, and never calls a write path.
"""
import traceback

from jb_core import *  # noqa: F401,F403

BRIEFING_REF = "briefing"            # ref_id marking a registry run as a briefing
BRIEFING_ID = "brf_current"          # THE briefing. One row, rewritten in place.
_BRIEFING_LOCK = threading.Lock()    # one at a time, even if a manual run races the loop

SYSTEM = (
    "You write a short standing briefing for one person, from facts already gathered for you.\n\n"
    "Absolute rules:\n"
    "1. Use ONLY the facts below. Never add a meeting, email, deadline or number that is not there.\n"
    "2. Where a line says a source was unavailable, stale or not configured, SAY THAT plainly in "
    "your briefing. Do not quietly omit it and do not guess what it might have contained — the "
    "person needs to know their briefing is partial.\n"
    "3. If there is genuinely little to report, say so in a sentence. Do not pad.\n\n"
    "Style: lead with what matters soonest. Short paragraphs or tight bullets, no headings unless "
    "there are several distinct areas, no preamble, no sign-off, no 'here is your briefing'. "
    "Write as if leaning over and telling them. Be specific with times and names.\n"
    "If coding agents worked since last time, include a short 'your agents' part: anything waiting "
    "on the user first, then one line per finished agent saying what it got done, then any that went quiet."
)


class BriefingMixin:
    # ---- gathering ---------------------------------------------------------

    @staticmethod
    def _event_local(value, all_day):
        """(local date, local HH:MM or None) for a stored event time.

        Everything in `events` is stored UTC with a Z (the mirror and CalDAV both
        do that). Printing those characters straight out is how a shift at 18:00
        Berlin got reported as 16:00, and how an all-day event stored as
        22:00Z the night before got reported as the wrong day. So: parse, convert
        to the machine's local zone, and only then format."""
        raw = (value or "").strip()
        if not raw:
            return "", None
        if "T" not in raw:                      # already a bare date — all-day, correct
            return raw[:10], None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw[:10], None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d"), (None if all_day else local.strftime("%H:%M"))

    def briefing_calendar_facts(self):
        """Today and the next 48h from whatever source is actually configured,
        with the provenance stated. Never silently empty."""
        cfg = self.pref("calendar_connection") or {}
        kind = (cfg.get("type") or "").strip().lower()
        if not kind:
            return "CALENDAR: no calendar source is connected, so this briefing has no calendar in it."
        now = datetime.now().astimezone()
        days = {(now + timedelta(days=d)).strftime("%Y-%m-%d") for d in (0, 1, 2)}
        # Fetch a day wider on each side in UTC terms, then filter on LOCAL dates —
        # a UTC-date SQL window silently drops or adds an evening's events.
        lo = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        hi = (now + timedelta(days=3)).strftime("%Y-%m-%d")
        if kind == "caldav":
            rows = self.store.rows(
                "select * from events where caldav_href != '' and substr(dtstart,1,10) between ? and ? "
                "order by dtstart", (lo, hi))
            src = f"CalDAV ({cfg.get('display') or 'server'})"
        else:
            rows = self.store.rows(
                "select * from events where caldav_href = '' and substr(dtstart,1,10) between ? and ? "
                "order by dtstart", (lo, hi))
            pushed = (self.pref("calendar_mirror_pushed_at")
                      or (self.store.one("select max(created_at) as t from events where caldav_href = ''")
                          or {}).get("t") or "")
            src = f"the Mac's EventKit mirror (last pushed {self._ago(pushed)})"
            if self._stale(pushed, hours=6):
                src += (" — WARNING: that is not current, so this calendar may be missing new events "
                        "and still showing ones that were deleted. Say so.")
        shown = []
        for r in rows:
            day, hhmm = self._event_local(r.get("dtstart"), r.get("all_day"))
            if day in days:
                shown.append((day, hhmm, r))
        if not shown:
            return (f"CALENDAR: source is {src}. Nothing scheduled between now and "
                    f"{(now + timedelta(days=2)).strftime('%A %d %B')}.")
        lines = [f"CALENDAR: source is {src}. All times below are LOCAL "
                 f"({now.strftime('%Z') or now.strftime('%z')}). Next 48h:"]
        for day, hhmm, r in shown[:25]:
            when = f"{day} {hhmm}" if hhmm else f"{day} (all day)"
            where = f" @ {r['location']}" if (r.get("location") or "").strip() else ""
            lines.append(f"- {when} {r.get('summary') or 'Untitled'}{where}")
        return "\n".join(lines)

    def briefing_reminder_facts(self):
        result = self.apple_reminders_list({"limit": 20}, refresh=True)
        if not result.get("connected") or not result.get("synced"):
            return "APPLE REMINDERS: unavailable. Full Reminders access has not been enabled in the iPhone app."
        prefix = "APPLE REMINDERS"
        if result.get("stale"): prefix += " (cached; last iPhone sync: " + str(result.get("last_synced_at") or "never") + "; say this source is not current)"
        rows = result.get("reminders") or []
        if not rows: return prefix + ": none pending."
        return prefix + " pending:\n" + "\n".join(
            f"- {r.get('due_at') or r.get('due_date') or 'No due date'} {r['text']} [{r.get('list_name') or 'Reminders'}]" for r in rows)

    def briefing_inbox_facts(self, since):
        rows = self.store.rows(
            "select * from inbox where datetime(created_at) > datetime(?) "
            "order by datetime(created_at) desc limit 15", (since,)) if since else self.inbox_rows(limit=10)
        if not rows:
            return "SINCE LAST BRIEFING: nothing new arrived in the inbox."
        return "SINCE LAST BRIEFING, these arrived:\n" + "\n".join(
            f"- [{r.get('kind')}] {(r.get('title') or '').strip()} {(r.get('body') or '')[:200]}"
            for r in rows)

    def briefing_agent_facts(self):
        rows = self.store.rows(
            "select * from agents where ref_id != ? order by datetime(created_at) desc limit 12",
            (BRIEFING_REF,))
        live = [r for r in rows if self.agent_status(r) in ("queued", "running")]
        bad = [r for r in rows if self.agent_status(r) == "failed"]
        bits = []
        if live:
            bits.append("Still running: " + "; ".join(
                f"{r.get('title')} ({r.get('kind')}, {r.get('progress') or 'no progress reported'})"
                for r in live))
        if bad:
            bits.append("Recently FAILED: " + "; ".join(
                f"{r.get('title')} ({r.get('error') or 'no error recorded'})" for r in bad[:4]))
        return "AGENTS: " + ("; ".join(bits) if bits else "nothing running, nothing failed.")

    def briefing_coding_facts(self, since=""):
        """Coding agents (Claude Code / Codex / OpenCode) since the last briefing:
        what finished, what is waiting on the user, what went quiet mid-task."""
        try:
            self._coding_table()
        except Exception:
            return "CODING AGENTS: not set up."
        import time as _t
        cutoff = _t.time() - 16 * 3600
        if since:
            try:
                cutoff = max(cutoff, datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        rows = self.store.rows("SELECT * FROM coding_agents WHERE last_activity>? ORDER BY last_activity DESC LIMIT 20", (cutoff,))
        if not rows:
            return "CODING AGENTS: none worked since the last briefing."
        now = _t.time()

        def line(r):
            detail = json.loads(r.get("detail_json") or "[]")
            last = next((d for d in reversed(detail) if d.startswith(("✓", "⏸", "›"))), detail[-1] if detail else "")
            return f"{r.get('name')} ({r['source']} on {r['host']}, {r.get('tool_calls') or 0} tool calls): {last[:180]}"
        waiting = [r for r in rows if r["status"] == "waiting"]
        quiet = [r for r in rows if r["status"] == "running" and now - r["last_activity"] > 25 * 60]
        done = [r for r in rows if r["status"] in ("idle", "ended")]
        busy = [r for r in rows if r["status"] == "running" and r not in quiet]
        bits = []
        for label, group in (("WAITING ON HIM (approval)", waiting), ("Went quiet mid-task", quiet),
                             ("Finished", done), ("Still working", busy)):
            if group:
                bits.append(label + ":\n" + "\n".join("- " + line(r) for r in group[:6]))
        return "CODING AGENTS since last briefing:\n" + "\n".join(bits)

    def briefing_email_facts(self):
        """Best-effort and read-only. A mail server being down is a fact to
        report, not a reason to fail the briefing."""
        try:
            if not self.email_sources():
                return "EMAIL: no mail account is connected, so this briefing has no email in it."
            data = json.loads(self.email_list_payload({"unread_only": True, "limit": 8}))
        except Exception as exc:
            return f"EMAIL: could not be read this time ({str(exc)[:120]}) — treat email as unchecked."
        mails = data.get("emails") or []
        if not mails:
            return "EMAIL: no unread mail."
        lines = [f"EMAIL: {data.get('total', len(mails))} unread. Most recent:"]
        for m in mails[:8]:
            frm = (m.get("from") or m.get("sender") or "")[:60]
            lines.append(f"- {frm}: {(m.get('subject') or '(no subject)')[:120]}")
        return "\n".join(lines)

    # ---- small helpers ----------------------------------------------------

    @staticmethod
    def _parse_iso(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    @classmethod
    def _ago(cls, value):
        dt = cls._parse_iso(value)
        if not dt:
            return "never"
        secs = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        if secs < 90:
            return "just now"
        if secs < 5400:
            return f"{int(secs / 60)} minutes ago"
        if secs < 172800:
            return f"{int(secs / 3600)} hours ago"
        return f"{int(secs / 86400)} days ago"

    @classmethod
    def _stale(cls, value, hours):
        dt = cls._parse_iso(value)
        if not dt:
            return True
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > hours * 3600

    # ---- the run ----------------------------------------------------------

    def briefing_config(self):
        return {
            "enabled": self.pref("briefing_enabled") is True,          # opt-in only
            "interval_hours": float(self.pref("briefing_interval_hours") or 2),
            "quiet_hours": self.pref("briefing_quiet_hours") or "23:30-06:30",
            "last_run": self.pref("briefing_last_run") or "",
            "notify": bool(self.pref("briefing_notify")),              # default OFF: it's pull, not push
        }

    def briefing_due(self, now=None):
        cfg = self.briefing_config()
        if not cfg["enabled"]:
            return False
        now = now or datetime.now().astimezone()
        if self._in_quiet_hours(cfg["quiet_hours"], now):
            return False
        last = self._parse_iso(cfg["last_run"])
        if not last:
            return True
        gap = (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
        return gap >= max(0.25, cfg["interval_hours"]) * 3600

    def run_briefing(self, force=False):
        """Gather, write, record. Returns the registry record."""
        if not _BRIEFING_LOCK.acquire(blocking=False):
            return {"ok": False, "detail": "a briefing is already being written"}
        try:
            if not force and not self.briefing_due():
                return {"ok": True, "ran": False, "detail": "not due"}
            cfg = self.briefing_config()
            ep, model = self._task_endpoint()
            self.prune_briefing_rows()
            aid = self.agent_upsert(BRIEFING_ID, "briefing", "Daily briefing", ref_id=BRIEFING_REF,
                                    model=model, endpoint_id=ep, progress="Gathering",
                                    notify=1 if cfg["notify"] else 0)
            # Claim the slot before the slow part: a crash mid-write must not put
            # the loop into a retry spin every few minutes.
            self.set_pref("briefing_last_run", now_iso())
            if not ep or not model:
                self.agent_close(aid, "failed", error="no model endpoint configured")
                return {"ok": False, "detail": "no model endpoint configured", "id": aid}
            self.agent_started(aid)
            # One row means no previous row to read a timestamp from; the pref is
            # the high-water mark for "what the user has already been told".
            since = self.pref("briefing_last_reported_at") or ""
            sections = []
            for label, fn in (("calendar", self.briefing_calendar_facts),
                              ("reminders", self.briefing_reminder_facts),
                              ("inbox", lambda: self.briefing_inbox_facts(since)),
                              ("agents", self.briefing_agent_facts),
                              ("coding agents", lambda: self.briefing_coding_facts(since)),
                              ("night shift", self.briefing_night_facts),
                              ("email", self.briefing_email_facts),
                              ("weather", self.briefing_weather_facts)):
                self.agent_progress(aid, f"Reading {label}")
                try:
                    sections.append(fn())
                except Exception as exc:
                    sections.append(f"{label.upper()}: could not be read ({str(exc)[:120]}).")
                if self.agent_run_cancelled(aid):
                    self.agent_close(aid, "cancelled", error="stopped while gathering")
                    return {"ok": True, "id": aid, "status": "cancelled"}
            now = datetime.now().astimezone()
            facts = (f"It is {now.strftime('%A %d %B %Y, %H:%M')}.\n\n" + "\n\n".join(sections))
            self.agent_progress(aid, "Writing it up")
            text = self._complete(ep, model, SYSTEM, facts, max_tokens=900)
            if not (text or "").strip():
                self.agent_close(aid, "failed", result=facts,
                                 error="the model returned nothing; raw facts kept as the result")
                return {"ok": False, "id": aid, "detail": "model returned nothing"}
            self.agent_close(aid, "done", result=text.strip())
            self.set_pref("briefing_last_reported_at", now_iso())
            if cfg["notify"]:
                self.notify(text.strip()[:1500], title="☀️ Briefing", kind="briefing", source=aid)
            return {"ok": True, "ran": True, "id": aid,
                    "agent": self.agent_record(self.store.one("select * from agents where id=?", (aid,)),
                                               full=True)}
        finally:
            _BRIEFING_LOCK.release()

    def prune_briefing_rows(self):
        """Collapse to the one singleton row.

        Earlier builds created a new row per run, and a client used to spawn its
        own "Daily Briefing" as a plain chat agent — so the list filled up with
        stale duplicates. Anything that is a briefing but is not THE row goes."""
        self.store.exec(
            "delete from agents where id != ? and (ref_id=? or lower(title) in "
            "('daily briefing','briefing'))", (BRIEFING_ID, BRIEFING_REF))

    def latest_briefing_row(self, exclude=""):
        """The singleton, whatever state it is in. Its `result` is the last good
        briefing even while a new run is being written."""
        row = self.store.one("select * from agents where id=?", (BRIEFING_ID,))
        if row and row["id"] != exclude:
            return row
        return None

    def latest_briefing_payload(self):
        """What a client polls. Always answers; says plainly when there isn't one
        yet rather than 404-ing."""
        row = self.latest_briefing_row()
        running = row if row and self.agent_status(row) in ("queued", "running") else None
        cfg = self.briefing_config()
        try:
            cards = self.night_review_cards()       # Night Shift review cards ride along with the briefing
        except Exception:
            cards = []
        return {
            "ok": True, "cards": cards,
            "briefing": self.agent_record(row, full=True) if row else None,
            "age": self._ago((row or {}).get("finished_at")) if row else "never",
            "writing_now": self.agent_record(running) if running else None,
            "schedule": {"enabled": cfg["enabled"], "every_hours": cfg["interval_hours"],
                         "quiet_hours": cfg["quiet_hours"], "last_run": cfg["last_run"]},
        }


def briefing_loop(server):
    """Daemon: check every 5 minutes whether a briefing is due.

    Interval-based rather than clock-based, so a restart can never make it skip
    a slot — it simply runs on the next tick after it falls due. That is the fix
    for the old behaviour, where a task fired only if a tick happened to land
    inside one exact minute."""
    import types as _types
    from joebro_backend import Handler
    time.sleep(45)                 # let the backend settle before the first one
    while True:
        try:
            h = Handler.__new__(Handler)
            h.server = _types.SimpleNamespace(store=server.store)
            if h.briefing_due():
                h.run_briefing()
        except Exception:
            traceback.print_exc()
        time.sleep(300)
