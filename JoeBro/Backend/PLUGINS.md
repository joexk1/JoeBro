# JoeBro Plugin Standard — `joebro.plugin/1`

A plugin is a folder with a `joebro-plugin.json` manifest. One plugin can give
JoeBro **tools** (functions the agent calls), a **guardrail** (an instruction
applied to every turn), and **panels** (its own UI inside the JoeBro Mac app).
Every plugin, built-in or custom, appears in the same place (Tools → Plugins)
with one on/off switch and one access level.

```
my-plugin/
  joebro-plugin.json      required
  tools.py                python runtime entry (if it has tools)
  ui/index.html           panel(s), everything under ui/
  README.md               optional
```

## Manifest

```json
{
  "schema": "joebro.plugin/1",
  "id": "plant-care",
  "name": "Plant care",
  "version": "1.0.0",
  "description": "Tracks when your plants were last watered and which are due.",
  "author": "JoeBro",
  "permission": "sandbox",
  "runtime": {"type": "python", "entry": "tools.py"},
  "contributes": {
    "tools": [
      {"name": "plants_due",
       "description": "List plants that need watering today. Use when the user asks about their plants.",
       "parameters": {"type": "object", "properties": {}}},
      {"name": "plant_watered",
       "description": "Record that a plant was just watered.",
       "parameters": {"type": "object", "properties": {"plant": {"type": "string"}}, "required": ["plant"]}}
    ],
    "guardrail": "When the user mentions watering a plant, record it with plant_watered.",
    "panels": [{"id": "plants", "title": "Plants", "icon": "leaf", "entry": "ui/index.html"}]
  }
}
```

| field | rules |
|---|---|
| `schema` | exactly `"joebro.plugin/1"` |
| `id` | slug: `a-z 0-9 -`, 2–40 chars, starts with a letter. The folder name. |
| `name`, `description` | required. The description is what the user reads in the app. |
| `version` | `1.0.0` style. Bump it on every change. |
| `permission` | `readonly` (never changes anything), `sandbox` (default), or `full` (computer use, sending, spending). A session only gets the plugin's tools if its own access level is at least this. The user can raise it in the app. |
| `runtime` | needed when it has tools. `{"type":"python","entry":"tools.py"}` or `{"type":"mcp","command":"node","args":["server.js"]}` (any MCP stdio server; command is python3/node/npx/uvx/uv/bun/deno/bash/sh or a relative path inside the plugin). |
| `contributes.tools` | python runtime: declare each tool (`name` snake_case, `description` says WHEN to use it, `parameters` JSON Schema `type: object`). mcp runtime: optional (tools are discovered). |
| `contributes.guardrail` | text, ≤ 2000 chars, added to the system prompt of every turn while the plugin is on. |
| `contributes.panels` | 1–5 of `{id, title, icon (SF Symbol), entry: "ui/….html", surfaces?: ["mac","ios","web"]}`. |

## Python tools

`tools.py` defines a plain function per declared tool, same name, keyword
arguments = the tool's parameters. Return a string or anything JSON-serialisable.
Raise an exception to report a failure (the agent sees the message). `print()`
goes to the log, never the protocol. Stdlib only unless the dependency is
already installed where the JoeBro backend runs.

```python
import json, os, time
DATA = os.path.join(os.environ["JOEBRO_PLUGIN_DATA"], "plants.json")

def _load():
    try: return json.load(open(DATA))
    except FileNotFoundError: return {}

def plants_due():
    now = time.time()
    return [p for p, t in _load().items() if now - t > 3 * 86400]

def plant_watered(plant):
    d = _load(); d[plant] = time.time(); json.dump(d, open(DATA, "w"))
    return "Noted: %s watered." % plant
```

The process runs where the JoeBro backend runs (your Mac, or your Pi) with `cwd` = the plugin folder and env:
`JOEBRO_PLUGIN_ID`, `JOEBRO_PLUGIN_DIR`, `JOEBRO_PLUGIN_DATA` (persistent,
writable, survives updates; keep state here, not in the plugin folder),
`JOEBRO_URL`. Each call is a fresh process with a 30 s limit, so tools must be
quick and stateless apart from `JOEBRO_PLUGIN_DATA`.

## Panels

A panel is an HTML page in `ui/`. Plain HTML/CSS/JS, no build step needed, and
all assets are relative paths inside `ui/`. The page cannot reach the network
(`connect-src 'none'`). It talks to its own plugin through the bridge the app
injects:

```js
const plants = await joebro.call("plants_due", {});   // -> parsed JSON (or string)
await joebro.call("plant_watered", {plant: "fern"});
joebro.plugin   // "plant-care"
joebro.theme    // "dark" | "light"
```

`joebro.call` only reaches this plugin's own tools. Use the system font,
transparent or dark-friendly backgrounds, and respect `prefers-color-scheme`.

## Installing

Install = POST the files, all at once, to the JoeBro backend (auth: bearer token, if you set one).

* **Coding agents**: the `joebro` MCP server → `joebro_plugin_spec`, `joebro_plugin_install`
  (a local folder path), `joebro_plugin_list`, `joebro_plugin_source`,
  `joebro_plugin_test`. Or the CLI: `joebro plugin init <id>`, `joebro plugin push <dir> [--enable]`.
* **JoeBro itself**: the same tools, when the `joebro` MCP server is added in Tools → MCP.
* **HTTP**: `POST /api/plugins/install {"files": {"joebro-plugin.json": "...", "tools.py": "..."}, "enable": false}`

Every install is validated (all problems are listed at once), compiled, staged
and swapped atomically. The previous version is kept: `POST /api/plugins/<id>/rollback`.
A **new** plugin starts **off** and you get a ping; an update keeps its switch.
Agents ask before switching one on, unless you asked for the plugin.

Other routes: `GET /api/plugins` (all, with `contributes`), `GET /api/plugins/spec`,
`GET /api/plugins/<id>/source`, `POST /api/plugins/<id>/call {"tool","arguments"}`
(test a tool), `POST /api/plugins/reload`, `DELETE /api/plugins/<id>`.

## Checklist for an agent building a plugin

1. Read this spec and `GET /api/plugins` (don't duplicate an existing plugin or a built-in tool).
2. Smallest useful version: one or two tools with descriptions that say *when* to use them.
3. Install, then `joebro_plugin_test` every tool with real arguments.
4. Only then a panel, if the user benefits from seeing/tapping it.
5. Tell the user what it does and that it is off until they switch it on.
