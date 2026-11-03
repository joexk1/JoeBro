"""JoeBro Plugin Standard v1 (`joebro.plugin/1`).

A plugin is a folder with a `joebro-plugin.json` manifest. It can contribute:
  - tools      -> run by jb_plugin_runtime.py as an MCP server, so they get the
                  whole existing MCP path for free (deferred schemas, find_tools,
                  per-session permission gating, timeouts, error capture);
  - guardrail  -> text applied to every turn (what "background plugins" were);
  - panels     -> HTML UIs under ui/, shown as panels in the Mac app (and any
                  client) through the authenticated /api/plugins/<id>/ui/ route,
                  with a `window.joebro` bridge to call the plugin's own tools.

Plugins live in ~/.joebro-personal/plugins/<id>/ and are installed through one
API (POST /api/plugins/install) that a person, JoeBro itself, or any coding
agent (JoeBro Agents MCP / `joebro-plugin push`) uses. Installs are validated,
staged and swapped atomically; the previous version is kept for rollback, and a
newly installed plugin starts DISABLED unless the installer asks otherwise.

The three original bundled plugins (Agent Desktop, Advisor, Agent Reach) stay
code-backed with their ids, but are described in the same shape so every
client lists one uniform kind of thing."""
import json
import mimetypes
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

SCHEMA = "joebro.plugin/1"
def _root():
    """Where installed plugins live. The production app points this at its data
    folder (JOEBRO_PLUGINS_DIR); the personal backend uses the default."""
    return Path(os.path.expanduser(os.environ.get("JOEBRO_PLUGINS_DIR", "~/.joebro-personal/plugins")))
RUNTIME = Path(__file__).with_name("jb_plugin_runtime.py")
SPEC_FILE = Path(__file__).with_name("PLUGINS.md")
ID_RX = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
TOOL_RX = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
PERMS = ("readonly", "sandbox", "full")
MCP_COMMANDS = {"python3", "python", "node", "npx", "uvx", "uv", "bun", "deno", "bash", "sh"}
MAX_FILES, MAX_BYTES = 80, 3_000_000
RESERVED = {"macos-use", "advisor", "agent-reach"}

# Code-backed plugins, described in the standard shape (ids kept for client compat).
BUILTIN = {
    "plugin_macos_use": {"version": "1.0.2", "contributes": {"tools": ["desktop_use"]},
                         "summary": "Drives Mac apps through Vercel agent-desktop (accessibility tree)."},
    "plugin_advisor": {"version": "1", "contributes": {"tools": ["advisor"]},
                       "summary": "Lets the everyday model consult a stronger one."},
    "plugin_agent_reach": {"version": "1", "contributes": {"tools": ["agent_reach"]},
                           "summary": "Reads a URL / video / post through agent-reach."},
}


class PluginError(ValueError):
    pass


