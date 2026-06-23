"""Model discovery, endpoints, API tools, plugins, MCP servers, prefs."""
from jb_core import *  # noqa: F401,F403


class ModelsMixin:

    def pref(self, key):
        row = self.store.one("select value from prefs where key=?", (key,))
        return self.loads(row["value"]) if row else None

    def set_pref(self, key, value):
        self.store.exec("insert or replace into prefs(key,value) values(?,?)", (key, json.dumps(value)))

    def _models_sig(self):
        """Cheap (DB-only) fingerprint of the endpoint config + hidden-model prefs.
        When it changes the model cache auto-invalidates — no need to bust it from
        every endpoint add/edit/delete/hide site."""
        parts = []
        for e in self.store.rows("select id,base_url,api_key,is_enabled from endpoints order by id"):
            parts.append(f"{e['id']}:{e['base_url']}:{e['is_enabled']}:{bool(e.get('api_key'))}")
            parts.append(",".join(sorted(self.pref("hidden_models_" + e["id"]) or [])))
        return "|".join(parts)

    def model_items(self, refresh=False):
        # Cached: model lists barely change but each endpoint needs a live network
        # call (one slow/sleeping endpoint can take ~16s), so recomputing on every
        # /api/models made the picker stall. Serve a cache keyed on the config
        # signature; any endpoint/visibility change refreshes it automatically.
        # Stale-while-revalidate: once warm we always answer instantly and refresh
        # in the background, so only the very first call after a restart can block.
        now = time.time()
        sig = self._models_sig()
        # All DB access happens HERE on the request thread — sqlite isn't safe to
        # touch from the background/worker threads below.
        specs = self._endpoint_specs()
        if not refresh:
            with _MODELS_CACHE_LOCK:
                c = _MODELS_CACHE
                same = c["items"] is not None and c.get("sig") == sig
                fresh = same and now - c["ts"] < _MODELS_TTL
                cached = c["items"] if same else None
            if fresh:
                return cached
            if cached is not None:
                # Same config, just stale — hand back the stale list now and
                # refresh off the request path (network only, no DB).
                threading.Thread(target=self._build_model_items, args=(specs, sig),
                                 daemon=True).start()
                return cached
        return self._build_model_items(specs, sig)

    def _endpoint_specs(self):
        """(id, name, base_url, api_key, hidden-set) per enabled endpoint — the
        DB-side data the model list needs, gathered on the request thread."""
        return [(ep["id"], ep["name"], ep["base_url"], ep.get("api_key") or "",
                 set(self.pref("hidden_models_" + ep["id"]) or []))
                for ep in self.store.rows("select * from endpoints where is_enabled=1 order by created_at")]

    def _build_model_items(self, specs, sig):
        """Network-only: fetch each endpoint's models IN PARALLEL and cache the
        assembled picker list. No DB access, so it's safe to run in a thread."""
        global _MODELS_CACHE
        import concurrent.futures
        fetched = {}
        if specs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(specs))) as ex:
                futs = {ex.submit(self._fetch_models, base, key): epid
                        for (epid, _name, base, key, _h) in specs}
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        fetched[futs[fut]] = fut.result()
                    except Exception:
                        fetched[futs[fut]] = []
        items = []
        for (epid, name, base, key, hidden) in specs:
            # Exclude models the user deselected (hidden) — the picker only
            # shows what's selected; the settings list uses the per-endpoint
            # /models route which still returns everything with is_hidden.
            models = [mid for mid in fetched.get(epid, []) if mid not in hidden]
            if not models:
                continue
            items.append({
                "url": base,
                "endpoint_id": epid,
                "endpoint_name": name,
                "category": "local",
                "offline": False,
                "models": models,
                "models_display": models,
            })
        with _MODELS_CACHE_LOCK:
            _MODELS_CACHE = {"ts": time.time(), "items": items, "sig": sig}
        return items

    def _fetch_models(self, base, api_key):
        """Network-only model-id list for one endpoint (no DB — thread-safe)."""
        base = (base or "").rstrip("/")
        if base.endswith("/v1"):
            candidates = [base + "/models", base[:-3] + "/api/tags"]
        else:
            candidates = [base + "/models", base + "/v1/models", base + "/api/tags"]
        is_anthropic = "anthropic.com" in base
        data = None
        for url in candidates:
            req = urllib.request.Request(url)
            # A real User-Agent — Groq/Cerebras sit behind Cloudflare, which
            # 1010-blocks urllib's default UA.
            req.add_header("User-Agent",
                           "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
            if api_key:
                if is_anthropic:
                    # Anthropic doesn't accept Bearer on /v1/models.
                    req.add_header("x-api-key", api_key)
                    req.add_header("anthropic-version", "2023-06-01")
                else:
                    req.add_header("Authorization", "Bearer " + api_key)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception:
                continue
        if not data:
            return []
        rows = data.get("data") or data.get("models") or []
        out = []
        for row in rows:
            mid = (row.get("id") or row.get("name") or row.get("model")) if isinstance(row, dict) else str(row)
            if mid:
                out.append(mid)
        return out

    def endpoint_models(self, ep_id):
        ep = self.store.one("select * from endpoints where id=?", (ep_id,))
        if not ep:
            return []
        hidden = set(self.pref("hidden_models_" + ep_id) or [])
        return [{"id": mid, "display": mid, "is_hidden": mid in hidden}
                for mid in self._fetch_models(ep["base_url"], ep.get("api_key") or "")]

    def upsert_api_tool(self, body, tool_id=None):
        """Create/update a custom API tool. Sanitizes the user's name into a
        collision-free func_name (kept stable across edits unless the name
        changes)."""
        tid = tool_id or body.get("id") or "tool_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from api_tools where id=?", (tid,))
        name = body.get("name") or (existing or {}).get("name") or "Untitled tool"
        base_url = body.get("base_url") or (existing or {}).get("base_url") or ""
        api_key = body.get("api_key")
        if api_key is None:
            api_key = (existing or {}).get("api_key") or ""
        method = (body.get("method") or (existing or {}).get("method") or "GET").strip().upper()
        if method not in ("GET", "POST"):
            method = "GET"
        description = body.get("description")
        if description is None:
            description = (existing or {}).get("description") or ""
        # Reuse the existing func_name when the display name is unchanged; only
        # re-sanitize when it changed (or on create). Never collide with built-ins
        # or another custom tool's func_name.
        if existing and name == existing.get("name"):
            func_name = existing.get("func_name")
        else:
            taken = {r["func_name"] for r in self.store.rows("select func_name from api_tools where id<>?", (tid,))}
            func_name = sanitize_tool_name(name, taken)
        enabled = body.get("is_enabled")
        if enabled is None:
            is_enabled = (existing or {}).get("is_enabled", 1) if existing else 1
            is_enabled = 1 if is_enabled else 0
        else:
            is_enabled = 1 if str(enabled).lower() in ("1", "true", "yes", "on") else 0
        if existing:
            self.store.exec(
                "update api_tools set name=?,func_name=?,base_url=?,api_key=?,method=?,description=?,is_enabled=?,updated_at=? where id=?",
                (name, func_name, base_url, api_key, method, description, is_enabled, now, tid),
            )
        else:
            self.store.exec(
                "insert into api_tools(id,name,func_name,base_url,api_key,method,description,is_enabled,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                (tid, name, func_name, base_url, api_key, method, description, is_enabled, now, now),
            )
        return {"ok": True, "tool": self.api_tool_json(self.store.one("select * from api_tools where id=?", (tid,)))}

    @staticmethod
    def api_tool_json(r):
        return {
            "id": r["id"],
            "name": r.get("name") or "Untitled tool",
            "func_name": r.get("func_name") or "",
            "base_url": r.get("base_url") or "",
            "has_key": bool((r.get("api_key") or "").strip()),
            "method": (r.get("method") or "GET").upper(),
            "description": r.get("description") or "",
            "is_enabled": bool(r.get("is_enabled")),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    def upsert_plugin(self, body, plugin_id=None):
        """Create/update a plugin. kind = 'foreground' (an active tool the model
        can call) or 'background' (a guardrail applied to every turn)."""
        pid = plugin_id or body.get("id") or "plugin_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from plugins where id=?", (pid,))
        name = body.get("name") or (existing or {}).get("name") or "Untitled plugin"
        kind = (body.get("kind") or (existing or {}).get("kind") or "foreground").strip().lower()
        if kind not in ("foreground", "background"):
            kind = "foreground"
        repo_path = body.get("repo_path")
        if repo_path is None:
            repo_path = (existing or {}).get("repo_path") or ""
        description = body.get("description")
        if description is None:
            description = (existing or {}).get("description") or ""
        source = (existing or {}).get("source") or body.get("source") or "user"
        pmode = (body.get("permission_mode") or (existing or {}).get("permission_mode") or "sandbox").strip() or "sandbox"
        if pmode not in ("sandbox", "readonly", "full"):
            pmode = "sandbox"
        enabled = body.get("is_enabled")
        if enabled is None:
            is_enabled = 1 if (not existing or existing.get("is_enabled", 1)) else 0
        else:
            is_enabled = 1 if str(enabled).lower() in ("1", "true", "yes", "on") else 0
        if existing:
            self.store.exec(
                "update plugins set name=?,kind=?,repo_path=?,description=?,is_enabled=?,permission_mode=?,updated_at=? where id=?",
                (name, kind, repo_path, description, is_enabled, pmode, now, pid),
            )
        else:
            self.store.exec(
                "insert into plugins(id,name,kind,repo_path,description,is_enabled,source,permission_mode,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                (pid, name, kind, repo_path, description, is_enabled, source, pmode, now, now),
            )
        return {"ok": True, "plugin": self.plugin_json(self.store.one("select * from plugins where id=?", (pid,)))}

    @staticmethod
    def plugin_json(r):
        return {
            "id": r["id"],
            "name": r.get("name") or "Untitled plugin",
            "kind": r.get("kind") or "foreground",
            "repo_path": r.get("repo_path") or "",
            "description": r.get("description") or "",
            "is_enabled": bool(r.get("is_enabled")),
            "source": r.get("source") or "user",
            "permission_mode": r.get("permission_mode") or "sandbox",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    def upsert_mcp_server(self, body, server_id=None):
        """Create or update an MCP server. When the (resulting) server is enabled,
        run discovery (spawn -> initialize -> tools/list -> kill, hard timeout) and
        persist the discovered tools. NEVER 500s: if discovery fails the server is
        still saved, with the error recorded and an empty tool list."""
        sid = server_id or body.get("id") or "mcp_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from mcp_servers where id=?", (sid,))
        name = body.get("name") or (existing or {}).get("name") or "MCP Server"
        command = body.get("command")
        if command is None:
            command = (existing or {}).get("command") or ""
        command = command.strip()
        args = body.get("args")
        if args is None:
            args = (existing or {}).get("args") or ""
        enabled_in = body.get("enabled")
        if enabled_in is None:
            enabled_in = body.get("is_enabled")
        if enabled_in is None:
            enabled = (existing or {}).get("enabled", 1) if existing else 1
            enabled = 1 if enabled else 0
        else:
            enabled = 1 if str(enabled_in).lower() in ("1", "true", "yes", "on") else 0

        # Keep the previously discovered tools unless we (re)discover below.
        tools_json = (existing or {}).get("tools_json") or "[]"
        error = ""
        if enabled and command:
            try:
                tools = mcp_discover(command, args)
                tools_json = json.dumps(tools)
                error = "" if tools else "Server reported no tools."
            except _MCPError as exc:
                tools_json = "[]"
                error = str(exc)[:500]
            except Exception as exc:
                tools_json = "[]"
                error = f"discovery failed: {exc}"[:500]
        elif not enabled:
            error = ""

        if existing:
            self.store.exec(
                "update mcp_servers set name=?,command=?,args=?,enabled=?,tools_json=?,error=?,updated_at=? where id=?",
                (name, command, args, enabled, tools_json, error, now, sid))
        else:
            self.store.exec(
                "insert into mcp_servers(id,name,command,args,enabled,tools_json,error,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                (sid, name, command, args, enabled, tools_json, error, now, now))
        return {"ok": True, "server": self.mcp_server_json(self.store.one("select * from mcp_servers where id=?", (sid,)))}

    @staticmethod
    def mcp_server_json(r):
        try:
            tools = json.loads(r.get("tools_json") or "[]")
        except Exception:
            tools = []
        return {
            "id": r["id"],
            "name": r.get("name") or "MCP Server",
            "command": r.get("command") or "",
            "args": r.get("args") or "",
            "enabled": bool(r.get("enabled")),
            "tools": [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")],
            "error": r.get("error") or "",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
