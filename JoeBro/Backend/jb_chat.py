"""Chat, agent loop, streaming, compaction, history, AI-detection."""
from jb_core import *  # noqa: F401,F403


class ChatMixin:

    def chat_stream(self):
        f = self._form_body()
        sid = f.get("session")
        message = f.get("message", "")
        sess = self.store.one("select * from sessions where id=?", (sid or "",)) or {}
        f.setdefault("model", sess.get("model") or "")
        f.setdefault("endpoint_id", sess.get("endpoint_id") or "")
        f.setdefault("endpoint_url", sess.get("endpoint_url") or "")
        if f.get("model") or f.get("endpoint_id") or f.get("endpoint_url"):
            self.store.exec(
                "update sessions set model=?, endpoint_id=?, endpoint_url=? where id=?",
                (
                    f.get("model") or sess.get("model") or "",
                    f.get("endpoint_id") or sess.get("endpoint_id") or "",
                    f.get("endpoint_url") or sess.get("endpoint_url") or "",
                    sid,
                ),
            )
        # Duplicate-submission guard: if the last message in this session is an
        # identical user message with no assistant reply yet, the previous turn
        # is still running (e.g. a slow edit and the user pressed send again).
        # Starting a second run here double-persists the message and races the
        # first run on the same files — which corrupts edits and shows the turn
        # twice. Acknowledge and stop instead of running it twice.
        if message.strip():
            last = self.store.one(
                "select role, content from messages where session_id=? order by id desc limit 1",
                (sid or "",))
            if last and last.get("role") == "user" and (last.get("content") or "") == message:
                self._send(200, "text/event-stream")
                try:
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return
        self.add_message(sid, "user", message, {})
        # Open the SSE stream BEFORE running the agent so tool rows and reasoning
        # arrive at the client AS THEY HAPPEN, not after the whole loop finishes.
        self._send(200, "text/event-stream")
        started = time.time()

        # If the user switches chats / closes the tab mid-run, the client drops
        # the SSE socket. Swallow the write error and keep computing server-side
        # so the agent still finishes and the reply is persisted (loaded when
        # they come back) — otherwise a backgrounded turn silently loses its answer.
        client_alive = {"ok": True}

        def sse(obj):
            if not client_alive["ok"]:
                return
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_alive["ok"] = False

        captured_meta = {"memories_used": [], "plugins_used": [], "skills_used": []}
        def emit(obj):
            # Live events from run_agent. A doc payload is streamed into the
            # editor; everything else (tool_start/tool_output/thinking delta)
            # passes straight through with the existing SSE shapes. memories_used
            # / plugins_used are also captured for the persisted message metadata.
            doc = obj.get("_doc")
            if doc is not None:
                if obj.get("_tool") == "create_document":
                    self._stream_document(sse, doc)
                else:
                    sse({"type": "doc_update", "doc_id": doc.get("doc_id", ""),
                         "title": doc.get("title", ""), "language": doc.get("language", "markdown"),
                         "content": doc.get("content", ""), "version": 1})
            else:
                if obj.get("type") == "memories_used":
                    captured_meta["memories_used"] = obj.get("data") or []
                elif obj.get("type") == "plugins_used":
                    captured_meta["plugins_used"] = obj.get("data") or []
                elif obj.get("type") == "skills_used":
                    captured_meta["skills_used"] = obj.get("data") or []
                sse(obj)

        # Hand the live SSE channel to the tool layer so document writes can ask
        # for approval before touching disk (see _gate_doc_edit). Request-scoped:
        # a fresh Handler per request, so there's no cross-talk between sessions.
        self._perm_emit = emit

        native = self.native_agent_tool_result(sid, message, f)
        thinking = ""
        tool_events = []
        streamed_live = False   # run_agent streams content+thinking deltas itself
        if native:
            if len(native) == 3:
                reply, tool_events, thinking = native
            else:
                reply, tool_events = native
            # Native path produced events up-front — replay them live.
            for ev in tool_events:
                emit({"type": "tool_start", "tool": ev.get("tool"), "command": ev.get("command", "")})
                emit({"type": "tool_output", "tool": ev.get("tool"), "command": ev.get("command", ""),
                      "output": ev.get("output", ""), "exit_code": ev.get("exit_code", 0)})
            if thinking:
                for chunk in self._chunks(thinking, 24):
                    sse({"delta": chunk, "thinking": True})
        else:
            # Multi-round agent loop — emits tool rows AND the final answer's
            # content + thinking deltas live (so it streams, not dumps).
            reply, thinking, agent_events = self.run_agent(f, emit=emit)
            tool_events.extend(agent_events)
            streamed_live = True
        # Catch any text-format tool calls in the FINAL reply + strip markup.
        # These weren't seen during the loop, so stream their rows now. In CHAT
        # mode there are no tools, so we only strip stray tool markup — we never
        # execute it (running it would let chat mode silently edit files).
        run_text_tools = (f.get("mode") or "").lower() == "agent"
        # The per-turn tool cap spans BOTH paths: when the model can't call tools
        # natively (or we cut it off), it emits tool markup as text and this pass
        # runs it. Subtract what the loop already used so the total honours the cap.
        _cap = max(0, int(f.get("max_tool_calls") or 0))
        _remaining = max(0, _cap - len(tool_events)) if _cap else None   # None = unlimited
        reply, tool_results = self.execute_ai_file_tools(sid, reply, f, run=run_text_tools, max_calls=_remaining)
        for ev in tool_results:
            emit({"type": "tool_start", "tool": ev.get("tool"), "command": ev.get("command", "")})
            emit({"type": "tool_output", "tool": ev.get("tool"), "command": ev.get("command", ""),
                  "output": ev.get("output", ""), "exit_code": ev.get("exit_code", 0)})
            doc = ev.get("doc")
            if doc and ev.get("exit_code") == 0:
                emit({"_doc": doc, "_tool": ev.get("tool")})
        tool_events.extend(tool_results)
        saved_reply = reply

        # Stream the visible reply tokens — UNLESS run_agent already streamed the
        # content live (word-splitting then would double it).
        if not streamed_live:
            for token in saved_reply.split(" "):
                sse({"delta": token + " "})
        # tok/s + context-window gauge. Endpoints rarely return token usage on a
        # stream, so estimate from text (~4 chars/token) — close enough for the
        # readout. The full prompt is every message stored for this session so far
        # (the assistant reply isn't persisted until below), plus the response.
        elapsed = round(time.time() - started, 2)
        m = self._response_stats(sid, saved_reply, thinking, f.get("model"), elapsed)
        sse({"type": "metrics", "data": {"total_time": elapsed, "response_time": elapsed,
                                         "tokens_per_second": m["tokens_per_second"],
                                         "context_percent": m["context_percent"]}})
        if client_alive["ok"]:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_alive["ok"] = False

        # Persist the assistant message AFTER streaming (with tool_events meta).
        metadata = {"model": f.get("model") or "Local fallback",
                    "response_time": elapsed,
                    "tokens_per_second": m["tokens_per_second"],
                    "context_percent": m["context_percent"]}
        if thinking:
            metadata["thinking"] = thinking
        if tool_events:
            # Persist tool rows without the bulky `doc` content blob.
            metadata["tool_events"] = [{k: v for k, v in e.items() if k != "doc"} for e in tool_events]
        if captured_meta["memories_used"]:
            metadata["memories_used"] = captured_meta["memories_used"]
        if captured_meta["plugins_used"]:
            metadata["plugins_used"] = captured_meta["plugins_used"]
        if captured_meta["skills_used"]:
            metadata["skills_used"] = captured_meta["skills_used"]
        self.add_message(sid, "assistant", saved_reply, metadata)

        # Self-improving: quietly learn a memory / skill from this turn (off the
        # request path so it never delays the reply). Skipped for task sessions.
        if not sid.startswith("task_session_"):
            threading.Thread(target=self._auto_learn,
                             args=(sid, message, saved_reply, f.get("endpoint_id"), f.get("model")),
                             daemon=True).start()

    @staticmethod
    def _chunks(text, n):
        for i in range(0, len(text), n):
            yield text[i:i + n]

    def _stream_document(self, sse, doc):
        """Open a freshly-created doc in the editor and type its content in,
        matching the Pi's doc_stream_open -> deltas -> doc_update sequence."""
        content = doc.get("content") or ""
        title = doc.get("title", "")
        language = doc.get("language", "markdown")
        sse({"type": "doc_stream_open", "title": title, "language": language})
        step = max(40, len(content) // 60)  # cap the animation at ~60 frames
        acc = ""
        for chunk in self._chunks(content, step):
            acc += chunk
            sse({"type": "doc_stream_delta", "content": acc})
            time.sleep(0.02)
        sse({"type": "doc_update", "doc_id": doc.get("doc_id", ""), "title": title,
             "language": language, "content": content, "version": 1})

    # no agent round cap — the loop runs until the model stops calling tools

    def _agent_system(self, f):
        # COMPACT prompt — we send native function schemas, so the model must
        # NOT be told a text tool format (verbose format instructions make weak
        # models echo the tool-call boilerplate INTO the file content).
        now = datetime.now().astimezone()
        system = (
            "You are the assistant inside the native JoeBro app. You can use the provided function "
            "tools across MULTIPLE steps: call a tool, read its result, then call another, until the "
            "task is done — then give a short prose summary. For email use the email tools (never bash/curl); "
            "for current info or 'research X' use web_search or trigger_research; use list_files/read_file/read_pdf "
            "to inspect bound-folder files, including local PDFs; to add to / change a file "
            "call edit_document (find/replace for a small change, or pass `content` with the COMPLETE new "
            "text to rewrite it; create_document only for a new file). "
            "To add a calendar event call create_event with ISO-8601 start/end datetimes that YOU resolve "
            "from the user's words. Never put tool syntax or raw file contents in your text reply.\n"
            f"Today is {now.strftime('%A, %Y-%m-%d')} and the current local time is {now.strftime('%H:%M')} "
            f"({now.strftime('%z') or 'local'}). Use this to resolve relative dates like 'tomorrow', "
            "'wednesday', or 'next week' into absolute ISO-8601 datetimes.\n"
            "CRITICAL: only claim you did something if the matching tool actually ran and returned success. "
            "To create, edit, or save a file you MUST call create_document or edit_document and see it succeed. "
            "Saying 'done', 'created', 'saved', or 'added' WITHOUT a successful tool call in this turn is forbidden — "
            "if you have not called the tool yet, call it NOW instead of claiming completion. "
            "If a tool isn't available to you, or returns an error / non-zero exit, tell the user plainly that "
            "it didn't happen and why — NEVER pretend a command ran or a file changed when it didn't."
        )
        mode = (f.get("permission_mode") or "").lower()
        if mode == "readonly":
            system += " You are in READ-ONLY mode: you may search and read, but must not create, edit, send, or delete anything."
        if str(f.get("allow_bash") or "").lower() not in ("1", "true", "yes") or mode != "full":
            system += (" The terminal is OFF: you have NO ability to run shell or Python commands or copy/move/delete "
                       "files. If the user asks for that, say the terminal toggle (Full Access) must be enabled — do not claim you did it.")
        doc = self.open_doc_context(f)
        if doc:
            system += (
                f"\n\nThe user has this document OPEN: path `{doc['path']}`. When they ask to add to / "
                f"append to / change \"the doc\", call edit_document for that path with `content` set to the "
                f"entire updated file (current text plus their additions) — do not create a new file. "
                f"Current content of `{doc['path']}`:\n\"\"\"\n{doc['content']}\n\"\"\""
            )
        return system

    def _chat_system(self, f):
        """System prompt for CHAT mode — pure conversation, NO tools. Spells out
        that the assistant cannot take actions so it stops claiming it edited the
        doc / sent mail / ran something, and stops emitting tool-call syntax."""
        now = datetime.now().astimezone()
        system = (
            "You are the assistant inside the native JoeBro app, in CHAT mode. This is a plain "
            "conversation: you have NO tools and CANNOT take any action on the user's machine. "
            "You cannot edit, create, or save files, cannot send or read email, cannot search the "
            "web, run commands, or change the calendar. NEVER claim or imply you did any of these — "
            "do not say you 'edited', 'updated', 'saved', 'created', 'sent', or 'added' anything, and "
            "never output tool-call syntax, XML tags, or function calls (they will not run). "
            "If the user wants you to actually DO something — edit the open document, manage email, "
            "search the web, run a task — tell them to turn on Agent mode (the hammer toggle next to "
            "the message box) and you'll carry it out there.\n"
            f"Today is {now.strftime('%A, %Y-%m-%d')} and the local time is {now.strftime('%H:%M')} "
            f"({now.strftime('%z') or 'local'})."
        )
        doc = self.open_doc_context(f)
        if doc:
            system += (
                f"\n\nThe user has this document OPEN: `{doc['path']}`. You may read, quote, and "
                f"discuss it, and you can describe or draft changes in your reply as text — but you "
                f"cannot modify the file itself in chat mode. Current content:\n"
                f"\"\"\"\n{doc['content']}\n\"\"\""
            )
        return system

    def _post_chat(self, f, messages, tools=None):
        """One raw chat-completions call with tool schemas. Returns the model's
        message dict, or an error string."""
        ep = self.store.one("select * from endpoints where id=?", (f.get("endpoint_id") or "",))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        schemas = tools if tools is not None else FUNCTION_TOOL_SCHEMAS
        payload = {"model": f.get("model") or "", "messages": messages,
                   "stream": False, "max_tokens": int(f.get("max_tokens") or 8192)}
        if schemas:   # omit entirely in chat mode — an empty `tools: []` upsets some APIs
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        # Groq/Cerebras sit behind Cloudflare, which 403s requests that have no
        # browser User-Agent (the model-list probe already does this; the chat
        # call did not, so those providers' models silently failed).
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        req.add_header("HTTP-Referer", "https://joebro.app")   # OpenRouter app attribution
        req.add_header("X-Title", "JoeBro")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("choices", [{}])[0].get("message", {}) or {}
        except urllib.error.HTTPError as exc:
            # urllib's str() is just "HTTP Error 403: Forbidden"; the provider
            # puts the real reason (rate limit, bad model, data policy) in the body.
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else (body.get("error") or body.get("detail"))
            except Exception:
                detail = ""
            return f"Endpoint error: HTTP {exc.code} {exc.reason}" + (f" — {detail}" if detail else "")
        except Exception as exc:
            return f"Endpoint error: {exc}"

    def _stream_chat(self, f, messages, tools, on_delta):
        """Like _post_chat but STREAMS. Calls on_delta({"delta": text}) for
        content and on_delta({"delta": text, "thinking": True}) for reasoning as
        tokens arrive, so the final answer (and its thinking) shows live instead
        of appearing all at once after the round finishes. Returns the assembled
        message dict {content, reasoning_content, tool_calls}, or an error string."""
        ep = self.store.one("select * from endpoints where id=?", (f.get("endpoint_id") or "",))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": f.get("model") or "", "messages": messages,
                   "stream": True, "max_tokens": int(f.get("max_tokens") or 8192)}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        req.add_header("HTTP-Referer", "https://joebro.app")
        req.add_header("X-Title", "JoeBro")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        content, reasoning, calls = [], [], {}
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        choice = (json.loads(data).get("choices") or [{}])[0]
                    except Exception:
                        continue
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning.append(rc); on_delta({"delta": rc, "thinking": True})
                    c = delta.get("content")
                    if c:
                        content.append(c); on_delta({"delta": c})
                    for tc in (delta.get("tool_calls") or []):
                        slot = calls.setdefault(tc.get("index", 0),
                                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"): slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"): slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"): slot["function"]["arguments"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else (body.get("error") or body.get("detail"))
            except Exception:
                detail = ""
            return f"Endpoint error: HTTP {exc.code} {exc.reason}" + (f" — {detail}" if detail else "")
        except Exception as exc:
            return f"Endpoint error: {exc}"
        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg

    def remote_reply(self, f):
        """Single-round helper (used by Tasks/Skills generation). Returns
        (content, reasoning, tool_calls) where tool_calls is [(name, body)]."""
        if not (f.get("endpoint_id") or "") or f.get("endpoint_id") == "local":
            return None, "", []
        msg = self._post_chat(f, [{"role": "system", "content": self._agent_system(f)}] + self._history_messages(f))
        if msg is None:
            return None, "", []
        if isinstance(msg, str):
            return msg, "", []
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            name = (fn.get("name") or "").strip().lower()
            if name not in PRODUCTION_AI_TOOLS:
                continue
            try:
                params = json.loads(fn.get("arguments") or "{}")
            except Exception:
                params = {}
            tool_calls.append((name, self._tool_body_from_params(name, params)))
        return msg.get("content") or "", reasoning, tool_calls

    def run_agent(self, f, emit=None):
        """Multi-round tool loop: call the model, run whatever tools it asks for,
        feed the results back, repeat until it stops calling tools (or the round
        cap). Returns (final_text, thinking, events). This is what lets the agent
        chain steps (list emails -> read -> summarize; create file -> edit it).

        If `emit(obj)` is given, it is called LIVE as work happens — reasoning
        deltas, tool_start/tool_output rows, and doc events — so chat_stream can
        forward them to the client the moment each tool runs (instead of only
        after the whole loop finishes). Returned events are still the full list
        for persistence; the caller must NOT re-stream tool rows it already saw."""
        emit = emit or (lambda obj: None)
        sid = f.get("session") or ""
        if not (f.get("endpoint_id") or "") or f.get("endpoint_id") == "local":
            return self.local_reply(f.get("message", "")), "", []
        readonly = (f.get("permission_mode") or "") == "readonly"
        allow_outside = (f.get("permission_mode") or "") == "full"
        session = self.store.one("select workdir from sessions where id=?", (sid,))
        root_text = ((session or {}).get("workdir") or "").strip()
        root = Path(root_text).expanduser().resolve() if root_text else None

        # Chat mode is pure conversation (no tools); agent mode gets the full
        # tool-using prompt. Using the agent prompt in chat mode made weak models
        # claim they'd "edited the doc" or emit tool syntax that never ran.
        agent = (f.get("mode") or "").lower() == "agent"
        system = self._agent_system(f) if agent else self._chat_system(f)
        skills_ctx, skills_used = self._use_relevant_skills(f.get("message", ""))   # inject + bump confidence
        if skills_ctx:
            system += "\n\n" + skills_ctx
        if skills_used:
            emit({"type": "skills_used", "data": skills_used})
        mem_ctx, mem_used = self._use_relevant_memories(f.get("message", ""))   # inject + bump use_count
        if mem_ctx:
            system += "\n\n" + mem_ctx
        if mem_used:
            emit({"type": "memories_used", "data": mem_used})
        # Background (guardrail) plugins shape every turn — inject them and report
        # which were applied, shown in the message footer like memories used.
        bg_plugins = self._active_background_plugins()
        if bg_plugins:
            guard = "\n".join(("- " + (p.get("name") or "") + ": " + (p.get("description") or "")).strip(" :") for p in bg_plugins)
            system += "\n\nActive guardrail plugins — follow these:\n" + guard
            emit({"type": "plugins_used", "data": [p.get("name") for p in bg_plugins]})
        messages = [{"role": "system", "content": system}] + self._history_messages(f)
        # Images the user attached to THIS turn (dropped/picked in chat): fold them
        # into the latest user message as vision blocks so the model can see them.
        att_imgs = self._attachment_image_blocks(f)
        if att_imgs:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    txt = messages[i].get("content")
                    if not isinstance(txt, str):
                        txt = f.get("message", "")
                    messages[i]["content"] = ([{"type": "text", "text": txt}] if txt else []) + att_imgs
                    break
        # Agent→Chat mid-conversation: the history can be full of earlier Agent-mode
        # actions ("I edited the doc", tool narration). In CHAT mode the model tends
        # to follow that in-context precedent over the system prompt and keep claiming
        # it acted. Plant a hard boundary right before the new user turn so the most
        # recent instruction is "you can no longer act, and never pretend you did".
        if not agent and len(messages) > 2:
            messages.insert(len(messages) - 1, {"role": "system", "content": (
                "MODE BOUNDARY — the conversation above may include earlier Agent-mode "
                "actions (editing files, saving documents, sending mail, running tools). "
                "You are now in CHAT mode with NO tools and NO ability to act. Do not "
                "continue those actions and never state or imply you edited, saved, "
                "created, sent, or changed anything now. If the user asks you to actually "
                "do something, tell them to switch on Agent mode (the hammer toggle).")})
        tools = self.tools_for(f, has_folder=root is not None)
        events, thinking_parts, final_text = [], [], ""
        # Optional hard cap on tool calls per turn (Settings). 0 = unlimited.
        max_tool_calls = max(0, int(f.get("max_tool_calls") or 0))
        tool_calls_made = 0
        rounds, call_sigs, stuck = 0, {}, False
        while True:
            rounds += 1
            # No artificial low cap, but never hang forever: if the model repeats
            # the SAME tool call (a read/list/edit loop), hits the user's tool-use
            # limit, or blows past a generous backstop, drop tools so it MUST give
            # a final answer this round.
            hit_limit = max_tool_calls and tool_calls_made >= max_tool_calls
            use_tools = None if (stuck or hit_limit or rounds > 60) else tools
            # Stream every round: content + reasoning deltas reach the client
            # live (the final answer no longer appears all at once after a wait).
            msg = self._stream_chat(f, messages, use_tools, emit)
            # If the call errored and we'd attached images, the model/endpoint
            # likely can't take them — drop the images and try once more so the
            # whole run doesn't die on one image.
            if isinstance(msg, str) and self._strip_image_blocks(messages):
                msg = self._stream_chat(f, messages, use_tools, emit)
            if msg is None:
                final_text = self.local_reply(f.get("message", "")); emit({"delta": final_text}); break
            if isinstance(msg, str):
                final_text = msg; emit({"delta": final_text}); break
            r = msg.get("reasoning_content") or msg.get("reasoning")
            if r:
                thinking_parts.append(r)   # already streamed live by _stream_chat
            raw_calls = msg.get("tool_calls") or []
            if not raw_calls or use_tools is None:
                final_text = msg.get("content") or ""   # already streamed live
                if not final_text and use_tools is None:
                    final_text = "I started repeating myself there, so I stopped. Tell me how you'd like to proceed."
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": raw_calls})
            pending_vis = []   # image vision messages, appended AFTER all tool replies
            for tc in raw_calls:
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip().lower()
                # Per-turn tool-call cap, enforced inside the round too: a model
                # can batch several calls in one round, so the per-round check
                # alone would let them all through. Acknowledge the skipped calls
                # (every tool_call_id needs a reply or the next request 400s) but
                # don't run them; the next round runs tools-off for a final answer.
                if max_tool_calls and tool_calls_made >= max_tool_calls:
                    messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                     "content": "Tool-use limit for this turn reached; not executed."})
                    continue
                try:
                    params = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    params = {}
                body = self._tool_body_from_params(name, params)
                sig = name + "|" + (body or "")[:300]
                call_sigs[sig] = call_sigs.get(sig, 0) + 1
                if call_sigs[sig] >= 3:
                    stuck = True   # same call 3× — wrap up on the next round
                command = self.tool_command_summary(name, body)
                # Claude-Code-style approval for commands in full mode.
                if name in ("bash", "python") and self._needs_permission(f, sid, name):
                    decision = self._await_permission(emit, name, command)
                    if decision == "always":
                        with _PERMISSION_GUARD:
                            _SESSION_ALLOW.setdefault(sid, set()).add(name)
                    elif decision != "allow":
                        emit({"type": "tool_start", "tool": name, "command": command})
                        emit({"type": "tool_output", "tool": name, "command": command,
                              "output": "Command denied by the user.", "exit_code": 1})
                        events.append({"tool": name, "command": command, "output": "Command denied by the user.", "exit_code": 1})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                         "content": "Command denied by the user."})
                        continue
                # Announce the tool the moment it starts so the row appears live.
                emit({"type": "tool_start", "tool": name, "command": command})
                tool_calls_made += 1
                event = self._run_one_tool(root, name, body, f, allow_outside, readonly)
                if event is None:
                    event = {"tool": name, "command": command,
                             "output": f"The {name} tool is not available in this mode.", "exit_code": 1}
                events.append(event)
                emit({"type": "tool_output", "tool": event.get("tool"), "command": event.get("command", ""),
                      "output": event.get("output", ""), "exit_code": event.get("exit_code", 0)})
                doc = event.get("doc")
                if doc and event.get("exit_code") == 0:
                    emit({"_doc": doc, "_tool": event.get("tool")})
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                 "content": (event.get("output") or "")})
                # If the agent read an image, remember it — but DON'T append it
                # here: a user vision message wedged between an assistant's
                # tool_calls and their tool replies breaks the pairing and the
                # endpoint 400s. Collect now, append after the whole batch.
                if name == "read_file" and event.get("exit_code") == 0:
                    vis = self._image_vision_message(f, root, body, allow_outside)
                    if vis:
                        pending_vis.append(vis)
            # All tool replies for this assistant turn are now contiguous; safe to
            # append the image(s) the model should see, then bound the image load.
            for vis in pending_vis:
                messages.append(vis)
            if pending_vis:
                self._prune_context_images(messages)
        if not final_text:
            final_text = "I stopped without a final answer. Ask me to continue."
        return final_text, "\n\n".join(thinking_parts), events

    def _attachment_image_blocks(self, f):
        """Vision blocks for files the user attached to this turn. The app uploads
        dropped/picked files and sends their ids in the `attachments` form field;
        each image becomes an inline data-URL block so the model actually sees it."""
        raw = (f.get("attachments") or "").strip()
        if not raw:
            return []
        try:
            ids = json.loads(raw)
        except Exception:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
        if not isinstance(ids, list):
            return []
        blocks = []
        for uid in ids:
            row = self.store.one("select * from uploads where id=?", (str(uid),))
            if not row:
                continue
            path = row.get("path") or ""
            mime = (row.get("mime") or "").lower()
            ext = os.path.splitext(row.get("name") or path)[1].lower()
            if not (mime.startswith("image/") or ext in IMAGE_EXTS):
                continue
            try:
                b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            except Exception:
                continue
            if not mime:
                mime = mimetypes.guess_type(path)[0] or "image/png"
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return blocks

    def _image_vision_message(self, f, root, body, allow_outside):
        """Build a vision user-message for an image the agent just read, so the
        model actually sees it — full quality. Request size is bounded by
        _prune_context_images, not by compressing the image."""
        rel = (self.parse_tool_fields(body).get("path") or "").strip()
        ext = os.path.splitext(rel)[1].lower()
        if not rel or ext not in IMAGE_EXTS:
            return None
        mime = mimetypes.guess_type(rel)[0] or "image/png"
        b64 = None
        try:
            target = self.resolve_tool_path(root, rel, allow_outside)
            if target.is_file():
                b64 = base64.b64encode(target.read_bytes()).decode("ascii")
        except Exception:
            b64 = None
        if not b64:
            return None
        return {"role": "user", "content": [
            {"type": "text", "text": f"Here is the image {rel} you opened:"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}

    def _prune_context_images(self, messages):
        """Keep only the most recent MAX_CONTEXT_IMAGES image messages with their
        image data; older image messages collapse to a short text note (the model
        has already seen and commented on them). This chunks the visual load."""
        keep = self.MAX_CONTEXT_IMAGES
        idxs = [i for i, m in enumerate(messages)
                if isinstance(m.get("content"), list)
                and any(isinstance(b, dict) and b.get("type") == "image_url" for b in m["content"])]
        for i in idxs[:-keep] if keep > 0 else idxs:
            c = messages[i]["content"]
            text = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            messages[i]["content"] = (text + " [viewed earlier]").strip()

    @staticmethod
    def _strip_image_blocks(messages):
        """Replace image content blocks with a text note (in place) so the run can
        continue on a model/endpoint that can't accept inlined images. True if changed."""
        changed = False
        for m in messages:
            c = m.get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image_url" for b in c):
                text = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                m["content"] = (text + " [an image was here; this model can't view images]").strip()
                changed = True
        return changed

    @staticmethod
    def _needs_permission(f, sid, tool):
        if str(f.get("ask_permission") or "").lower() not in ("1", "true", "yes"):
            return False
        if (f.get("permission_mode") or "") != "full":
            return False
        with _PERMISSION_GUARD:
            return tool not in _SESSION_ALLOW.get(sid, set())

    def _await_permission(self, emit, tool, command):
        """Ask the app to approve a command; block until it answers (or 3 min)."""
        rid = os.urandom(8).hex()
        ev = threading.Event()
        with _PERMISSION_GUARD:
            _PERMISSION_REGISTRY[rid] = {"event": ev, "decision": "deny"}
        emit({"type": "permission_request", "request_id": rid, "tool": tool, "command": command})
        ev.wait(timeout=180)
        with _PERMISSION_GUARD:
            return _PERMISSION_REGISTRY.pop(rid, {}).get("decision", "deny")

    @classmethod
    def _aicheck_chunks(cls, text):
        """Split long text on paragraph boundaries (hard-slice giant paragraphs)
        so each chunk reads coherently for ZeroGPT."""
        text = text.strip()
        if len(text) <= cls._AICHECK_CHUNK:
            return [text]
        size, out, cur = cls._AICHECK_CHUNK, [], ""
        for para in text.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            if len(cur) + len(para) + 2 <= size:
                cur = (cur + "\n\n" + para) if cur else para
                continue
            if cur:
                out.append(cur); cur = ""
            if len(para) <= size:
                cur = para
            else:
                for i in range(0, len(para), size):
                    out.append(para[i:i + size])
        if cur:
            out.append(cur)
        return out

    def _zerogpt_score(self, chunk):
        req = urllib.request.Request(
            "https://api.zerogpt.com/api/detect/detectText",
            data=json.dumps({"input_text": chunk}).encode("utf-8"), method="POST")
        for k, v in {"Content-Type": "application/json", "Origin": "https://www.zerogpt.com",
                     "Referer": "https://www.zerogpt.com/", "User-Agent": self._AICHECK_UA}.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not body.get("success"):
            raise RuntimeError(body.get("message") or "ZeroGPT reported failure")
        data = body.get("data") or {}
        return {"ai_words": int(data.get("aiWords") or 0),
                "total_words": int(data.get("textWords") or 0),
                "flagged_sentences": list(data.get("h") or [])}

    def ai_check(self, body):
        """AI-text detection via ZeroGPT's public endpoint. Long inputs are
        chunked, each chunk scored, and the result is a word-weighted average
        with every flagged sentence surfaced. Ported from the Pi."""
        text = (body.get("text") or "").strip()
        if not text:
            return self.json({"detail": "text is required"}, 400)
        if len(text.split()) < 20:
            return self.json({"detail": "Input too short — paste at least ~20 words for a useful estimate."}, 400)
        results = []
        chunks = self._aicheck_chunks(text)
        for i, c in enumerate(chunks):
            try:
                results.append(self._zerogpt_score(c))
            except Exception as exc:
                return self.json({"detail": f"AI-check service unreachable: {exc}"}, 502)
            if len(chunks) > 1 and i < len(chunks) - 1:
                time.sleep(0.4)   # polite spacing so the per-IP throttle doesn't reject the batch
        total_words = sum(r["total_words"] for r in results) or 1
        ai_words = sum(r["ai_words"] for r in results)
        ai_percent = ai_words / total_words * 100.0
        flagged = []
        for r in results:
            flagged.extend(r["flagged_sentences"])
        return self.json({"ai_percent": round(ai_percent, 1), "human_percent": round(100.0 - ai_percent, 1),
                          "ai_words": ai_words, "total_words": total_words, "chunks": len(chunks),
                          "flagged_sentences": flagged, "provider": "zerogpt"})

    def _history_messages(self, f):
        """The session's conversation as OpenAI messages, so the model has
        memory of prior turns. The current user turn is already the last stored
        row. Older turns are compacted into a summary when the history grows."""
        sid = f.get("session") or ""
        rows = self.store.rows(
            "select role, content from messages where session_id=? order by id", (sid,)) if sid else []
        msgs = []
        prefix = []   # a manually-compacted summary, carried verbatim into context
        for r in rows:
            role = r.get("role")
            content = (r.get("content") or "").strip()
            if role == "system" and content.startswith("[Conversation summary"):
                prefix = [{"role": "system", "content": content}]
                continue
            if role not in ("user", "assistant") or not content:
                continue
            msgs.append({"role": role, "content": content})
        if not msgs:
            return prefix + [{"role": "user", "content": f.get("message", "")}]

        total = sum(len(m["content"]) for m in msgs)
        if len(msgs) <= self.COMPACT_KEEP_RECENT or total <= self.COMPACT_CHAR_BUDGET:
            return prefix + msgs[-30:]

        older = msgs[:-self.COMPACT_KEEP_RECENT]
        recent = msgs[-self.COMPACT_KEEP_RECENT:]
        summary = self._compact_history(f, sid, older)
        if summary:
            return prefix + [{"role": "system",
                     "content": "[Conversation summary — earlier turns were compacted]\n" + summary}] + recent
        return prefix + msgs[-30:]   # summary unavailable — fall back to naive trim

    def _compact_history(self, f, sid, older):
        """Summarize `older` turns into one dense note via the session's own
        endpoint. Cached per session in prefs and only refreshed once the older
        window has grown by a few turns, so we don't pay to re-summarize every
        turn. Incremental: a prior summary is folded into the next one."""
        cache = self.pref("compact_" + sid) or {}
        cached_n = int(cache.get("n") or 0)
        cached_summary = cache.get("summary") or ""
        # Reuse the cached summary until at least 6 new turns have aged out.
        if cached_summary and len(older) - cached_n < 6:
            return cached_summary

        convo = "\n".join(f"{m['role'].upper()}: {m['content'][:2000]}" for m in older)
        user = convo
        if cached_summary:
            user = ("Existing summary of earlier turns:\n" + cached_summary +
                    "\n\n---\nNewer turns to fold in:\n" + convo)
        summary = self._complete(
            f.get("endpoint_id") or "", f.get("model") or "",
            self.COMPACT_SYSTEM_PROMPT, user, max_tokens=1024)
        if not summary:
            return cached_summary or ""
        self.set_pref("compact_" + sid, {"n": len(older), "summary": summary})
        return summary

    def manual_compact(self, sid):
        """Force-compact a session on demand (the 'Compact Chat' button):
        summarize all but the recent tail into one stored system note and
        replace those older rows. Destructive, unlike the on-the-fly summary
        used during chat. The note is carried back into context by
        _history_messages and shown as a pill by the app."""
        sess = self.store.one("select * from sessions where id=?", (sid,))
        if not sess:
            return {"ok": False, "error": "no such session"}
        rows = self.store.rows(
            "select id, role, content from messages where session_id=? order by id", (sid,))
        convo = [r for r in rows
                 if r.get("role") in ("user", "assistant") and (r.get("content") or "").strip()]
        if len(convo) <= self.COMPACT_KEEP_RECENT:
            return {"ok": True, "compacted": False}   # not enough history to bother
        older = convo[:-self.COMPACT_KEEP_RECENT]
        text = "\n".join(f"{r['role'].upper()}: {(r['content'] or '')[:2000]}" for r in older)
        # fold any prior manual summary into the new one so nothing is lost
        prior = next((r for r in rows if r.get("role") == "system"
                      and (r.get("content") or "").startswith("[Conversation summary")), None)
        if prior:
            text = "Existing summary:\n" + prior["content"] + "\n\n---\nNewer turns:\n" + text
        summary = self._complete(
            sess.get("endpoint_id") or "", sess.get("model") or "",
            self.COMPACT_SYSTEM_PROMPT, text, max_tokens=1024)
        if not summary:
            return {"ok": False, "error": "couldn't summarize — model unavailable"}
        drop_ids = [r["id"] for r in older] + ([prior["id"]] if prior else [])
        keep_id = min(drop_ids)   # reuse the lowest id so the note sorts first
        q = ",".join("?" * len(drop_ids))
        self.store.exec(f"delete from messages where id in ({q})", tuple(drop_ids))
        self.store.exec(
            "insert into messages(id, session_id, role, content, metadata, created_at) values(?,?,?,?,?,?)",
            (keep_id, sid, "system",
             "[Conversation summary — earlier turns were compacted]\n" + summary,
             json.dumps({"compacted": True}), now_iso()))
        self.store.exec("delete from prefs where key=?", ("compact_" + sid,))   # stale cache
        return {"ok": True, "compacted": True, "removed": len(older)}

    def open_doc_context(self, f):
        """The path + current text of the document open in the app, so the model
        edits it instead of creating a new file. Truncated to bound spend."""
        doc_id = (f.get("active_doc_id") or "").strip()
        if not doc_id or doc_id == "_streaming_":
            return None
        session = self.store.one("select workdir from sessions where id=?", (f.get("session") or "",))
        root_text = ((session or {}).get("workdir") or "").strip()
        # The app opens bound-folder files natively with client-local ids
        # ("local-<relpath>") that aren't in the documents table — resolve them
        # straight from disk so the AI knows the open file.
        if doc_id.startswith("local-") and root_text:
            sub = doc_id[len("local-"):]
            try:
                target = self.resolve_in_workdir(Path(root_text).expanduser().resolve(), sub)
                if target.is_file():
                    return {"path": sub, "content": read_doc_text(target)}
            except Exception:
                pass
            return None
        row = self.store.one("select file_path, current_content from documents where id=?", (doc_id,))
        if not row:
            return None
        file_path = row.get("file_path") or ""
        rel = file_path
        if root_text and file_path:
            try:
                rel = str(Path(file_path).resolve().relative_to(Path(root_text).expanduser().resolve()))
            except Exception:
                rel = Path(file_path).name
        return {"path": rel or Path(file_path).name, "content": (row.get("current_content") or "")}

    def chat_reply_with_tools(self, f):
        sid = f.get("session") or ""
        message = f.get("message", "")
        self.add_message(sid, "user", message, {})
        reply, _reasoning, structured = self.remote_reply(f)
        reply = reply or ("" if structured else self.local_reply(message))
        struct_results = self.execute_structured_tools(sid, structured, f)
        reply, tool_results = self.execute_ai_file_tools(sid, reply, f)
        tool_results = struct_results + tool_results
        if tool_results:
            reply = reply + "\n\n" + "\n".join(e.get("output", "") for e in tool_results)
        self.add_message(sid, "assistant", reply, {"model": f.get("model") or "Local fallback"})
        return reply

    def add_message(self, sid, role, content, metadata):
        if not sid:
            return
        self.store.exec(
            "insert into messages(session_id,role,content,metadata,created_at) values(?,?,?,?,?)",
            (sid, role, content or "", json.dumps(metadata or {}), now_iso()),
        )

    @staticmethod
    def _context_window(model):
        """Best-guess context-window size (tokens) for the gauge. Approximate —
        it only drives the 'context used' circle, not any real limit."""
        m = (model or "").lower()
        if "claude" in m:
            return 200_000
        if "gemini" in m:
            return 1_000_000
        if any(k in m for k in ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4", "gpt-oss", "120b")):
            return 128_000
        if "deepseek" in m:
            return 128_000
        if any(k in m for k in ("llama", "qwen", "mistral", "gemma", "phi")):
            return 32_000
        return 128_000

    def _response_stats(self, sid, reply, thinking, model, elapsed):
        """tok/s + context-window-used %, estimated from text (~4 chars/token).
        Endpoints seldom return real usage on a stream, and this only feeds the
        readout, so an estimate is fine and works for every endpoint."""
        out_chars = len(reply or "") + len(thinking or "")
        out_tokens = out_chars / 4.0
        tps = round(out_tokens / elapsed, 1) if elapsed and elapsed > 0 else 0.0
        # Prompt ≈ every message stored for this session so far (the assistant
        # reply isn't persisted yet) + ~1.5k for the system prompt / tool schemas.
        try:
            prompt_chars = sum(len(r.get("content") or "")
                               for r in self.store.rows(
                                   "select content from messages where session_id=?", (sid,)))
        except Exception:
            prompt_chars = 0
        used = prompt_chars / 4.0 + 1500 + out_tokens
        window = self._context_window(model)
        ctx = round(min(100.0, max(0.0, 100.0 * used / window)), 1) if window else 0.0
        return {"tokens_per_second": tps, "context_percent": ctx}

    def _complete(self, endpoint_id, model, system, user, max_tokens=1500):
        """One-shot text completion against an endpoint (no tools). Used for
        background work like research synthesis."""
        ep = self.store.one("select * from endpoints where id=?", (endpoint_id,))
        if not ep:
            return None
        base = ep["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": model or "", "stream": False, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
        if ep.get("api_key"):
            req.add_header("Authorization", "Bearer " + ep["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return (data.get("choices", [{}])[0].get("message", {}) or {}).get("content")
        except Exception:
            return None