def _rel_ok(rel):
    p = Path(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and not rel.startswith(("~", "."))


def validate_manifest(m, pdir=None):
    """Normalised manifest, or PluginError listing every problem at once (agents
    fix all of them in one round instead of one per install)."""
    errs = []
    if not isinstance(m, dict):
        raise PluginError("joebro-plugin.json must be a JSON object")
    if m.get("schema") != SCHEMA:
        errs.append('schema must be "%s"' % SCHEMA)
    pid = str(m.get("id") or "")
    if not ID_RX.match(pid):
        errs.append("id must be lowercase letters/digits/dashes, 2-40 chars, starting with a letter")
    elif pid in RESERVED:
        errs.append("id %r is reserved" % pid)
    for k, n in (("name", 60), ("description", 600)):
        if not isinstance(m.get(k), str) or not m[k].strip() or len(m[k]) > n:
            errs.append("%s is required (<= %d chars)" % (k, n))
    if not re.match(r"^\d+(\.\d+){0,2}([-+][\w.]+)?$", str(m.get("version") or "")):
        errs.append('version must look like "1.0.0"')
    perm = m.get("permission", "sandbox")
    if perm not in PERMS:
        errs.append("permission must be one of " + ", ".join(PERMS))
    c = m.get("contributes")
    if not isinstance(c, dict) or not c:
        errs.append("contributes must be an object with at least one of tools, guardrail, panels")
        c = {}
    unknown = set(c) - {"tools", "guardrail", "panels"}
    if unknown:
        errs.append("unknown contributes keys: " + ", ".join(sorted(unknown)))
    rt = m.get("runtime")
    tools = c.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not tools or len(tools) > 30:
            errs.append("contributes.tools must be a list of 1-30 tools")
            tools = []
        seen = set()
        for i, t in enumerate(tools):
            if not isinstance(t, dict) or not TOOL_RX.match(str(t.get("name") or "")):
                errs.append("tools[%d].name must be snake_case (2-48 chars)" % i)
                continue
            if t["name"] in seen:
                errs.append("duplicate tool %s" % t["name"])
            seen.add(t["name"])
            if not isinstance(t.get("description"), str) or len(t["description"].strip()) < 10:
                errs.append("tools[%d].description is required (say when the model should use it)" % i)
            p = t.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(p, dict) or p.get("type") != "object":
                errs.append("tools[%d].parameters must be a JSON Schema with type object" % i)
    if rt is not None or tools is not None:
        if not isinstance(rt, dict) or rt.get("type") not in ("python", "mcp"):
            errs.append('tools need runtime {"type":"python","entry":"tools.py"} or {"type":"mcp","command":..,"args":[..]}')
        elif rt["type"] == "python":
            if not tools:
                errs.append("a python runtime needs contributes.tools declared")
            if not _rel_ok(str(rt.get("entry") or "")) or not str(rt.get("entry")).endswith(".py"):
                errs.append("runtime.entry must be a relative .py path inside the plugin")
            elif pdir and not (Path(pdir) / rt["entry"]).is_file():
                errs.append("runtime.entry %s does not exist" % rt["entry"])
        else:
            cmd = str(rt.get("command") or "")
            if cmd not in MCP_COMMANDS and not _rel_ok(cmd):
                errs.append("runtime.command must be one of %s or a relative path inside the plugin" % ", ".join(sorted(MCP_COMMANDS)))
            if not isinstance(rt.get("args", []), list) or not all(isinstance(a, str) for a in rt.get("args", [])):
                errs.append("runtime.args must be a list of strings")
    g = c.get("guardrail")
    if g is not None and (not isinstance(g, str) or not g.strip() or len(g) > 2000):
        errs.append("contributes.guardrail must be text (<= 2000 chars)")
    panels = c.get("panels")
    if panels is not None:
        if not isinstance(panels, list) or not panels or len(panels) > 5:
            errs.append("contributes.panels must be a list of 1-5 panels")
            panels = []
        for i, p in enumerate(panels):
            if not isinstance(p, dict) or not ID_RX.match(str(p.get("id") or "")) or not str(p.get("title") or "").strip():
                errs.append("panels[%d] needs id (slug) and title" % i)
                continue
            entry = str(p.get("entry") or "")
            if not entry.startswith("ui/") or not _rel_ok(entry) or not entry.endswith(".html"):
                errs.append("panels[%d].entry must be an .html file under ui/" % i)
            elif pdir and not (Path(pdir) / entry).is_file():
                errs.append("panels[%d].entry %s does not exist" % (i, entry))
            if p.get("surfaces") is not None and not set(p["surfaces"]) <= {"mac", "ios", "web"}:
                errs.append("panels[%d].surfaces may only contain mac, ios, web" % i)
    if errs:
        raise PluginError("invalid joebro-plugin.json:\n- " + "\n- ".join(errs))
    m = dict(m, permission=perm)
    return m


def row_id(slug):
    return "plugin_" + slug.replace("-", "_")


def mcp_id(slug):
    return "mcp_plugin_" + slug.replace("-", "_")


class PluginsMixin:
    # ------------------------------------------------------------------ schema
    def _plugins_schema(self):
        for col, decl in (("manifest_json", "text default ''"), ("version", "text default ''"),
                          ("error", "text default ''"), ("slug", "text default ''")):
            try:
                self.store.exec("alter table plugins add column %s %s" % (col, decl))
            except Exception:
                pass

    def _manifest_row(self, ident):
        """Row by row id (plugin_x) or slug (x)."""
        self._plugins_schema()
        return self.store.one("select * from plugins where id=? or (slug<>'' and slug=?)", (ident, ident))

    # ------------------------------------------------------------------ install / sync
    def plugin_install(self, body):
        """body: {files: {relpath: text}, enable?: bool} or {path: "/abs/dir"} (on the backend machine).
        Validates, stages, swaps, registers, discovers tools. Returns the plugin."""
        self._plugins_schema()
        _root().mkdir(parents=True, exist_ok=True)
        stage = _root() / (".staging-" + os.urandom(4).hex())
        try:
            if body.get("path"):
                src = Path(str(body["path"])).expanduser()
                if not (src / "joebro-plugin.json").is_file():
                    raise PluginError("no joebro-plugin.json in %s" % src)
                shutil.copytree(src, stage, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".DS_Store"))
            else:
                files = body.get("files")
                if not isinstance(files, dict) or "joebro-plugin.json" not in files:
                    raise PluginError("files must be an object of {relative path: text} including joebro-plugin.json")
                if len(files) > MAX_FILES:
                    raise PluginError("too many files (max %d)" % MAX_FILES)
                total = 0
                for rel, text in files.items():
                    if not _rel_ok(rel) or not isinstance(text, str):
                        raise PluginError("bad file entry %r (relative path, text content)" % rel)
                    total += len(text.encode())
                    if total > MAX_BYTES:
                        raise PluginError("plugin too large (max %d bytes)" % MAX_BYTES)
                    dest = stage / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text)
            try:
                manifest = json.loads((stage / "joebro-plugin.json").read_text())
            except ValueError as exc:
                raise PluginError("joebro-plugin.json is not valid JSON: %s" % exc)
            manifest = validate_manifest(manifest, stage)
            rt = manifest.get("runtime") or {}
            if rt.get("type") == "python":
                try:
                    compile((stage / rt["entry"]).read_text(), rt["entry"], "exec")
                except SyntaxError as exc:
                    raise PluginError("%s does not compile: line %s: %s" % (rt["entry"], exc.lineno, exc.msg))
            slug = manifest["id"]
            target = _root() / slug
            existing = self._manifest_row(slug)
            if existing and existing.get("source") != "manifest":
                raise PluginError("id %r is taken by a non-manifest plugin" % slug)
            if target.exists():
                trash = _root() / ".previous"
                trash.mkdir(exist_ok=True)
                prev = trash / slug
                if prev.exists():
                    shutil.rmtree(prev)
                target.rename(prev)
            stage.rename(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        enable = body.get("enable")
        if enable is None:
            enabled = bool(existing and existing.get("is_enabled"))   # updates keep their toggle; new = off
        else:
            enabled = str(enable).lower() in ("1", "true", "yes", "on")
        out = self.plugin_sync(slug, enabled=enabled)
        if not existing:
            try:
                bits = [("tools: " + ", ".join(out["contributes"]["tools"])) if out["contributes"]["tools"] else "",
                        ("panels: " + ", ".join(p["title"] for p in out["contributes"]["panels"])) if out["contributes"]["panels"] else "",
                        "guardrail" if out["contributes"]["guardrail"] else ""]
                self.notify("New plugin “%s” installed%s — %s" % (out["name"], "" if enabled else " (off until you switch it on in Tools)",
                                                                  "; ".join(b for b in bits if b) or "no contributions"),
                            title="🧩 Plugin", kind="plugin", source=out["id"])
            except Exception:
                pass
        return {"ok": True, "plugin": out}

    def plugin_sync(self, slug, enabled=None):
        """Folder -> plugins row (+ mcp_servers row when it has tools)."""
        self._plugins_schema()
        pdir = _root() / slug
        rid = row_id(slug)
        existing = self.store.one("select * from plugins where id=?", (rid,))
        try:
            manifest = validate_manifest(json.loads((pdir / "joebro-plugin.json").read_text()), pdir)
            error = ""
        except (OSError, ValueError) as exc:
            manifest, error = None, str(exc)[:1500]
        if enabled is None:
            enabled = bool(existing and existing.get("is_enabled"))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if manifest is None:
            enabled = False
        c = (manifest or {}).get("contributes") or {}
        kind = "background" if c.get("guardrail") and not c.get("tools") and not c.get("panels") else "foreground"
        vals = dict(name=(manifest or {}).get("name") or (existing or {}).get("name") or slug,
                    kind=kind, repo_path=str(pdir), description=(manifest or {}).get("description") or "",
                    is_enabled=1 if enabled else 0,
                    # the manifest declares the default; a level the user set in the app wins
                    permission_mode=(existing or {}).get("permission_mode") or (manifest or {}).get("permission") or "sandbox",
                    manifest_json=json.dumps(manifest or {}), version=(manifest or {}).get("version") or "",
                    error=error, slug=slug)
        if existing:
            self.store.exec("update plugins set " + ",".join(k + "=?" for k in vals) + ",updated_at=? where id=?",
                            tuple(vals.values()) + (now, rid))
        else:
            self.store.exec("insert into plugins(id,source,created_at,updated_at," + ",".join(vals) + ") values(?,?,?,?" +
                            ",?" * len(vals) + ")", (rid, "manifest", now, now) + tuple(vals.values()))
        self._plugin_mirror_mcp(rid, force=True)
        return self.plugin_public(self.store.one("select * from plugins where id=?", (rid,)))

    def _plugin_mirror_mcp(self, rid, force=False):
        """Keep the plugin's MCP row in step with its toggle/permission (called
        after every plugin update, including the app's switch)."""
        row = self.store.one("select * from plugins where id=?", (rid,))
        if not row or row.get("source") != "manifest":
            return
        manifest = json.loads(row.get("manifest_json") or "{}")
        mid = mcp_id(row["slug"] or rid[7:])
        if not manifest.get("runtime"):
            self.store.exec("delete from mcp_servers where id=?", (mid,))
            return
        mrow = self.store.one("select * from mcp_servers where id=?", (mid,))
        args = shlex.join([str(RUNTIME), row["repo_path"]])
        want = (1 if row["is_enabled"] else 0, row["permission_mode"], args)
        if not force and mrow and (mrow["enabled"], mrow.get("permission_mode"), mrow["args"]) == want \
                and not (want[0] and mrow.get("error")):
            return
        self.upsert_mcp_server({"name": "%s (plugin)" % row["name"], "command": sys.executable, "args": args,
                                "enabled": "1" if row["is_enabled"] else "0",
                                "permission_mode": row["permission_mode"]}, mid)

    def plugin_reload(self):
        """Rescan the plugins folder: sync every manifest, drop rows whose folder is gone."""
        self._plugins_schema()
        _root().mkdir(parents=True, exist_ok=True)
        seen, out = set(), []
        for d in sorted(_root().iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "joebro-plugin.json").is_file():
                seen.add(d.name)
                out.append(self.plugin_sync(d.name))
        for r in self.store.rows("select id, slug from plugins where source='manifest'"):
            if r["slug"] not in seen:
                self.store.exec("delete from plugins where id=?", (r["id"],))
                self.store.exec("delete from mcp_servers where id=?", (mcp_id(r["slug"]),))
        return {"ok": True, "plugins": out}

    def plugin_delete(self, ident):
        row = self._manifest_row(ident)
        if not row:
            return False
        if row.get("source") == "manifest":
            src = _root() / row["slug"]
            if src.exists():
                trash = _root() / ".previous"
                trash.mkdir(parents=True, exist_ok=True)
                dst = trash / ("%s-deleted-%d" % (row["slug"], int(time.time())))
                src.rename(dst)
            self.store.exec("delete from mcp_servers where id=?", (mcp_id(row["slug"]),))
        self.store.exec("delete from plugins where id=?", (row["id"],))
        return True

    def plugin_rollback(self, ident):
        row = self._manifest_row(ident)
        if not row or row.get("source") != "manifest":
            raise PluginError("no such installed plugin")
        prev = _root() / ".previous" / row["slug"]
        if not prev.is_dir():
            raise PluginError("no previous version kept for %s" % row["slug"])
        cur = _root() / row["slug"]
        tmp = _root() / (".swap-" + os.urandom(3).hex())
        cur.rename(tmp)
        prev.rename(cur)
        tmp.rename(prev)
        return {"ok": True, "plugin": self.plugin_sync(row["slug"])}

    # ------------------------------------------------------------------ read
    def plugin_public(self, r):
        """plugin_json + the standard description of what it contributes."""
        out = self.plugin_json(r)
        manifest = {}
        try:
            manifest = json.loads(r.get("manifest_json") or "{}")
        except ValueError:
            pass
        c = manifest.get("contributes") or {}
        tools = [t["name"] for t in c.get("tools") or []]
        if r.get("source") == "manifest" and manifest.get("runtime", {}).get("type") == "mcp":
            m = self.store.one("select tools_json from mcp_servers where id=?", (mcp_id(r.get("slug") or ""),))
            try:
                tools = [t.get("name") for t in json.loads((m or {}).get("tools_json") or "[]")]
            except ValueError:
                pass
        b = BUILTIN.get(r["id"])
        if b:
            tools = b["contributes"]["tools"]
        mrow = self.store.one("select error from mcp_servers where id=?", (mcp_id(r.get("slug") or ""),)) if r.get("slug") else None
        out.update({
            "schema": SCHEMA, "slug": r.get("slug") or "", "version": r.get("version") or (b or {}).get("version") or "",
            "author": manifest.get("author") or ("JoeBro" if r.get("source") == "bundled" else ""),
            "managed": r.get("source") == "manifest",
            "contributes": {"tools": tools, "guardrail": bool(c.get("guardrail")) or (r.get("kind") == "background"),
                            "panels": [{"id": p["id"], "title": p["title"], "icon": p.get("icon") or "puzzlepiece.extension",
                                        "url": "/api/plugins/%s/ui/%s" % (r.get("slug"), p["entry"][3:]),
                                        "surfaces": p.get("surfaces") or ["mac", "ios", "web"]}
                                       for p in c.get("panels") or []]},
            "error": r.get("error") or ((mrow or {}).get("error") if r.get("is_enabled") else "") or "",
        })
        return out

    def plugins_list(self):
        self._plugins_schema()
        rows = self.store.rows("select * from plugins order by case when source='bundled' then 0 else 1 end, datetime(created_at) desc")
        return {"plugins": [self.plugin_public(r) for r in rows], "schema": SCHEMA}

    def plugin_source(self, ident):
        row = self._manifest_row(ident)
        if not row or row.get("source") != "manifest":
            raise PluginError("only installed (manifest) plugins have editable source")
        base = _root() / row["slug"]
        files = {}
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.stat().st_size < 400_000:
                try:
                    files[str(p.relative_to(base))] = p.read_text()
                except UnicodeDecodeError:
                    pass
        return {"ok": True, "id": row["id"], "slug": row["slug"], "files": files}

    def plugin_ui_file(self, slug, rel):
        """(bytes, mime) of a file under the plugin's ui/ folder, or None."""
        row = self._manifest_row(slug)
        if not row or row.get("source") != "manifest" or not row.get("is_enabled"):
            return None
        rel = rel or "index.html"
        if not _rel_ok(rel):
            return None
        base = (_root() / row["slug"] / "ui").resolve()
        path = (base / rel).resolve()
        if base not in path.parents or not path.is_file():
            return None
        return path.read_bytes(), (mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    def plugin_call(self, ident, tool, arguments):
        """A plugin's panel (or a test) calling one of the plugin's own tools."""
        row = self._manifest_row(ident)
        if not row or row.get("source") != "manifest":
            raise PluginError("no such installed plugin")
        if not row.get("is_enabled"):
            raise PluginError("plugin is switched off")
        mrow = self.store.one("select * from mcp_servers where id=?", (mcp_id(row["slug"]),))
        if not mrow:
            raise PluginError("plugin has no tools")
        return self.run_mcp_tool(mrow, str(tool or ""), arguments if isinstance(arguments, dict) else {})

    def plugin_guardrails(self):
        """Guardrails from enabled manifest plugins, shaped like background rows."""
        self._plugins_schema()
        out = []
        for r in self.store.rows("select * from plugins where source='manifest' and is_enabled=1 order by name"):
            try:
                g = (json.loads(r.get("manifest_json") or "{}").get("contributes") or {}).get("guardrail")
            except ValueError:
                g = None
            if g:
                out.append({"name": r["name"], "description": g})
        return out

    @staticmethod
    def plugin_spec():
        try:
            return SPEC_FILE.read_text()
        except OSError:
            return "Spec file missing (%s)" % SPEC_FILE
