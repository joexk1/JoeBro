"""Night Shift against a temp git repo and a fake agent: queue, window, worktree run,
diff/test/screenshot capture, review card, approve (keep / merge), reject, follow-up,
the time cap, never pushing, and starting/resuming a session through the same dispatcher."""
import json, os, stat, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

import jb_nightshift as ns
import jb_nightworker as worker
import jb_sharedmem as sm
from jb_core import Store, WRITE_SIDE_EFFECT_TOOLS
from jb_coding import CodingAgentsMixin

FAKE_AGENT = r'''
import os, subprocess, sys, time
prompt = sys.argv[1]
if "SLEEP" in prompt:
    time.sleep(30)
if "NOTHING" in prompt:
    print("SUMMARY: nothing to do."); sys.exit(0)
n = len([f for f in os.listdir(".") if f.startswith("feature")])
open("feature%d.py" % n, "w").write("def answer():\n    return 42\n")
open("app.py", "a").write("# touched by the agent\n")
subprocess.run(["git", "add", "feature%d.py" % n]); subprocess.run(["git", "commit", "-qm", "add feature"])
push = subprocess.run(["git", "push", "origin", "HEAD"], capture_output=True, text=True)
os.makedirs(".joebro/night", exist_ok=True)
open(".joebro/night/screenshot.png", "wb").write(b"\x89PNG fake")
print("working...\nPUSH_EXIT=%d" % push.returncode)
print("SUMMARY: Added feature%d and touched app.py. task=%s" % (n, os.environ.get("JOEBRO_NIGHT_TASK")))
print("LEARNED: Feature modules live at the repo root and are named featureN.py.")
'''


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class H(ns.NightShiftMixin, CodingAgentsMixin):
    def __init__(self):
        self.server = type("S", (), {"store": Store(Path(tempfile.mkdtemp()))})()

    store = property(lambda self: self.server.store)

    def pref(self, k):
        r = self.store.one("select value from prefs where key=?", (k,))
        return json.loads(r["value"]) if r else None

    def set_pref(self, k, v):
        self.store.exec("insert or replace into prefs(key,value) values(?,?)", (k, json.dumps(v)))

    def parse_tool_args(self, body):
        return json.loads(body)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo, self.remote = os.path.join(self.tmp, "proj"), os.path.join(self.tmp, "remote.git")
        os.makedirs(self.repo)
        sh(self.tmp, "git", "init", "-q", "--bare", self.remote)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@example.com"); sh(self.repo, "git", "config", "user.name", "T")
        Path(self.repo, "app.py").write_text("print('hi')\n")
        sh(self.repo, "git", "add", "."); sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "remote", "add", "origin", self.remote); sh(self.repo, "git", "push", "-q", "origin", "main")
        self.fake = os.path.join(self.tmp, "fake_agent.py")
        Path(self.fake).write_text(FAKE_AGENT)
        self.h = H()
        self.h.set_pref("night_shift_commands", {"claude": [sys.executable, self.fake, "{prompt}"]})
        ns._RUNNING.clear()

    def run_task(self, **kw):
        t = self.h.night_add(dict({"repo": self.repo, "prompt": "Add the feature", "agent": "claude"}, **kw))
        self.h.night_run_now(t["id"])
        return self.wait(t["id"])

    def wait(self, tid):
        for _ in range(300):
            t = self.h.night_out(self.h.night_task(tid))
            if t["status"] != "running" and tid not in ns._RUNNING:
                return t
            time.sleep(0.05)
        self.fail("task did not finish")


class Queue(Base):
    def test_add_edit_reorder_cancel(self):
        h = self.h
        a = h.night_add({"repo": self.repo, "prompt": "First task\nmore", "agent": "codex", "model": "gpt-5", "window_start": "23:00", "window_end": "06:00"})
        b = h.night_add({"repo": self.repo, "prompt": "Second task"})
        self.assertEqual((a["title"], a["status"], a["position"], b["position"]), ("First task", "queued", 1, 2))
        self.assertEqual((b["budget_usd"], b["max_minutes"]), (5.0, 60))            # every task is capped by default
        self.assertEqual(h.night_edit(b["id"], {"budget_usd": 999, "max_minutes": 9999, "test_command": "make test"})["budget_usd"], ns.MAX_BUDGET)
        self.assertEqual([t["id"] for t in h.night_reorder([b["id"], a["id"]])], [b["id"], a["id"]])
        self.assertEqual(h.night_cancel(a["id"])["status"], "cancelled")
        for bad in ({"repo": self.repo}, {"repo": self.repo, "prompt": "x", "agent": "gemini"}, {"repo": self.repo, "prompt": "x", "window_start": "25:00"}):
            with self.assertRaises(ValueError):
                h.night_add(bad)
        with self.assertRaises(ValueError):
            h.night_edit(a["id"], {"prompt": "too late"})

    def test_window(self):
        at = lambda hh, mm: time.mktime(time.localtime()[:3] + (hh, mm, 0, 0, 0, -1))
        self.assertTrue(ns.in_window("23:00", "07:00", at(2, 0)))
        self.assertTrue(ns.in_window("23:00", "07:00", at(23, 30)))
        self.assertFalse(ns.in_window("23:00", "07:00", at(12, 0)))
        self.assertTrue(ns.in_window("", "", at(12, 0)))
        t = self.h.night_add({"repo": self.repo, "prompt": "later"})
        self.assertEqual(self.h.night_tick(now=at(12, 0)), [])                      # outside the window: nothing starts
        self.assertEqual(self.h.night_task(t["id"])["status"], "queued")
        self.h.set_pref("night_shift_enabled", False)
        self.assertEqual(self.h.night_tick(now=at(2, 0)), [])


