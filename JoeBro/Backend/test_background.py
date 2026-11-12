"""Background engine: budget, back-off, quiet hours, change detection, autonomy routines, notices.
No network, no model: the model call and push are stubs."""
import json, sys, tempfile, types, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import jb_ext
from jb_core import Store
from jb_heartbeat import HeartbeatMixin, is_nothing
from jb_fleet import FleetMixin
import jb_autonomy
from jb_autonomy import AutonomyMixin


class H(AutonomyMixin, HeartbeatMixin, FleetMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
        self.sent = []

    def pref(self, k):
        r = self.store.one("select value from prefs where key=?", (k,))
        return json.loads(r["value"]) if r else None

    def set_pref(self, k, v):
        self.store.exec("insert or replace into prefs(key,value) values(?,?)", (k, json.dumps(v)))

    def parse_tool_args(self, body):
        return json.loads(body)

    def push_telegram(self, body, title=""):
        return False


class Budget(unittest.TestCase):
    def test_cap_is_enforced_and_noticed_once(self):
        h = H(); h.set_pref("background_daily_cap", 2)
        with patch.object(H, "_complete", lambda *a, **k: "ok", create=True):
            h.bg_model = lambda: ("e", "m")
            self.assertEqual(h.bg_complete("s", "u"), "ok")
            self.assertEqual(h.bg_complete("s", "u"), "ok")
            self.assertIsNone(h.bg_complete("s", "u"))        # over the cap: no call
            self.assertIsNone(h.bg_complete("s", "u"))
        notices = h.store.rows("select * from inbox where kind='background'")
        self.assertEqual(len(notices), 1)

    def test_backoff_grows_with_empty_runs_and_resets(self):
        h = H()
        base = h.bg_backoff_minutes(30)
        for _ in range(4):
            h.bg_note_run(False)
        self.assertGreater(h.bg_backoff_minutes(30), base)
        h.bg_note_run(True)
        self.assertEqual(h.bg_backoff_minutes(30), base)

    def test_quiet_hours_wrap_midnight(self):
        h = H()
        self.assertTrue(h._in_quiet_hours("23:00-08:00", datetime(2026, 10, 13, 0, 30)))
        self.assertFalse(h._in_quiet_hours("23:00-08:00", datetime(2026, 10, 13, 12, 0)))

    def test_nothing_variants_are_silence(self):
        for t in ("", "NOTHING", "nothing.", " Nothing to report! "):
            self.assertTrue(is_nothing(t), t)
        self.assertFalse(is_nothing("Your flight moved to 9:40"))

    def test_deltas_report_only_changed_items(self):
        data = {"calendar": [{"id": "1", "title": "A", "text": "x"}, {"id": "2", "title": "B", "text": "y"}]}
        changed, seen = H.bg_deltas(data, {})
        self.assertEqual(len(changed), 2)
        data["calendar"][1]["text"] = "z"
        changed, _ = H.bg_deltas(data, seen)
        self.assertEqual([c["id"] for c in changed], ["2"])


class Autonomy(unittest.TestCase):
    def setUp(self):
        jb_ext._HOOKS.pop("autonomy_routines", None)
        calls = self.calls = []
        def report(cfg, prev, read):
            calls.append(1)
            return {"status": "ok", "body": "all quiet " + str(len(calls)), "publish": True}
        jb_ext.on("autonomy_routines", lambda: {"demo": {"title": "Demo", "enabled": True, "every_days": 1, "at": "00:00",
                                                         "timezone": "UTC", "quiet_hours": "00:00-00:00", "report": report}})
        self.addCleanup(lambda: jb_ext._HOOKS.pop("autonomy_routines", None))

    def test_core_ships_no_routines_and_extension_routines_run_without_a_model(self):
        h = H()
        self.assertIn("demo", h.autonomy_config())
        out = h.run_autonomy("demo", force=True)
        self.assertTrue(out["ran"] and out["published"] and out["llm_calls"] == 0)
        self.assertEqual(len(h.store.rows("select * from inbox where kind='autonomy'")), 1)
        again = h.run_autonomy("demo", force=True)           # body differs -> published again; same body would be deduped
        self.assertTrue(again["ran"])

    def test_pause_blocks_everything(self):
        h = H(); h.set_pref("personal_automation_paused", True)
        self.assertFalse(h.run_autonomy("demo", force=True)["ran"])
        self.assertEqual(h.run_autonomy_due(), [])
        self.assertEqual(self.calls, [])

    def test_settings_are_validated(self):
        h = H()
        for bad in ({"every_days": 99}, {"at": "25:00"}, {"timezone": "Mars/Base"}, {"nope": 1}):
            with self.assertRaises(ValueError):
                h.manage_autonomy_tool(json.dumps({"action": "set", "routine": "demo", "settings": bad}))
        ok = json.loads(h.manage_autonomy_tool(json.dumps({"action": "set", "routine": "demo", "settings": {"every_days": 3}})))
        self.assertEqual(ok["config"]["every_days"], 3)


class Notices(unittest.TestCase):
    def test_plain_notice_goes_to_inbox_and_chat_kinds_to_the_phone_chat(self):
        h = H()
        h.ensure_phone_session = lambda: None
        saved = []
        h.add_message = lambda sid, role, body, meta=None: saved.append((sid, body)) or 7
        h.notify("hello", kind="note", channel="inbox")
        self.assertEqual(len(h.store.rows("select * from inbox")), 1)
        out = h.notify("task result", kind="personal_task", session="phone", channel="inbox")
        self.assertTrue(out["id"].startswith("msg_"))
        self.assertEqual(len(h.store.rows("select * from inbox")), 1)       # no second inbox row
        self.assertEqual(saved[0][1], "task result")


if __name__ == "__main__":
    unittest.main()
