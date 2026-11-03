"""Proactive mode, confirmed-date->calendar, shopping, health, coding agents, desktop_use.
Real SQLite store in a temp dir; model, calendar server and push are stubbed."""
import json, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jb_core import Store
from jb_heartbeat import HeartbeatMixin
from jb_proactive import ProactiveMixin
from jb_coding import CodingAgentsMixin
from jb_desktop import DesktopUseMixin
import jb_coding

TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc


class F(ProactiveMixin, HeartbeatMixin, CodingAgentsMixin, DesktopUseMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
        self.created, self.deleted, self.notes, self.msgs = [], [], [], []
        self.model_reply = '{"suggestions":[]}'
        self.snapshot = {}
        self.set_pref("background_model", "m"); self.set_pref("background_endpoint_id", "e")
    def pref(self, k):
        r = self.store.one("select value from prefs where key=?", (k,))
        return json.loads(r["value"]) if r else None
    def set_pref(self, k, v): self.store.exec("insert or replace into prefs(key,value) values(?,?)", (k, json.dumps(v)))
    def parse_tool_args(self, b): return json.loads(b) if b else {}
    def create_event_result(self, body):
        uid = "ev%d" % len(self.created); self.created.append(body)
        self.store.exec("insert into events(id,summary,dtstart,dtend,all_day,location,description,created_at) values(?,?,?,?,?,?,?,?)",
                        (uid, body["summary"], body["dtstart"], body["dtend"], int(body["all_day"]), "", "", "x"))
        return {"ok": True, "uid": uid}
    def delete_event_row(self, row, uid=None):
        self.deleted.append(uid); self.store.exec("delete from events where id=?", (uid,))
    def notify(self, *a, **k): self.notes.append((a, k)); return {"delivered": True}
    def add_message(self, sid, role, content, meta=None):
        self.store.exec("insert into messages(session_id,role,content,metadata,created_at) values(?,?,?,?,?)",
                        (sid, role, content, json.dumps(meta or {}), "x"))
    def bg_complete(self, system, user, max_tokens=300):
        self.last_system = system; return self.model_reply
    def _heartbeat_snapshot(self, now): return self.snapshot
    def phone_post(self, text, source, title="", **k): self.msgs.append((title, text))


def rec(text, mine=True, ident="docs:d1", source="docs"):
    return {ident: {"source": source, "id": ident, "title": "Notes", "text": text,
                    "from": "me" if mine else "Sam", "date": "2026-11-02T10:00:00+01:00"}}


class Proactive(unittest.TestCase):
    """The production background pass: records -> one bg_complete -> validated actions."""
    def setUp(self):
        self.f = F()
        self.f.posts = []
        self.f.phone_post = lambda text, source, title="", **k: self.f.posts.append((title, text))
        self.when = (datetime.now(TZ) + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)

    def run_pass(self, records, events=None, shopping=None):
        self.f.proactive_records = lambda now: records
        key = next(iter(records))
        self.f.model_reply = json.dumps({"events": events or [], "shopping": shopping or []}).replace("KEY", key)
        return self.f.proactive_pass(force=True)

    def ev(self, quote, start=None):
        return [{"title": "Dinner with Sam", "start": (start or self.when).isoformat(), "end": None, "all_day": False,
                 "location": "Cafe X", "source_id": "KEY", "quote": quote}]

    def test_full_mode_adds_once_and_undo(self):
        self.f.set_proactive_mode("full")
        text = "Booking confirmed: Dinner Thursday at 19:00, table for 2"
        r = self.run_pass(rec(text, ident="email:1", source="email"), self.ev("Dinner Thursday at 19:00"))
        self.assertEqual(len(self.f.created), 1, r)
        self.assertEqual(len(self.f.notes), 1)
        self.run_pass(rec(text + " (updated)", ident="email:1", source="email"), self.ev("Dinner Thursday at 19:00"))
        self.assertEqual(len(self.f.created), 1)   # same plan -> no duplicate
        aid = r["result"]["events_added"][0]["action_id"]
        self.f.proactive_undo(aid)
        self.assertEqual(len(self.f.deleted), 1)

    def test_rejects_fabricated_quote_and_past(self):
        self.f.set_proactive_mode("full")
        self.run_pass(rec("maybe dinner sometime?"), self.ev("Dinner Thursday at 19:00 works"))
        self.run_pass(rec("Dinner yesterday 19:00 was great", ident="docs:d2"),
                      self.ev("Dinner yesterday 19:00", start=datetime.now(TZ) - timedelta(days=1)))
        self.assertEqual(self.f.created, [])

    def test_read_only_suggests_then_approve_executes(self):
        self.f.set_proactive_mode("read_only")
        self.run_pass(rec("Dinner Thursday at 19:00 works for me"), self.ev("Dinner Thursday at 19:00 works"))
        self.assertEqual(self.f.created, [])
        listed = self.f.proactive_tool('{"action":"list"}')
        self.assertIn("[suggested]", listed)
        aid = listed.split()[0]
        self.f.proactive_approve(aid)
        self.assertEqual(len(self.f.created), 1)

    def test_off_mode_does_nothing(self):
        self.f.set_proactive_mode("off")
        r = self.run_pass(rec("Dinner Thursday at 19:00 works for me"), self.ev("Dinner Thursday at 19:00 works"))
        self.assertFalse(r["ran"]); self.assertEqual(self.f.created, [])

    def test_shopping_from_own_words_only(self):
        self.f.set_proactive_mode("full")
        self.run_pass(rec("ugh we need oat milk and eggs", ident="docs:d3"),
                      shopping=[{"item": "oat milk", "source_id": "KEY", "quote": "need oat milk"}])
        self.run_pass(rec("buy me a ferrari", mine=False, ident="email:4", source="email"),
                      shopping=[{"item": "ferrari", "source_id": "KEY", "quote": "buy me a ferrari"}])
        self.assertEqual([i["text"] for i in self.f.shopping_items()["items"]], ["oat milk"])


class Lists(unittest.TestCase):
    def test_shopping_crud(self):
        f = F()
        f.shopping_update({"action": "add", "item": "bread, milk; apples"})
        f.shopping_update({"action": "add", "item": "Milk"})          # dup ignored
        f.shopping_update({"action": "check", "item": "milk"})
        items = f.shopping_items()["items"]
        self.assertEqual(len(items), 3); self.assertTrue(items[-1]["checked"])
        self.assertIn("2 to get", f.shopping_list_tool('{"action":"list"}'))
        f.shopping_update({"action": "clear_checked"})
        self.assertEqual(len(f.shopping_items()["items"]), 2)

    def test_health(self):
        f = F(); today = datetime.now(TZ).date()
        days = [{"day": (today - timedelta(days=i)).isoformat(), "steps": 10000 if i < 7 else 5000, "sleep_hours": 7.5,
                 "workouts": [{"type": "run", "minutes": 30, "kcal": 300}] if i == 1 else []} for i in range(14)]
        self.assertEqual(f.health_ingest({"days": days})["saved_days"], 14)
        s = f.health_summary()
        self.assertEqual(s["metrics"]["steps"]["change_pct"], 100)
        self.assertIn("Steps 10,000", f.health_text(s))
        f.set_proactive_mode("read_only")
        sunday = datetime(2026, 11, 8, 19, 0, tzinfo=TZ)
        self.assertIsNotNone(f.health_weekly_review(sunday)); self.assertIsNone(f.health_weekly_review(sunday))


class Coding(unittest.TestCase):
    def test_state_machine_and_records(self):
        f = F()
        base = {"source": "claude", "host": "pc", "session_id": "abc", "cwd": "/home/joe/projects/wudland", "kitty_window_id": "3"}
        f.coding_event(dict(base, event="start"))
        f.coding_event(dict(base, event="prompt", text="fix the island"))
        f.coding_event(dict(base, event="tool", tool="Edit", text="Island.swift"))
        recs = f.coding_agent_records()
        self.assertEqual(recs[0]["kind"], "coding"); self.assertTrue(recs[0]["live"]); self.assertIn("wudland", recs[0]["title"])
        f.coding_event(dict(base, event="waiting", text="Bash: rm -rf build"))
        self.assertTrue(f.coding_nodes()[0]["waiting"])
        f.coding_event(dict(base, event="stop", text="done"))
        self.assertFalse(f.coding_agent_records()[0]["live"])
        self.assertIn("kitty window 3", f.coding_agents_tool('{"action":"list"}'))
        with self.assertRaises(ValueError): f.coding_event({"source": "x", "host": "pc", "session_id": "a", "event": "stop"})

    def test_stale_running(self):
        f = F()
        f.coding_event({"source": "codex", "host": "mac", "session_id": "s", "event": "prompt", "text": "hi"})
        f.store.exec("update coding_agents set last_activity=?", (time.time() - jb_coding.STALE_RUNNING - 5,))
        self.assertEqual(f.coding_nodes()[0]["status"], "stale")
        self.assertFalse(f.coding_agent_records()[0]["live"])


class Desktop(unittest.TestCase):
    def test_argv_gates(self):
        f = F()
        self.assertEqual(f.desktop_use_argv({"action": "snapshot", "app": "Notes"})[-3:], ["Notes", "--compact", "-i"])
        with self.assertRaises(ValueError): f.desktop_use_argv({"action": "snapshot", "app": "Revolut"}, ["revolut"])
        with self.assertRaises(ValueError): f.desktop_use_argv({"action": "click", "ref": "@zzz:e1"})
        f._DESKTOP_REFS["abc123"] = "Revolut"
        with self.assertRaises(ValueError): f.desktop_use_argv({"action": "click", "ref": "@abc123:e1"}, ["Revolut"])
        f._DESKTOP_REFS["def456"] = "Notes"
        self.assertEqual(f.desktop_use_argv({"action": "type", "ref": "@def456:e4", "text": "hi"})[1:], ["type", "@def456:e4", "hi"])


if __name__ == "__main__":
    unittest.main()


class CodingControl(unittest.TestCase):
    def setUp(self):
        self.f = F(); self.calls = []
        self.sessions = [{"host": "pc", "kind": "kitty", "agent_type": "claude", "pid": "111", "controllable": True,
                          "claude_session_id": "aaa", "socket": "/tmp/k", "window_id": "1", "session_id": "pc:kitty:x/1", "cwd": "/p/one"},
                         {"host": "mac", "kind": "native", "agent_type": "codex", "pid": "222", "controllable": False, "cwd": "/p/two"}]
        def ac(tool, args):
            self.calls.append((tool, args))
            return {"sessions": self.sessions, "host_errors": []} if tool == "agent_list_sessions" else {"ok": True}
        self.f._agent_control = ac

    def test_reconcile_ends_closed_and_adopts_open(self):
        f = self.f
        f.coding_event({"source": "claude", "host": "pc", "session_id": "aaa", "event": "prompt", "pid": 111})
        f.coding_event({"source": "opencode", "host": "pc", "session_id": "gone", "event": "prompt", "pid": 999})
        r = f.coding_reconcile_open()
        self.assertEqual((r["ended"], r["adopted"]), (1, 1))
        ids = sorted(n["id"] for n in f.coding_nodes())
        self.assertEqual(ids, ["code:mac:codex:pid222", "code:pc:claude:aaa"])
        # hooks start for the adopted process -> the pid-keyed row is retired, no duplicate
        f.coding_event({"source": "codex", "host": "mac", "session_id": "thread-9", "event": "prompt", "pid": 222})
        self.assertEqual(sorted(n["id"] for n in f.coding_nodes()), ["code:mac:codex:thread-9", "code:pc:claude:aaa"])

    def test_unreachable_host_left_alone(self):
        f = self.f
        f.coding_event({"source": "codex", "host": "mac", "session_id": "t", "event": "prompt", "pid": 5})
        self.sessions = []
        f._agent_control = lambda tool, args: {"sessions": [], "host_errors": [{"host": "mac", "error": "offline"}]}
        f.coding_reconcile_open()
        self.assertEqual(len(f.coding_nodes()), 1)

    def test_send_terminal_when_idle_else_queue(self):
        f = self.f
        f.coding_event({"source": "claude", "host": "pc", "session_id": "aaa", "event": "stop", "pid": 111})
        self.assertEqual(f.coding_send("code:pc:claude:aaa", "run the tests")["delivered"], "terminal")
        self.assertIn("agent_send_input", [c[0] for c in self.calls])
        f.coding_event({"source": "claude", "host": "pc", "session_id": "aaa", "event": "prompt"})
        self.assertEqual(f.coding_send("code:pc:claude:aaa", "also lint")["delivered"], "queued")
        got = f.coding_inbox_pull({"host": "pc", "source": "claude", "session_id": "aaa"})
        self.assertEqual([m["text"] for m in got["messages"]], ["also lint"])
        self.assertEqual(f.coding_inbox_pull({"host": "pc", "source": "claude", "session_id": "aaa"})["messages"], [])
        with self.assertRaises(ValueError): f.coding_keys("code:pc:claude:aaa", "rm -rf")


class CodingPingTests(unittest.TestCase):
    def test_ping_delivers_rate_limits_and_caps(self):
        f = F()
        f.coding_event({"source": "claude", "host": "pc", "session_id": "s1", "event": "start", "cwd": "/w/proj"})
        r = f.coding_ping({"message": "Build finished, 3 tests failing", "source": "claude", "host": "pc",
                           "session_id": "s1", "cwd": "/w/proj"})
        self.assertTrue(r["ok"])
        self.assertEqual(f.notes[-1][1]["title"], "🤖 Claude Code on the PC · proj")
        self.assertIn("📣", f.coding_nodes()[0]["detail"][-1])
        r2 = f.coding_ping({"message": "again", "source": "claude", "host": "pc", "session_id": "s1"})
        self.assertFalse(r2["ok"])
        self.assertIn("rate limited", r2["detail"])
        with self.assertRaises(ValueError):
            f.coding_ping({"message": "  "})
        f.store.exec("UPDATE coding_pings SET created_at=created_at-120")
        for i in range(29):
            f.store.exec("INSERT INTO coding_pings(sender,title,message,created_at) VALUES('pc:claude:s1','t','m',?)", (time.time() - 3600,))
        r3 = f.coding_ping({"message": "third", "source": "claude", "host": "pc", "session_id": "s1"})
        self.assertIn("daily", r3["detail"])