class Run(Base):
    def test_run_captures_everything_and_never_pushes(self):
        t = self.run_task(test_command="python3 -c \"import feature0; assert feature0.answer() == 42\"")
        self.assertEqual(t["status"], "review", t["error"])
        res = t["result"]
        self.assertEqual((res["diff"]["files"], res["diff"]["insertions"], res["commits"]), (2, 3, 2))   # the uncommitted edit was committed too
        self.assertEqual({f["path"] for f in res["diff"]["file_list"]}, {"app.py", "feature0.py"})
        self.assertTrue(res["tests"]["passed"])
        self.assertTrue(res["screenshot"])
        self.assertIn("Added feature0", res["summary"])
        self.assertIn("task=" + t["id"], res["summary"])
        self.assertTrue(t["branch"].startswith("joebro/night/" + t["id"]))
        # never pushes: the agent's own push failed and the remote has only the first commit
        log = open(os.path.join(self.h._night_dir(t["id"]), "agent.log")).read()
        self.assertRegex(log, r"PUSH_EXIT=[1-9]")
        self.assertEqual(sh(self.remote, "git", "rev-list", "--all", "--count"), "1")
        self.assertEqual(sh(self.remote, "git", "branch", "--list").lstrip("* "), "main")
        # the user's checkout is untouched
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.repo, "git", "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "feature0.py")))
        # learned -> Shared Memory, with provenance
        facts = sm.browse(self.h.store, sm.resolve_project(self.h.store, path=self.repo))
        self.assertEqual([(f["kind"], f["provenance"]["source"], f["provenance"]["session"]) for f in facts], [("learned", "night_shift", t["id"])])
        # review card + inbox row for the actionable push
        card = self.h.night_review_cards()[0]
        self.assertEqual((card["kind"], card["task_id"], card["state"]), ("review", t["id"], "review"))
        self.assertEqual([m["value"] for m in card["metrics"]], ["2", "+3", "-0", "passed"])
        self.assertEqual([a["id"] for a in card["actions"]], ["approve", "reject", "followup"])
        self.assertTrue(card["screenshot_url"].endswith("/screenshot"))
        self.assertIn("Add the feature", self.h.briefing_night_facts())
        self.assertEqual(len(self.h.store.rows("select * from inbox where kind='night_review' and source=?", (t["id"],))), 1)
        self.assertIn("feature0.py", worker.diff_text(self.repo, t["base_commit"], t["head_commit"])[0])
        # approve (default: keep the branch) removes the worktree, keeps the branch, merges nothing
        out = self.h.night_decide(t["id"], {"decision": "approve"})
        self.assertEqual((out["task"]["status"], out["task"]["decision"]["merged"]), ("approved", False))
        self.assertFalse(os.path.exists(t["worktree"]))
        self.assertIn(t["branch"], sh(self.repo, "git", "branch", "--list", t["branch"]))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "feature0.py")))
        self.assertEqual(self.h.night_review_cards(), [])
        with self.assertRaises(ValueError):
            self.h.night_decide(t["id"], {"decision": "reject"})                    # already decided

    def test_approve_merge_is_local_only(self):
        t = self.run_task(on_approve="merge")
        out = self.h.night_decide(t["id"], {"decision": "approve"})
        self.assertTrue(out["task"]["decision"]["merged"], out["detail"])
        self.assertTrue(os.path.exists(os.path.join(self.repo, "feature0.py")))
        self.assertEqual(sh(self.remote, "git", "rev-list", "--all", "--count"), "1")       # still not pushed

    def test_merge_falls_back_to_keep_when_the_checkout_is_dirty(self):
        t = self.run_task()
        Path(self.repo, "app.py").write_text("print('my own edit')\n")
        out = self.h.night_decide(t["id"], {"decision": "approve", "mode": "merge"})
        self.assertFalse(out["task"]["decision"]["merged"])
        self.assertIn("uncommitted", out["detail"])
        self.assertEqual(Path(self.repo, "app.py").read_text(), "print('my own edit')\n")

    def test_reject_discards_worktree_and_branch(self):
        t = self.run_task()
        self.assertTrue(os.path.isdir(t["worktree"]))
        out = self.h.night_decide(t["id"], {"decision": "reject"})
        self.assertEqual(out["task"]["status"], "rejected")
        self.assertFalse(os.path.exists(t["worktree"]))
        self.assertEqual(sh(self.repo, "git", "branch", "--list", t["branch"]), "")
        self.assertEqual(sh(self.repo, "git", "cat-file", "-t", t["head_commit"]), "commit")   # still recoverable by sha

    def test_followup_requeues_on_the_same_branch(self):
        t = self.run_task()
        with self.assertRaises(ValueError):
            self.h.night_decide(t["id"], {"decision": "followup"})
        out = self.h.night_decide(t["id"], {"decision": "followup", "text": "and a second one", "now": True})
        t2 = self.wait(t["id"])
        self.assertEqual((t2["status"], t2["attempts"], t2["branch"], t2["worktree"]), ("review", 2, t["branch"], t["worktree"]))
        self.assertEqual(t2["followups"][0]["text"], "and a second one")
        self.assertEqual(t2["result"]["diff"]["files"], 3)                          # feature0 + feature1 + app.py

    def test_no_changes_is_failed_and_parallel_limit_holds(self):
        a = self.h.night_add({"repo": self.repo, "prompt": "SLEEP please", "agent": "claude"})
        b = self.h.night_add({"repo": self.repo, "prompt": "NOTHING to do", "agent": "claude"})
        self.h.night_run_now(a["id"]); self.h.night_run_now(b["id"])
        self.assertEqual(self.h.night_task(b["id"])["status"], "queued")            # parallel = 1
        self.assertEqual(self.h.night_cancel(a["id"])["id"], a["id"])               # stop the running agent
        ta = self.wait(a["id"])
        self.assertEqual((ta["status"], ta["error"]), ("failed", "cancelled"))
        self.h.night_tick()
        tb = self.wait(b["id"])
        self.assertEqual((tb["status"], tb["error"]), ("failed", "the agent made no changes"))
        self.assertEqual(self.h.night_decide(b["id"], {"decision": "reject"})["task"]["status"], "rejected")

    def test_time_cap_kills_the_agent(self):
        t0 = time.time()
        out = worker.run_agent([sys.executable, self.fake, "SLEEP"], self.repo, max_minutes=0.01, log_path=os.path.join(self.tmp, "log"))
        self.assertTrue(out["timed_out"])
        self.assertLess(time.time() - t0, 10)

    def test_default_commands_use_the_agents_own_clis_with_a_budget(self):
        self.assertEqual(worker.agent_argv("claude", "do it", "opus", 5)[-4:], ["--model", "opus", "--max-budget-usd", "5"])
        self.assertEqual(worker.agent_argv("codex", "do it")[:4], ["codex", "exec", "-s", "workspace-write"])   # --full-auto is gone in codex 0.167
        self.assertNotIn("--full-auto", worker.COMMANDS["codex"])
        self.assertEqual(worker.agent_argv("opencode", "go", resume_id="ses_1")[-2:], ["--session", "ses_1"])
        self.assertEqual(worker.session_argv("claude", "go on", resume_id="abc"), ["claude", "--resume", "abc", "go on"])
        self.assertEqual(worker.session_argv("codex", "go on", resume_id="abc"), ["codex", "resume", "abc", "go on"])


