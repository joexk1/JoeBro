"""Tool dispatch, execution, web/image search, tool parsing."""
import jb_ext
from jb_core import *  # noqa: F401,F403
from jb_desktop import DESKTOP_USE_TOOL, agent_desktop_bin
from jb_reach import AGENT_REACH_TOOL

# AI tools that write a document to disk. When the app has "ask before editing"
# on it sends ask_doc_edit and these are gated behind explicit approval BEFORE
# the file is touched (see _gate_doc_edit) — so an edit can't land behind your
# back while JoeBro is in the background or you're on another chat.

# Models (deepseek especially) invent parameter names — map the common ones
# onto ours instead of silently dropping them and failing with "requires path".
_DOC_PARAM_ALIASES = {
    "file": "path", "file_path": "path", "filepath": "path", "filename": "path",
    "file_name": "path", "document": "path", "doc": "path", "target": "path",
    "page": "pages", "page_number": "pages", "page_range": "pages", "line": "lines", "line_range": "lines",
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


def _perm_allows(item_mode, session_mode):
    """Whether an MCP server / plugin tagged `item_mode` may be offered to (and
    called from) a session whose permission_mode is `session_mode`.
    An item's 'readonly' means it CANNOT write anything, so it is safe in every
    session, sandbox included. Anything else ('sandbox', 'full') must not exceed the
    session's own level on the _PERM_RANK scale. Do not compare the two with a raw
    rank inequality: that ranks a harmless read-only server ABOVE a sandbox session
    and hides it."""
    item_mode = item_mode or "sandbox"
    if item_mode == "readonly":
        return True
    return _PERM_RANK.get(item_mode, 0) <= _PERM_RANK.get(session_mode or "sandbox", 0)

# Built-in tools that are LOADED ON DEMAND rather than sent on every round, for the
# phone agent (and any chat with pref `lazy_tools` on). Email, calendar and
# reminder schemas are a couple of thousand tokens that every round of every turn
# would otherwise pay. A group is offered when the latest message (or the last few
# turns, so "yes, send it" still works) mentions it; otherwise find_tools pulls it
# in. Nothing is removed, only not advertised until relevant. Fails open.
_SCHOLAR_RE = (r"\b(?:papers?|journals?|\bdoi\b|citations?|scholar|academic|literature|publications?|"
               r"\bstudies\b|research|books?|authors?|pubmed|arxiv|crossref|openalex|doaj|isbn|dataset|cite)")
DEFERRED_GROUPS = {
    "email": ({"list_email_accounts", "list_emails", "read_email", "send_email", "archive_email",
               "delete_email", "mark_email_read", "reply_to_email", "bulk_email"},
              r"\b(?:e-?mails?|inbox|gmail|outlook|imap|mailbox|unsubscribe|attachment|\breply\b|\bsend (it|this|that|them)\b|"
              r"\bdraft\b|list_emails|send_email|read_email)"),
    "calendar": ({"create_event"},
                 r"\b(?:calendar|events?\b|meetings?|appointments?|schedul|flights?|booking|itinerary|invite|"
                 r"birthday|dinner|lunch|reschedul|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|"
                 r"tomorrow|tonight|next week|this week|weekend|\bat \d{1,2}(:\d\d)?\s?(am|pm)?\b|\bical\b|caldav)"),
    "reminders": ({"remind_me", "manage_reminders", "manage_pulse", "push_status"},
                  r"\b(?:remind|reminders?|pulse|checklist|standing instruction|alert me|let me know|tell me (when|if)|"
                  r"ping me|nudge|keep an eye|monitor|watch for|watchlist|routines?|push|notifications?|weekly|daily|don.t forget|\bin \d+ ?(min|minutes|hours?|h)\b)"),
    "shopping": ({"shopping_list"},
                 r"\b(?:shop\w*|groceries|grocery|buy|supermarket|need (some|more)|run out|ran out|out of|\blist\b|tick)"),
    "health": ({"health_data"},
               r"\b(?:health|sleep|slept|steps|workout|exercise|heart|hrv|resting|weight|fitness|calories|kcal|tired|energy|\brun\b|gym|apple health|recovery)"),
    "coding": ({"coding_agents"},
               r"\b(?:agents?|claude|codex|opencode|coding|session|terminal|running|working on|build|deploy|repo|pr\b|project)"),
    "proactive": ({"proactive_actions"},
                  r"\b(?:proactive|spotted|background|you added|did you add|undo|suggest\w*|approve)"),
    "transfer": ({"export_to_artifact", "deliver_artifact", "move_file", "describe_image"},
                 r"\b(?:save (it|this|that|the)|move (it|this|that|the)|copy (it|this|that|the)|download|upload|forward|attach\w*|"
                 r"send (it|this|that|them|the|her|him|over)|export|photos?|pictures?|images?|pdf|files?|artifact|document)"),
}


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
            if self._phone_toolset(f):
                if (f.get("permission_mode") or "") == "readonly":
                    drop = drop | WRITE_SIDE_EFFECT_TOOLS
                    schemas = [t for t in schemas if t["function"]["name"] not in drop]
                schemas = self._phone_schemas(f, drop) + schemas
                hidden = self.deferred_tool_names(f)
                schemas = [t for t in schemas if t["function"]["name"] not in hidden]
                return (schemas + [t for t in self.custom_tool_schemas() if t["function"]["name"] not in hidden]
                        + self.relevant_mcp_schemas(f) + self.find_tools_schema(f, hidden))
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
        if self._phone_toolset(f):         # silent phone runs (pulse, phone-agent tasks)
            schemas = self._phone_schemas(f, drop) + schemas
        # Enabled custom API tools are offered alongside the built-ins (agent mode
        # only — we already returned [] for chat above). Their description is the
        # schema; the model infers the single `query` string from it.
        if self._lazy_tools(f):
            hidden = self.deferred_tool_names(f)
            schemas = [t for t in schemas if t["function"]["name"] not in hidden]
            schemas += [t for t in self.custom_tool_schemas() if t["function"]["name"] not in hidden]
            schemas += self.relevant_mcp_schemas(f)
            schemas += self.find_tools_schema(f, hidden)
        else:
            schemas += self.custom_tool_schemas()
            schemas += self.mcp_tool_schemas(f)
        if self._plugin_allowed("plugin_macos_use", f):
            # Agent Desktop (Vercel's agent-desktop) when it's installed; the
            # dependency-free osascript version otherwise.
            schemas.append(DESKTOP_USE_TOOL if agent_desktop_bin() else MACOS_USE_TOOL)
        if self._plugin_allowed("plugin_agent_reach", f):
            schemas.append(AGENT_REACH_TOOL)
        if self._plugin_allowed("plugin_advisor", f) and (self.pref("advisor_model") or ""):
            schemas.append(advisor_tool_schema(self.pref("advisor_description") or ""))
        return schemas

    # ----- the phone agent's tools (schemas live in jb_core) ------------------

    @staticmethod
    def _truthy_flag(v):
        return str(v or "").lower() in ("1", "true", "yes")

    def _phone_toolset(self, f):
        """The phone toolset belongs to the phone chat and the silent runs that
        serve it (pulse, phone-agent tasks). Ordinary Mac chats never get it."""
        return (f.get("session") or "") == PHONE_SESSION_ID or self._truthy_flag(f.get("phone_tools"))

    def _lazy_tools(self, f):
        return self._phone_toolset(f) or self._truthy_flag(self.pref("lazy_tools"))

    def _phone_schemas(self, f, drop=()):
        """The phone toolset for this turn. Memory and phone-task tools are for the
        phone chat only; a subagent is a leaf and never gets the spawn tools."""
        on_phone = (f.get("session") or "") == PHONE_SESSION_ID
        skip = set(drop) | {"find_tools"}
        if not on_phone:
            skip |= {"muse_recall", "muse_remember", "muse_update", "muse_forget", "notify_me",
                     "personal_task_create", "personal_task_list", "personal_task_update",
                     "personal_task_delete", "personal_task_run_now"}
        if self._truthy_flag(f.get("subagent")):
            skip |= {"spawn_subagent", "spawn_agent"}
        # An extension can replace a phone schema by name or add its own (hook
        # `phone_tool_schemas` -> list of function schemas).
        extra = {t["function"]["name"]: t for group in jb_ext.call("phone_tool_schemas") for t in group}
        out = []
        for t in PHONE_TOOL_SCHEMAS:
            name = t["function"]["name"]
            if name in skip:
                continue
            t = extra.pop(name, t)
            out.append(self.subagent_schema(t) if name == "spawn_subagent" else t)
        out += [t for n, t in extra.items() if n not in skip]
        return out

    def phone_tool(self, name, body, form):
        """Run one phone-toolset tool. Every branch returns text and never raises
        for a missing capability: it says how to enable it."""
        form = form or {}
        if not self._phone_toolset(form):
            raise ValueError(name + " is only available to the phone agent")
        if getattr(self, "_turn_form", None) is None:
            self._turn_form = form
        taken = jb_ext.first("phone_tool", self, name, body, form)     # an extension may implement or override a tool
        if taken is not None:
            return taken
        if name == "manage_plan":
            return self.manage_plan_tool(body)
        if name == "remind_me":
            return self.remind_me_tool(body)
        if name == "manage_reminders":
            return self.manage_reminders_tool(body)
        if name == "notify_me":
            return self.notify_me_tool(body)
        if name == "manage_pulse":
            return self.manage_pulse_tool(body)
        if name == "spawn_agent":
            return self.spawn_agent_tool(body)
        if name == "spawn_subagent":
            return self.spawn_subagent_tool(body)
        if name == "manage_agents":
            return self.manage_agents_tool(body)
        if name == "get_location":
            return self.get_location_tool(body)
        if name == "get_weather":
            return self.get_weather_tool(body)
        if name == "push_status":
            from jb_push import push_status
            return json.dumps(push_status(self.store), ensure_ascii=False)
        if name == "describe_image":
            from jb_vision import describe_image_tool
            return describe_image_tool(self, self.parse_tool_args(body))
        if name == "find_tools":
            return self.find_tools_tool(body)
        if name in self.MUSE_TOOL_NAMES:
            return self.muse_tool(name, body, form)
        if name == "manage_autonomy":
            return self.manage_autonomy_tool(body)
        if name.startswith("personal_task_"):
            return self.ptask_tool(name, body, form)
        if name in TRANSFER_AI_TOOLS:
            from jb_transfer import run_transfer_tool
            return run_transfer_tool(self, name, self.parse_tool_args(body))
        raise ValueError("unsupported tool " + name)

    # ----- lazy loading: find_tools ------------------------------------------

    MCP_AUTO_CAP = 8
    _STOP = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "my", "me", "i",
             "is", "it", "that", "this", "with", "what", "can", "you", "please", "get", "do"}

    def _recent_text(self, f, turns=4):
        """The latest message plus the last few turns' text (lowercased): what
        decides whether a deferred tool group is relevant."""
        parts = [str(f.get("message") or "")]
        sid = f.get("session") or ""
        if sid:
            try:
                rows = self.store.rows(
                    "select content from messages where session_id=? and role in ('user','assistant') "
                    "order by id desc limit ?", (sid, turns))
                parts += [str(r.get("content") or "")[:1500] for r in rows]
            except Exception:
                pass
        return " ".join(parts).lower()

    def deferred_tool_names(self, f):
        """Names of built-in tools NOT advertised this turn (still reachable via
        find_tools). Fails open: any problem hides nothing."""
        try:
            text = self._recent_text(f)
            hidden = set()
            for names, rx in DEFERRED_GROUPS.values():
                if not re.search(rx, text):
                    hidden |= names
            # Custom API tools (Crossref, OpenAlex, PubMed...) are deferred the same way:
            # shown when the conversation is about papers or shares a word with the tool name.
            if not re.search(_SCHOLAR_RE, text):
                words = {w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 3}
                for t in self.custom_tool_schemas():
                    fn = t["function"]
                    if not (words & set(re.findall(r"[a-z0-9]+", fn["name"].lower()))):
                        hidden.add(fn["name"])
            return hidden
        except Exception:
            return set()

    def find_tools_schema(self, f, hidden=()):
        """The find_tools schema, or [] when there is nothing it could load."""
        if not self._lazy_tools(f):
            return []
        find = next(t for t in PHONE_TOOL_SCHEMAS if t["function"]["name"] == "find_tools")
        names = sorted({s["function"]["description"][1:].split("]")[0]
                        for s in self.mcp_tool_schemas(f)
                        if s["function"].get("description", "").startswith("[")})
        later = sorted({g for g, (gn, _) in DEFERRED_GROUPS.items() if gn & set(hidden)})
        if not names and not later and (f.get("session") or "") != PHONE_SESSION_ID:
            return []
        note = ((" Connected integrations: " + ", ".join(names) + ".") if names else "")
        if later:
            note += " Also loadable on demand: " + ", ".join(later) + "."
        out = json.loads(json.dumps(find))
        out["function"]["description"] += note
        return [out]

    def relevant_mcp_schemas(self, f, cap=None):
        """MCP tools worth offering unprompted: ranked by word overlap with what
        the user actually said. No overlap, no tools; find_tools covers the rest."""
        all_schemas = self.mcp_tool_schemas(f)
        if not all_schemas:
            return []
        cap = self.MCP_AUTO_CAP if cap is None else cap
        words = {w for w in re.findall(r"[a-z0-9]+", (f.get("message") or "").lower())
                 if len(w) > 2 and w not in self._STOP}
        if not words:
            return []
        scored = []
        for sch in all_schemas:
            fn = sch["function"]
            hay = set(re.findall(r"[a-z0-9]+", (fn["name"] + " " + fn.get("description", "")).lower()))
            hits = words & hay
            if hits:
                name_words = set(re.findall(r"[a-z0-9]+", fn["name"].lower()))
                scored.append((len(hits) + 2 * len(words & name_words), fn["name"], sch))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [sch for _, _, sch in scored[:cap]]

    def find_tools_tool(self, body):
        """Pull tools into the CURRENT turn: the schemas are appended to the live
        list run_agent passes each round, so they are callable on the next step.
        With nothing to match it says so plainly (no error)."""
        args = self.parse_tool_args(body)
        query = (args.get("query") or args.get("q") or "").strip()
        if not query:
            raise ValueError("find_tools needs a query: what capability do you need?")
        words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in self._STOP}
        active = getattr(self, "_turn_tools", None)
        have = {t["function"]["name"] for t in (active or []) if t.get("function")}
        turn_form = getattr(self, "_turn_form", {}) or {}
        readonly = (turn_form.get("permission_mode") or "") == "readonly"
        unsafe = {"bash", "python", "list_files", "read_file", "read_pdf", "create_document",
                  "edit_document", "update_document", "suggest_document", "find_tools"}
        candidates = list(self.mcp_tool_schemas(turn_form))
        candidates += [s for s in self._phone_schemas(turn_form) if s["function"]["name"] not in unsafe]
        candidates += [s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] not in unsafe | {"manage_chats"}]
        candidates += self.custom_tool_schemas()
        group_names = set()
        for names, rx in DEFERRED_GROUPS.values():
            if re.search(rx, query.lower()):
                group_names |= names
        found, seen_names = [], set()
        for sch in candidates:
            fn = sch["function"]
            if fn["name"] in have or fn["name"] in seen_names:
                continue
            if readonly and fn["name"] in WRITE_SIDE_EFFECT_TOOLS:
                continue
            seen_names.add(fn["name"])
            hay = set(re.findall(r"[a-z0-9]+", (fn["name"] + " " + fn.get("description", "")).lower()))
            hits = words & hay
            if fn["name"] in group_names:
                hits = hits | {"_group"}
            if hits:
                found.append((len(hits), sch))
        found.sort(key=lambda x: -x[0])
        picked = [sch for _, sch in found[:12]]
        if not picked:
            return json.dumps({"ok": True, "found": 0, "tools": [],
                               "note": "No matching capability. If the user wants it, they can connect one "
                                       "in Tools on the Mac (Tools > MCP Servers)."}, ensure_ascii=False)
        if active is not None:
            active.extend(picked)          # live list: callable from the next round
        try:
            from jb_prompt import _advice_for
            how = "\n".join(_advice_for([s["function"]["name"] for s in picked]))
        except Exception:
            how = ""
        return json.dumps({"ok": True, "found": len(picked),
                           "note": "these are now available to you, call them directly"
                                   + (("\nUsage notes:\n" + how) if how else ""),
                           "tools": [{"name": s["function"]["name"], "description": s["function"]["description"][:200]}
                                     for s in picked]}, ensure_ascii=False)

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
        if self.pref("cloak_mode") is not False:
            query = cloak_text(query)
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
            if not _perm_allows(r.get("permission_mode"), (f or {}).get("permission_mode")):
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
                if (f or {}).get("permission_mode") == "readonly" and (t.get("annotations") or {}).get("readOnlyHint") is False:
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
        taken = jb_ext.first("mcp_call", self, row, real_tool, arguments)   # an extension may serve this tool itself
        if taken is not None:
            return taken
        try:
            text, is_error = mcp_call_tool(row.get("command") or "", row.get("args") or "",
                                           real_tool, arguments)
            if not is_error:
                filtered = jb_ext.first("mcp_output", row, real_tool, text or "")   # e.g. shrink an oversized result
                if filtered is not None:
                    text = filtered
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
        return _perm_allows(row.get("permission_mode"), (f or {}).get("permission_mode"))

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
        if root is None and name in _FILE_TOOLS:
            return {"tool": name, "command": self.tool_command_summary(name, body),
                    "exit_code": 1, "output": "No folder is bound to this chat. Bind a folder before using file tools; no file was created."}
        if readonly and name in WRITE_SIDE_EFFECT_TOOLS:
            return None
        if name.startswith("mcp_"):
            # MCP tool (mcp_<serverid>__<tool>). The body is the JSON-serialized
            # arguments dict (_tool_body_from_params json.dumps()es unknown-name
            # params); parse it back so we pass the real arguments to tools/call.
            mrow, real_tool = self.mcp_server_for_func(name)
            if mrow:
                if readonly:
                    definitions = json.loads(mrow.get("tools_json") or "[]")
                    definition = next((t for t in definitions if t.get("name") == real_tool), {})
                    if (definition.get("annotations") or {}).get("readOnlyHint") is False:
                        return {"tool": name, "command": real_tool, "exit_code": 1,
                                "output": "This MCP tool writes files or changes an account; read-only agent access cannot run it."}
                if not _perm_allows(mrow.get("permission_mode"), (form or {}).get("permission_mode")):
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
        if name == "shopping_list":
            if readonly and (self.parse_tool_args(body) or {}).get("action", "list") != "list":
                return {"tool": name, "command": name, "exit_code": 1, "output": "Read-only access: can only list the shopping list."}
            return {"tool": name, "command": "shopping_list", "exit_code": 0, "output": self.shopping_list_tool(body)}
        if name == "health_data":
            return {"tool": name, "command": "health_data", "exit_code": 0, "output": self.health_tool(body)}
        if name == "coding_agents":
            return {"tool": name, "command": "coding_agents", "exit_code": 0, "output": self.coding_agents_tool(body)}
        if name == "proactive_actions":
            if readonly and (self.parse_tool_args(body) or {}).get("action", "list") != "list":
                return {"tool": name, "command": name, "exit_code": 1, "output": "Read-only access: can only list proactive actions."}
            return {"tool": name, "command": "proactive_actions", "exit_code": 0, "output": self.proactive_tool(body)}
        if name == "desktop_use" or (name == "macos_use" and agent_desktop_bin()):
            if not self._plugin_allowed("plugin_macos_use", form):
                return {"tool": "desktop_use", "command": "desktop_use", "exit_code": 1,
                        "output": "The Agent Desktop plugin is switched off (or needs a higher agent access level) — tell the user; do not retry."}
            try:
                args = json.loads(body) if body else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            if readonly and args.get("action") not in ("list_apps", "snapshot", "find"):
                return {"tool": "desktop_use", "command": "desktop_use", "exit_code": 1,
                        "output": "Read-only access: desktop_use can only observe (list_apps, snapshot, find)."}
            return self.run_desktop_use(form, args)
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
        if name == "agent_reach":
            if not self._plugin_allowed("plugin_agent_reach", form):
                return {"tool": "agent_reach", "command": "agent_reach", "exit_code": 1,
                        "output": "The agent_reach plugin is switched off (or needs a higher agent access level) — tell the user; do not retry."}
            return self.run_agent_reach(form, self.parse_tool_args(body))
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
                event["doc"] = self._doc_event_payload(root, name, body, allow_outside, form)
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

    def _doc_event_payload(self, root, name, body, allow_outside, form=None):
        """The {doc_id,title,language,content} for a document the AI just wrote,
        so the app can open it / refresh the open tab. Matches the app's
        "local-<relpath>" id scheme so the open tab updates in place. For a chat
        bound to a remote DEVICE folder (e.g. a Mac-local folder while this
        backend runs on the Pi) the file lives on the device, so read it back
        through the bridge — otherwise the Pi reads a path it can't see and the
        editor never refreshes."""
        import posixpath
        try:
            fields = self.parse_tool_fields(body)
            rel = (fields.get("path") or fields.get("title") or "").strip()
            if not rel:
                return None
            bridge = self._device_bridge(self._session_host(form)) if form else None
            if bridge:
                sub = rel.lstrip("/")
                content = self._read_open_doc_content(form, sub)
                if content is None:
                    return None
                base = posixpath.basename(sub) or sub
                return {"doc_id": "local-" + sub, "title": base,
                        "language": (os.path.splitext(base)[1].lstrip(".") or "markdown"),
                        "content": content}
            if root is None:
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
            return {"doc_id": "local-" + sub, "title": target.name,
                    "language": target.suffix.lstrip(".") or "markdown",
                    "content": content}
        except Exception:
            return None

    def _read_open_doc_content(self, f, rel):
        """Read a bound-folder file's text for the app (open-doc context + doc
        events). Bridges to the device when the chat is bound to a remote host's
        folder, so a Mac-local folder works even though this backend is the Pi."""
        bridge = self._device_bridge(self._session_host(f))
        if bridge:
            if rel.lower().endswith(".docx"):
                return self._bridge_docx_text(bridge, self._session_host(f), self._remote_path(f, rel))
            if rel.lower().endswith(".apkg"):
                try:
                    return self._bridge_apkg_text(bridge, self._session_host(f), self._remote_path(f, rel))
                except Exception:
                    return None
            url, token = bridge
            r = bridge_call(url, token, "/v1/read_file",
                            {"path": self._remote_path(f, rel), "max_chars": 100_000_000})
            if r is None or r.get("error"):
                return None
            return r.get("content") or ""
        session = self.store.one("select workdir from sessions where id=?", (f.get("session") or "",))
        root_text = ((session or {}).get("workdir") or "").strip()
        if not root_text:
            return None
        try:
            target = self.resolve_in_workdir(Path(root_text).expanduser().resolve(), rel)
            if target.is_file():
                return read_doc_text(target)
        except Exception:
            pass
        return None

    def tool_command_summary(self, name, body):
        fields = self.parse_tool_fields(body)
        target = fields.get("path") or fields.get("title") or ""
        if target.strip():
            return target.strip()
        if body.strip():
            return body.strip().splitlines()[0][:160]
        return name

    @staticmethod
    def _parse_range(spec, total, unit="page"):
        """'7', '3-9', '12-' -> inclusive 1-based (first, last), clamped to total."""
        text = str(spec or "").strip().replace("\u2013", "-").replace(" ", "")
        m = re.fullmatch(r"(\d+)(?:-(\d*))?", text)
        if not m:
            raise ValueError(f"bad {unit} range '{spec}' — use '7', '3-9' or '12-'")
        first = int(m.group(1))
        if first > total:
            raise ValueError(f"{unit} {first} is past the end — there are only {total}")
        if m.group(2) is None:
            last = first
        elif m.group(2) == "":
            last = total
        else:
            last = int(m.group(2))
        return max(1, first), min(total, max(first, last))

    def _pdf_page_texts(self, target: Path):
        """Per-page text, page 1 at index 0 — blank pages kept so the numbering
        the agent pages by matches the numbering in the actual PDF."""
        errors = []
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(target))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
            if any(pages):
                return pages
        except Exception as exc:
            errors.append(f"pypdf: {exc}")
        try:
            from Foundation import NSURL
            from Quartz import PDFDocument
            doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(target)))
            count = int(doc.pageCount()) if doc else 0
            pages = []
            for i in range(count):
                page = doc.pageAtIndex_(i)
                pages.append(str(page.string() or "").strip() if page else "")
            if any(pages):
                return pages
        except Exception as exc:
            errors.append(f"Quartz: {exc}")
        try:
            proc = subprocess.run(
                ["/usr/bin/mdls", "-raw", "-name", "kMDItemTextContent", str(target)],
                text=True, capture_output=True, timeout=12)
            text = (proc.stdout or "").strip()
            if text and text != "(null)":
                # ponytail: Spotlight hands back one blob with no page breaks, so
                # it is one "page". Only ever reached when both real extractors fail.
                return [text]
        except Exception as exc:
            errors.append(f"Spotlight: {exc}")
        raise ValueError("Could not extract text from PDF" + (": " + "; ".join(errors) if errors else ""))

    def _read_budget(self):
        return getattr(self, "MAX_TOOL_OUTPUT_CHARS", 24000) - 400   # room for the footer

    def read_pdf_text(self, target: Path, pages=None):
        """Text from a local PDF. `pages` ('7', '3-9', '12-') reads just that
        slice. With no `pages` we send whole pages up to the tool-output cap and
        say where to resume — a long PDF gets worked through page by page instead
        of having its middle silently clipped out."""
        page_texts = self._pdf_page_texts(target)
        total = len(page_texts)
        first, last = self._parse_range(pages, total) if str(pages or "").strip() else (1, total)
        budget, out, used, shown = self._read_budget(), [], 0, first - 1
        for n in range(first, last + 1):
            body = page_texts[n - 1]
            shown = n
            if not body:
                continue
            chunk = f"--- Page {n} ---\n{body}"
            if out and used + len(chunk) > budget:
                shown = n - 1          # whole pages only — never cut one in half
                break
            out.append(chunk)
            used += len(chunk) + 2
        if not out:
            raise ValueError(f"no text on pages {first}-{last} of {total} in {target.name}")
        text = "\n\n".join(out)
        if (first, shown) != (1, total):
            text += f"\n\n--- shown: pages {first}-{shown} of {total}. " + (
                f'Call read_pdf again with pages: "{shown + 1}-" for the rest. ---'
                if shown < total else "End of document. ---")
        return text

    def _slice_lines(self, text, spec):
        """The `lines` window for a text file — what `pages` is for a PDF. Same
        deal: no spec means whole lines up to the cap plus where to resume."""
        lines = text.splitlines()
        total = len(lines)
        budget = self._read_budget()
        if str(spec or "").strip():
            first, last = self._parse_range(spec, total, unit="line")
        elif len(text) <= budget:
            return text
        else:
            first, last, used = 1, 0, 0
            for i, line in enumerate(lines, start=1):
                used += len(line) + 1
                if used > budget and last:
                    break
                last = i
        body = "\n".join(lines[first - 1:last])
        if (first, last) == (1, total):
            return body
        return body + f"\n\n--- shown: lines {first}-{last} of {total}. " + (
            f'Call read_file again with lines: "{last + 1}-" for the rest. ---'
            if last < total else "End of file. ---")

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
                return self.read_pdf_text(target, fields.get("pages"))
            if target.suffix.lower() in IMAGE_EXTS:
                # The actual image is attached to the conversation by run_agent so
                # the model can see it; this is just the tool's text acknowledgement.
                return f"[image {rel} is shown to you below — look at it directly]"
            # `pages` on a text file means lines — the model reaches for it out of habit.
            return self._slice_lines(read_doc_context(target), fields.get("lines") or fields.get("pages"))
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
            return self.read_pdf_text(target, fields.get("pages"))
        if name == "create_document":
            fields = self.parse_tool_fields(body)
            content = fields.get("content") or ""
            append = str(fields.get("append") or "").lower() in ("1", "true", "yes")
            if not (fields.get("path") or fields.get("title") or "").strip() and not content.strip():
                # A mangled tool call arrives with neither — that is what leaves
                # empty Untitled.md files behind.
                raise ValueError("create_document got no path and no content — nothing was written; "
                                 "call it again with both")
            rel = (fields.get("path") or fields.get("title") or "Untitled.md").strip()
            target = self.resolve_tool_path(root, rel, allow_outside)
            with lock_for_path(target):
                if append and target.is_file():
                    content = self._join_chunk(read_doc_text(target), content, target.suffix.lower())
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
                    note = ""
                    if "content" in fields:
                        updated = fields["content"]
                    else:
                        updated, note = apply_find_replace(
                            text, fields.get("find"), fields.get("replace", ""),
                            replace_all=str(fields.get("replace_all") or "").lower() in ("1", "true", "yes"),
                            occurrence=fields.get("occurrence"), name=target.name)
                    backup_existing(target)
                    write_doc_text(target, updated)
            row = self.store.one("select id from documents where file_path=?", (str(target),))
            if row:
                self.store.exec(
                    "update documents set current_content=?, updated_at=? where id=?",
                    (updated, now_iso(), row["id"]),
            )
            return f"File edited: {target}" + (f" — {note}" if note else "")
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

    def _resolve_chat(self, ref, folder=""):
        """Session by id, else name (exact, then substring), optionally scoped to a
        folder group/bound path. Several substring hits -> error listing them."""
        ref, folder = (ref or "").strip(), (folder or "").strip()
        scope, ps = ("", ())
        if folder:
            scope, ps = " and (lower(folder)=lower(?) or workdir=?)", (folder, folder)
        sel = "select * from sessions where id not like 'task\\_session\\_%' escape '\\'" + scope
        if not ref:
            rows = self.store.rows(sel + " order by datetime(created_at) desc limit 6", ps) if folder else []
            if len(rows) > 1:
                raise ValueError("several chats in that folder, pass chat: " + "; ".join(f"{r['id']} {r.get('name')}" for r in rows))
            return rows[0] if rows else None
        row = self.store.one("select * from sessions where id=?", (ref,))
        if row and (not folder or self.store.one(sel + " and id=?", ps + (ref,))):
            return row
        row = self.store.one(sel + " and lower(name)=lower(?) order by datetime(created_at) desc limit 1", ps + (ref,))
        if row:
            return row
        rows = self.store.rows(sel + " and name like ? order by datetime(created_at) desc limit 6", ps + (f"%{ref}%",))
        if len(rows) > 1:
            raise ValueError("ambiguous chat, pass its id: " + "; ".join(f"{r['id']} {r.get('name')}" for r in rows))
        return rows[0] if rows else None

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

    def _phone_may_run(self, form):
        """False only for the phone chat when the user has NOT switched on "Let the
        phone agent run commands" (pref mobile_allow_full)."""
        if ((form or {}).get("session") or "") != PHONE_SESSION_ID:
            return True
        return self._truthy_flag(self.pref("mobile_allow_full"))

    def _phone_bind_ok(self, form, folder):
        """The folder a phone-driven bind may use (see jb_files.phone_folder_check)."""
        if folder and ((form or {}).get("session") or "") == PHONE_SESSION_ID:
            from jb_files import phone_folder_check
            return phone_folder_check(folder)
        return folder

    def manage_chats_tool(self, body, form=None):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action == "list":
            folder = (args.get("folder") or "").strip()
            limit = min(500, max(1, int(args.get("limit") or 20)))
            offset = max(0, int(args.get("offset") or 0))
            if folder == "?":   # folder names + chat counts
                rows = self.store.rows("select coalesce(folder,'') f, count(*) c from sessions where id not like 'bot\\_tg\\_%' escape '\\' "
                                       "and id not like 'task\\_session\\_%' escape '\\' and id != ? group by lower(coalesce(folder,''))", (PHONE_SESSION_ID,))
                return json.dumps([{"folder": r["f"], "chats": r["c"]} for r in rows], ensure_ascii=False)
            rows = self.store.rows(
                "select * from sessions where id not like 'bot\\_tg\\_%' escape '\\' "
                "and id not like 'task\\_session\\_ptask\\_%' escape '\\' "
                "and id != ? and (?='' or lower(folder)=lower(?) or workdir=?) "
                "and (?='' or name like ?) "
                "order by datetime(created_at) desc limit ? offset ?",
                (PHONE_SESSION_ID, folder, folder, folder, (args.get("query") or "").strip(), f"%{(args.get('query') or '').strip()}%", limit, offset))
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
            fo, ch = (args.get("folder") or "").strip(), (args.get("chat") or "").strip()
            rows = self.store.rows(
                """select m.session_id, s.name, m.role, m.content, m.created_at
                   from messages m join sessions s on s.id=m.session_id
                   where m.content like ? and (?='' or lower(s.folder)=lower(?) or s.workdir=?)
                   and (?='' or s.id=? or lower(s.name)=lower(?)) order by m.id desc limit 40""",
                (f"%{q}%", fo, fo, fo, ch, ch, ch))
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
            if perm == "full" and not self._phone_may_run(form):
                perm = "sandbox"
            folder = self._phone_bind_ok(form, (args.get("folder") or "").strip())
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
        target = self._resolve_chat(args.get("chat"), "" if action == "bind_folder" else args.get("folder"))
        if not target:
            raise ValueError(f"chat not found: {args.get('chat') or ''}")
        if target["id"] == PHONE_SESSION_ID:
            raise ValueError("The main JoeBro conversation cannot be managed as a delegated chat")
        if action in ("list_files", "read_file"):
            bound = (target.get("workdir") or "").strip()
            if not bound:
                raise ValueError("That chat has no connected folder")
            path = str(args.get("path") or ".")
            result = self.execute_file_tool(Path(bound), action, f"path: {path}" + (f"\nlines: {args['lines']}" if args.get("lines") else ""), allow_outside=False)
            return json.dumps({"chat":target["id"],"path":path,"output":result[:8000],"truncated":len(result)>8000}, ensure_ascii=False)
        if action == "bind_folder":
            folder = self._phone_bind_ok(form, (args.get("folder") or "").strip())
            self.store.exec("update sessions set workdir=? where id=?", (folder, target["id"]))
            return f"Bound chat '{target.get('name')}' to folder: {folder or '(none)'}"
        if action == "set_permission":
            perm = (args.get("permission") or "").lower()
            if perm not in ("sandbox", "readonly", "full"):
                raise ValueError("permission must be sandbox, readonly or full")
            if perm == "full" and not self._phone_may_run(form):
                raise ValueError("Full Access can only be given from the Mac: the user has not allowed the "
                                 "phone agent to run commands (Settings > Mobile)")
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
                "select id,role,content,created_at from messages where session_id=? order by id desc limit ?",
                (target["id"], n))
            return json.dumps({"chat": target.get("name"), "id": target["id"],
                               "mode": target.get("mode") or "chat",
                               "permission": target.get("permission_mode") or "sandbox",
                               "model": target.get("model") or "",
                               "message_order": "newest_first",
                               "messages": [{"id": r["id"], "role": r["role"],
                                             "content": r.get("content") or "",
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
            if perm == "full" and not self._phone_may_run(form):
                perm = "sandbox"       # the phone cannot borrow a chat's Full Access
            sub = {"session": target["id"], "message": msg,
                   "model": target.get("model") or "", "endpoint_id": target.get("endpoint_id") or "",
                   "endpoint_url": target.get("endpoint_url") or "",
                   "mode": (target.get("mode") or "agent"), "permission_mode": perm,
                   "allow_web_search": "true"}
            if perm == "full":
                sub["allow_bash"] = "true"
            if bool(self.pref("bot_ask_permissions")) or str((form or {}).get("ask_permission") or "").lower() in ("1", "true", "yes"):
                sub["ask_permission"] = "true"
                # doc-edit approval only for Telegram (no editor there); the phone has none either but no prompt UI for it
                from_bot = str((form or {}).get("session") or "").startswith("bot_tg_")
                if (from_bot and bool(self.pref("bot_ask_permissions"))) or str((form or {}).get("ask_doc_edit") or "").lower() in ("1", "true", "yes"):
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
        if name == "read_email_attachment":
            return self.email_attachment_payload(args)
        if name == "save_email_attachment":
            uid = args.get("uid")
            index = args.get("index")
            if not isinstance(uid, str) or not uid.strip():
                raise ValueError("uid required")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("index must be a non-negative integer from the attachment listing")
            from artifact_store import save_bytes
            data, mime, filename = self.email_attachment(uid, index, args.get("folder") or "INBOX")
            return json.dumps(save_bytes(data, filename, mime, source="email"), ensure_ascii=False)
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
        if name == "read_attachment":
            args = self.parse_tool_args(body)
            row = self.store.one("select * from uploads where id=?", (str(args.get("attachment_id") or ""),))
            if not row:
                raise ValueError("Unknown uploaded attachment")
            path = Path(row["path"])
            if path.suffix.lower() not in (".docx", ".pdf", ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".xlsx", ".apkg"):
                raise ValueError("This attachment needs a compatible visual or binary reader")
            text = self.read_pdf_text(path) if path.suffix.lower() == ".pdf" else read_doc_context(path)
            return self._slice_lines(text, args.get("lines")).replace("Call read_file again", "Call read_attachment again")
        if name in PHONE_TOOL_NAMES:
            return self.phone_tool(name, body, form)
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
        if name == "manage_events":
            return self.manage_events_tool(body)
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
        # Timezone: the zone of the DEPARTURE/origin place for the start time.
        # Missing or invalid is an error, never a silent reinterpretation.
        tz_arg = (args.get("timezone") or args.get("tz") or "").strip()
        assumed = False
        if not tz_arg and not all_day:
            raise ValueError("create_event requires `timezone` (IANA id, e.g. Europe/Amsterdam; for travel the "
                             "DEPARTURE location's zone). Naive times are rejected, never assumed to be the server's zone.")
        tz = tz_arg or self.backend_zone_name()
        end_tz = (args.get("end_timezone") or "").strip() or tz
        self.resolve_zone(tz); self.resolve_zone(end_tz)
        if not all_day:
            start = self.localize_event_time(start, tz)
            end = self.localize_event_time(end, end_tz)
        event = {"summary": summary, "dtstart": start, "dtend": end, "all_day": all_day, "tz": tz,
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
            self.queue_calendar_ops([dict(event, op="create", uid=uid)])
            return json.dumps({"ok": True, "uid": uid, "event": event, "queued_for_mac": True,
                               **({"timezone_assumed": f"no timezone was given; read the times as {tz}. Tell the user, and if wrong fix with manage_events."} if assumed else {})}, ensure_ascii=False)
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
    def _join_chunk(existing, chunk, suffix=""):
        """Join the next part of a chunked create_document onto what is already
        saved. Tool arguments arrive with their trailing newlines trimmed, so
        without a separator two parts would run together; parts start at a
        paragraph or heading boundary, so a blank line is the natural join
        (a single newline for row-based files like CSV/XLSX)."""
        if not existing or existing.endswith("\n") or chunk.startswith("\n"):
            return existing + chunk
        return existing + ("\n" if suffix in (".csv", ".xlsx", ".tsv", ".apkg") else "\n\n") + chunk

    @staticmethod
    def _tool_call_truncated(fn):
        """True when a native tool_call's arguments are non-empty but not valid
        JSON — the reply hit max_tokens mid-call (typically an edit_document
        `content` rewrite of a long file), so the params would silently parse
        as {} and the tool would fail with a misleading "requires path"."""
        raw = (fn or {}).get("arguments")
        if isinstance(raw, dict) or not str(raw or "").strip():
            return False
        text = str(raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            json.loads(text)
            return False
        except Exception:
            return True

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
            keys = ("path", "pages", "lines") if name in ("list_files", "read_file", "read_pdf") else (
                "path", "title", "language", "region", "find", "replace", "reason",
                "replace_all", "occurrence", "append", "content")
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
                "path", "title", "language", "region", "content", "find", "replace", "reason",
                "pages", "lines", "replace_all", "occurrence", "append"
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
