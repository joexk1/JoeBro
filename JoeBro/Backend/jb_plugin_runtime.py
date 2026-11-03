#!/usr/bin/env python3
"""JoeBro plugin runtime: the one process every plugin's tools run in.

    jb_plugin_runtime.py <plugin_dir>

The backend launches this as an MCP stdio server for any plugin whose manifest
contributes tools. It reads <plugin_dir>/joebro-plugin.json, then either:

  runtime "python"  -> imports `entry` (e.g. tools.py) and serves the tools the
                       manifest declares, calling the function of the same name
                       with the arguments as keyword args;
  runtime "mcp"     -> chdir into the plugin, exec the declared MCP server
                       command (any language) — it speaks MCP itself.

Either way the plugin gets the same context: cwd = its folder, and env
JOEBRO_PLUGIN_ID, JOEBRO_PLUGIN_DIR, JOEBRO_PLUGIN_DATA (a persistent writable
folder of its own), JOEBRO_URL. Stdlib only.
"""
import importlib.util
import inspect
import json
import os
import sys
import traceback

DATA_ROOT = os.path.expanduser(os.environ.get("JOEBRO_PLUGIN_DATA_ROOT", "~/.joebro-personal/plugin-data"))


def context(pdir, manifest):
    pid = manifest["id"]
    data = os.path.join(DATA_ROOT, pid)
    os.makedirs(data, exist_ok=True)
    os.environ.update(JOEBRO_PLUGIN_ID=pid, JOEBRO_PLUGIN_DIR=pdir, JOEBRO_PLUGIN_DATA=data,
                      JOEBRO_URL=os.environ.get("JOEBRO_URL", "http://127.0.0.1:8771"))
    os.chdir(pdir)


def serve_python(pdir, manifest, rt, proto):
    entry = os.path.join(pdir, rt["entry"])
    sys.path.insert(0, pdir)
    spec = importlib.util.spec_from_file_location("joebro_plugin_" + manifest["id"].replace("-", "_"), entry)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = manifest.get("contributes", {}).get("tools") or []
    by_name = {t["name"]: t for t in tools}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if "id" not in req:
            continue
        method, params = req.get("method"), req.get("params") or {}
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": manifest["id"], "version": manifest.get("version", "0")}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": t["name"], "description": t.get("description", ""),
                                     "inputSchema": t.get("parameters") or {"type": "object", "properties": {}}}
                                    for t in tools]}
            elif method == "tools/call":
                name = params.get("name")
                fn = getattr(mod, name, None) if name in by_name else None
                if not callable(fn):
                    raise ValueError("unknown tool %r" % name)
                args = params.get("arguments") or {}
                accepted = inspect.signature(fn).parameters
                if not any(p.kind == p.VAR_KEYWORD for p in accepted.values()):
                    args = {k: v for k, v in args.items() if k in accepted}
                try:
                    value = fn(**args)
                    is_error = False
                except Exception as exc:   # a tool failure is a result, not a crash
                    value, is_error = "%s: %s" % (type(exc).__name__, exc), True
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            elif method and method.startswith("notifications/"):
                continue
            else:
                raise ValueError("method not found")
            out = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except Exception as exc:
            out = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": str(exc)[:500]}}
        proto.write(json.dumps(out) + "\n")
        proto.flush()


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: jb_plugin_runtime.py <plugin_dir>")
    pdir = os.path.realpath(sys.argv[1])
    manifest = json.load(open(os.path.join(pdir, "joebro-plugin.json")))
    rt = manifest.get("runtime") or {}
    context(pdir, manifest)
    # Plugin code must never write to stdout outside the protocol: route print() to stderr.
    if rt.get("type") == "python":
        proto = sys.stdout
        sys.stdout = sys.stderr
        try:
            serve_python(pdir, manifest, rt, proto)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)
    elif rt.get("type") == "mcp":
        argv = [rt["command"]] + list(rt.get("args") or [])
        os.execvpe(argv[0], argv, os.environ)
    else:
        sys.exit("manifest has no runtime")


if __name__ == "__main__":
    main()