class Sessions(Base):
    def test_personal_agent_resumes_the_last_session_through_the_dispatcher(self):
        h = self.h
        bindir = os.path.join(self.tmp, "bin"); os.makedirs(bindir)
        rec = os.path.join(self.tmp, "tmux-args.json")
        tmux = os.path.join(bindir, "tmux")
        Path(tmux).write_text("#!%s\nimport json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n" % (sys.executable, rec))
        os.chmod(tmux, 0o755)
        old = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        for sid, text in (("sess-1", "Refactored the uploader"), ("sess-2", "Half way through the retry logic; tests still red")):
            h.coding_event({"source": "claude", "host": "mac", "session_id": sid, "event": "prompt", "cwd": self.repo, "model": "opus", "text": "go"})
            h.coding_event({"source": "claude", "host": "mac", "session_id": sid, "event": "stop", "cwd": self.repo, "text": text})
            time.sleep(0.01)
        listing = h.recent_coding_sessions_tool("{}")
        self.assertIn("Half way through the retry logic", listing)
        self.assertIn("resume id sess-2", listing)
        self.assertIn("branch main", listing)
        out = h.start_coding_session_tool(json.dumps({"project": "proj", "prompt": "finish the retry logic", "resume": True}))
        self.assertIn("Resumed Claude Code", out)
        args = json.load(open(rec))
        self.assertEqual(args[:2], ["new-session", "-d"])
        self.assertEqual(args[args.index("-c") + 1], self.repo)
        self.assertEqual(args[-4:], ["claude", "--resume", "sess-2", "finish the retry logic"])
        task = h.night_list()[0]
        self.assertEqual((task["kind"], task["status"], task["resume_id"]), ("session", "launched", "sess-2"))
        self.assertEqual(h.night_review_cards(), [])                                # a session is not a review
        self.assertIn("No earlier session", H().start_coding_session_tool(json.dumps({"repo": self.repo, "prompt": "x", "resume": True})))
        # the apps' Resume button: POST api/coding-agents/start {resume: <agent id>, session_id, title} resumes exactly that session
        os.remove(rec)
        out = h.start_coding_session_tool({"resume": "code:mac:claude:sess-1", "session_id": "", "title": "x"})
        self.assertIn("Resumed Claude Code", out)
        self.assertEqual(json.load(open(rec))[-3:-1], ["--resume", "sess-1"])
        self.assertIn("No recorded session", h.start_coding_session_tool({"resume": "code:mac:claude:nope"}))
        self.assertIn("start_coding_session", WRITE_SIDE_EFFECT_TOOLS)              # hidden from read-only chats


