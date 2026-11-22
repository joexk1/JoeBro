"""Coding agent detail reads like a chat: roles, approval request, send."""
import tempfile, unittest
from pathlib import Path
from jb_core import Store
from jb_coding import CodingAgentsMixin


class F(CodingAgentsMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
    def pref(self, k): return None


class Chat(unittest.TestCase):
    def test_detail_roles_and_approval(self):
        f = F(); b = {"source": "claude", "host": "mac", "session_id": "s1", "cwd": "/w/api"}
        aid = f.coding_event(dict(b, event="prompt", text="fix it"))["id"]
        f.coding_event(dict(b, event="tool", tool="Bash", text="ls"))
        f.coding_event(dict(b, event="waiting", text="run rm"))
        d = f.coding_agent_detail(aid)
        self.assertEqual([m["role"] for m in d["messages"]], ["user", "tool", "approval"])
        self.assertEqual(d["messages"][0]["content"], "fix it")
        self.assertTrue(d["waiting"]); self.assertEqual(d["pending_approval"], "run rm")
        f.coding_event(dict(b, event="stop", text="done"))
        d = f.coding_agent_detail(aid)
        self.assertEqual(d["messages"][-1]["role"], "assistant"); self.assertFalse(d["waiting"])

    def test_limit_notice(self):
        f = F(); b = {"source": "codex", "host": "mac", "session_id": "s2"}
        aid = f.coding_event(dict(b, event="prompt", text="go"))["id"]
        f.coding_event(dict(b, event="stop", text="", notice="Usage limit reached \u2014 resets 3pm"))
        d = f.coding_agent_detail(aid)
        self.assertEqual(d["status"], "limited"); self.assertFalse(d["live"])
        self.assertEqual(d["messages"][-1], dict(d["messages"][-1], role="notice", content="Usage limit reached \u2014 resets 3pm"))
        self.assertTrue(d["result"].endswith("\u26a0 Usage limit reached \u2014 resets 3pm"))
        f.coding_event(dict(b, event="prompt", text="retry"))
        self.assertEqual(f.coding_agent_detail(aid)["status"], "running")

    def test_send_queues_for_running_agent(self):
        f = F(); b = {"source": "claude", "host": "mac", "session_id": "s1"}
        aid = f.coding_event(dict(b, event="prompt", text="go"))["id"]
        r = f.coding_send(aid, "also add tests", sender="phone")
        self.assertEqual(r["delivered"], "queued")
        self.assertEqual(f.coding_inbox_pull({"host": "mac", "source": "claude", "session_id": "s1"})["messages"][0]["text"], "also add tests")

if __name__ == "__main__": unittest.main()
