"""Telegram control bot — the orchestrator over all your chats.

A single daemon thread long-polls getUpdates. Each incoming message runs one
agent turn (via Handler.bot_run) in a dedicated per-Telegram-chat session, with
the full agent toolset including manage_chats. Config lives in prefs, written by
the app's Message Bot settings page, so toggling it on/off needs no restart.

Stdlib only (urllib). No webhook / public URL needed — long-polling pulls.
"""
from jb_core import *  # noqa: F401,F403  (json, urllib, threading, time, now_iso, _PERMISSION_*)
import types as _types
import html as _htmllib

API = "https://api.telegram.org/bot{token}/{method}"
HELP = ("📲 **JoeBro Message Bot**\n"
        "I'm your orchestrator — I manage and drive all your chats and agents: I search and read "
        "them, create chats and bind them to folders, set their mode and permissions, and delegate "
        "work to them. I also use your email, calendar, memory, web search and deep research.\n\n"
        "Just talk to me. Commands:\n"
        "**/chats** — list all your chats\n"
        "**/compact** — summarize & shrink this conversation\n"
        "**/help** — this message")

_TOOL_LABELS = {
    "manage_chats": "checking your chats", "manage_memory": "recalling memory",
    "web_search": "searching the web", "trigger_research": "starting deep research",
    "manage_research": "checking research", "list_emails": "reading email",
    "read_email": "reading email", "send_email": "sending email",
    "create_event": "adding a calendar event", "manage_tasks": "managing tasks",
    "manage_skills": "managing skills",
}


def _friendly_tool(name):
    return _TOOL_LABELS.get((name or "").lower(), (name or "working").replace("_", " "))


