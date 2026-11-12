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

A failing extension is logged and skipped; it never takes the backend down.
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
