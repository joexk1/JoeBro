"""Shared Memory: supersede/archive (never delete), strength, dedupe, cap, the brief's
token budget, import of agent memory files, scopes, session records. No network, no model."""
import json, os, subprocess, tempfile, time, unittest
from pathlib import Path

import jb_sharedmem as sm
from jb_core import Store

CLAUDE_MD = """# Acme API

## Commands
Run the tests:
```
make test
```
- Lint with `ruff check .` before committing.
- The dev server is `make dev` and listens on port 8080.

## Conventions
- All database access goes through the repository classes in app/repo.
- Never import from app.internal outside the app package.
- Money is stored as integer cents, never floats.

## Gotchas
The upload worker needs Redis running locally or the integration tests hang without an error.
""" + "\n".join("- Module number %d owns feature area %d and has its own README with details about wiring." % (i, i) for i in range(60))
AGENTS_MD = """# Agents
## Commands
- Lint with `ruff check .` before committing.
- The dev server is `make dev` and listens on port 8080.
## Conventions
- Money is stored as integer cents, never floats.
- Pull requests need a changelog entry in CHANGELOG.md.
"""


def store():
    return Store(Path(tempfile.mkdtemp()))


class Memory(unittest.TestCase):
    def test_supersede_keeps_the_old_fact_and_links_both(self):
        s = store(); k = sm.resolve_project(s, remote="git@github.com:Acme/API.git", path="/code/api")
        self.assertEqual(k, "github.com/acme/api")
        old, outcome = sm.remember(s, k, "Tests run with make test against Postgres on port 5432.", "testing",
                                   prov={"agent": "claude", "model": "opus", "session": "s1", "host": "mac", "source": "mcp"})
        self.assertEqual(outcome, "added")
        new = sm.supersede(s, old["id"], "Tests run with make test against Postgres on port 5433.", {"agent": "codex"}, "port changed")
        old = sm.fact_out(s, sm._row(s, old["id"]))
        self.assertEqual((old["status"], old["superseded_by"]), ("superseded", new["id"]))
        self.assertTrue(old["valid_to"])
        self.assertEqual(old["provenance"], {"agent": "claude", "model": "opus", "session": "s1", "host": "mac", "source": "mcp"})
        self.assertEqual(new["supersedes"], old["id"])
        self.assertEqual([f["text"][-5:] for f in sm.browse(s, k)], ["5433."])
        out = sm._dispatch(s, "GET", new["id"], {}, {})
        self.assertEqual([f["id"] for f in out["history"]], [old["id"], new["id"]])

    def test_write_time_dedupe_and_supersede(self):
        s = store(); k = "p"
        a, _ = sm.remember(s, k, "Money is stored as integer cents, never floats.")
        b, outcome = sm.remember(s, k, "Money is stored as integer cents, never as floats")
        self.assertEqual((outcome, b["id"], b["recalls"]), ("deduped", a["id"], 1))
        c, outcome = sm.remember(s, k, "Money is stored as integer pence in the ledger tables, never floats.")
        self.assertEqual(outcome, "superseded")
        self.assertEqual(len(sm.browse(s, k)), 1)
        self.assertEqual(len(sm.browse(s, k, status="all")), 2)

    def test_rejects_logs_and_secrets(self):
        s = store()
        for bad in ("ok", "x" * 500, "the deploy password = hunter2hunter2 for prod", "key sk-abcdefghijklmnopqrstuvwx"):
            with self.assertRaises(ValueError):
                sm.remember(s, "p", bad)

    def test_strength_grows_on_recall_and_decays(self):
        s = store(); k = "p"
        a, _ = sm.remember(s, k, "The upload worker needs Redis running locally.")
        b, _ = sm.remember(s, k, "Deploys go through the release script in scripts/release.sh.")
        hits = sm.search(s, k, "redis upload worker", do_reinforce=True)
        self.assertEqual(hits[0]["id"], a["id"])
        self.assertGreater(sm._row(s, a["id"])["strength"], 0.6)
        self.assertEqual(sm._row(s, b["id"])["strength"], 0.5)        # not recalled, not reinforced
        s.exec("UPDATE shared_facts SET last_used=? WHERE id=?", (time.time() - 60 * 86400, b["id"]))
        self.assertAlmostEqual(sm.effective(sm._row(s, b["id"])), 0.125, places=2)
        sm.edit(s, b["id"], {"pinned": True})
        s.exec("UPDATE shared_facts SET last_used=? WHERE id=?", (time.time() - 600 * 86400, b["id"]))
        self.assertAlmostEqual(sm.effective(sm._row(s, b["id"])), 0.125, places=2)   # pinned: frozen

    def test_consolidation_merges_weak_similar_notes_and_archives(self):
        s = store(); k = "p"
        ids = [sm._insert(s, k, t, "ci", "fact", False, {}, 0.5)["id"] for t in (
            "CI runs the integration suite on every push to main using GitHub Actions.",
            "The integration suite runs in CI on GitHub Actions for each push.",
            "An unrelated note about the colour palette of the marketing site.")]
        s.exec("UPDATE shared_facts SET last_used=?", (time.time() - 45 * 86400,))       # weak, not gone
        s.exec("UPDATE shared_facts SET last_used=? WHERE id=?", (time.time() - 90 * 86400, ids[2]))
        dry = sm.consolidate(s, k, dry_run=True)
        self.assertEqual((dry["merged"], dry["archived"]), (1, 1))
        self.assertEqual(len(sm.browse(s, k)), 3)
        out = sm.consolidate(s, k)
        self.assertEqual((out["merged"], out["archived"]), (1, 1))
        live = sm.browse(s, k)
        self.assertEqual(len(live), 1)
        self.assertEqual(len(live[0]["merged_from"]), 1)
        self.assertEqual(s.one("SELECT COUNT(*) AS n FROM shared_facts")["n"], 3)        # archived, never deleted
        back = sm.restore(s, ids[2])
        self.assertEqual(back["status"], "active")

    def test_project_cap_archives_the_weakest(self):
        s = store(); k = "p"
        for i in range(8):
            sm._insert(s, k, "Distinct durable fact number %d about subsystem %d." % (i, i), "", "fact", False, {}, 0.1 + i / 10)
        self.assertEqual(sm.enforce_cap(s, k, cap=5), 3)
        self.assertEqual(sorted(f["base_strength"] for f in sm.browse(s, k)), [0.4, 0.5, 0.6, 0.7, 0.8])

    def test_brief_stays_under_400_tokens_and_ranks_by_strength(self):
        s = store(); k = sm.resolve_project(s, project="proj")
        for i in range(120):
            sm._insert(s, k, "Fact %d: the %d-th module has a long explanation about how its wiring works in practice here." % (i, i),
                       "arch", "fact", False, {}, 0.3)
        top = sm._insert(s, k, "Run the whole test suite with make test.", "testing", "howto", True, {}, 0.9)
        text, facts, used = sm.brief(s, k)
        self.assertLessEqual(used, sm.BRIEF_TOKENS)
        self.assertLessEqual(len(text), sm.BRIEF_TOKENS * 4)
        self.assertEqual(facts[0]["id"], top["id"])
        self.assertLess(len(facts), 30)
        self.assertEqual(sm._row(s, top["id"])["recalls"], 0)       # reading the brief does not reinforce
        self.assertLessEqual(sm.brief(s, k, budget=5000)[2], sm.BRIEF_TOKENS)       # the cap is hard
        self.assertLess(sm.brief(store(), "global")[2], 60)         # empty memory costs almost nothing

    def test_recall_is_compact(self):
        s = store(); k = "p"
        for i in range(40):
            sm._insert(s, k, "The payment module %d retries failed charges with backoff." % i, "", "fact", False, {}, 0.5)
        out = sm._dispatch(s, "POST", "recall", {}, {"project": k, "query": "payment retries"})
        self.assertEqual(len(out["facts"]), sm.RECALL_LIMIT)
        self.assertLess(sm.tokens(" ".join(f["text"] for f in out["facts"])), 150)

    def test_import_dedupes_agent_files_and_saves_tokens(self):
        s = store(); k = sm.resolve_project(s, project="acme")
        out = sm.import_files(s, k, [{"path": "CLAUDE.md", "text": CLAUDE_MD, "agent": "claude"},
                                     {"path": "AGENTS.md", "text": AGENTS_MD, "agent": "codex"}])
        self.assertEqual(out["files"], 2)
        self.assertGreaterEqual(out["deduped"], 3)                  # the lines both files shared are stored once
        texts = [f["text"] for f in sm.browse(s, k)]
        self.assertEqual(len([t for t in texts if "integer cents" in t]), 1)
        self.assertTrue(any("make test" in t for t in texts))       # the command in a code block survives
        self.assertTrue(any("changelog" in t.lower() for t in texts))
        self.assertLessEqual(out["brief_tokens"], sm.BRIEF_TOKENS)
        self.assertLess(out["pointer_tokens"], 150)
        self.assertGreater(out["tokens_before"], 1500)
        self.assertLess(out["tokens_after"], out["tokens_before"] / 2)
        print("\n  import: %d tokens of native memory files -> %d per session (brief %d + pointer %d)" % (
            out["tokens_before"], out["tokens_after"], out["brief_tokens"], out["pointer_tokens"]))
        again = sm.import_files(s, k, [{"path": "CLAUDE.md", "text": CLAUDE_MD + sm.slim_pointer(), "agent": "claude"}])
        self.assertEqual(again["added"], 0)                         # importing twice adds nothing

    def test_user_scope_never_reaches_a_project(self):
        s = store(); k = sm.resolve_project(s, project="proj")
        sm.remember(s, "user", "He prefers short answers and British spelling.", prov={"agent": "joebro", "source": "app"})
        sm.remember(s, "global", "Always write commit messages in the imperative mood.")
        sm.remember(s, k, "This repo uses British spelling in user-facing strings.")
        text, facts, _ = sm.brief(s, k)
        self.assertNotIn("short answers", text)
        self.assertIn("imperative", text)
        hits = sm._dispatch(s, "POST", "recall", {}, {"project": k, "query": "British spelling short answers"})["facts"]
        self.assertEqual({f["scope"] for f in hits}, {"project"})
        self.assertNotIn("short answers", json.dumps(sm.mirror_files(s, k)))
        self.assertIn("short answers", sm.brief(s, "user")[0])      # the personal agent's own brief
        everything = sm._dispatch(s, "POST", "recall", {}, {"project": "user", "query": "British spelling", "all_projects": True})["facts"]
        self.assertEqual({f["scope"] for f in everything}, {"project", "user"})
        with self.assertRaises(ValueError):
            sm._dispatch(s, "POST", "import", {}, {"project": "user", "files": []})

    def test_mirror_is_written_into_the_repo_when_it_opted_in(self):
        s = store(); repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        k = sm.resolve_project(s, path=repo)
        sm.remember(s, k, "The build needs Node 22 or newer.")
        self.assertFalse(os.path.exists(os.path.join(repo, ".joebro")))        # not without opting in
        os.makedirs(os.path.join(repo, ".joebro", "memory"))
        f, _ = sm.remember(s, k, "Run database migrations with make migrate.")
        md = open(os.path.join(repo, ".joebro", "memory", "MEMORY.md")).read()
        self.assertIn("make migrate", md)
        sm.archive(s, f["id"], "wrong"); sm.write_mirror(s, k)
        self.assertIn("make migrate", open(os.path.join(repo, ".joebro", "memory", "ARCHIVE.md")).read())
        self.assertNotIn("make migrate", open(os.path.join(repo, ".joebro", "memory", "MEMORY.md")).read())

    def test_session_records_keep_the_last_n_and_stay_out_of_the_brief(self):
        s = store()
        for i in range(sm.SESSIONS_KEEP + 4):
            sm.record_session(s, {"id": "code:mac:claude:s%d" % i, "cwd": "/code/api", "source": "claude", "model": "opus",
                                  "host": "mac", "session_id": "s%d" % i}, "Finished step %d of the uploader work" % i, "feature/x")
        rows = sm.recent_sessions(s, "/code/api")
        self.assertEqual(len(rows), sm.SESSIONS_KEEP)
        self.assertEqual((rows[0]["resume_id"], rows[0]["branch"], rows[0]["agent"]), ("s%d" % (sm.SESSIONS_KEEP + 3), "feature/x", "claude"))
        self.assertNotIn("uploader", sm.brief(s, "/code/api")[0])


if __name__ == "__main__":
    unittest.main()
