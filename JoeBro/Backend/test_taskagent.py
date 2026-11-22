"""Tasks that start a coding agent through the Night Shift runner, and the hosts picker data."""
import unittest

import jb_nightshift as ns
import jb_taskagent as ta
import test_nightshift as tn


class TaskAgent(tn.Base):
    def task_row(self, **kw):
        h = self.h
        h._night_table()
        h.store.exec("insert into tasks(id,title,prompt,schedule,scheduled_time,status,created_at,updated_at) values('t1','Tidy','Add the feature','daily','07:00','active','x','x')")
        vals = ta.parse(dict({"coding_agent": "claude", "coding_repo": self.repo}, **kw), None)
        ta.save(h, "t1", vals)
        return h.store.one("select * from tasks where id='t1'")

    def test_parse_validates(self):
        self.assertIsNone(ta.parse({"title": "x"}, None))
        with self.assertRaises(ValueError):
            ta.parse({"coding_agent": "gemini", "coding_repo": "/x"}, None)
        with self.assertRaises(ValueError):
            ta.parse({"coding_agent": "claude"}, None)
        self.assertEqual(ta.parse({"coding_agent": ""}, {"coding_agent": "claude", "coding_repo": "/x"}), ("", "", "", ""))

    def test_run_starts_the_night_runner_and_status_flows_back(self):
        row = self.task_row()
        msg = ta.run(self.h, row)
        self.assertIn("Claude Code", msg)
        nid = self.h.store.one("select * from tasks where id='t1'")["coding_night_id"]
        t = self.wait(nid)
        self.assertEqual((t["status"], t["task_ref"]), ("review", "t1"))
        self.assertEqual(self.h.store.one("select * from tasks where id='t1'")["coding_status"], "review")
        self.h.night_decide(nid, {"decision": "approve"})
        self.assertEqual(self.h.store.one("select * from tasks where id='t1'")["coding_status"], "approved")

    def test_a_run_in_flight_is_not_doubled(self):
        row = self.task_row()
        ta.run(self.h, row)
        row = self.h.store.one("select * from tasks where id='t1'")
        self.h._night_set(row["coding_night_id"], status="queued")
        self.assertIn("Still running", ta.run(self.h, row))

    def test_hosts_local_plus_nodes_and_extension_labels(self):
        ns._NODES.clear()
        hosts = self.h.night_hosts()
        self.assertEqual([(x["local"], x["up"]) for x in hosts], [(True, True)])
        ns.note_node("macbook")
        self.assertEqual({x["id"]: x["up"] for x in self.h.night_hosts()}["macbook"], True)
        ns._NODES["macbook"] = 0
        self.assertFalse({x["id"]: x for x in self.h.night_hosts()}["macbook"]["up"])

    def test_pinned_to_a_linked_node_that_is_down_waits_instead_of_running_here(self):
        ns._NODES["macbook"] = 0
        t = self.h.night_add({"repo": self.repo, "prompt": "x", "agent": "claude", "host": "macbook"})
        self.h.night_run_now(t["id"])
        self.assertEqual(self.h.night_task(t["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
