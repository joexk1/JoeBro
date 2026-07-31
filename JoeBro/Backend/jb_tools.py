"""Tool dispatch, execution, web/image search, tool parsing."""
from jb_core import *  # noqa: F401,F403

# AI tools that write a document to disk. When the app has "ask before editing"
# on it sends ask_doc_edit and these are gated behind explicit approval BEFORE
# the file is touched (see _gate_doc_edit) — so an edit can't land behind your
# back while JoeBro is in the background or you're on another chat.

# Models (deepseek especially) invent parameter names — map the common ones
# onto ours instead of silently dropping them and failing with "requires path".
_DOC_PARAM_ALIASES = {
    "file": "path", "file_path": "path", "filepath": "path", "filename": "path",
    "file_name": "path", "document": "path", "doc": "path", "target": "path",
    "text": "content", "body": "content", "new_content": "content",
    "full_content": "content", "markdown": "content",
    "old_text": "find", "old_string": "find", "old_str": "find",
    "search": "find", "find_text": "find",
    "new_text": "replace", "new_string": "replace", "new_str": "replace",
    "replacement": "replace", "replace_text": "replace",
}

DOC_WRITE_TOOLS = {"create_document", "edit_document", "update_document"}

# Agent-access levels, ranked. An MCP server tagged with a level is only offered
# to (and callable by) a session whose permission_mode is at least that level —
# so e.g. an iPhone-control server can require Full access.
_PERM_RANK = {"sandbox": 0, "readonly": 1, "full": 2}