class NodeRunner(Base):
    def test_claim_report_park_decision_and_timeout(self):
        h = H()
        t = h.night_add({"repo": self.repo, "prompt": "x", "title": "Node job", "agent": "claude"})
        other = h.night_add({"repo": self.repo, "prompt": "y", "host": "elsewhere"})
        tid = t["id"] if isinstance(t, dict) else t
        with self.assertRaises(PermissionError): h.night_claim(other["id"], {"node": "box"})          # pinned to another computer
        c = h.night_claim(tid, {"node": "box"})
        self.assertEqual((c["task"]["status"], c["task"]["runner"], c["task"]["attempts"]), ("running", "node:box", 1))
        self.assertIn("Never push", c["prompt"])
        with self.assertRaises(PermissionError): h.night_claim(tid, {"node": "box2"})                  # atomic: one winner
        with self.assertRaises(PermissionError): h.night_report(tid, {"node": "box2", "status": "failed"})
        png = __import__("base64").b64encode(b"\x89PNG fake").decode()
        h.night_report(tid, {"node": "box", "status": "review", "branch": "night/x", "head_commit": "abc", "agent": "claude",
                             "result": {"summary": "done", "learned": ["Modules live at the repo root."], "commits": 1},
                             "diff_text": "diff --git a b\n", "screenshot_b64": png})
        r = h.night_out(h.night_task(tid))
        self.assertEqual((r["status"], r["branch"], r["result"]["summary"]), ("review", "night/x", "done"))
        self.assertTrue(os.path.isfile(os.path.join(h._night_dir(tid), "screenshot.png")))
        self.assertEqual(h.night_task(tid)["diff_text"], "diff --git a b\n")
        self.assertTrue(h.store.one("select 1 from shared_facts where text like 'Modules live%' and source='night_shift'"))
        out = h.night_decide(tid, {"decision": "approve", "mode": "merge"})                              # parked, no git run here
        self.assertTrue(out["parked"]); self.assertEqual(h.night_out(h.night_task(tid))["pending_action"], "approve:merge")
        self.assertEqual(h.night_task(tid)["status"], "review")
        h.night_report(tid, {"node": "box", "decision": {"decision": "approve", "mode": "merge", "merged": True, "detail": "merged"}})
        r = h.night_out(h.night_task(tid))
        self.assertEqual((r["status"], r["pending_action"], r["decision"]["merged"]), ("approved", "", True))
        t2 = h.night_add({"repo": self.repo, "prompt": "z", "max_minutes": 5})
        h.night_claim(t2["id"], {"node": "box"})
        h.night_tick(time.time() + 20 * 60); self.assertEqual(h.night_task(t2["id"])["status"], "running")
        h.night_tick(time.time() + 40 * 60); self.assertEqual(h.night_task(t2["id"])["status"], "failed")
        self.assertIn("node went away", h.night_task(t2["id"])["error"])


if __name__ == "__main__":
    unittest.main()
