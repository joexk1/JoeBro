"""Memory unification: jb_memory + muse files -> Shared Memory `user` scope. Fixture DB, no network."""
import json, os, tempfile, unittest
from pathlib import Path

from jb_core import Store, data_dir
import jb_sharedmem as sm
import jb_usermem as um
from jb_memory import MemoryMixin
from jb_muse import MuseMixin


class H(MemoryMixin, MuseMixin):
    def __init__(self, store): self.store = store
    def parse_tool_args(self, b): return json.loads(b) if isinstance(b, str) else b


def fixture():
    store = Store(Path(tempfile.mkdtemp()))
    store.exec("insert into memory(id,text,category,kind,topic,entities,pinned,created_at,updated_at,use_count) values(?,?,?,?,?,?,?,?,?,?)",
               ("mem_a1", "Joe prefers dark roast coffee", "food", "opinion", "food", "coffee", 1, "2026-10-01T09:00:00", "2026-10-02T09:00:00", 3))
    store.exec("insert into memory(id,text,category,kind,topic,entities,pinned,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)",
               ("mem_b2", "Lives in a flat above the bakery", "home", "world", "home", "", 0, "2026-10-03T09:00:00", "2026-10-03T09:00:00"))
    d = Path(data_dir(store)) / "muse"
    d.mkdir(parents=True, exist_ok=True)
    (d / "people.md").write_text("# People\n- 2026-10-05: Sam is Joe's flatmate\n- 2026-10-06: Kim is his cousin in Leeds\n")
    (d / "about-me.md").write_text("# About Me\n- 2026-10-01: Name: Joe\nfree text line without a bullet\n")
    return store, d


class Migration(unittest.TestCase):
    def test_idempotent_no_loss_and_originals_kept(self):
        store, d = fixture()
        before = {p.name: p.read_text() for p in d.glob("*.md")}
        n1 = um.migrate(store)
        self.assertEqual(n1, {"jb_memory": 2, "muse": 4})
        self.assertEqual(um.migrate(store), {"jb_memory": 0, "muse": 0})
        self.assertEqual(store.one("select count(*) n from shared_facts where project='user'")["n"], 6)
        self.assertEqual(before, {p.name: p.read_text() for p in d.glob("*.md")})        # old files untouched
        self.assertEqual(store.one("select count(*) n from memory")["n"], 2)             # old table untouched
        f = store.one("select * from shared_facts where id='mem_a1'")
        self.assertEqual((f["pinned"], f["recalls"], f["kind"], f["topic"]), (1, 3, "opinion", "food"))

    def test_archive_sticks_after_rerun(self):
        store, _ = fixture()
        h = H(store)
        um.ensure(store)
        self.assertTrue(any(r["id"] == "mem_b2" for r in um.mem_rows(store)))
        um.mem_delete(store, "mem_b2")
        um.migrate(store)
        self.assertFalse(any(r["id"] == "mem_b2" for r in um.mem_rows(store)))
        self.assertEqual(store.one("select status from shared_facts where id='mem_b2'")["status"], "archived")   # kept, not deleted

    def test_brain_alias_shape_and_writes(self):
        store, _ = fixture()
        h = H(store)
        rows = [h.memory_json(r) for r in um.mem_rows(store)]
        self.assertEqual({r["id"] for r in rows}, {"mem_a1", "mem_b2"})                 # muse lines are not Brain rows
        self.assertEqual(rows[0]["id"], "mem_a1")                                       # pinned first
        self.assertEqual((rows[0]["entities"], rows[0]["use_count"], rows[0]["pinned"]), ("coffee", 3, True))
        mid, how = h.retain_memory({"text": "Joe prefers dark roast coffee black", "topic": "food", "kind": "opinion"})
        self.assertEqual(how, "merged")
        r = h.upsert_memory({"text": "Allergic to peanuts", "category": "health"})
        self.assertEqual(h.memory_json(um.mem_get(store, r["id"]))["category"], "health")
        out = json.loads(h.manage_memory_tool({"action": "search", "text": "peanuts allergic"}))
        self.assertEqual(out[0]["id"], r["id"])
        block, used = h.recall_memories("what coffee does he like, dark roast?")
        self.assertIn("coffee", block)
        # and it is a real user-scope fact
        self.assertEqual(sm.fact_out(store, store.one("select * from shared_facts where id=?", (r["id"],)))["scope"], "user")

    def test_muse_alias_roundtrip(self):
        store, _ = fixture()
        h = H(store)
        self.assertEqual({x["name"] for x in h.muse_list()} >= {"people", "about-me", "preferences"}, True)
        self.assertIn("Sam is Joe's flatmate", h.muse_read("people"))
        self.assertEqual(h.muse_append("people", "Lee plays football on Sundays"), "added")
        self.assertEqual([x["line"] for x in h.muse_numbered("people")], [2, 3, 4])
        h.muse_update("people", "Kim", "Kim moved to York")
        self.assertEqual([x["text"] for x in h.muse_numbered("people")][1].split(": ", 1)[1], "Kim moved to York")   # same position
        self.assertEqual(h.muse_forget("people", "Sam"), 1)
        self.assertIsNone(h.muse_read("nope"))
        self.assertTrue(h.muse_delete("people"))
        self.assertIsNone(h.muse_read("people"))
        self.assertIn("Name: Joe", h.muse_system_block())
        h.muse_write("about-me", "# About Me\n- 2026-10-01: Name: Joseph\n")
        self.assertIn("Joseph", h.muse_system_block()); self.assertNotIn("Name: Joe\n", h.muse_system_block())

    def test_user_scope_never_reaches_projects_but_agent_can_recall_across(self):
        store, _ = fixture()
        um.ensure(store)
        k = sm.resolve_project(store, project="demo")
        sm.remember(store, k, "The demo repo deploys with make ship to staging.")
        self.assertNotIn("coffee", sm.brief(store, k)[0])
        self.assertTrue(all(f["scope"] != "user" for f in sm.search(store, k, "coffee flatmate Sam")))
        self.assertNotIn("coffee", json.dumps(sm.mirror_files(store, k)))
        self.assertIn("coffee", sm.brief(store, "user")[0])
        h = H(store)
        h.muse_remember = None
        hits = um.recall_everywhere(store, "deploys staging")
        self.assertEqual(hits[0]["scope"], "project")