class ToolsMixin:

    def tools_for(self, f, has_folder):
        """The tool schemas to offer this turn. Don't advertise tools that can't
        actually run in the current context, or the model wastes a round calling
        them and then apologises (e.g. create_document in a plain chat with no
        bound folder)."""
        # The Telegram orchestrator bot MANAGES chats/agents — it never codes or
        # touches files itself (it delegates that to chats). So it gets the
        # high-level tools only, with bash/python/file tools removed.
        if str(f.get("orchestrator") or "").lower() in ("1", "true", "yes"):
            drop = {"bash", "python", "list_files", "read_file", "read_pdf",
                    "create_document", "edit_document", "update_document", "suggest_document"}
            schemas = [t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] not in drop]
            return schemas + self.custom_tool_schemas() + self.mcp_tool_schemas(f)
        # Chat mode is pure conversation — no tools at all. Tools (search, email,
        # files, research, …) are an AGENT-mode capability.
        if (f.get("mode") or "").lower() != "agent":
            return []
        readonly = (f.get("permission_mode") or "") == "readonly"
        full = (f.get("permission_mode") or "") == "full"
        terminal_on = str(f.get("allow_bash") or "").lower() in ("1", "true", "yes") and full
        # manage_chats belongs to the Telegram orchestrator ONLY (early-returned
        # above). Offered to a normal chat, the model starts playing message bot:
        # reading other chats, flipping their modes/permissions, and delegating
        # its own work into them instead of doing it.
        drop = {"manage_chats"}
        if readonly:
            drop |= WRITE_SIDE_EFFECT_TOOLS            # read-only: no side effects at all
        if not has_folder and not full:
            # File writes need a bound folder (or Full Access to a scratch dir).
            drop |= {"list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"}
        if not terminal_on:
            # Never advertise the shell when the terminal toggle is off — the
            # model would call it, get refused, then lie that it "ran" the command.
            drop |= {"bash", "python"}
        if str(f.get("allow_web_search") or "").lower() not in ("1", "true", "yes"):
            # The composer's search toggle gates web_search — off means the agent
            # can't reach the web (it would otherwise always be available).
            drop |= {"web_search"}
        schemas = [t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] not in drop]
        # Enabled custom API tools are offered alongside the built-ins (agent mode
        # only — we already returned [] for chat above). Their description is the
        # schema; the model infers the single `query` string from it.
        schemas += self.custom_tool_schemas()
        schemas += self.mcp_tool_schemas(f)
        if self._plugin_allowed("plugin_macos_use", f):
            schemas.append(MACOS_USE_TOOL)
        if self._plugin_allowed("plugin_advisor", f) and (self.pref("advisor_model") or ""):
            schemas.append(advisor_tool_schema(self.pref("advisor_description") or ""))
        return schemas

    def custom_tool_schemas(self):
        """OpenAI-style schemas for every enabled custom API tool."""
        try:
            rows = self.store.rows("select * from api_tools where is_enabled=1 order by created_at")
        except Exception:
            return []
        out = []
        for r in rows:
            desc = (r.get("description") or "").strip() or (r.get("name") or "custom tool")
            out.append(_fn(r["func_name"], desc,
                           {"query": {"type": "string", "description": "the query/argument to send to this API"}},
                           ["query"]))
        return out

    def custom_tool_by_func(self, func_name):
        """The api_tools row whose func_name matches, or None."""
        try:
            return self.store.one("select * from api_tools where func_name=?", (func_name,))
        except Exception:
            return None

    def run_custom_api_tool(self, row, params):
        """Make the registered HTTP request for a custom API tool and return its
        event dict. Never raises — errors come back as exit_code 1."""
        name = row["func_name"]
        query = ""
        if isinstance(params, dict):
            query = params.get("query") or params.get("q") or params.get("input") or ""
            if not query and len(params) == 1:
                query = str(next(iter(params.values())))
        query = str(query or "")
        method = (row.get("method") or "GET").strip().upper()
        base = (row.get("base_url") or "").strip()
        try:
            if "{query}" in base:
                # Placeholder form: drop the (URL-encoded) query anywhere in the
                # URL, so any API shape works — e.g. ".../search?name={query}".
                url = base.replace("{query}", urllib.parse.quote(query, safe=""))
                if method == "POST":
                    data = json.dumps({"query": query}).encode("utf-8")
                    req = urllib.request.Request(url, data=data, method="POST")
                    req.add_header("Content-Type", "application/json")
                else:
                    req = urllib.request.Request(url, method="GET")
            elif method == "POST":
                url = base
                data = json.dumps({"query": query}).encode("utf-8")
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
            else:
                sep = "&" if ("?" in base) else "?"
                url = base + sep + urllib.parse.urlencode({"query": query})
                req = urllib.request.Request(url, method="GET")
            api_key = (row.get("api_key") or "").strip()
            if api_key:
                req.add_header("Authorization", "Bearer " + api_key)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
            # Pretty-print JSON if it parses; otherwise return the raw text.
            try:
                output = json.dumps(json.loads(raw), ensure_ascii=False)
            except Exception:
                output = raw
            return {"tool": name, "command": (row.get("name") or name) + ": " + query,
                    "output": output[:60000], "exit_code": 0}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            return {"tool": name, "command": (row.get("name") or name) + ": " + query,
                    "output": f"HTTP {e.code} calling {row.get('name') or name}: {detail}"[:4000], "exit_code": 1}
        except Exception as exc:
            return {"tool": name, "command": (row.get("name") or name) + ": " + query,
                    "output": f"Error calling {row.get('name') or name}: {exc}", "exit_code": 1}

    # ----- MCP servers (stdio) -----------------------------------------------
    # MCP tools are exposed to the model as function names "mcp_<serverid>_<tool>"
    # (the server id is itself "mcp_<hex>"). The cached tool list (discovered when
    # a server is added/enabled) drives the schemas; calling re-spawns the server
    # for a single initialize -> tools/call. Every method here is defensive: a bad
    # server can only ever skip itself or return a tool-error, never raise.

    def mcp_func_name(self, server_id, tool_name):
        # Lowercased to match the agent loop, which lowercases every tool-call
        # name before dispatch (server ids are already lowercase hex).
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", str(tool_name or "")).strip("_").lower() or "tool"
        return f"{server_id}__{safe}".lower()

    def mcp_tool_schemas(self, f=None):
        """OpenAI-style schemas for every tool of every ENABLED MCP server, read
        from the cached tools_json (no subprocess spawn here). Servers whose
        required agent-access level is above the session's are skipped. Never raises."""
        out = []
        sess_rank = _PERM_RANK.get((f or {}).get("permission_mode") or "sandbox", 0)
        try:
            rows = self.store.rows("select * from mcp_servers where enabled=1 order by created_at")
        except Exception:
            return out
        for r in rows:
            if _PERM_RANK.get(r.get("permission_mode") or "sandbox", 0) > sess_rank:
                continue   # needs a higher agent-access level than this session has
            try:
                tools = json.loads(r.get("tools_json") or "[]")
            except Exception:
                tools = []
            if not isinstance(tools, list):
                continue
            sname = r.get("name") or "MCP"
            for t in tools:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                fname = self.mcp_func_name(r["id"], t["name"])
                schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else None
                if not schema or schema.get("type") != "object":
                    schema = {"type": "object", "properties": (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}}
                desc = (t.get("description") or "").strip()
                desc = f"[{sname}] " + (desc or t["name"])
                out.append({"type": "function", "function": {
                    "name": fname, "description": desc[:1024], "parameters": schema}})
        return out

    def mcp_server_for_func(self, func_name):
        """Find the (server_row, tool_name) a "mcp_<id>__<tool>" name routes to,
        or (None, None). Matches by the server-id prefix so tool names that
        themselves contain '__' still resolve."""
        if not (func_name or "").startswith("mcp_"):
            return None, None
        try:
            rows = self.store.rows("select * from mcp_servers where enabled=1")
        except Exception:
            return None, None
        for r in rows:
            prefix = r["id"] + "__"
            if func_name.startswith(prefix):
                tool = func_name[len(prefix):]
                # Confirm the tool is one we discovered (don't trust the name blindly).
                try:
                    tools = json.loads(r.get("tools_json") or "[]")
                except Exception:
                    tools = []
                names = {t.get("name") for t in tools if isinstance(t, dict)}
                # tool name was sanitized into the func name; match by sanitized form
                for real in names:
                    if self.mcp_func_name(r["id"], real) == func_name:
                        return r, real
                return r, tool
        return None, None

    @staticmethod
    def _mcp_inject_repo_path(server_args, arguments):
        """If a server is launched with --repository <path> (mcp-server-git),
        pin the call's repo_path to it so the model never has to know or guess
        the path. Returns the (possibly updated) arguments dict."""
        toks = mcp_split_args(server_args)
        repo = ""
        for i, t in enumerate(toks):
            if t in ("--repository", "--repo") and i + 1 < len(toks):
                repo = toks[i + 1]
            elif t.startswith("--repository="):
                repo = t.split("=", 1)[1]
        if repo and arguments.get("repo_path") != repo:
            arguments = dict(arguments)
            arguments["repo_path"] = repo
        return arguments

    def run_mcp_tool(self, row, real_tool, arguments):
        """Invoke one MCP tool (spawn -> initialize -> tools/call -> kill) and
        return the standard event dict. NEVER raises — failures come back as
        exit_code 1 so the agent turn always completes."""
        name = self.mcp_func_name(row["id"], real_tool)
        label = (row.get("name") or "MCP") + ": " + real_tool
        if not isinstance(arguments, dict):
            arguments = {}
        # Path-bound servers (mcp-server-git) take a REQUIRED repo_path the model
        # often omits or guesses ("."), which the server then rejects. The server
        # is already pinned to one repo via --repository, so force repo_path to it
        # — there's no other repo it would accept anyway.
        arguments = self._mcp_inject_repo_path(row.get("args") or "", arguments)
        try:
            text, is_error = mcp_call_tool(row.get("command") or "", row.get("args") or "",
                                           real_tool, arguments)
            return {"tool": name, "command": label,
                    "output": (text or "")[:60000], "exit_code": 1 if is_error else 0}
        except Exception as exc:
            return {"tool": name, "command": label,
                    "output": f"MCP error from {row.get('name') or name}: {exc}"[:4000], "exit_code": 1}

    def _tool_ctx(self, sid, form):
        """(root, readonly, allow_outside) for one session's tool run."""
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root_text = ((session or {}).get("workdir") or "").strip()
        root = Path(root_text).expanduser().resolve() if root_text else None
        mode = form.get("permission_mode") or ""
        return root, mode == "readonly", mode == "full"

    def execute_ai_file_tools(self, sid, reply, form=None, run=True, max_calls=None):
        if not sid or not reply:
            return reply, []
        form = form or {}
        reply = self.normalize_xml_tools(reply)
        root, readonly, allow_outside = self._tool_ctx(sid, form)
        events = []
        # Strip every recognized tool block from the displayed text — executed
        # tools are shown as tool rows, not raw markup. (The Pi does the same.)
        recognized = PRODUCTION_AI_TOOLS | OBSOLETE_AI_TOOLS | {"write_file"}
        def strip_recognized(match):
            return "" if match.group(1).strip().lower() in recognized else match.group(0)
        cleaned = re.sub(r"\n{3,}", "\n\n", TOOL_BLOCK_RE.sub(strip_recognized, reply)).strip()
        if not run:
            return cleaned, []
        for tool, body in TOOL_BLOCK_RE.findall(reply):
            if max_calls is not None and len(events) >= max_calls:
                break   # per-turn tool-use cap reached (shared with the agent loop)
            name = tool.strip().lower()
            if name == "write_file" or name in OBSOLETE_AI_TOOLS:
                continue
            event = self._run_one_tool(root, name, body, form, allow_outside, readonly)
            if event:
                events.append(event)
        return cleaned, events

    def execute_structured_tools(self, sid, blocks, form=None):
        """Run the model's STRUCTURED function calls (name, body) — same
        execution + doc-event path as text-parsed tool blocks."""
        if not sid or not blocks:
            return []
        form = form or {}
        root, readonly, allow_outside = self._tool_ctx(sid, form)
        events = []
        for name, body in blocks:
            event = self._run_one_tool(root, name, body, form, allow_outside, readonly)
            if event:
                events.append(event)
        return events

    def _plugin_allowed(self, plugin_id, f=None):
        """Bundled-plugin gate, checked BOTH when offering schemas and when
        executing a call: the plugin's toggle must be ON and the session's
        agent-access level at or above the plugin's configured one. The
        execution-time check matters — a model can emit a plugin call it
        learned from chat history even when the tool was never advertised
        this turn, and 'switched off' has to mean off."""
        row = self.store.one("select is_enabled, permission_mode from plugins where id=?", (plugin_id,))
        if not row or not row.get("is_enabled"):
            return False
        return _PERM_RANK.get(row.get("permission_mode") or "sandbox", 0) <= \
            _PERM_RANK.get((f or {}).get("permission_mode") or "sandbox", 0)

    def _macos_blocked_apps(self):
        """User's blocked-apps list (macos_blocked_apps pref) - names the plugin
        must never open, click, type into or read."""
        raw = self.pref("macos_blocked_apps") or []
        if isinstance(raw, str):
            raw = re.split(r"[,\n]", raw)
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]

    def run_macos_use(self, f, params):
        """Execute a JoeBro macOS Use action locally (this backend runs on the Mac)."""
        action = (params or {}).get("action") or ""
        try:
            argv = macos_use_argv(action, params, blocked=self._macos_blocked_apps())
        except ValueError as exc:
            return {"tool": "macos_use", "command": "macos_use " + str(action), "output": str(exc), "exit_code": 1}
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            out = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "(done)"
            if argv[0] == "screencapture" and proc.returncode == 0:
                out = "Screenshot saved to " + argv[-1]
            if proc.returncode != 0 and ("-1719" in out or "not allowed" in out.lower()):
                out += " - grant JoeBro Accessibility access in System Settings > Privacy & Security > Accessibility."
            return {"tool": "macos_use", "command": "macos_use " + action, "output": out, "exit_code": proc.returncode}
        except Exception as exc:
            return {"tool": "macos_use", "command": "macos_use " + action, "output": "macOS Use error: " + str(exc), "exit_code": 1}

    def run_advisor(self, f, params):
        """Foreground plugin: one-shot consult of the user's chosen stronger
        model. The advisor sees ONLY the question — no chat history, no tools —
        so the calling model must pack all context into it."""
        q = ((params or {}).get("question") or (params or {}).get("query") or "").strip()
        if not q:
            return {"tool": "advisor", "command": "advisor", "output": "Pass a `question` for the advisor.", "exit_code": 1}
        label = "advisor: " + q[:120] + ("…" if len(q) > 120 else "")
        model, ep = self.pref("advisor_model") or "", self.pref("advisor_endpoint_id") or ""
        if not model or not ep:
            return {"tool": "advisor", "command": label, "exit_code": 1,
                    "output": "No advisor model is set — pick one in Tools > Plugins > Advisor (gear)."}
        msg = self._post_chat(
            {"model": model, "endpoint_id": ep, "max_tokens": "4000"},
            [{"role": "system", "content":
              "You are the Advisor: a stronger model that the user's everyday assistant consults "
              "when it needs help. Answer the question directly with expert, actionable advice. "
              "You have no tools and cannot see their conversation — work from the question alone."},
             {"role": "user", "content": q}], tools=[])
        if msg is None:
            return {"tool": "advisor", "command": label, "exit_code": 1,
                    "output": "The advisor's endpoint is gone — re-pick the advisor model in its settings."}
        if isinstance(msg, str):
            return {"tool": "advisor", "command": label, "output": msg, "exit_code": 1}
        out = (msg.get("content") or "").strip() or "(the advisor returned nothing)"
        return {"tool": "advisor", "command": label, "output": "Advisor (" + model + "):\n" + out, "exit_code": 0}

    def _run_one_tool(self, root, name, body, form, allow_outside, readonly):
        """Execute a single tool (name, body) and return its event, or None if
        the tool is disabled/unknown. Shared by the text and structured paths."""
        name = (name or "").strip().lower()
        if name == "write_file" or name in OBSOLETE_AI_TOOLS:
            return None
        if readonly and name in WRITE_SIDE_EFFECT_TOOLS:
            return None
        if name.startswith("mcp_"):
            # MCP tool (mcp_<serverid>__<tool>). The body is the JSON-serialized
            # arguments dict (_tool_body_from_params json.dumps()es unknown-name
            # params); parse it back so we pass the real arguments to tools/call.
            mrow, real_tool = self.mcp_server_for_func(name)
            if mrow:
                if _PERM_RANK.get(mrow.get("permission_mode") or "sandbox", 0) > \
                   _PERM_RANK.get((form or {}).get("permission_mode") or "sandbox", 0):
                    return {"tool": name, "command": real_tool or name, "exit_code": 1,
                            "output": f"'{mrow.get('name') or 'This MCP server'}' requires a higher agent access level — switch the chat's access to use it."}
                try:
                    args = json.loads(body) if body else {}
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                return self.run_mcp_tool(mrow, real_tool, args)
            # Unknown/disabled MCP name — fall through (returns None below).
        if name == "macos_use":
            if not self._plugin_allowed("plugin_macos_use", form):
                return {"tool": "macos_use", "command": "macos_use", "exit_code": 1,
                        "output": "The Computer Use plugin is switched off (or needs a higher agent access level) — tell the user; do not retry."}
            try:
                args = json.loads(body) if body else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            return self.run_macos_use(form, args)
        if name == "advisor":
            if not self._plugin_allowed("plugin_advisor", form):
                return {"tool": "advisor", "command": "advisor", "exit_code": 1,
                        "output": "The Advisor plugin is switched off — tell the user; do not retry."}
            return self.run_advisor(form, self.parse_tool_args(body))
        if name not in PRODUCTION_AI_TOOLS:
            # Custom API tools live in the api_tools table, not PRODUCTION_AI_TOOLS;
            # route them to the HTTP executor (their body is a JSON {"query": ...}).
            crow = self.custom_tool_by_func(name)
            if crow:
                try:
                    params = json.loads(body) if body else {}
                    if not isinstance(params, dict):
                        params = {"query": str(params)}
                except Exception:
                    params = {"query": body or ""}
                return self.run_custom_api_tool(crow, params)
            return None
        gate = self._gate_doc_edit(name, body, form)
        if gate is not None:
            return gate
        try:
            output = self.execute_production_tool(root, name, body, form, allow_outside=allow_outside)
            event = {"tool": name, "command": self.tool_command_summary(name, body),
                     "output": output, "exit_code": 0}
            if name in ("create_document", "edit_document", "update_document"):
                event["doc"] = self._doc_event_payload(root, name, body, allow_outside)
            return event
        except Exception as exc:
            return {"tool": name, "command": self.tool_command_summary(name, body),
                    "output": str(exc), "exit_code": 1}

    def _gate_doc_edit(self, name, body, form):
        """Approval gate for AI document writes when the caller sends ask_doc_edit.
        ONLY the Telegram bot sends it now ("Ask before commands & edits") — it has
        no editor to show a banner in, so approval goes over the prompt channel.
        The app NEVER sends it: "Require approval for AI edits" is the editor's
        client-side Apply/Reject banner, and "Ask before running commands" is the
        only thing that raises blocking popups (bash/python).
        An edit to a doc that's OPEN in the editor passes straight through: the
        app holds it as a pending Apply/Reject diff in the editor, so there's no
        blocking prompt and no timeout. Anything else needs the permission prompt
        BEFORE the file is touched; if there's no live prompt channel the write
        is refused rather than silently applied. Returns a denied event to skip
        the write, or None to allow it."""
        if name not in DOC_WRITE_TOOLS:
            return None
        f = form or {}
        if str(f.get("ask_doc_edit") or "").lower() not in ("1", "true", "yes"):
            return None
        sid = f.get("session") or ""
        with _PERMISSION_GUARD:
            if "doc_edit" in _SESSION_ALLOW.get(sid, set()):
                return None
        if self._doc_edit_is_open(body, f):
            return None
        command = self.tool_command_summary(name, body)
        emit = getattr(self, "_perm_emit", None)
        if not emit:
            return {"tool": name, "command": command,
                    "output": "Edit not applied — approval is required but there's no open editor tab or live chat to ask in.",
                    "exit_code": 1}
        decision = self._await_permission(emit, name, command)
        if decision == "always":
            with _PERMISSION_GUARD:
                _SESSION_ALLOW.setdefault(sid, set()).add("doc_edit")
            return None
        if decision == "allow":
            return None
        return {"tool": name, "command": command,
                "output": "Edit not applied — you declined it.", "exit_code": 1}

    def _doc_edit_is_open(self, body, f):
        """True when the write targets a doc that's open in the app's editor
        (the app sends open tab ids/titles as open_docs). Matches the same
        local-<relpath> / basename scheme the editor uses. Rich .doc/.docx tabs
        render from disk, so they can't show a pending diff — never 'open'."""
        raw = f.get("open_docs") or ""
        if not raw:
            return False
        fields = self.parse_tool_fields(body)
        rel = (fields.get("path") or fields.get("title") or "").strip()
        if not rel or rel.lower().endswith((".doc", ".docx")):
            return False
        entries = {s.strip() for s in raw.split("\n") if s.strip()}
        sub = rel.lstrip("/")
        if sub.startswith("./"):
            sub = sub[2:]
        return ("local-" + sub) in entries or sub.rsplit("/", 1)[-1] in entries

    def _doc_event_payload(self, root, name, body, allow_outside):
        """The {doc_id,title,language,content} for a document the AI just wrote,
        so the app can open it / refresh the open tab. Reads the file from disk
        (natively-opened files have no documents row) and matches the app's
        "local-<relpath>" id scheme so the open tab updates in place."""
        try:
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "").strip()
            if not rel or root is None:
                return None
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                return None
            content = read_doc_text(target)
            try:
                sub = str(target.relative_to(root))
            except Exception:
                sub = target.name
            # Bound-folder files are opened in the editor as "local-<sub>", so the
            # doc event MUST use that id (not a documents-table row id) or the open
            # tab won't match — the edit then won't show live or trigger approval.
            doc_id = "local-" + sub
            return {"doc_id": doc_id, "title": target.name,
                    "language": target.suffix.lstrip(".") or "markdown",
                    "content": content}
        except Exception:
            return None

    def tool_command_summary(self, name, body):
        fields = self.parse_tool_fields(body)
        target = fields.get("path") or fields.get("title") or ""
        if target.strip():
            return target.strip()
        if body.strip():
            return body.strip().splitlines()[0][:160]
        return name

    def read_pdf_text(self, target: Path):
        """Extract text from a local PDF without shell/Pi dependencies."""
        pieces = []
        errors = []
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(target))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            for i, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    pieces.append(f"--- Page {i} ---\n{text.strip()}")
        except Exception as exc:
            errors.append(f"pypdf: {exc}")
        if not pieces:
            try:
                from Foundation import NSURL
                from Quartz import PDFDocument
                doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(target)))
                count = int(doc.pageCount()) if doc else 0
                for i in range(count):
                    page = doc.pageAtIndex_(i)
                    text = str(page.string() or "") if page else ""
                    if text.strip():
                        pieces.append(f"--- Page {i + 1} ---\n{text.strip()}")
            except Exception as exc:
                errors.append(f"Quartz: {exc}")
        if not pieces:
            try:
                proc = subprocess.run(
                    ["/usr/bin/mdls", "-raw", "-name", "kMDItemTextContent", str(target)],
                    text=True, capture_output=True, timeout=12)
                text = (proc.stdout or "").strip()
                if text and text != "(null)":
                    pieces.append(text)
            except Exception as exc:
                errors.append(f"Spotlight: {exc}")
        text = "\n\n".join(pieces).strip()
        if not text:
            raise ValueError("Could not extract text from PDF" + (": " + "; ".join(errors) if errors else ""))
        return text   # no char cap — compaction handles context if it grows

    def execute_file_tool(self, root, name, body, allow_outside=False, form=None):
        root = Path(root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("session workdir is not available")
        if name == "list_files":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip() or ".").strip()
            base = self.resolve_tool_path(root, rel, allow_outside)
            if base.is_file():
                return str(base.relative_to(root) if base.is_relative_to(root) else base)
            if not base.exists() or not base.is_dir():
                raise ValueError(f"folder not found: {rel}")
            # One level only (dirs first, marked with a trailing /), so the agent
            # gets a clean, navigable listing and drills in with `path`.
            rows = []
            for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                rows.append(p.name + ("/" if p.is_dir() else ""))
                if len(rows) >= 400:
                    rows.append("… (truncated — list a sub-folder to narrow down)")
                    break
            header = "" if rel in (".", "") else rel.rstrip("/") + "/\n"
            return header + ("\n".join(rows) or "(empty folder)")
        if name == "read_file":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip()).strip()
            if not rel:
                raise ValueError("read_file requires path")
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                raise ValueError(f"file not found: {rel}")
            if target.suffix.lower() == ".pdf":
                return self.read_pdf_text(target)
            if target.suffix.lower() in IMAGE_EXTS:
                # The actual image is attached to the conversation by run_agent so
                # the model can see it; this is just the tool's text acknowledgement.
                return f"[image {rel} is shown to you below — look at it directly]"
            text = read_doc_text(target)
            return text   # no char cap — compaction handles context if it grows
        if name == "read_pdf":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or body.strip()).strip()
            if not rel:
                raise ValueError("read_pdf requires path")
            target = self.resolve_tool_path(root, rel, allow_outside)
            if not target.is_file():
                raise ValueError(f"PDF not found: {rel}")
            if target.suffix.lower() != ".pdf":
                raise ValueError(f"not a PDF: {rel}")
            return self.read_pdf_text(target)
        if name == "create_document":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "Untitled.md").strip()
            content = fields.get("content") or ""
            target = self.resolve_tool_path(root, rel, allow_outside)
            with lock_for_path(target):
                backup_existing(target)
                write_doc_text(target, content)
            language = fields.get("language") or target.suffix.lstrip(".") or "markdown"
            ts = now_iso()
            row = self.store.one("select id from documents where file_path=?", (str(target),))
            if row:
                self.store.exec(
                    "update documents set title=?, language=?, current_content=?, updated_at=? where id=?",
                    (target.name, language, content, ts, row["id"]),
                )
            else:
                self.store.exec(
                    """insert into documents(id,title,language,current_content,version_count,file_path,archived,created_at,updated_at)
                       values(?,?,?,?,?,?,?,?,?)""",
                    ("doc_" + os.urandom(8).hex(), target.name, language, content, 1, str(target), 0, ts, ts),
                )
            return f"File written: {target}"
        if name == "edit_document":
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "").strip()
            if not rel:
                # The model often means "the doc" — the one open in the editor.
                rel = ((self.open_doc_context(form or {}) or {}).get("path") or "").strip()
            if not rel:
                raise ValueError("edit_document requires path — pass the file's path from the bound folder (see list_files)")
            target = self.resolve_tool_path(root, rel, allow_outside)
            region = (fields.get("region") or "body").strip().lower()
            # Lock the whole read-modify-write so a concurrent edit can't be lost.
            with lock_for_path(target):
                if is_docx(target) and region in ("header", "footer"):
                    cur = docx_extras(target).get(region, "")
                    if "content" in fields:
                        new = fields["content"]
                    else:
                        find = fields.get("find")
                        if find is None:
                            raise ValueError("edit_document requires find/replace or content")
                        if find not in cur:
                            raise ValueError(f"could not find text in {target.name} {region}")
                        new = cur.replace(find, fields.get("replace", ""), 1)
                    backup_existing(target)
                    docx_write_extra(target, region, new)
                    return f"File edited ({region}): {target}"
                # .docx find/replace patches the file in place so every bit of
                # formatting, colour and imagery survives the edit.
                if is_docx(target) and "content" not in fields:
                    find = fields.get("find")
                    if find is None:
                        raise ValueError("edit_document requires find/replace or content")
                    backup_existing(target)
                    if not docx_replace_in_place(target, find, fields.get("replace", "")):
                        raise ValueError(f"could not find text in {target.name}")
                    updated = read_doc_text(target)
                else:
                    text = read_doc_text(target)
                    if "content" in fields:
                        updated = fields["content"]
                    else:
                        find = fields.get("find")
                        replace = fields.get("replace", "")
                        if find is None:
                            raise ValueError("edit_document requires find/replace or content")
                        if find not in text:
                            raise ValueError(f"could not find text in {target.name}")
                        updated = text.replace(find, replace, 1)
                    backup_existing(target)
                    write_doc_text(target, updated)
            row = self.store.one("select id from documents where file_path=?", (str(target),))
            if row:
                self.store.exec(
                    "update documents set current_content=?, updated_at=? where id=?",
                    (updated, now_iso(), row["id"]),
            )
            return f"File edited: {target}"
        raise ValueError(f"unsupported tool {name}")

    def require_full_terminal(self, form, tool):
        if str(form.get("allow_bash") or "").lower() != "true":
            raise ValueError(f"{tool} requires the terminal toggle to be enabled.")
        if (form.get("permission_mode") or "") != "full":
            raise ValueError(f"{tool} requires Full access. Bound-folder mode is not a real shell sandbox.")

    def bash_tool(self, root, body, form, allow_outside=False):
        self.require_full_terminal(form, "bash")
        cwd = str(root) if root and root.exists() else str(Path.home())
        proc = subprocess.run(
            ["/bin/bash", "-lc", body],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=60,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(out.strip() or f"bash exited {proc.returncode}")
        return out.strip() or "(no output)"

    def python_tool(self, root, body, form, allow_outside=False):
        self.require_full_terminal(form, "python")
        cwd = str(root) if root and root.exists() else str(Path.home())
        proc = subprocess.run(
            ["python3", "-I", "-c", body],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=60,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(out.strip() or f"python exited {proc.returncode}")
        return out.strip() or "(no output)"

    def web_search_tool(self, body):
        args = self.parse_tool_args(body)
        query = (args.get("query") or body.splitlines()[0] if body.strip() else "").strip()
        if not query:
            raise ValueError("web_search requires query")
        results = self.search_web(query, count=5)
        rows = []
        for r in results:
            rows.append(f"- {r['title']}\n  {r['url']}")
            if r.get("snippet"):
                rows.append(f"  {r['snippet']}")
        return "\n".join(rows) if rows else "No results found."

    def search_web(self, query, count=5):
        if self.pref("cloak_mode") is not False:
            query = cloak_text(query)
        self.ensure_searxng()   # prefer the local searxng (better results) ...
        for base in self.searxng_urls():
            try:
                results = self.searxng_search(base, query, count)
                if results:
                    return results
            except Exception:
                continue
        return self.duckduckgo_search(query, count)   # ... DuckDuckGo fallback

    @staticmethod
    def _searxng_home():
        return Path.home() / ".joebro-searxng"

    @classmethod
    def find_searxng(cls):
        """(venv_python, src_dir) for the bundled/local searxng, or (None, None)."""
        home = cls._searxng_home()
        py, src = home / "venv" / "bin" / "python", home / "src"
        if py.is_file() and (src / "searx" / "webapp.py").is_file():
            return str(py), str(src)
        return None, None

    def ensure_searxng(self):
        """Boot the local searxng meta-search if it isn't already up. Best-effort:
        if it's not installed, web search just falls through to DuckDuckGo."""
        if self.port_open("127.0.0.1", 8888):
            return True
        py, src = self.find_searxng()
        if not py:
            return False
        settings = self._searxng_home() / "settings.yml"
        if not settings.exists():
            try:
                settings.write_text(
                    "use_default_settings: true\n"
                    "server:\n"
                    f"  secret_key: \"{os.urandom(16).hex()}\"\n"
                    "  bind_address: \"127.0.0.1\"\n"
                    "  port: 8888\n"
                    "  limiter: false\n"
                    "  public_instance: false\n"
                    "search:\n"
                    "  formats:\n    - html\n    - json\n")
            except Exception:
                return False
        env = dict(os.environ)
        env["SEARXNG_SETTINGS_PATH"] = str(settings)
        logs = self.store.root / "searxng"
        logs.mkdir(exist_ok=True)
        try:
            subprocess.Popen([py, "-m", "searx.webapp"], cwd=src, env=env,
                             stdout=open(logs / "searxng.log", "ab"), stderr=subprocess.STDOUT)
        except Exception:
            return False
        deadline = time.time() + 12   # searxng takes a few seconds to come up
        while time.time() < deadline:
            if self.port_open("127.0.0.1", 8888):
                return True
            time.sleep(0.3)
        return False

    def searxng_urls(self):
        # Production is Pi-independent: only an explicitly-configured searxng
        # (env/pref) or a LOCAL one is used. Web/image search otherwise falls
        # back to Pi-free sources (DuckDuckGo / Openverse). No remote hosts here.
        raw = [
            os.environ.get("JOEBRO_SEARXNG_URL", ""),
            os.environ.get("SEARXNG_INSTANCE", ""),
            os.environ.get("SEARXNG_URL", ""),
            str(self.pref("searxng_url") or ""),
            "http://127.0.0.1:8888",
            "http://127.0.0.1:8080",
            "http://localhost:8888",
            "http://localhost:8080",
        ]
        urls = []
        for url in raw:
            url = (url or "").strip().rstrip("/")
            if url and url not in urls and not url.startswith("http://searxng:"):
                urls.append(url)
        return urls

    def search_images(self, query, count=2):
        """Best-effort image URLs for a research topic (searxng images category,
        else Openverse). Images are a nice-to-have."""
        self.ensure_searxng()
        for base in self.searxng_urls():
            try:
                params = {"q": query, "format": "json", "language": "en", "categories": "images"}
                url = base + "/search?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                out = []
                for item in data.get("results", []):
                    src = (item.get("img_src") or item.get("thumbnail_src") or item.get("url") or "").strip()
                    if src.startswith("http") and src not in out:
                        out.append(src)
                    if len(out) >= count:
                        break
                if out:
                    return out
            except Exception:
                continue
        return self._openverse_images(query, count)   # Pi-free fallback

    def _openverse_images(self, query, count):
        """Pi-free image source: Openverse (openly-licensed images, no API key)."""
        try:
            url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
                {"q": query, "page_size": max(count, 3), "mature": "false"})
            req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            out = []
            for item in data.get("results", []):
                src = (item.get("url") or item.get("thumbnail") or "").strip()
                if src.startswith("http") and src not in out:
                    out.append(src)
                if len(out) >= count:
                    break
            return out
        except Exception:
            return []

    def searxng_search(self, base, query, count):
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "categories": "general",
        }
        engines = os.environ.get("JOEBRO_SEARXNG_GENERAL_ENGINES") or os.environ.get("SEARXNG_GENERAL_ENGINES")
        if engines:
            params["engines"] = engines
        url = base + "/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = []
        for item in data.get("results", [])[:count]:
            href = (item.get("url") or "").strip()
            title = re.sub(r"\s+", " ", (item.get("title") or "").strip())
            if not href or not title:
                continue
            results.append({
                "title": title,
                "url": href,
                "snippet": re.sub(r"\s+", " ", (item.get("content") or item.get("snippet") or "").strip()),
            })
        return results

    def duckduckgo_search(self, query, count):
        # Best-effort, no-key fallback when no searxng is reachable. DuckDuckGo
        # blocks obvious bot user-agents and bare GETs, so POST the HTML endpoint
        # with a real browser UA + Referer. Parse both the modern (class before
        # href) and older (href before class) anchor layouts so a markup tweak
        # doesn't silently zero out results again.
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
        html = ""
        for base in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            try:
                req = urllib.request.Request(
                    base, data=urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode(),
                    method="POST",
                    headers={"User-Agent": ua, "Accept": "text/html",
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Referer": "https://duckduckgo.com/"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                if "result__a" in html or "result-link" in html:
                    break
            except Exception:
                continue
        results, seen = [], set()
        patterns = [
            r'<a[^>]+class="(?:result__a|result-link)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            r'<a[^>]+href="([^"]+)"[^>]*class="(?:result__a|result-link)"[^>]*>(.*?)</a>',
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, html, re.S):
                href = m.group(1).replace("&amp;", "&")
                if "uddg=" in href:
                    href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
                if href.startswith("//"):
                    href = "https:" + href
                href = urllib.parse.unquote(href)
                title = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", m.group(2))).strip()
                if title and href.startswith("http") and href not in seen:
                    seen.add(href)
                    results.append({"title": title, "url": href, "snippet": ""})
                if len(results) >= count:
                    return results
            if results:
                break
        return results

    def suggest_document_tool(self, root, body, allow_outside=False):
        fields = self.parse_tool_fields(body)
        rel = (fields.get("path") or fields.get("title") or "").strip()
        if not rel:
            raise ValueError("suggest_document requires path")
        target = self.resolve_tool_path(root, rel, allow_outside)
        text = read_doc_text(target)
        find = fields.get("find", "")
        replace = fields.get("replace", "")
        reason = fields.get("reason", "")
        if find and find not in text:
            raise ValueError(f"could not find text in {target.name}")
        return json.dumps({
            "path": str(target),
            "suggestions": [{
                "find": find,
                "replace": replace,
                "reason": reason or "Suggested improvement",
            }],
            "count": 1 if find or replace else 0,
        }, ensure_ascii=False)

    def manage_tasks_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        # Scheduled tasks later run at whatever permission_mode they were saved
        # with, so letting the MODEL set it is a privilege escalation: a sandboxed
        # agent (or a prompt injected into one) could schedule itself write-anywhere
        # access for tomorrow. Only the Tasks UI sets this, over the HTTP route.
        args.pop("permission_mode", None)
        if action in ("add", "create"):
            return json.dumps(self.upsert_task(args), ensure_ascii=False)
        if action in ("edit", "update"):
            tid = args.get("id") or args.get("task_id")
            return json.dumps(self.upsert_task(args, tid), ensure_ascii=False)
        if action == "delete":
            tid = args.get("id") or args.get("task_id")
            if not tid:
                raise ValueError("task id required")
            self.store.exec("delete from tasks where id=?", (tid,))
            return f"Deleted task {tid}"
        rows = self.store.rows("select * from tasks order by datetime(created_at) desc")
        return json.dumps([self.task_json(r) for r in rows], ensure_ascii=False)

    def manage_skills_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action in ("add", "create"):
            return json.dumps(self.upsert_skill(args), ensure_ascii=False)
        if action in ("edit", "update"):
            sid = args.get("id") or args.get("skill_id")
            return json.dumps(self.upsert_skill(args, sid), ensure_ascii=False)
        if action == "delete":
            sid = args.get("id") or args.get("skill_id")
            if not sid:
                raise ValueError("skill id required")
            self.store.exec("delete from skills where id=?", (sid,))
            return f"Deleted skill {sid}"
        rows = self.store.rows("select * from skills order by datetime(created_at) desc")
        return json.dumps([self.skill_json(r) for r in rows], ensure_ascii=False)

    def _resolve_chat(self, ref):
        """Find a session by id, then exact name, then name LIKE. None if no match."""
        ref = (ref or "").strip()
        if not ref:
            return None
        row = self.store.one("select * from sessions where id=?", (ref,))
        if row:
            return row
        row = self.store.one("select * from sessions where name=? order by datetime(created_at) desc limit 1", (ref,))
        if row:
            return row
        return self.store.one(
            "select * from sessions where name like ? order by datetime(created_at) desc limit 1", (f"%{ref}%",))

    def _resolve_model_endpoint(self, model):
        """The endpoint_id that serves `model`, or None — so set_model points a
        chat at a model and the endpoint that can actually run it."""
        try:
            for it in self.model_items():
                if model in (it.get("models") or []):
                    return it.get("endpoint_id")
        except Exception:
            pass
        return None

    def _remember_focus(self, form, chat_id, chat_name):
        """Record the chat the bot just worked with, keyed by the bot's own
        session, so a follow-up ('ask them…', 'tell that chat…') resolves to it.
        Read back in _orchestrator_system."""
        sid = (form or {}).get("session") or ""
        if sid:
            self.set_pref("bot_focus_" + sid, {"id": chat_id, "name": chat_name or ""})

    def manage_chats_tool(self, body, form=None):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action == "list":
            rows = self.store.rows(
                "select * from sessions where id not like 'bot\\_tg\\_%' escape '\\' order by datetime(created_at) desc limit 100")
            out = []
            for r in rows:
                last = self.store.one(
                    "select created_at from messages where session_id=? order by id desc limit 1", (r["id"],))
                cnt = self.store.one("select count(*) c from messages where session_id=?", (r["id"],))
                out.append({"id": r["id"], "name": r.get("name") or "Untitled",
                            "folder": r.get("folder") or "", "mode": r.get("mode") or "chat",
                            "permission": r.get("permission_mode") or "sandbox",
                            "model": r.get("model") or "",
                            "bound_folder": (r.get("workdir") or "").strip(),
                            "messages": int((cnt or {}).get("c") or 0),
                            "last_activity": (last or {}).get("created_at")})
            return json.dumps(out, ensure_ascii=False)
        if action == "models":
            try:
                items = self.model_items()
            except Exception:
                items = []
            return json.dumps([{"endpoint": it.get("endpoint_name"), "models": it.get("models") or []}
                               for it in items], ensure_ascii=False)
        if action == "search":
            q = (args.get("query") or "").strip()
            if not q:
                raise ValueError("search requires query")
            rows = self.store.rows(
                """select m.session_id, s.name, m.role, m.content, m.created_at
                   from messages m join sessions s on s.id=m.session_id
                   where m.content like ? order by m.id desc limit 40""", (f"%{q}%",))
            return json.dumps([{"chat_id": r["session_id"], "chat": r.get("name"),
                                "role": r.get("role"), "when": r.get("created_at"),
                                "snippet": (r.get("content") or "")[:300]} for r in rows], ensure_ascii=False)
        if action == "create":
            new_id = "s_" + os.urandom(8).hex()
            name = (args.get("name") or "New chat").strip() or "New chat"
            mode = (args.get("mode") or "agent").lower()
            mode = mode if mode in ("chat", "agent") else "agent"
            perm = (args.get("permission") or "sandbox").lower()
            perm = perm if perm in ("sandbox", "readonly", "full") else "sandbox"
            folder = (args.get("folder") or "").strip()
            # Inherit a working model/endpoint so the new chat can actually run:
            # the bot's configured model first, then the app default.
            ep = self.pref("bot_endpoint_id") or self.pref("default_endpoint_id") or ""
            model = self.pref("bot_model") or self.pref("default_model") or ""
            self.store.exec(
                "insert into sessions(id,name,model,endpoint_id,mode,permission_mode,workdir,created_at) values(?,?,?,?,?,?,?,?)",
                (new_id, name, model, ep, mode, perm, folder, now_iso()))
            self._remember_focus(form, new_id, name)
            return json.dumps({"id": new_id, "name": name, "mode": mode,
                               "permission": perm, "folder": folder}, ensure_ascii=False)
        target = self._resolve_chat(args.get("chat"))
        if not target:
            raise ValueError(f"chat not found: {args.get('chat') or ''}")
        if action == "bind_folder":
            folder = (args.get("folder") or "").strip()
            self.store.exec("update sessions set workdir=? where id=?", (folder, target["id"]))
            return f"Bound chat '{target.get('name')}' to folder: {folder or '(none)'}"
        if action == "set_permission":
            perm = (args.get("permission") or "").lower()
            if perm not in ("sandbox", "readonly", "full"):
                raise ValueError("permission must be sandbox, readonly or full")
            self.store.exec("update sessions set permission_mode=? where id=?", (perm, target["id"]))
            return f"Set chat '{target.get('name')}' file access to {perm}"
        if action == "set_model":
            model = (args.get("model") or "").strip()
            if not model:
                raise ValueError("set_model requires model")
            # Resolve which endpoint serves this model so the chat can actually
            # run it; keep the chat's existing endpoint if it's not found.
            ep = self._resolve_model_endpoint(model) or target.get("endpoint_id") or ""
            self.store.exec("update sessions set model=?, endpoint_id=? where id=?", (model, ep, target["id"]))
            return f"Set chat '{target.get('name')}' model to {model}"
        if action == "read":
            n = max(1, min(100, int(args.get("limit") or 20)))
            rows = self.store.rows(
                "select role,content,created_at from messages where session_id=? order by id desc limit ?",
                (target["id"], n))
            rows = list(reversed(rows))
            return json.dumps({"chat": target.get("name"), "id": target["id"],
                               "mode": target.get("mode") or "chat",
                               "permission": target.get("permission_mode") or "sandbox",
                               "model": target.get("model") or "",
                               "messages": [{"role": r["role"], "content": r.get("content") or "",
                                             "when": r.get("created_at")} for r in rows]}, ensure_ascii=False)
        if action == "set_mode":
            mode = (args.get("mode") or "").lower()
            if mode not in ("chat", "agent"):
                raise ValueError("mode must be chat or agent")
            self.store.exec("update sessions set mode=? where id=?", (mode, target["id"]))
            return f"Set chat '{target.get('name')}' to {mode} mode"
        if action == "send":
            msg = (args.get("message") or "").strip()
            if not msg:
                raise ValueError("send requires message")
            # Delegate: run a turn IN the target chat with ITS model, mode,
            # bound folder and stored permission level. Full Access also enables
            # the terminal for that chat. If the bot's "ask before commands &
            # edits" is on, route the chat's approval prompts back to Telegram via
            # the orchestrator's live emit (set on self during the bot's run).
            perm = (target.get("permission_mode") or "sandbox").lower()
            sub = {"session": target["id"], "message": msg,
                   "model": target.get("model") or "", "endpoint_id": target.get("endpoint_id") or "",
                   "endpoint_url": target.get("endpoint_url") or "",
                   "mode": (target.get("mode") or "agent"), "permission_mode": perm,
                   "allow_web_search": "true"}
            if perm == "full":
                sub["allow_bash"] = "true"
            if bool(self.pref("bot_ask_permissions")):
                sub["ask_permission"] = "true"
                sub["ask_doc_edit"] = "true"
            reply, _meta = self.bot_run(sub, emit=getattr(self, "_perm_emit", None))
            self._remember_focus(form, target["id"], target.get("name"))
            return json.dumps({"chat": target.get("name"), "reply": reply}, ensure_ascii=False)
        raise ValueError(f"unknown manage_chats action: {action}")

    def trigger_research_tool(self, body, form=None):
        form = form or {}
        args = self.parse_tool_args(body)
        query = args.get("topic") or args.get("query") or body.strip()
        rid = self.create_research(query or "Untitled research", form.get("endpoint_id"), form.get("model"))
        return json.dumps({"id": rid, "query": query, "status": "running",
                           "note": f"Deep research started. Track it in the Deep Research panel: [{query}](#research-{rid})"},
                          ensure_ascii=False)

    def manage_research_tool(self, body, form=None):
        form = form or {}
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action in ("add", "create", "start"):
            rid = self.create_research(args.get("query") or args.get("topic") or "Untitled research",
                                       form.get("endpoint_id"), form.get("model"))
            return json.dumps({"id": rid, "status": "running"}, ensure_ascii=False)
        if action in ("view", "get"):
            rid = args.get("id") or args.get("research_id")
            row = self.store.one("select * from research where id=?", (rid,))
            if not row:
                raise ValueError("research item not found")
            return json.dumps(self.research_json(row), ensure_ascii=False)
        if action in ("delete", "archive"):
            rid = args.get("id") or args.get("research_id")
            if not rid:
                raise ValueError("research id required")
            if action == "archive":
                self.store.exec("update research set archived=1, updated_at=? where id=?", (now_iso(), rid))
                return f"Archived research {rid}"
            self.store.exec("delete from research where id=?", (rid,))
            return f"Deleted research {rid}"
        rows = self.store.rows("select * from research where archived=0 order by datetime(created_at) desc")
        return json.dumps([self.research_json(r) for r in rows], ensure_ascii=False)

    def email_tool(self, name, body):
        args = self.parse_tool_args(body)
        if name == "list_email_accounts":
            return json.dumps([self.email_source_json(r) for r in self.email_sources()], ensure_ascii=False)
        if name == "list_emails":
            return self.email_list_payload(args)
        if name == "read_email":
            uid = args.get("uid") or body.strip()
            return self.email_read_payload(uid, args.get("folder") or "INBOX")
        if name == "send_email":
            return self.email_send_payload(args)
        if name == "reply_to_email":
            uid = args.get("uid")
            if not uid:
                raise ValueError("uid required")
            msg = self.email_read_data(uid, args.get("folder") or "INBOX")
            return self.email_send_payload({
                "to": msg.get("from_address") or msg.get("from") or "",
                "subject": "Re: " + (msg.get("subject") or ""),
                "body": args.get("body") or "",
            })
        if name in ("archive_email", "delete_email", "mark_email_read"):
            uid = args.get("uid")
            if not uid:
                raise ValueError("uid required")
            if name == "archive_email":
                return self.email_flag_payload(uid, "\\Deleted", add=False, archive=True, folder=args.get("folder") or "INBOX")
            if name == "delete_email":
                return self.email_flag_payload(uid, "\\Deleted", add=True, expunge=True, folder=args.get("folder") or "INBOX")
            return self.email_flag_payload(uid, "\\Seen", add=bool(args.get("read", True)), folder=args.get("folder") or "INBOX")
        if name == "bulk_email":
            action = args.get("action")
            uids = args.get("uids") or []
            outs = []
            for uid in uids:
                if action == "delete":
                    outs.append(self.email_flag_payload(uid, "\\Deleted", add=True, expunge=True, folder=args.get("folder") or "INBOX"))
                elif action == "archive":
                    outs.append(self.email_flag_payload(uid, "\\Deleted", add=False, archive=True, folder=args.get("folder") or "INBOX"))
                elif action in ("mark_read", "mark_unread"):
                    outs.append(self.email_flag_payload(uid, "\\Seen", add=action == "mark_read", folder=args.get("folder") or "INBOX"))
            return "\n".join(outs) or "No email UIDs supplied."
        raise ValueError(f"unsupported email tool {name}")

    def execute_production_tool(self, root, name, body, form, allow_outside=False):
        if name in ("list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"):
            if root is None:
                # No bound folder: only Full Access may use a scratch workspace
                # (sandbox/read-only correctly get "no folder").
                if not allow_outside:
                    raise ValueError("No folder is bound to this chat. Bind a folder, or use Full Access.")
                root = (app_support_dir() / "workspace")
                root.mkdir(parents=True, exist_ok=True)
            if name in ("list_files", "read_file", "read_pdf"):
                return self.execute_file_tool(root, name, body, allow_outside=allow_outside, form=form)
            if name == "suggest_document":
                return self.suggest_document_tool(root, body, allow_outside=allow_outside)
            return self.execute_file_tool(root, name, body, allow_outside=allow_outside, form=form)
        if name == "bash":
            return self.bash_tool(root, body, form, allow_outside=allow_outside)
        if name == "python":
            return self.python_tool(root, body, form, allow_outside=allow_outside)
        if name == "web_search":
            return self.web_search_tool(body)
        if name == "manage_memory":
            return self.manage_memory_tool(body)
        if name == "manage_tasks":
            return self.manage_tasks_tool(body)
        if name == "manage_skills":
            return self.manage_skills_tool(body)
        if name == "manage_chats":
            return self.manage_chats_tool(body, form)
        if name == "trigger_research":
            return self.trigger_research_tool(body, form)
        if name == "manage_research":
            return self.manage_research_tool(body, form)
        if name == "create_event":
            return self.create_event_tool(body)
        if name in EMAIL_AI_TOOLS:
            return self.email_tool(name, body)
        raise ValueError(f"unsupported tool {name}")

    def create_event_tool(self, body):
        """Create a calendar event from the AI's structured args (it parses the
        natural-language date/time itself into ISO-8601). For a macOS calendar,
        create_event_result raises (native app owns EventKit writes), so we store
        the event in the local `events` table at the AI-resolved time and return
        success — the app's Calendar panel reads that table."""
        args = self.parse_tool_args(body)
        summary = (args.get("summary") or args.get("title") or "New event").strip() or "New event"
        start = (args.get("start") or args.get("dtstart") or "").strip()
        end = (args.get("end") or args.get("dtend") or "").strip()
        all_day = bool(args.get("all_day"))
        if not start:
            raise ValueError("create_event requires an ISO-8601 start datetime")
        if not end:
            end = start
        event = {"summary": summary, "dtstart": start, "dtend": end, "all_day": all_day,
                 "location": args.get("location") or "", "description": args.get("description") or ""}
        cfg = self.pref("calendar_connection") or {}
        if cfg.get("type") == "macos":
            # Native app owns EventKit; still persist locally so the panel shows
            # it at the correct time, and hand the data back for EventKit later.
            uid = "ev_" + os.urandom(6).hex()
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (uid, summary, start, end, 1 if all_day else 0, event["location"], event["description"], "", "", now_iso()),
            )
            return json.dumps({"ok": True, "uid": uid, "event": event}, ensure_ascii=False)
        result = self.create_event_result(event)
        return json.dumps({"ok": True, "uid": result.get("uid", ""), "event": event}, ensure_ascii=False)

    @staticmethod
    def _tool_call_params(fn):
        """A native tool_call's arguments as a dict — tolerant of endpoints that
        return a parsed object instead of a JSON string, and of models that wrap
        the JSON in a markdown fence. Never raises; worst case is {}."""
        raw = (fn or {}).get("arguments")
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            parsed = json.loads(text or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _tool_body_from_params(name, params, fallback=""):
        """Serialize a {param: value} dict into the body text each executor
        expects. Mirrors the Pi's function_call_to_tool_block shaping."""
        if name == "bash":
            return params.get("command") or params.get("cmd") or fallback
        if name == "python":
            return params.get("code") or params.get("script") or fallback
        if name == "web_search":
            return params.get("query") or params.get("q") or fallback
        if name in ("list_files", "read_file", "read_pdf", "create_document", "edit_document", "update_document", "suggest_document"):
            params = {str(k).strip().lower(): v for k, v in (params or {}).items()}
            for alias, canon in _DOC_PARAM_ALIASES.items():
                if canon not in params and alias in params:
                    params[canon] = params[alias]
            # `key: value` inline (even multi-line) — parse_tool_fields re-splits
            # the body, so an inline first line avoids a spurious leading newline.
            keys = ("path",) if name in ("list_files", "read_file", "read_pdf") else (
                "path", "title", "language", "region", "find", "replace", "reason", "content")
            lines = [f"{key}: {str(params[key] if params[key] is not None else '').strip(chr(10))}"
                     for key in keys
                     if key in params]
            return "\n".join(lines) if lines else fallback
        # manage_*, email, research: executors accept JSON via parse_tool_args.
        if params:
            return json.dumps(params)
        return fallback

    @classmethod
    def normalize_xml_tools(cls, reply):
        """Rewrite every XML-style tool call the models emit — DeepSeek DSML,
        <tool_call>/<function_call><invoke name=..><parameter ..> wrappers, and
        direct <create_document><path>..</path>.. tags — into the fenced form the
        existing parser/executor handle. Ported from the Pi's tool_parsing."""
        text = reply or ""
        known = PRODUCTION_AI_TOOLS | OBSOLETE_AI_TOOLS | {"write_file"}

        # 1. DeepSeek DSML markup -> standard <invoke>/<parameter>.
        if "DSML" in text:
            pipes = r"[｜|]+"
            text = re.sub(rf"<\s*{pipes}\s*DSML\s*{pipes}\s*tool_calls\s*>", "<tool_call>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*tool_calls\s*>", "</tool_call>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*{pipes}\s*DSML\s*{pipes}\s*invoke\s+name=", "<invoke name=", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*invoke\s*>", "</invoke>", text, flags=re.IGNORECASE)
            text = re.sub(rf'<\s*{pipes}\s*DSML\s*{pipes}\s*parameter\s+name=(["\'][^"\']+["\'])[^>]*>',
                          r"<parameter name=\1>", text, flags=re.IGNORECASE)
            text = re.sub(rf"<\s*/\s*{pipes}\s*DSML\s*{pipes}\s*parameter\s*>", "</parameter>", text, flags=re.IGNORECASE)

        # 2. <invoke name="tool"><parameter name="k">v</parameter></invoke>
        def invoke_repl(m):
            name = TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
            if name not in known:
                return m.group(0)
            params = {pm.group(1): pm.group(2).strip()
                      for pm in XML_PARAM_RE.finditer(m.group(2))}
            body = cls._tool_body_from_params(name, params)
            return f"```{name}\n{body}\n```"
        text = XML_INVOKE_RE.sub(invoke_repl, text)
        # Drop now-empty <tool_call>/<function_call> wrapper tags.
        text = re.sub(r"</?(?:[\w]+:)?(?:tool_call|function_call)>", "", text, flags=re.IGNORECASE)

        # 3. Direct <create_document><path>..</path><content>..</content></...>
        def direct_repl(m):
            name = TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
            if name not in known:
                return m.group(0)
            inner = m.group(2)
            params = {fm.group(1).lower(): fm.group(2).strip("\n")
                      for fm in XML_FIELD_RE.finditer(inner)}
            body = cls._tool_body_from_params(name, params, fallback=inner.strip())
            return f"```{name}\n{body}\n```"
        text = XML_TOOL_RE.sub(direct_repl, text)
        return text

    @staticmethod
    def parse_tool_fields(body):
        fields = {}
        current = None
        buf = []
        block = False  # current field used a YAML block scalar (content: |-)
        key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_ -]*):\s*(.*)$")

        def flush():
            text = "\n".join(buf)
            # Block scalars come indented under the key — dedent to recover it.
            return (textwrap.dedent(text) if block else text).rstrip("\n")

        for line in body.splitlines():
            m = key_re.match(line)
            if m and m.group(1).strip().lower().replace(" ", "_") in {
                "path", "title", "language", "region", "content", "find", "replace", "reason"
            }:
                if current is not None:
                    fields[current] = flush()
                current = m.group(1).strip().lower().replace(" ", "_")
                val = m.group(2)
                # YAML block scalar indicator (|, |-, >, >- …): real content is
                # on the following indented lines — models love this for content.
                if val.strip() in ("|", "|-", "|+", ">", ">-", ">+"):
                    block = True
                    buf = []
                else:
                    block = False
                    # `content:` alone must not seed an empty first line — that
                    # prepends a spurious newline to the file.
                    buf = [val] if val else []
            elif current is not None:
                buf.append(line)
        if current is not None:
            fields[current] = flush()
        return fields

    @classmethod
    def parse_tool_args(cls, body):
        text = body or ""
        try:
            parsed = json.loads(text) if text.strip() else {}
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        fields = cls.parse_tool_fields(text)
        if fields:
            return fields
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return {}
        if len(lines) == 1:
            return {"query": lines[0], "text": lines[0], "topic": lines[0]}
        return {"action": lines[0].lower(), "text": "\n".join(lines[1:])}

    @staticmethod
    def resolve_in_workdir(root, rel):
        if not rel:
            raise ValueError("path is required")
        target = (root / rel).resolve()
        if target != root and not str(target).startswith(str(root) + "/"):
            raise ValueError("path escapes session workdir")
        return target

    @classmethod
    def resolve_tool_path(cls, root, rel, allow_outside=False):
        if allow_outside:
            path = Path(rel).expanduser()
            if path.is_absolute():
                return path.resolve()
        return cls.resolve_in_workdir(root, rel)

    def local_reply(self, message):
        if message.strip():
            return "JoeBro is running privately on this Mac. Add an OpenAI-compatible local endpoint in Settings to generate model replies. You said: " + message
        return "JoeBro is running privately on this Mac."

    def native_agent_tool_result(self, sid, message, form):
        # Calendar is now a real AI tool (create_event) the model calls in the
        # agent loop with ISO-8601 datetimes it parses itself — no hardcoded
        # regex interception. Nothing else is intercepted here.
        return None
