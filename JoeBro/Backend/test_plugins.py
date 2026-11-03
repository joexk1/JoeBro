"""joebro.plugin/1: validation, install/stage/swap, MCP-backed tools end to end,
guardrails, panels, toggle mirroring, rollback, delete. Real store + real
runtime subprocess; nothing on the network."""
import json, os, tempfile, unittest
from pathlib import Path

os.environ["JOEBRO_PLUGINS_DIR"] = tempfile.mkdtemp()
os.environ["JOEBRO_PLUGIN_DATA_ROOT"] = tempfile.mkdtemp()

from jb_core import Store, now_iso  # noqa: E402
from jb_models import ModelsMixin  # noqa: E402
from jb_tools import ToolsMixin  # noqa: E402
from jb_assistant import AssistantMixin  # noqa: E402
from jb_plugins import PluginsMixin, PluginError, _root, validate_manifest  # noqa: E402

MANIFEST = {
    "schema": "joebro.plugin/1", "id": "plant-care", "name": "Plant care", "version": "1.0.0",
    "description": "Tracks watering.", "permission": "sandbox",
    "runtime": {"type": "python", "entry": "tools.py"},
    "contributes": {
        "tools": [{"name": "plants_due", "description": "List plants that need watering.",
                   "parameters": {"type": "object", "properties": {}}},
                  {"name": "plant_watered", "description": "Record that a plant was watered.",
                   "parameters": {"type": "object", "properties": {"plant": {"type": "string"}}, "required": ["plant"]}}],
        "guardrail": "When the user mentions watering a plant, record it.",
        "panels": [{"id": "plants", "title": "Plants", "icon": "leaf", "entry": "ui/index.html"}]}}
TOOLS = '''import json, os
DATA = os.path.join(os.environ["JOEBRO_PLUGIN_DATA"], "p.json")
def _load():
    try: return json.load(open(DATA))
    except FileNotFoundError: return {}
def plants_due():
    print("noise that must not break the protocol")
    return sorted(_load())
def plant_watered(plant, extra_ignored=None):
    d = _load(); d[plant] = 1; json.dump(d, open(DATA, "w")); return "ok " + plant
'''


def files(manifest=None, tools=TOOLS, **extra):
    f = {"joebro-plugin.json": json.dumps(manifest or MANIFEST), "tools.py": tools, "ui/index.html": "<h1>Plants</h1>"}
    f.update(extra)
    return f


