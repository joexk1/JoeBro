"""Backend extensions: private add-ons that extend the core without forking it.

At import, every *.py in JOEBRO_EXT_DIR (default ~/.joebro-personal/ext; unset
or missing = no extensions, which is the normal shipped app) is loaded once, in
name order. An extension module may define any of:

    MIXINS = [SomeMixin, ...]      extra Handler base classes (methods, state)
    LOOPS  = [fn, ...]             fn(server) run in a daemon thread at startup
    def route(handler, method, path, query) -> bool
                                   handle a request first; return True if it
                                   sent a reply. Runs after auth (_guard).
    def setup(ext)                 register named hooks: ext.on("name", fn)

Core code calls named hooks where behaviour is extensible:

    ext.call("name", *args)   -> list of every hook's non-None result
    ext.first("name", *args)  -> the first non-None result, else None

A failing extension is logged and skipped; it never takes the backend down. A hook may
refuse a request on purpose by raising ValueError (or a subclass): that propagates to the
caller, which reports it to the user like any other validation error.

Hook names core calls today (each is optional; absent = the generic behaviour):

  pref_default(key) -> value              default for a pref the user never set
  ptask_seeds() -> [(id,title,prompt,schedule,time,weekday,status)]   phone-agent tasks seeded once
  ptask_run(handler,row) -> reply|None    run a phone-agent task without a model
  autonomy_routines() -> {id: {title,enabled,every_days,at,timezone,quiet_hours,report}}
                                          no-model scheduled routines (see jb_autonomy)
  autonomy_precheck(routine,cfg) -> reason|None   refuse a run (e.g. a watchlist is empty)
  phone_tool_schemas() -> [schema]        add or replace phone-toolset function schemas
  phone_tool(handler,name,body,form) -> text|None   implement or override a phone-toolset tool
  route_tool_schemas(handler,form,schemas) -> schemas   trim/reorder the schema list for a turn
  tool_advice() -> ({tool: advice}, {tool: advice_key})  per-tool lines for the system prompt
  mcp_extra_tools(server_row) -> [tool def]       extra tools a stored MCP server offers
  mcp_call(handler,row,tool,args) -> event|None   serve an MCP tool yourself
  mcp_output(row,tool,text) -> text|None          rewrite an MCP result (e.g. shrink it)
  vision_default_target(handler) -> (endpoint_id, model)   fallback image-caption model
  email_account_from_login(address,secret) -> account|None  provider settings, e.g. a local bridge
  email_bridge_ensure(handler) -> error|None      start that bridge;  email_bridge_types() -> {types}
  notify_chat_kinds() -> {kinds}          notice kinds that live in the phone chat, not the Inbox
  bg_sources(handler,now) -> {source: [items]}    extra sources for the heartbeat and pulse
  proactive_records(handler,now) -> {key: record} extra records for the proactive pass
  transfer_parse(scheme,role,rest,query) -> {"kind":..}|None;  transfer_export(h,p,filename) -> ref|None
  transfer_deliver(h,p,ref,data,filename,args) -> result|None;  transfer_outbound_kinds() -> {kinds}
  transfer_preview(p) -> {..};  transfer_prepare(p,ref,args);  transfer_folder_written(root,folder) -> {..}
  janitor_scratch(data_dir) -> {"scratch":[..],"tmp_patterns":[..],"config":{..}};  janitor_run(janitor)

Extensions also reach the core by importing it (every jb_* module is loaded first). They add tool
names to jb_core.PRODUCTION_AI_TOOLS / PHONE_TOOL_NAMES when they add tools.

Stdlib only, like the rest of the backend.
"""
import importlib.util
import os
import sys
import threading
import traceback
from pathlib import Path

_HOOKS = {}
_MODULES = []


def _log(msg):
    print("[ext] " + msg, file=sys.stderr, flush=True)


def on(name, fn):
    _HOOKS.setdefault(name, []).append(fn)


def call(name, *args, **kw):
    out = []
    for fn in _HOOKS.get(name, ()):
        try:
            r = fn(*args, **kw)
        except ValueError:
            raise
        except Exception:
            _log("hook %s failed:\n%s" % (name, traceback.format_exc()))
            continue
        if r is not None:
            out.append(r)
    return out


def first(name, *args, **kw):
    for fn in _HOOKS.get(name, ()):
        try:
            r = fn(*args, **kw)
        except ValueError:
            raise
        except Exception:
            _log("hook %s failed:\n%s" % (name, traceback.format_exc()))
            continue
        if r is not None:
            return r
    return None


def has(name):
    return bool(_HOOKS.get(name))


def mixins():
    return tuple(m for mod in _MODULES for m in getattr(mod, "MIXINS", ()))


def route(handler, method, path, query):
    for mod in _MODULES:
        fn = getattr(mod, "route", None)
        if fn is None:
            continue
        try:
            if fn(handler, method, path, query):
                return True
        except Exception:
            _log("route in %s failed:\n%s" % (mod.__name__, traceback.format_exc()))
            handler.json({"detail": "extension error"}, 500)
            return True
    return False


def start_loops(server):
    for mod in _MODULES:
        for fn in getattr(mod, "LOOPS", ()):
            threading.Thread(target=fn, args=(server,), daemon=True,
                             name="ext-%s-%s" % (mod.__name__, fn.__name__)).start()


def names():
    return [m.__name__ for m in _MODULES]


_LOADED = []


def load(directory=None):
    """Load the extension folder, once. Called by joebro_backend after every core
    module is imported (and before Handler is built), so an extension can import
    any jb_* module. Importing jb_ext itself has no side effects."""
    if _LOADED:
        return
    _LOADED.append(True)
    d = directory or os.environ.get("JOEBRO_EXT_DIR") or "~/.joebro-personal/ext"
    root = Path(os.path.expanduser(d))
    if not root.is_dir():
        return
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))     # extensions may import their own helpers
    for f in sorted(root.glob("*.py")):
        if f.name.startswith("_") or f.name.startswith("test_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("jbext_" + f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "setup"):
                mod.setup(sys.modules[__name__])
            _MODULES.append(mod)
        except Exception:
            sys.modules.pop("jbext_" + f.stem, None)
            _log("could not load %s:\n%s" % (f.name, traceback.format_exc()))
    if _MODULES:
        _log("loaded: " + ", ".join(names()))