def _md_to_html(text):
    """Render the model's Markdown as the small HTML subset Telegram supports
    (b/i/s/code/pre/a). Unsupported syntax degrades to plain text; the caller
    falls back to a plain send/edit if Telegram rejects the HTML."""
    text = text or ""
    stash = []
    def _hold(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"
    # Protect code first — its contents must survive the other rules verbatim.
    text = re.sub(r"```[^\n]*\n?(.*?)```", lambda m: _hold(f"<pre>{_htmllib.escape(m.group(1))}</pre>"), text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", lambda m: _hold(f"<code>{_htmllib.escape(m.group(1))}</code>"), text)
    text = _htmllib.escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*#*$", r"<b>\1</b>", text)   # headers -> bold line
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)                       # bullets
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    for i, s in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", s)
    return text


# --- Telegram API (best-effort; None on network failure) ---

def _tg(token, method, payload, timeout=35):
    url = API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _get_updates(token, offset):
    out = _tg(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=35)
    if not out or not out.get("ok"):
        return None
    return out.get("result") or []


def _send(token, chat_id, text, html=False):
    # Telegram caps messages at 4096 chars; chunk long replies. With html=True
    # each chunk is rendered, with a plain re-send if Telegram rejects the HTML.
    text = text or "(no response)"
    for i in range(0, len(text), 3800):
        chunk = text[i:i + 3800]
        if html:
            ok = _tg(token, "sendMessage",
                     {"chat_id": chat_id, "text": _md_to_html(chunk), "parse_mode": "HTML"}, timeout=20)
            if not (ok and ok.get("ok")):
                _tg(token, "sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=20)
        else:
            _tg(token, "sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=20)


def _send_id(token, chat_id, text):
    """Send a message and return its message_id (for the streaming placeholder)."""
    out = _tg(token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout=20)
    return (out or {}).get("result", {}).get("message_id") if (out and out.get("ok")) else None


def _edit(token, chat_id, mid, text, html=False):
    payload = {"chat_id": chat_id, "message_id": mid, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    out = _tg(token, "editMessageText", payload, timeout=20)
    return bool(out and out.get("ok"))


def _typing(token, chat_id):
    _tg(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)


# --- config + sessions ---

def _new_handler(server):
    from joebro_backend import Handler  # lazy: avoid import cycle at module load
    h = Handler.__new__(Handler)
    h.server = _types.SimpleNamespace(store=server.store)
    return h


def _parse_ids(raw):
    out = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(part.lstrip("@"))
    return out


def _cfg(h):
    def pb(key, default=False):
        v = h.pref(key)
        return default if v is None else bool(v)
    return {
        "enabled": pb("bot_enabled"),
        "token": (h.pref("bot_token") or "").strip(),
        "allowed": _parse_ids(h.pref("bot_allowed_ids") or ""),
        "model": h.pref("bot_model") or h.pref("default_model") or "",
        "endpoint_id": h.pref("bot_endpoint_id") or h.pref("default_endpoint_id") or "",
        "ask_permissions": pb("bot_ask_permissions"),
        "show_skills": pb("bot_show_skills"),
        "show_memories": pb("bot_show_memories"),
        "show_tools": pb("bot_show_tools", True),
        "show_plugins": pb("bot_show_plugins"),
    }


def _session(h, chat_id, cfg):
    sid = "bot_tg_" + str(chat_id)
    row = h.store.one("select * from sessions where id=?", (sid,))
    if not row:
        h.store.exec(
            "insert into sessions(id,name,model,endpoint_url,endpoint_id,mode,created_at) values(?,?,?,?,?,?,?)",
            (sid, "📲 Message Bot", cfg["model"], "", cfg["endpoint_id"], "agent", now_iso()))
    else:
        # Keep the session's model/endpoint aligned with current config.
        h.store.exec("update sessions set model=?, endpoint_id=? where id=?",
                     (cfg["model"], cfg["endpoint_id"], sid))
    return sid


def _footers(token, chat_id, cfg, meta):
    lines = []
    if cfg["show_skills"] and meta.get("skills_used"):
        lines.append("🎓 Skills: " + ", ".join(meta["skills_used"]))
    if cfg["show_memories"] and meta.get("memories_used"):
        lines.append("🧠 Memories: " + ", ".join(meta["memories_used"]))
    if cfg["show_plugins"] and meta.get("plugins_used"):
        lines.append("🧩 Plugins: " + ", ".join(meta["plugins_used"]))
    if cfg["show_tools"] and meta.get("tool_events"):
        names = [e.get("tool") for e in meta["tool_events"] if e.get("tool")]
        if names:
            lines.append("🔧 Tools: " + ", ".join(names))
    if lines:
        _send(token, chat_id, "—\n" + "\n".join(lines))


def _resolve_permission(rid, decision):
    with _PERMISSION_GUARD:
        entry = _PERMISSION_REGISTRY.get(rid)
        if entry:
            entry["decision"] = decision
            entry["event"].set()


# --- message handling ---

def _handle(server, token, chat_id, text, pending_perm, busy):
    lock = busy.setdefault(chat_id, threading.Lock())
    if not lock.acquire(blocking=False):
        _send(token, chat_id, "⏳ Still working on your last message…")
        return
    try:
        h = _new_handler(server)
        cfg = _cfg(h)
        sid = _session(h, chat_id, cfg)
        low = text.lower()
        if low in ("/start", "/help"):
            _send(token, chat_id, HELP, html=True)
            return
        if low.startswith("/compact"):
            h.manual_compact(sid)
            _send(token, chat_id, "🧹 Compacted this conversation.")
            return
        if low.startswith("/chats"):
            rows = h.store.rows(
                "select name,mode from sessions where id not like 'bot\\_tg\\_%' escape '\\' "
                "order by datetime(created_at) desc limit 50")
            if not rows:
                _send(token, chat_id, "No chats yet.")
            else:
                lines = ["**Your chats:**"] + [f"• {r.get('name') or 'Untitled'} ({r.get('mode') or 'chat'})" for r in rows]
                _send(token, chat_id, "\n".join(lines), html=True)
            return
        if not cfg["model"]:
            _send(token, chat_id,
                  "⚠️ No model selected. Open JoeBro → Settings → Message Bot and choose a model, then try again.")
            return
        _typing(token, chat_id)

        # Live streaming: send a placeholder, then EDIT it as the reply streams in
        # (Telegram has no token stream — repeated editMessageText is the pattern).
        # Edits are throttled to ~1.2s to stay under Telegram's rate limit, and we
        # send plain text while streaming (partial Markdown has unbalanced entities);
        # the final message is re-edited with rendered HTML.
        st = {"buf": "", "status": "", "mid": _send_id(token, chat_id, "…"),
              "last": 0.0, "shown": ""}

        def _stream():
            if not st["mid"]:
                return
            body = (st["buf"].strip() or st["status"] or "…")[:4000]
            if body != st["shown"] and time.time() - st["last"] >= 1.2:
                if _edit(token, chat_id, st["mid"], body):
                    st["shown"] = body
                    st["last"] = time.time()

        def emit(obj):
            if obj.get("delta") is not None and not obj.get("thinking"):
                st["buf"] += obj["delta"]
                _stream()
            elif obj.get("type") == "tool_start" and not st["buf"].strip():
                st["status"] = "🔧 " + _friendly_tool(obj.get("tool")) + "…"
                _stream()
            elif obj.get("type") == "permission_request" and cfg["ask_permissions"]:
                pending_perm[chat_id] = obj.get("request_id")
                cmd = (obj.get("command") or "")[:300]
                _send(token, chat_id, f"🔐 Allow {obj.get('tool')}?\n{cmd}\n\nReply y or n.")

        # The bot is the ORCHESTRATOR: it manages/delegates, never codes or touches
        # files itself. Approval prompts come from the chats it delegates into
        # (manage_chats send), routed back here via this emit.
        f = {"session": sid, "message": text, "model": cfg["model"],
             "endpoint_id": cfg["endpoint_id"], "mode": "agent", "orchestrator": "true"}
        reply, meta = h.bot_run(f, emit=emit)
        pending_perm.pop(chat_id, None)

        # Finalize: render the authoritative reply as HTML into the placeholder,
        # falling back to plain text if Telegram rejects the entities. Overflow
        # past one message goes as follow-up sends.
        final_text = (reply or st["buf"]).strip() or "(no response)"
        first = final_text[:4000]
        if st["mid"]:
            if not _edit(token, chat_id, st["mid"], _md_to_html(first), html=True):
                _edit(token, chat_id, st["mid"], first)
        else:
            _send(token, chat_id, first, html=True)
        if len(final_text) > 4000:
            _send(token, chat_id, final_text[4000:], html=True)
        _footers(token, chat_id, cfg, meta)
    except Exception as e:
        _send(token, chat_id, f"⚠️ Error: {e}")
    finally:
        lock.release()


def telegram_bot_loop(server):
    """Daemon entry: long-poll while the bot is enabled, dispatch each message."""
    offset = 0
    pending_perm = {}   # chat_id -> permission request id awaiting y/n
    busy = {}           # chat_id -> Lock (one in-flight run per chat)
    while True:
        try:
            h = _new_handler(server)
            cfg = _cfg(h)
            if not cfg["enabled"] or not cfg["token"]:
                time.sleep(5)
                continue
            token = cfg["token"]
            updates = _get_updates(token, offset)
            if updates is None:
                time.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id is None or not text:
                    continue
                # Authorization: allowlist of numeric Telegram chat IDs.
                if cfg["allowed"] and str(chat_id) not in cfg["allowed"]:
                    _send(token, chat_id,
                          f"Not authorized. Your Telegram ID is {chat_id} — add it in "
                          "JoeBro → Settings → Message Bot to enable.")
                    continue
                # A pending y/n answers an open permission prompt (handled inline so
                # the poll loop never blocks on the worker that's awaiting it).
                rid = pending_perm.get(chat_id)
                if rid and text.lower() in ("y", "yes", "allow", "n", "no", "deny"):
                    decision = "allow" if text.lower() in ("y", "yes", "allow") else "deny"
                    _resolve_permission(rid, decision)
                    pending_perm.pop(chat_id, None)
                    _send(token, chat_id, "✅ Allowed." if decision == "allow" else "🚫 Denied.")
                    continue
                threading.Thread(target=_handle,
                                 args=(server, token, chat_id, text, pending_perm, busy),
                                 daemon=True).start()
        except Exception:
            time.sleep(3)