class F(PluginsMixin, ModelsMixin, ToolsMixin, AssistantMixin):
    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()))
        self.notes = []
        now = now_iso()
        for pid, name, src in (("plugin_macos_use", "Agent Desktop", "bundled"), ("plugin_advisor", "Advisor", "bundled")):
            self.store.exec("insert or ignore into plugins(id,name,source,created_at,updated_at) values(?,?,?,?,?)",
                            (pid, name, src, now, now))
    def notify(self, *a, **k): self.notes.append((a, k)); return {}
    def pref(self, k): return None


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.f = F()

    def test_validation_lists_every_problem(self):
        bad = dict(MANIFEST, schema="x", id="Bad Id", version="v1", permission="god",
                   contributes={"tools": [{"name": "Bad-Name"}], "panels": [{"id": "p", "title": "P", "entry": "../x.html"}], "nope": 1})
        with self.assertRaises(PluginError) as cm:
            validate_manifest(bad)
        msg = str(cm.exception)
        for bit in ("schema", "id must", "version", "permission", "unknown contributes", "snake_case", "panels[0]"):
            self.assertIn(bit, msg)

    def test_rejects_traversal_syntax_errors_and_cleans_staging(self):
        with self.assertRaises(PluginError):
            self.f.plugin_install({"files": files(**{"../evil.py": "x"})})
        with self.assertRaises(PluginError) as cm:
            self.f.plugin_install({"files": files(tools="def broken(:\n")})
        self.assertIn("does not compile", str(cm.exception))
        self.assertFalse([p for p in _root().iterdir() if p.name.startswith(".staging")])
        self.assertIsNone(self.f._manifest_row("plant-care"))

    def test_install_off_then_enable_runs_tools_end_to_end(self):
        out = self.f.plugin_install({"files": files()})["plugin"]
        self.assertFalse(out["is_enabled"])
        self.assertEqual(out["contributes"]["tools"], ["plants_due", "plant_watered"])
        self.assertEqual(out["contributes"]["panels"][0]["url"], "/api/plugins/plant-care/ui/index.html")
        self.assertEqual(len(self.f.notes), 1)
        mrow = self.f.store.one("select * from mcp_servers where id='mcp_plugin_plant_care'")
        self.assertEqual(mrow["enabled"], 0)
        with self.assertRaises(PluginError):
            self.f.plugin_call("plant-care", "plants_due", {})
        # the app's switch
        self.f.upsert_plugin({"is_enabled": "true"}, "plugin_plant_care")
        mrow = self.f.store.one("select * from mcp_servers where id='mcp_plugin_plant_care'")
        self.assertEqual(mrow["enabled"], 1, mrow["error"])
        self.assertEqual([t["name"] for t in json.loads(mrow["tools_json"])], ["plants_due", "plant_watered"])
        r = self.f.plugin_call("plant-care", "plant_watered", {"plant": "fern", "extra_ignored": 1, "unknown": 2})
        self.assertEqual(r["exit_code"], 0, r)
        r = self.f.plugin_call("plant-care", "plants_due", {})
        self.assertEqual(json.loads(r["output"]), ["fern"])
        # tools reach the agent through the normal MCP path, gated by session access
        names = [s["function"]["name"] for s in self.f.mcp_tool_schemas({"permission_mode": "sandbox"})]
        self.assertTrue(any("plants_due" in n for n in names), names)

    def test_guardrail_panel_and_switch_off(self):
        self.f.plugin_install({"files": files(), "enable": True})
        guards = [g["description"] for g in self.f._active_background_plugins()]
        self.assertIn(MANIFEST["contributes"]["guardrail"], guards)
        self.assertEqual(self.f.plugin_ui_file("plant-care", "index.html")[0], b"<h1>Plants</h1>")
        self.assertIsNone(self.f.plugin_ui_file("plant-care", "../tools.py"))
        self.assertIsNone(self.f.plugin_ui_file("plant-care", "../joebro-plugin.json"))
        self.f.upsert_plugin({"is_enabled": "false"}, "plugin_plant_care")
        self.assertIsNone(self.f.plugin_ui_file("plant-care", "index.html"))
        self.assertEqual(self.f._active_background_plugins(), [])
        self.assertEqual(self.f.store.one("select enabled from mcp_servers where id='mcp_plugin_plant_care'")["enabled"], 0)

    def test_update_keeps_switch_and_permission_rollback_and_delete(self):
        self.f.plugin_install({"files": files(), "enable": True})
        self.f.upsert_plugin({"permission_mode": "full"}, "plugin_plant_care")
        m2 = dict(MANIFEST, version="1.1.0")
        out = self.f.plugin_install({"files": files(m2)})["plugin"]
        self.assertTrue(out["is_enabled"])
        self.assertEqual(out["version"], "1.1.0")
        self.assertEqual(out["permission_mode"], "full")
        self.assertEqual(len(self.f.notes), 1)   # updates don't ping
        self.assertEqual(self.f.plugin_rollback("plant-care")["plugin"]["version"], "1.0.0")
        src = self.f.plugin_source("plant-care")["files"]
        self.assertIn("tools.py", src)
        self.assertTrue(self.f.plugin_delete("plant-care"))
        self.assertIsNone(self.f._manifest_row("plant-care"))
        self.assertIsNone(self.f.store.one("select 1 from mcp_servers where id='mcp_plugin_plant_care'"))
        self.assertFalse((_root() / "plant-care").exists())

    def test_list_describes_builtins_uniformly(self):
        self.f.plugin_install({"files": files()})
        items = {p["id"]: p for p in self.f.plugins_list()["plugins"]}
        self.assertEqual(items["plugin_macos_use"]["contributes"]["tools"], ["desktop_use"])
        self.assertTrue(items["plugin_plant_care"]["managed"])
        self.assertFalse(items["plugin_macos_use"]["managed"])
        for p in items.values():
            self.assertEqual(p["schema"], "joebro.plugin/1")

    def test_reserved_and_taken_ids(self):
        with self.assertRaises(PluginError):
            validate_manifest(dict(MANIFEST, id="advisor"))
        self.f.plugin_reload()   # empty dir: nothing breaks, bundled rows untouched
        self.assertIsNotNone(self.f.store.one("select 1 from plugins where id='plugin_advisor'"))


if __name__ == "__main__":
    unittest.main()
