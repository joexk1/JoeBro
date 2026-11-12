"""Importance gate: only the important fixture events may ping."""
import json, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jb_importance as imp
from jb_core import Store
from jb_heartbeat import HeartbeatMixin
from jb_fleet import FleetMixin

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
KNOWN = ["sam@friend.example"]
FIXTURES = {
    "newsletter": ({"source": "email", "from": "news@weekly.example", "title": "This week in widgets", "text": "unsubscribe here"}, False),
    "receipt": ({"source": "email", "from": "orders@shop.example", "title": "Your receipt", "text": "order confirmation, total 12.00"}, False),
    "random email": ({"source": "email", "from": "stranger@corp.example", "title": "Quick question", "text": "hi, do you have a minute?"}, False),
    "summary": ({"source": "email", "from": "me", "title": "You have 7 new emails", "text": ""}, False),
    "frequent contact today": ({"source": "email", "from": "Sam <sam@friend.example>", "title": "Can you confirm the flat by 5pm today?", "text": "need a reply please"}, True),
    "agent approval": ({"source": "approval", "title": "Claude Code wants to run a command", "text": ""}, True),
    "meeting in 10": ({"source": "calendar", "title": "Standup", "start": NOW + timedelta(minutes=10)}, True),
    "meeting tomorrow": ({"source": "calendar", "title": "Dentist", "start": NOW + timedelta(days=1)}, False),
}


class H(HeartbeatMixin, FleetMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
    def pref(self, k):
        r = self.store.one("select value from prefs where key=?", (k,))
        return json.loads(r["value"]) if r else None
    def set_pref(self, k, v):
        self.store.exec("insert or replace into prefs(key,value) values(?,?)", (k, json.dumps(v)))
    def bg_now(self):
        return NOW


class Gate(unittest.TestCase):
    def test_only_important_ping(self):
        for name, (item, want) in FIXTURES.items():
            self.assertEqual(imp.classify(item, NOW, "important", KNOWN)[0], want, name)

    def test_level_defaults_to_important(self):
        self.assertEqual(imp.level_of(None), "important")
        self.assertEqual(imp.level_of("normal"), "normal")

    def test_dedupe_and_cap(self):
        h = H()
        self.assertTrue(imp.allow_ping(h, "Reply to the landlord about the flat", "personal_urgent", NOW)[0])
        self.assertFalse(imp.allow_ping(h, "reply to the landlord about the flat now", "personal_urgent", NOW)[0])
        for i in range(3):
            self.assertTrue(imp.allow_ping(h, f"unique topic number{i} alpha{i}x", "personal_urgent", NOW)[0] or i == 2)
        self.assertFalse(imp.allow_ping(h, "something else entirely different", "personal_urgent", NOW)[0])
        self.assertTrue(imp.allow_ping(h, "agent blocked on approval", "approval", NOW)[0])   # actionable: exempt

    def test_pulse_makes_no_model_call_without_important_items(self):
        h = H(); h.set_pref("pulse_enabled", True)
        h.bg_collect = lambda *a, **k: {"email": [FIXTURES["newsletter"][0] | {"id": "1"}], "calendar": []}
        h.bg_complete = lambda *a, **k: self.fail("model called")
        out = h.run_pulse(force=True)
        self.assertFalse(out["spoke"]); self.assertEqual(out["llm_calls"], 0)

    def test_summary_text_is_dropped(self):
        self.assertFalse(H().bg_may_ping("You have 5 new emails in your inbox"))


if __name__ == "__main__":
    unittest.main()