class CallerGate(unittest.TestCase):
    """The user scope never reaches a coding agent / MCP / node, whatever the request body says."""
    def setUp(self):
        self.store, _ = fixture()
        um.ensure(self.store)
        sm.remember(self.store, sm.resolve_project(self.store, project="demo"), "The demo repo deploys with make ship to staging.")

    def H(self, ua="Python-urllib/3.12", lan=False, hdr=None):
        import types
        return types.SimpleNamespace(server=types.SimpleNamespace(is_lan=lan), headers=dict({"User-Agent": ua}, **(hdr or {})))

    def call(self, caller, rest, method="POST", **body):
        return sm._dispatch(self.store, method, rest, body if method == "GET" else {}, body if method != "GET" else {}, caller)

    def test_caller_comes_from_the_auth_path(self):
        self.assertEqual(sm.caller_of(self.H()), "agent")                                  # MCP / plugin / node / curl
        self.assertEqual(sm.caller_of(self.H(ua="JoeBro/1.0 CFNetwork/1498 Darwin/23")), "app")
        self.assertEqual(sm.caller_of(self.H(lan=True)), "app")                           # authenticated phone listener
        os.environ["JOEBRO_APP_TOKEN"] = "s3cret"
        try:
            self.assertEqual(sm.caller_of(self.H(ua="JoeBro CFNetwork")), "agent")        # a UA is not enough once a token is set
            self.assertEqual(sm.caller_of(self.H(hdr={"X-JoeBro-App": "s3cret"})), "app")
        finally:
            del os.environ["JOEBRO_APP_TOKEN"]

    def test_mcp_style_all_projects_recall_gets_zero_user_facts(self):
        for body in ({"query": "coffee flatmate Sam bakery", "all_projects": True},
                     {"query": "coffee flatmate Sam bakery", "all_projects": True, "include_user": True, "project": "global"},
                     {"query": "coffee", "all_projects": True, "include_archived": True}):
            r = self.call("agent", "recall", **body)
            self.assertFalse([f for f in r["facts"] if f["scope"] == "user"], body)
        self.assertTrue(self.call("agent", "recall", query="deploys staging", all_projects=True)["facts"])   # projects still work
        self.assertTrue([f for f in self.call("app", "recall", query="coffee", all_projects=True)["facts"] if f["scope"] == "user"])

    def test_every_other_door_is_closed_for_agents(self):
        self.assertNotIn("user", [p["key"] for p in self.call("agent", "projects", "GET")["projects"]])
        self.assertIn("user", [p["key"] for p in self.call("app", "projects", "GET")["projects"]])
        self.assertFalse([f for f in self.call("agent", "", "GET")["facts"] if f["scope"] == "user"])
        self.assertFalse([f for f in self.call("agent", "", "GET", q="coffee")["facts"] if f["scope"] == "user"])
        for call in (lambda: self.call("agent", "brief", "GET", project="user"), lambda: self.call("agent", "recall", project="user", query="coffee"),
                     lambda: self.call("agent", "", text="Joe likes tea a lot.", project="user"), lambda: self.call("agent", "mem_a1", "GET"),
                     lambda: self.call("agent", "mem_a1/pin")):
            with self.assertRaises(LookupError):
                call()


if __name__ == "__main__":
    unittest.main()
