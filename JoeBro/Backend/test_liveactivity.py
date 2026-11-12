"""Live subagents (feature-api.md s3) and Live Activity push-to-start (s4). Real SQLite store,
mocked APNs sender: nothing here can reach the network."""
import json, tempfile, time, unittest
from pathlib import Path

from jb_core import Store
from jb_coding import CodingAgentsMixin
import jb_push
from jb_push import LiveActivityPusher, register_activity, PushError

HEX = "ab" * 20


class F(CodingAgentsMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
    def pref(self, k): return None


class Subagents(unittest.TestCase):
    def setUp(self):
        self.f = F()
        self.b = {"source": "claude", "host": "mac", "session_id": "p1", "cwd": "/w/api"}

    def test_tree_counts_and_order(self):
        f, b = self.f, self.b
        f.coding_event(dict(b, event="prompt", text="go"))
        r = f.coding_event(dict(b, event="subagent_start", session_id="s1", parent_session_id="p1", subagent_type="Explore"))
        self.assertEqual((r["kind"], r["parent"]), ("subagent", "code:mac:claude:p1"))
        f.coding_event(dict(b, event="subagent_start", session_id="s2", parent_session_id="p1", subagent_type="Plan"))
        f.coding_event(dict(b, event="tool", tool="Grep", text="x", session_id="s1", parent_session_id="p1"))   # ordinary event -> child row
        top = f.coding_nodes()
        self.assertEqual([n["kind"] for n in top], ["coding", "subagent", "subagent"])
        self.assertEqual(top[0]["subagents"], {"total": 2, "live": 2})
        self.assertEqual(len(top[0]["children"]), 2)
        self.assertEqual(top[1]["tool_calls"], 1)
        tree = f.coding_nodes(tree=True)
        self.assertEqual(len(tree), 1); self.assertEqual(len(tree[0]["child_nodes"]), 2)
        f.coding_event(dict(b, event="subagent_stop", session_id="s1", parent_session_id="p1", text="found it"))
        recs = f.coding_agent_records()
        self.assertEqual([r["kind"] for r in recs], ["coding", "subagent", "subagent"])
        self.assertEqual((recs[0]["subagent_count"], recs[0]["subagent_live"]), (2, 1))
        self.assertEqual(recs[1]["parent_id"], recs[0]["id"]); self.assertEqual(recs[1]["status"], "done")
        self.assertEqual(recs[1]["agent_type"], "Explore")
        self.assertEqual(len(f.coding_agent_detail(recs[0]["id"])["child_agents"]), 2)

    def test_ends_with_parent_and_expiry(self):
        f, b = self.f, self.b
        f.coding_event(dict(b, event="prompt"))
        f.coding_event(dict(b, event="subagent_start", session_id="s1", parent_session_id="p1", ends_with_parent=True))
        f.coding_event(dict(b, event="stop"))
        self.assertEqual(f.coding_nodes()[1]["status"], "done")
        f.store.exec("update coding_agents set last_activity=? where kind='subagent'", (time.time() - 31 * 60,))
        self.assertEqual([n["kind"] for n in f.coding_nodes()], ["coding"])

    def test_bad_and_no_session_record(self):
        with self.assertRaises(ValueError):
            self.f.coding_event(dict(self.b, event="subagent_start"))
        import jb_coding; self.assertEqual(jb_coding.live_coding_count(self.f.store), 0)


def runs(n_sub=0, waiting=False, rid="code:mac:claude:p1"):
    return [{"id": rid, "title": "Claude Code · api", "kind": "coding", "detail": "Edit: a.py", "queued": False,
             "subagents": n_sub, "waiting": waiting}]


class PushToStart(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()))
        self.sent = []
        self.code = 200
        self.p = LiveActivityPusher(self.store, send=self._send)

    def _send(self, env, token, payload, prio):
        self.sent.append((env, token, payload["aps"]["event"], prio, payload)); return self.code, "{}"

    def test_register_shapes(self):
        s = self.store
        self.assertEqual(register_activity(s, {"token": HEX, "environment": "production"}),
                         {"ok": True, "start_token": False, "activity_token": True})
        r = register_activity(s, {"install_id": "i1", "push_to_start_token": HEX, "environment": "development", "push_token": "cd" * 20, "activity_id": "A"})
        self.assertEqual((r["start_token"], r["activity_token"]), (True, True))
        register_activity(s, {"install_id": "i1", "ended": True})
        self.assertIsNone(s.one("select 1 from push_activities where install_id='i1'"))
        self.assertIsNotNone(s.one("select 1 from push_start_tokens where install_id='i1'"))
        for bad in ({"push_to_start_token": HEX, "environment": "production"}, {"token": "zz", "environment": "production"}, {}):
            with self.assertRaises(PushError): register_activity(s, bad)

    def test_start_update_end_and_rate_limits(self):
        s, p = self.store, self.p
        register_activity(s, {"install_id": "i1", "push_to_start_token": HEX, "environment": "production"})
        self.assertEqual(p.tick([], 1000)["start"], 0)
        out = p.tick(runs(), 1000)
        self.assertEqual(out["start"], 1)
        st = self.sent[-1]
        self.assertEqual(st[2], "start"); self.assertEqual(st[4]["aps"]["attributes-type"], "JoeBroActivityAttributes")
        self.assertEqual(st[4]["aps"]["relevance-score"], 80); self.assertNotIn("sceneName", st[4]["aps"]["attributes"])
        self.assertEqual(st[4]["aps"]["content-state"]["subagents"], 0)
        p.tick(runs(), 1030)
        self.assertEqual(len(self.sent), 1)            # one start per 60 s
        register_activity(s, {"install_id": "i1", "token": "cd" * 20, "environment": "production", "activity_id": "A"})
        p.tick(runs(), 1100)                            # has an update token now: no new start; first update
        self.assertEqual([x[2] for x in self.sent], ["start", "update"])
        p.tick(runs(2), 1101)                           # changed but < 3 s since the last update
        self.assertEqual(len(self.sent), 2)
        p.tick(runs(2), 1104)
        self.assertEqual(self.sent[-1][2], "update"); self.assertEqual(self.sent[-1][4]["aps"]["content-state"]["subagents"], 2)
        self.assertEqual(self.sent[-1][3], 10)
        p.tick(runs(2, True), 1108)                     # structural again within 15 s -> low priority
        self.assertEqual(self.sent[-1][3], 5); self.assertEqual(self.sent[-1][4]["aps"]["relevance-score"], 100)
        n = len(self.sent); p.tick(runs(2, True), 1150)
        self.assertEqual(len(self.sent), n)             # nothing changed, no keep-alive yet
        p.tick(runs(2, True), 1235)
        self.assertEqual(len(self.sent), n + 1)         # keep-alive
        out = p.tick([], 1240)
        self.assertEqual(out["end"], 1); self.assertEqual(self.sent[-1][2], "end")
        self.assertIsNone(s.one("select 1 from push_activities"))
        self.assertEqual(p.tick([], 1245)["end"], 0)

    def test_dead_tokens_dropped_and_scene(self):
        register_activity(self.store, {"install_id": "i1", "push_to_start_token": HEX, "environment": "production"})
        self.code = 410
        self.p.scene = "garden"
        self.p.tick(runs(), 1000)
        self.assertEqual(self.sent[0][4]["aps"]["attributes"]["sceneName"], "garden")
        self.assertIsNone(self.store.one("select 1 from push_start_tokens"))

    def test_event_wakes_loop(self):
        jb_push.WAKE.clear()
        F().coding_event({"source": "claude", "host": "mac", "session_id": "x", "event": "start"})
        self.assertTrue(jb_push.WAKE.is_set())


if __name__ == "__main__":
    unittest.main()
