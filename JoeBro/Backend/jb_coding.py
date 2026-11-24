"""Coding agents (Claude Code, Codex, opencode, AGY) on the user's computers,
pushed in by the harness hooks from joebro-plugins (`joebro-agents report`),
shown next to JoeBro's own agents and readable by the personal agent.

Node shape follows Wudland's collector contract (id/parent/source/host/name/
status/busy/model/cwd/started/last_activity/tool_calls/detail) so either side
can consume the other. State is event-driven with staleness guards, because a
harness that crashes never sends its Stop."""
from jb_core import PHONE_SESSION_ID
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

UTC = timezone.utc
SOURCES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "agy": "Antigravity"}
DEFAULT_MODELS = {
    "claude": [("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("default", "Default")],
    "agy": [("gemini-4-neon-high", "Gemini 4 Neon (High)"), ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"),
            ("gemini-4-carbon-high", "Gemini 4 Carbon (High)"), ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
            ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)"),
            ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"), ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)")],
    "codex": [("gpt-6.2-sol", "GPT-6.2 Sol"), ("gpt-6.2-astra", "GPT-6.2 Astra"),
              ("gpt-6.1-terra", "GPT-6.1 Terra"), ("gpt-5-codex", "GPT-5 Codex"), ("gpt-5", "GPT-5")],
    "opencode": [("qwen3-coder:30b", "Qwen3 Coder 30B"), ("llama3.3:70b", "Llama 3.3 70B"), ("deepseek-v3.2", "DeepSeek V3.2")],
}
HOST_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")   # the computer name each link reports as
EVENTS = {"start", "prompt", "tool", "tool_done", "waiting", "stop", "end", "subagent_start", "subagent_stop"}
STALE_RUNNING = 25 * 60     # running with no event this long -> assume it died
STALE_WAITING = 3 * 3600
SUB_DONE_SHOW = 30 * 60   # finished subagents stay listed this long
SHOW_IDLE = 6 * 3600        # idle sessions stay listed this long
DROP_ENDED = 15 * 60

CODING_AGENTS_TOOL = {"type": "function", "function": {
    "name": "coding_agents",
    "description": ("See the user's coding agents (Claude Code, Codex, OpenCode, Antigravity) running on their linked computers: "
                    "what each is working on, whether it is busy, waiting for approval or done, and its recent "
                    "activity. `list` = all current ones; `detail` = one agent's recent lines. To give one a "
                    "follow-up or answer it, use the joebro MCP tools (coding_agent_send / coding_agent_control) "
                    "if they are connected."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "detail"]},
        "id": {"type": "string", "description": "detail: the agent id from list"}},
        "required": ["action"]}}}


def _iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


def _clip(v, n):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v if len(v) <= n else v[: n - 1] + "…"



def _where(host):
    """' on the Mac' / ' on the PC' / ' on studio' for a host slug."""
    host = str(host or "")
    if host in ("pc", "mac"):
        return " on the " + ("PC" if host == "pc" else "Mac")
    return (" on " + host) if host else ""

class CodingAgentsMixin:
    def _coding_table(self):
        self.store.exec("""CREATE TABLE IF NOT EXISTS coding_agents(id TEXT PRIMARY KEY, host TEXT NOT NULL,
            source TEXT NOT NULL, session_id TEXT NOT NULL, parent TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, tool_calls INTEGER NOT NULL DEFAULT 0,
            detail_json TEXT NOT NULL DEFAULT '[]', started REAL NOT NULL, last_activity REAL NOT NULL,
            pid INTEGER, terminal_json TEXT NOT NULL DEFAULT '{}')""")
        for col in ("kind TEXT NOT NULL DEFAULT 'coding'", "agent_type TEXT NOT NULL DEFAULT ''", "ends_with_parent INTEGER NOT NULL DEFAULT 0"):
            try:
                self.store.exec("ALTER TABLE coding_agents ADD COLUMN " + col)
            except Exception:
                pass

    def coding_event(self, ev):
        """One normalised hook event from joebro-agent-report."""
        self._coding_table()
        if not isinstance(ev, dict):
            raise ValueError("event must be an object")
        source = str(ev.get("source") or "").lower()
        host = str(ev.get("host") or "").lower()
        sid = re.sub(r"[^A-Za-z0-9_.:-]", "", str(ev.get("session_id") or ""))[:80]
        kind = str(ev.get("event") or "").lower()
        if source not in SOURCES or not HOST_RX.match(host) or not sid or kind not in EVENTS:
            raise ValueError("bad event (source/host/session_id/event)")
        aid = "code:%s:%s:%s" % (host, source, sid)
        psid = re.sub(r"[^A-Za-z0-9_.:-]", "", str(ev.get("parent_session_id") or ""))[:80]
        parent_id = "code:%s:%s:%s" % (host, source, psid) if psid and psid != sid else str(ev.get("parent") or "")[:120]
        is_sub = bool(psid and psid != sid) or kind in ("subagent_start", "subagent_stop")
        if kind in ("subagent_start", "subagent_stop") and not psid:
            raise ValueError("subagent events need parent_session_id")
        now = time.time()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if row and row.get("kind") == "subagent":
            is_sub = True
        detail = json.loads(row["detail_json"]) if row else []
        status = row["status"] if row else "idle"
        tool_calls = row["tool_calls"] if row else 0
        # Replies are the conversation: keep them whole enough to read (prompts/replies 4k, tool lines short).
        text = _clip(ev.get("text"), 4000 if kind in ("stop", "prompt", "subagent_stop", "end") else 300)
        tool = _clip(ev.get("tool"), 60)
        said = _clip(ev.get("said"), 4000)          # text the agent wrote before this tool call
        if said and ("✓ " + said) not in detail[-3:]:
            # Text picked up just after its tool started belongs before that tool line.
            if kind == "tool_done" and detail and not detail[-1].startswith(("✓ ", "› ", "⏸ ")):
                detail.insert(len(detail) - 1, "✓ " + said)
            else:
                detail.append("✓ " + said)
        if is_sub:
            if kind in ("subagent_stop", "stop", "end"):
                status = "done"
                if text:
                    detail.append("✓ " + text)
            else:
                status = "running" if kind != "waiting" else "waiting"
                if kind == "tool":
                    tool_calls += 1
                    detail.append(tool + (": " + text if text else ""))
                elif text:
                    detail.append(("⏸ " if kind == "waiting" else "› ") + text)
        elif kind == "start":
            status = "idle" if not row or status in ("ended", "stale") else status
        elif kind == "prompt":
            status = "running"
            if text:
                detail.append("› " + text)
        elif kind == "tool":
            status = "running"
            tool_calls += 1
            detail.append(tool + (": " + text if text else ""))
        elif kind == "tool_done":
            status = "running"
        elif kind == "waiting":
            status = "waiting"
            detail.append("⏸ " + (text or "needs your approval"))
        elif kind == "stop":
            status = "idle"
            if text and ("✓ " + text) not in detail[-3:] and text.strip() not in (said or ""):
                detail.append("✓ " + text)
            notice = _clip(ev.get("notice"), 300)
            if notice:                      # usage/rate limit or API error: the agent stopped, not finished
                status = "limited"
                if ("⚠ " + notice) not in detail[-3:]:
                    detail.append("⚠ " + notice)
        elif kind == "end":
            status = "ended"
        elif kind in ("subagent_start", "subagent_stop"):
            pass
        # Keep the conversation: replies and prompts survive; tool lines are trimmed first.
        talk = [i for i, d in enumerate(detail) if d[:1] in ("›", "✓", "⏸", "⚠")]
        if len(detail) > 60:
            keep = set(talk[-30:]) | set(range(len(detail) - 20, len(detail)))
            detail = [d for i, d in enumerate(detail) if i in keep][-60:]
        cwd = str(ev.get("cwd") or (row or {}).get("cwd") or "")[:300]
        name = _clip(ev.get("name"), 80) or (row or {}).get("name") or (os.path.basename(cwd.rstrip("/")) or SOURCES[source])
        model = _clip(ev.get("model"), 60) or (row or {}).get("model") or ""
        term = {k: str(ev[k])[:200] for k in ("kitty_window_id", "kitty_listen_on", "tmux_pane", "term") if ev.get(k)}
        if row and not term:
            term = json.loads(row.get("terminal_json") or "{}")
        sub_kind = "subagent" if is_sub else "coding"
        agent_type = _clip(ev.get("subagent_type"), 40) or (row or {}).get("agent_type") or ""
        if is_sub and not _clip(ev.get("name"), 80) and not (row or {}).get("name"):
            name = agent_type or "subagent"
        if is_sub and not row:
            prow = self.store.one("SELECT cwd,model FROM coding_agents WHERE id=?", (parent_id,)) or {}
            cwd, model = cwd or prow.get("cwd", ""), model or prow.get("model", "")
        pid = ev.get("pid") if isinstance(ev.get("pid"), int) else (row or {}).get("pid")
        self.store.exec("""INSERT INTO coding_agents(id,host,source,session_id,parent,name,cwd,model,status,tool_calls,detail_json,
                started,last_activity,pid,terminal_json,kind,agent_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,cwd=excluded.cwd,model=excluded.model,status=excluded.status,
                tool_calls=excluded.tool_calls,detail_json=excluded.detail_json,last_activity=excluded.last_activity,
                pid=excluded.pid,terminal_json=excluded.terminal_json,parent=excluded.parent,kind=excluded.kind,
                agent_type=excluded.agent_type""",
                        (aid, host, source, sid, parent_id if is_sub else (str(ev.get("parent") or "")[:120] or (row or {}).get("parent") or ""), name, cwd, model, status, tool_calls,
                         json.dumps(detail, ensure_ascii=False), now, now, pid, json.dumps(term), sub_kind, agent_type))
        if is_sub and ev.get("ends_with_parent"):
            self.store.exec("UPDATE coding_agents SET ends_with_parent=1 WHERE id=?", (aid,))
        # Subagents from harnesses that send no stop signal (flagged ends_with_parent) close with the parent's
        # turn. Others (Claude Code sends SubagentStop) keep running after the parent's turn ends: background
        # subagents outlive it. A session that ends closes all of them.
        if not is_sub and kind == "stop":
            self.store.exec("UPDATE coding_agents SET status='done',last_activity=? WHERE parent=? AND kind='subagent' "
                            "AND ends_with_parent=1 AND status IN ('running','waiting')", (now, aid))
        elif not is_sub and kind == "end":
            self.store.exec("UPDATE coding_agents SET status='done',last_activity=? WHERE parent=? AND kind='subagent' "
                            "AND status IN ('running','waiting')", (now, aid))
        try:
            from jb_push import WAKE
            WAKE.set()
        except Exception:
            pass
        if not row and pid and not is_sub:   # same process adopted earlier under a pid-based id -> one row only
            self.store.exec("UPDATE coding_agents SET status='ended' WHERE host=? AND pid=? AND id<>? AND status<>'ended'",
                            (host, pid, aid))
        self.store.exec("DELETE FROM coding_agents WHERE last_activity<?", (now - 3 * 86400,))
        if kind in ("stop", "end") and not is_sub:       # one compact session record in Shared Memory (no model call)
            try:
                import jb_sharedmem
                last = text if kind == "stop" else ""
                last = last or next((d[2:] for d in reversed(detail) if d.startswith("✓ ")), "") or \
                    next((d[2:] for d in reversed(detail) if d.startswith("› ")), "")
                jb_sharedmem.record_session(self.store, {"id": aid, "cwd": cwd, "source": source, "model": model, "host": host,
                                                         "session_id": sid}, last, str(ev.get("branch") or ""))
            except Exception:
                pass
        return {"ok": True, "id": aid, "status": status, "kind": sub_kind, "parent": parent_id if is_sub else ""}

    def coding_nodes(self, include_idle=True, tree=False):
        """Coding agents plus their subagents. Subagent rows follow their parent; `tree` nests them
        under it as `child_nodes`. Finished subagents stay listed for SUB_DONE_SHOW."""
        self._coding_table()
        now = time.time()
        flat, hidden = [], set()
        rows = self.store.rows("SELECT * FROM coding_agents ORDER BY last_activity DESC LIMIT 120")
        # A parent with a subagent still working stays listed, however long the parent itself has been idle.
        working_parents = {r["parent"] for r in rows if r.get("kind") == "subagent" and r["parent"] and (
            (r["status"] == "running" and now - r["last_activity"] <= STALE_RUNNING) or
            (r["status"] == "waiting" and now - r["last_activity"] <= STALE_WAITING))}
        for r in rows:
            age = now - r["last_activity"]
            status = r["status"]
            is_sub = r.get("kind") == "subagent"
            if status == "running" and age > STALE_RUNNING:
                status = "stale"
            if status == "waiting" and age > STALE_WAITING:
                status = "idle"
            if (status == "ended" and r["id"] not in working_parents) or (is_sub and status == "done" and age > SUB_DONE_SHOW) or \
                    (is_sub and status in ("stale", "idle", "limited") and age > SUB_DONE_SHOW):
                hidden.add(r["id"])
                continue
            if not is_sub and status in ("idle", "stale", "limited") and r["id"] not in working_parents and (age > SHOW_IDLE or not include_idle):
                hidden.add(r["id"])
                continue
            detail = json.loads(r["detail_json"] or "[]")
            flat.append({"id": r["id"], "kind": "subagent" if is_sub else "coding", "parent": r["parent"] or None,
                         "children": [], "agent_type": r.get("agent_type") or "", "subagents": {"total": 0, "live": 0},
                         "source": r["source"], "host": r["host"],
                         "name": r["name"], "status": status, "busy": status in ("running", "waiting"),
                         "waiting": status == "waiting", "failed": False, "model": r["model"], "cwd": r["cwd"],
                         "started": _iso(r["started"]), "last_activity": _iso(r["last_activity"]),
                         "tool_calls": r["tool_calls"], "detail": detail,
                         "terminal": json.loads(r.get("terminal_json") or "{}")})
        by_id = {n["id"]: n for n in flat}
        subs = [n for n in flat if n["kind"] == "subagent"]
        for n in subs:
            par = by_id.get(n["parent"])
            if par is not None and par["kind"] == "coding":
                par["children"].append(n["id"])
                par["subagents"]["total"] += 1
                par["subagents"]["live"] += 1 if n["busy"] else 0
        tops = [n for n in flat if n["kind"] == "coding" or n["parent"] not in by_id and n["parent"] not in hidden]
        tops.sort(key=lambda n: (not n["busy"] and not any(by_id[c]["busy"] for c in n["children"])))
        if tree:
            for n in tops:
                n["child_nodes"] = [by_id[c] for c in n["children"]]
            return tops
        out = []
        for n in tops:
            out.append(n)
            out.extend(by_id[c] for c in n["children"])
        return out

    def coding_agent_records(self):
        """Nodes in the iOS/Mac agent-registry wire shape (kind=coding)."""
        recs = []
        for n in self.coding_nodes():
            live = n["busy"]
            sub = n["kind"] == "subagent"
            label = SOURCES.get(n["source"], n["source"])
            name = n["name"]
            if re.match(r"^New session - \d{4}-", name or "") or not name:   # OpenCode's placeholder title
                name = os.path.basename(str(n["cwd"] or "").rstrip("/")) or label
            recs.append({
                "id": n["id"], "kind": n["kind"], "title": ("%s · %s" % (label, name)) if not sub else name,
                # Still a conversation (open in its terminal): the one rule every list uses for "coding agents as chats".
                "open": not sub and n["status"] in ("idle", "running", "waiting", "limited"),
                "agent_type": n["agent_type"], "children": n["children"],
                "subagent_count": n["subagents"]["total"], "subagent_live": n["subagents"]["live"],
                "status": n["status"] if n["status"] != "idle" else "done", "limited": n["status"] == "limited", "live": live,
                "progress": ("Needs you — " if n["waiting"] else "") + (n["detail"][-1] if n["detail"] else _where(n["host"]).strip()),
                "session_id": "", "model": n["model"], "ref_id": "", "parent_id": n["parent"] or "",
                "created_at": n["started"], "started_at": n["started"],
                "finished_at": "" if live else n["last_activity"], "error": "",
                "cancel_requested": False, "host": n["host"], "source": n["source"], "cwd": n["cwd"],
                "result": "\n".join(n["detail"][-6:]), "tool_calls": n["tool_calls"]})
        return recs

    def _resolve_coding_id(self, aid):
        """Follow a retired pid-placeholder id (code:host:source:pidN) to the real hook-fed
        session that replaced it, so an app tapping a stale card doesn't hit 'no such agent'."""
        self._coding_table()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if row and row["status"] != "ended":
            return aid
        parts = str(aid or "").split(":", 3)
        if len(parts) == 4 and parts[0] == "code":
            _, host, source, sid = parts
            pid = (row or {}).get("pid")
            if not pid and sid.startswith("pid") and sid[3:].isdigit():
                pid = int(sid[3:])
            if pid:
                hit = self.store.one(
                    "SELECT id FROM coding_agents WHERE host=? AND source=? AND pid=? AND kind='coding' AND status<>'ended' ORDER BY last_activity DESC LIMIT 1",
                    (host, source, pid))
                if hit:
                    return hit["id"]
            cwd = (row or {}).get("cwd") or ""
            if cwd:
                hits = self.store.rows(
                    "SELECT id FROM coding_agents WHERE host=? AND source=? AND cwd=? AND kind='coding' AND status<>'ended' ORDER BY last_activity DESC LIMIT 2",
                    (host, source, cwd))
                if len(hits) == 1:
                    return hits[0]["id"]
        return aid

    def coding_models_for(self, source, current=""):
        """Model choices for one coding-agent harness: presets + any models recently seen for it."""
        seen, out = set(), []
        def add(mid, label=""):
            mid = str(mid or "").strip()
            if not mid or mid.lower() in seen:
                return
            seen.add(mid.lower())
            out.append({"id": mid, "label": label or mid.split("/")[-1]})
        if current:
            preset_label = dict(DEFAULT_MODELS.get(source, ())).get(current, "")
            add(current, preset_label)
        for mid, label in DEFAULT_MODELS.get(source, ()):
            add(mid, label)
        try:
            self._coding_table()
            for r in self.store.rows(
                    "SELECT DISTINCT model FROM coding_agents WHERE source=? AND model<>'' ORDER BY last_activity DESC LIMIT 12", (source,)):
                add(r["model"])
        except Exception:
            pass
        return out

    def coding_agent_detail(self, aid):
        aid = self._resolve_coding_id(aid)
        rec = next((r for r in self.coding_agent_records() if r["id"] == aid), None)
        if not rec:
            return None
        node = next(n for n in self.coding_nodes() if n["id"] == aid)
        rec["result"] = "\n".join(node["detail"])
        rec["child_agents"] = [r for r in self.coding_agent_records() if r["parent_id"] == aid]
        rec["messages"] = self._tidy_approvals([m for m in (self._detail_message(line, node["last_activity"]) for line in node["detail"]) if m],
                                               bool(node["waiting"]))
        rec["waiting"] = bool(node["waiting"])
        rec["can_send"] = node["status"] != "ended"
        rec["available_models"] = self.coding_models_for(node["source"], node.get("model") or "")
        pending = next((d[2:] for d in reversed(node["detail"]) if d.startswith("⏸")), "") if node["waiting"] else ""
        rec["pending_approval"] = pending or ("needs your approval" if node["waiting"] else "")
        # A question or menu on its screen: the apps show it as buttons (only looked for while it waits on you).
        rec["prompt"] = self.coding_prompt(aid) if node["status"] in ("waiting", "running") and node["waiting"] else None
        return rec

    @staticmethod
    def _tidy_approvals(msgs, waiting):
        """One approval card per request, live only while it's pending; answered ones
        become a quiet event ("Approved: Write"), and the answer isn't shown as a typed message."""
        out = []
        for i, m in enumerate(msgs):
            if m["role"] == "user" and m["content"] in ("approved from phone", "denied from phone"):
                verdict = "Approved" if m["content"].startswith("approved") else "Denied"
                prev = next((o for o in reversed(out) if o["role"] in ("approval", "event")), None)
                if prev and prev["role"] == "approval":
                    what = re.sub(r"^(approve|needs your approval:?)\s*", "", prev["content"], flags=re.I)
                    prev.update(role="event", content="%s%s" % (verdict, (": " + what) if what and "permission" not in what else ""))
                else:
                    out.append(dict(m, role="event", content=verdict))
                continue
            if m["role"] == "approval" and out and out[-1]["role"] == "approval":
                if "permission" in m["content"].lower():   # generic follow-up to a specific request
                    continue
                out[-1] = m
                continue
            out.append(m)
        if not waiting:   # nothing pending: any leftover request was answered in the terminal
            for m in out:
                if m["role"] == "approval":
                    m.update(role="event", content="Asked: " + re.sub(r"^(approve|question:)\s*", "", m["content"], flags=re.I))
        return out

    @staticmethod
    def _detail_message(line, when):
        """One hook-reported line as a chat message: user prompt, assistant reply, approval request or tool call."""
        mark = line[:1]
        role = {"›": "user", "✓": "assistant", "⏸": "approval", "⚠": "notice"}.get(mark, "tool")
        content = line[2:] if role != "tool" else line
        if role == "user":
            event = _harness_event(content)
            if event is not None:
                return {"role": "event", "content": event, "created_at": when} if event else None
            content = _clean_prompt(content)
            if not content:
                return None
        return {"role": role, "content": content, "created_at": when}

    def coding_agents_tool(self, body):
        args = self.parse_tool_args(body) if isinstance(body, str) else (body or {})
        if (args.get("action") or "list") == "detail":
            n = next((x for x in self.coding_nodes() if x["id"] == args.get("id")), None)
            return json.dumps(n, ensure_ascii=False) if n else "No coding agent with that id."
        nodes = self.coding_nodes()
        if not nodes:
            return "No linked coding agents active in the last few hours."
        lines = []
        for n in nodes:
            term = n["terminal"]
            where = ("kitty window %s" % term["kitty_window_id"]) if term.get("kitty_window_id") else (term.get("tmux_pane") or "terminal unknown")
            lines.append("- [%s] %s on %s · %s · %s · %d tools · last: %s · id=%s" % (
                n["status"], SOURCES.get(n["source"], n["source"]), n["host"], n["name"], where,
                n["tool_calls"], (n["detail"][-1] if n["detail"] else "-")[:140], n["id"]))
        return "Coding agents:\n" + "\n".join(lines)

    def coding_cancel(self, aid):
        """App 'Stop' on a coding agent: press Escape in exactly its terminal via
        Agent Control (interrupts the turn, like the user pressing Esc; it never
        closes the window). Refuses rather than guessing when the terminal
        can't be matched exactly."""
        aid = self._resolve_coding_id(aid)
        self._coding_table()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if not row:
            return {"ok": False, "detail": "no such coding agent"}, 404
        try:
            term = self._coding_terminal(row)
        except ValueError as exc:
            return {"ok": False, "detail": str(exc)}, 409
        if not term:
            return {"ok": False, "detail": "Couldn't match that agent to exactly one controllable terminal (it must run in Kitty or tmux)."}, 409
        sent = self._agent_control("agent_send_keys", {"session_id": term["session_id"], "host": row["host"], "keys": "escape"})
        if sent.get("error"):
            return {"ok": False, "detail": str(sent["error"])[:300]}, 502
        self.coding_event({"source": row["source"], "host": row["host"], "session_id": row["session_id"],
                           "event": "stop", "text": "interrupted from JoeBro"})
        return {"ok": True, "detail": "Interrupted"}, 200

    # ------------------------------------------------------------------ open-in-terminal reconcile
    def _agent_control(self, tool, args, timeout_ok=True):
        mcp = next((r for r in self.store.rows("SELECT * FROM mcp_servers WHERE enabled=1")
                    if "agent_control" in str(r.get("args") or "") or str(r.get("name") or "").lower() == "agent control"), None)
        if not mcp:
            raise ValueError("Agent Control is not enabled")
        ev = self.run_mcp_tool(mcp, tool, args)
        try:
            data = json.loads(ev.get("output") or "{}")
        except ValueError:
            data = {"error": str(ev.get("output"))[:300]}
        if ev.get("exit_code") and not data.get("error"):
            data["error"] = str(ev.get("output"))[:300]
        return data

    @staticmethod
    def _exact_match(row, s):
        if row["source"] == "claude" and s.get("claude_session_id") == row["session_id"]:
            return True
        if row.get("pid") and str(s.get("pid")) == str(row["pid"]):
            return True
        return False

    @staticmethod
    def _session_matches(row, s):
        if CodingAgentsMixin._exact_match(row, s):
            return True
        term = json.loads(row.get("terminal_json") or "{}")
        if term.get("tmux_pane") and s.get("kind") == "tmux" and s.get("session_id") == "tmux:" + str(term["tmux_pane"]) \
                and s.get("agent_type") == row["source"]:
            return True
        sock = str(term.get("kitty_listen_on") or "").replace("unix:", "")
        return bool(sock and str(s.get("socket")) == sock and str(s.get("window_id")) == str(term.get("kitty_window_id"))
                    and s.get("agent_type") == row["source"])

    def coding_reconcile_open(self):
        """A coding agent is listed only while it is open in a terminal: rows whose
        process/terminal is gone are ended; open agents that never sent a hook
        (started before the hooks existed) are adopted. Hosts that can't be
        reached are left alone rather than guessed about."""
        self._coding_table()
        data = self._agent_control("agent_list_sessions", {"host": "all", "filter_type": "all"})
        if data.get("error"):
            return {"ok": False, "error": data["error"]}
        sessions = [s for s in data.get("sessions") or [] if s.get("agent_type") in SOURCES]
        errs = data.get("host_errors") or []
        down = {str(e.get("host") if isinstance(e, dict) else e).split(":")[0].lower() for e in errs} \
            if isinstance(errs, list) else {str(k).lower() for k in errs}
        rows = self.store.rows("SELECT * FROM coding_agents WHERE status<>'ended' AND kind='coding' ORDER BY last_activity DESC")
        # Real hook-fed rows claim their terminal before pid-placeholder rows and exited nested runs.
        rows.sort(key=lambda r: (str(r.get("session_id") or "").startswith("pid"), -(r.get("last_activity") or 0)))
        ended = adopted = 0
        claimed, matched_rows = set(), set()
        # Pass 1: exact matches (claude_session_id or PID) so one live terminal never keeps exited child runs alive.
        for row in rows:
            if row["host"] in down:
                continue
            hit = next((s for s in sessions if id(s) not in claimed and s.get("host") == row["host"]
                        and self._exact_match(row, s)), None)
            if hit:
                claimed.add(id(hit))
                matched_rows.add(row["id"])
        # Pass 2: unclaimed rows match only unclaimed terminals (tmux pane, Kitty window, or unique folder).
        for row in rows:
            if row["host"] in down or row["id"] in matched_rows:
                continue
            avail = [s for s in sessions if id(s) not in claimed and s.get("host") == row["host"]]
            hit = next((s for s in avail if self._session_matches(row, s)), None) \
                or next(iter(self._same_folder(row, avail)), None)
            if hit:
                claimed.add(id(hit))
            else:
                self.store.exec("UPDATE coding_agents SET status='ended' WHERE id=?", (row["id"],))
                ended += 1
        now = time.time()
        for s in sessions:
            if id(s) in claimed or not HOST_RX.match(str(s.get("host") or "")):
                continue
            sid = s.get("claude_session_id") or ("pid%s" % s.get("pid"))
            aid = "code:%s:%s:%s" % (s["host"], s["agent_type"], sid)
            if self.store.one("SELECT id FROM coding_agents WHERE id=?", (aid,)):
                self.store.exec("UPDATE coding_agents SET status=CASE WHEN status='ended' THEN 'idle' ELSE status END WHERE id=?", (aid,))
                continue
            term = {"kitty_window_id": str(s.get("window_id"))} if s.get("window_id") else {}
            if s.get("socket"):
                term["kitty_listen_on"] = "unix:" + str(s["socket"])
            cwd = str(s.get("cwd") or "")
            self.store.exec("""INSERT INTO coding_agents(id,host,source,session_id,name,cwd,status,detail_json,started,last_activity,pid,terminal_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (aid, s["host"], s["agent_type"], sid, os.path.basename(cwd.rstrip("/")) or SOURCES[s["agent_type"]],
                                                    cwd, "idle", "[]", now, now, int(s["pid"]) if str(s.get("pid", "")).isdigit() else None, json.dumps(term)))
            adopted += 1
        return {"ok": True, "ended": ended, "adopted": adopted, "hosts_down": sorted(down)}

    # ------------------------------------------------------------------ command & control
    def _coding_row(self, aid):
        aid = self._resolve_coding_id(aid)
        self._coding_table()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if not row or row["status"] == "ended":
            raise ValueError("no open coding agent with that id (list them first)")
        return row

    def _coding_terminal(self, row):
        """The exact Agent Control session for this agent, or None."""
        data = self._agent_control("agent_list_sessions", {"host": row["host"], "filter_type": "all"})
        sessions = [s for s in data.get("sessions") or [] if s.get("controllable")]
        # Prefer exact session/PID match before Kitty window or folder fallback.
        match = [s for s in sessions if row["source"] == "claude" and s.get("claude_session_id") == row["session_id"]] \
            or [s for s in sessions if row.get("pid") and str(s.get("pid")) == str(row["pid"])] \
            or [s for s in sessions if self._session_matches(row, s)]
        if not match:
            match = self._same_folder(row, sessions)
        return match[0] if len(match) == 1 else None

    @staticmethod
    def _same_folder(row, sessions):
        """Fallback when ids/pids don't line up (Codex hooks run under a helper process, so
        their pid isn't the agent's): the one terminal running that agent in that folder."""
        cwd = str(row.get("cwd") or "").rstrip("/")
        hits = [s for s in sessions if cwd and s.get("agent_type") == row["source"]
                and str(s.get("cwd") or "").rstrip("/") == cwd and not s.get("claude_session_id")
                or (cwd and row["source"] == "claude" and s.get("claude_session_id") == row["session_id"])]
        return hits if len(hits) == 1 else []

    def _coding_inbox_table(self):
        self.store.exec("""CREATE TABLE IF NOT EXISTS coding_inbox(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, text TEXT NOT NULL,
            sender TEXT NOT NULL DEFAULT 'joebro', created_at REAL NOT NULL, delivered_at REAL, via TEXT)""")

    def coding_send(self, aid, text, sender="joebro", mode="auto"):
        """Give a coding agent an instruction. idle+terminal -> typed in now;
        otherwise queued and delivered by the agent's own hooks (Stop hook makes
        Claude/Codex continue with it; the next prompt otherwise)."""
        row = self._coding_row(aid)
        text = str(text or "").strip()
        if not text:
            raise ValueError("text is empty")
        self._coding_inbox_table()
        live = row["status"] in ("running", "waiting")
        # A controllable terminal gets it typed in, exactly as if you typed it (Claude Code/Codex queue it
        # themselves while busy). Nothing is logged here: the agent's own prompt hook reports it when it
        # actually takes it, so the feed order and "queued" stay true. Waiting on an approval: never typed.
        typeable = not live or (row["source"] in ("claude", "codex") and row["status"] == "running")
        if mode in ("auto", "terminal") and typeable:
            try:
                term = self._coding_terminal(row)
            except ValueError:          # Agent Control is off or unreachable: queue for the hooks instead
                if mode == "terminal":
                    raise
                term = None
            if term:
                r = self._agent_control("agent_send_input", {"session_id": term["session_id"], "host": row["host"], "text": text, "press_enter": True})
                if r.get("error"):
                    print("coding_send: typing into %s failed: %s" % (term["session_id"], r["error"]), flush=True)
                if not r.get("error"):
                    # The agent's own prompt hook reports the typed message: logging it here too duplicated it.
                    return {"ok": True, "delivered": "terminal"}
            if mode == "terminal":
                raise ValueError("this agent isn't in a controllable Kitty/tmux terminal")
        mid = "cin_" + os.urandom(5).hex()
        self.store.exec("INSERT INTO coding_inbox(id,agent_id,text,sender,created_at) VALUES(?,?,?,?,?)",
                        (mid, aid, text[:4000], sender, time.time()))
        return {"ok": True, "delivered": "queued", "id": mid,
                "detail": "Queued: the agent gets it at its next step."}

    def coding_inbox_pull(self, q):
        """Called by the reporter hook (Stop / UserPromptSubmit) of that agent."""
        self._coding_inbox_table()
        aid = "code:%s:%s:%s" % (str(q.get("host") or ""), str(q.get("source") or ""), str(q.get("session_id") or ""))
        rows = self.store.rows("SELECT * FROM coding_inbox WHERE agent_id=? AND delivered_at IS NULL ORDER BY created_at", (aid,))
        for r in rows:
            self.store.exec("UPDATE coding_inbox SET delivered_at=?, via=? WHERE id=?", (time.time(), str(q.get("via") or "hook")[:20], r["id"]))
        return {"ok": True, "messages": [{"from": r["sender"], "text": r["text"]} for r in rows]}

    def coding_set_model(self, aid, model):
        """Switch the active model of a running/idle coding agent (Claude Code, Codex, OpenCode, Antigravity)."""
        row = self._coding_row(aid)
        model = re.sub(r"[\r\n]+", " ", str(model or "")).strip()[:120]
        if not model:
            raise ValueError("model is required")
        src = row["source"]
        if src == "opencode":
            # OpenCode's plugin applies a queued /model directive on its next prompt (and reports back).
            res = self.coding_send(row["id"], "/model " + model, sender="app", mode="queue")
        else:
            # Claude Code, Codex, and Antigravity take `/model <model>` in their idle Kitty/tmux terminal.
            res = self.coding_send(row["id"], "/model " + model, sender="app", mode="terminal")
        self.store.exec("UPDATE coding_agents SET model=? WHERE id=?", (model, row["id"]))
        return dict(res, model=model, id=row["id"])

    def coding_keys(self, aid, keys):
        row = self._coding_row(aid)
        allowed = {"escape", "enter", "ctrl+c", "y", "n", "tab", "up", "down", "1", "2", "3", "shift+tab"}
        if str(keys).lower() not in allowed:
            raise ValueError("keys must be one of: " + ", ".join(sorted(allowed)))
        term = self._coding_terminal(row)
        if not term:
            raise ValueError("this agent isn't in a controllable Kitty/tmux terminal")
        r = self._agent_control("agent_send_keys", {"session_id": term["session_id"], "host": row["host"], "keys": str(keys).lower()})
        if r.get("error"):
            raise ValueError(str(r["error"])[:300])
        return {"ok": True}

    def coding_screen(self, aid, lines=60):
        row = self._coding_row(aid)
        term = self._coding_terminal(row)
        if not term:
            node = next((n for n in self.coding_nodes() if n["id"] == aid), {})
            return {"ok": True, "source": "hooks", "lines": node.get("detail") or []}
        r = self._agent_control("agent_get_screen", {"session_id": term["session_id"], "host": row["host"],
                                                     "lines": max(10, min(int(lines or 60), 300)), "clear_ansi": True})
        if r.get("error"):
            raise ValueError(str(r["error"])[:300])
        return {"ok": True, "source": "terminal", "screen": r.get("screen") or r.get("text") or r}

    # ------------------------------------------------------------------ questions and menus, answered from the apps
    # Whatever an agent puts up as a menu (Claude's AskUserQuestion and permission prompts, Codex approvals,
    # the folder-trust prompt...) is read off its terminal and answered with the same keys you'd press there.
    _MENU_HINT = re.compile(r"enter to (select|confirm|submit)|esc to (cancel|exit|interrupt)|to navigate|tab/arrow|↑/?↓|\(y/n\)|\[y/n\]", re.I)
    _OPTION = re.compile(r"^\s*([❯›>▶●])?\s*(?:(\d{1,2})[.)]\s+)?(\S.*?)\s*$")
    _CHROME = re.compile(r"^[\s─━═\-_│|╭╮╰╯]*$")
    _SKIP = re.compile(r"^(chat about this|type here to tell)", re.I)
    _FREEFORM = re.compile(r"^(type something|other|no, and tell|tell (claude|codex|antigravity|agy) what)", re.I)

    @classmethod
    def parse_menu(cls, screen):
        """The menu at the bottom of a terminal screen, or None.
        {question, header, options: [{key, label, description, selected, freeform}], numbered}"""
        lines = [l.rstrip() for l in str(screen or "").splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        tail = lines[-40:]
        hint = next((i for i in range(len(tail) - 1, max(-1, len(tail) - 8), -1) if cls._MENU_HINT.search(tail[i])), None)
        if hint is None:
            # No key hint on screen (cut off): only a cursor on a numbered row right at the bottom counts.
            if not any(re.match(r"^\s*[❯›>▶]\s*\d{1,2}[.)]\s", l) for l in tail[-6:]):
                return None
            hint = len(tail)
        body = tail[:hint]
        # Walk up from the hint collecting the option block (options, their indented descriptions, rules).
        opts, i, numbered = [], len(body) - 1, False
        pending_desc, plain = [], []          # plain: unnumbered rows below the cursor row (label column, label)
        while i >= 0:
            raw = body[i]
            if cls._CHROME.match(raw):
                i -= 1
                continue
            m = cls._OPTION.match(raw)
            cursor, num, label = m.group(1), m.group(2), m.group(3)
            col = raw.index(label)
            indent = len(raw) - len(raw.lstrip())
            if num or cursor:
                numbered = numbered or bool(num)
                if cursor and not num and plain:      # unnumbered menu: rows below in the same column are its options
                    opts[:0] = [{"key": "", "label": l, "description": "", "selected": False} for c, l in plain if c == col]
                    plain = []
                opts.insert(0, {"key": num or "", "label": label, "description": " ".join(pending_desc).strip(),
                                "selected": bool(cursor)})
                pending_desc = []
            elif opts and not numbered and any(not o["key"] for o in opts) and col == next(
                    (body[k].index(o["label"]) for k in range(i + 1, len(body)) for o in opts[:1] if o["label"] in body[k]), -1):
                opts.insert(0, {"key": "", "label": label, "description": "", "selected": False})
            elif not opts and indent <= 4 and len(label) < 80 and not pending_desc:
                plain.insert(0, (col, label))
            elif indent >= 4 and len(label) < 200:
                pending_desc.insert(0, label)
            else:
                break
            i -= 1
        if pending_desc and not opts:
            return None
        if numbered:
            opts = [o for o in opts if o["key"]]
        opts = [o for o in opts if not cls._SKIP.match(o["label"])]
        if len(opts) < 2 or not any(o["selected"] for o in opts) and not numbered:
            return None
        for o in opts:
            o["freeform"] = bool(cls._FREEFORM.match(o["label"]))
            o["label"] = re.sub(r"\s*\((y|n|esc|enter|shift\+tab|tab)\)\s*$", "", o["label"], flags=re.I)
        # The question: nearest real line above the block; a "☐ Colour ☐ Size" bar gives the header.
        question, header = "", ""
        j = i
        while j >= 0 and j > i - 8:
            t = body[j].strip()
            if t and not cls._CHROME.match(t):
                if re.search(r"[☐☒✔]", t):
                    header = " · ".join(re.findall(r"[☐☒]\s*([^☐☒✔→←]+?)\s*(?=[☐☒✔→]|$)", t))
                elif not question:
                    question = t.lstrip("❯›> ").strip()
                    if header:
                        break
                else:
                    break
            j -= 1
        if "?" not in question:      # prefer the actual question a few lines up ("…one you trust?")
            asked = next((body[k].strip() for k in range(i, max(-1, i - 10), -1) if k >= 0 and "?" in body[k]), "")
            if asked:
                question = re.split(r"(?<=\?)\s", asked)[0]
        if "review your answers" in question.lower() or any(o["label"].lower().startswith("submit answers") for o in opts):
            question = "Review your answers"
        return {"question": question[:400], "header": header[:120], "options": opts[:12], "numbered": numbered}

    def coding_prompt(self, aid, row=None):
        """The menu the agent is showing right now (cached briefly: the apps poll the detail)."""
        cache = self.__dict__.setdefault("_prompt_cache", {})
        hit = cache.get(aid)
        if hit and time.time() - hit[0] < 1.5:
            return hit[1]
        row = row or self._coding_row(aid)
        menu = None
        try:
            term = self._coding_terminal(row)
            if term:
                r = self._agent_control("agent_get_screen", {"session_id": term["session_id"], "host": row["host"], "lines": 45, "clear_ansi": True})
                menu = self.parse_menu(r.get("screen") or r.get("text") or "")
        except Exception:
            menu = None
        cache[aid] = (time.time(), menu)
        return menu

    def coding_answer(self, aid, key=None, text=None):
        """Pick an option of the agent's on-screen menu (key = its number, or its label for an
        unnumbered menu), optionally typing free text for a "Type something" option.
        Returns the next menu, if one follows (multi-question forms, the review screen)."""
        row = self._coding_row(aid)
        term = self._coding_terminal(row)
        if not term:
            raise ValueError("this agent isn't in a controllable Kitty/tmux terminal")
        self.__dict__.setdefault("_prompt_cache", {}).pop(aid, None)
        menu = self.coding_prompt(aid, row)
        if not menu:
            if text:      # no menu up: a plain reply
                return self.coding_send(aid, text, "app")
            raise ValueError("the agent isn't asking anything right now")
        key, text = str(key or "").strip(), str(text or "").strip()
        opt = next((o for o in menu["options"] if key and (o["key"] == key or o["label"] == key)), None)
        if not opt and text:
            opt = next((o for o in menu["options"] if o["freeform"]), None)
        if not opt:
            raise ValueError("no such option")
        send = lambda k: self._agent_control("agent_send_keys", {"session_id": term["session_id"], "host": row["host"], "keys": k})
        if menu["numbered"]:
            r = send(opt["key"])
        else:
            idx = menu["options"].index(opt)
            cur = next((n for n, o in enumerate(menu["options"]) if o["selected"]), 0)
            for _ in range(abs(idx - cur)):
                send("down" if idx > cur else "up")
                time.sleep(0.08)
            r = send("enter")
        if r.get("error"):
            raise ValueError(str(r["error"])[:300])
        if opt["freeform"] and text:
            time.sleep(0.4)
            r = self._agent_control("agent_send_input", {"session_id": term["session_id"], "host": row["host"], "text": text, "press_enter": True})
            if r.get("error"):
                raise ValueError(str(r["error"])[:300])
        # Logged like a typed message (the agent's hooks don't report menu picks), shown as "Answered: …".
        said = text if (opt["freeform"] and text) else opt["label"]
        if menu["question"] != "Review your answers":     # the review screen's "Submit" isn't an answer
            det = json.loads(self.store.one("SELECT detail_json FROM coding_agents WHERE id=?", (aid,))["detail_json"] or "[]")
            det.append("› ⟦answer⟧ " + said[:300])
            self.store.exec("UPDATE coding_agents SET detail_json=? WHERE id=?", (json.dumps(det[-200:], ensure_ascii=False), aid))
        time.sleep(0.7)
        self.__dict__["_prompt_cache"].pop(aid, None)
        nxt = self.coding_prompt(aid, row)
        if not nxt and row["status"] == "waiting":
            # Answered: back to work (the agent's next hook confirms it).
            self.store.exec("UPDATE coding_agents SET status='running' WHERE id=? AND status='waiting'", (aid,))
        return {"ok": True, "answered": opt["label"], "prompt": nxt}

    # ------------------------------------------------------------------ approvals from the phone
    def _coding_decisions_table(self):
        self.store.exec("""CREATE TABLE IF NOT EXISTS coding_decisions(agent_id TEXT PRIMARY KEY, decision TEXT NOT NULL,
            created_at REAL NOT NULL, applied_at REAL, result TEXT)""")

    def coding_approval_push(self, row):
        """The agent has sat on an approval prompt for a while -> ping the phone.
        Agents in a controllable terminal get Approve / Deny buttons."""
        if self.pref("coding_approval_push") == "off":
            return
        term = json.loads(row.get("terminal_json") or "{}")
        actionable = bool(term.get("kitty_window_id") or term.get("tmux_pane"))
        detail = json.loads(row.get("detail_json") or "[]")
        what = next((d[2:] for d in reversed(detail) if d.startswith("⏸")), "") or "needs your approval"
        title = "%s%s needs you" % (SOURCES.get(row["source"], row["source"]), _where(row["host"]))
        body = "%s · %s" % (row.get("name") or "", what)
        nid = "inb_" + os.urandom(6).hex()
        try:
            self.store.exec(
                "insert into inbox(id,kind,title,body,source,read,created_at) values(?,?,?,?,?,0,?)",
                (nid, "coding_approval", title, body[:300], row["id"], _iso(time.time())))
        except Exception:
            return False  # Never push an approval the Notifications list cannot retrieve.
        try:
            from jb_push import send_push
            data = {"agent": row["id"], "kind": "coding_approval"}
            if nid:
                data["inbox_id"] = nid
            # A question needs its options (in the app); only yes/no approvals get lock-screen buttons.
            question = what.startswith("Question:")
            if question:
                title = "%s%s is asking" % (SOURCES.get(row["source"], row["source"]), _where(row["host"]))
            send_push(self.store, title, body[:300], category="CODING_APPROVAL" if actionable and not question else None,
                      data=data)
        except Exception:
            pass
        return True

    def coding_decide(self, aid, decision):
        """Approve / deny the agent's pending permission prompt (lock-screen buttons).
        Only presses keys when the terminal really shows an approval prompt; if the
        prompt isn't up yet (e.g. a desktop hook is still holding it) the decision is
        kept and retried for 3 minutes."""
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        row = self._coding_row(aid)
        if row["status"] != "waiting":
            return {"ok": True, "applied": False, "detail": "already answered"}
        self._coding_decisions_table()
        self.store.exec("INSERT OR REPLACE INTO coding_decisions(agent_id,decision,created_at) VALUES(?,?,?)",
                        (aid, decision, time.time()))
        return self.coding_apply_decision(aid)

    APPROVAL_RX = re.compile(r"Do you want|Would you like|Allow (once|always|this)|Yes, proceed|Approve|Run command|approv|\(y/n\)|\[y/N\]|Permission required", re.I)

    def coding_apply_decision(self, aid):
        self._coding_decisions_table()
        d = self.store.one("SELECT * FROM coding_decisions WHERE agent_id=? AND applied_at IS NULL", (aid,))
        if not d:
            return {"ok": True, "applied": False}
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if not row or row["status"] != "waiting" or time.time() - d["created_at"] > 180:
            self.store.exec("UPDATE coding_decisions SET applied_at=?, result='expired' WHERE agent_id=?", (time.time(), aid))
            return {"ok": True, "applied": False, "detail": "the prompt is gone"}
        term = self._coding_terminal(row)
        if not term:
            return {"ok": True, "applied": False, "pending": True, "detail": "terminal not reachable yet; will retry"}
        r = self._agent_control("agent_get_screen", {"session_id": term["session_id"], "host": row["host"], "lines": 30, "clear_ansi": True})
        screen = str(r.get("screen") or r.get("text") or "")
        if not self.APPROVAL_RX.search(screen[-2500:]):
            return {"ok": True, "applied": False, "pending": True, "detail": "no approval prompt on screen yet; will retry"}
        approve = d["decision"] == "approve"
        key = ("y" if row["source"] == "codex" else "enter") if approve else "escape"
        r = self._agent_control("agent_send_keys", {"session_id": term["session_id"], "host": row["host"], "keys": key})
        if r.get("error"):
            return {"ok": False, "applied": False, "detail": str(r["error"])[:200]}
        self.store.exec("UPDATE coding_decisions SET applied_at=?, result=? WHERE agent_id=?", (time.time(), key, aid))
        self.coding_event({"source": row["source"], "host": row["host"], "session_id": row["session_id"],
                           "event": "prompt" if approve else "stop",
                           "text": "approved from phone" if approve else "denied from phone"})
        return {"ok": True, "applied": True, "decision": d["decision"]}

    def coding_approval_tick(self, pushed):
        """Island loop: push for prompts left waiting >45 s, retry pending phone decisions."""
        self._coding_table()
        now = time.time()
        for row in self.store.rows("SELECT * FROM coding_agents WHERE status='waiting' AND last_activity BETWEEN ? AND ?",
                                   (now - 1800, now - 45)):
            key = (row["id"], row["last_activity"])
            if key not in pushed and now - pushed.get(row["id"], 0) > 120:
                if self.coding_approval_push(row):
                    pushed[key] = pushed[row["id"]] = now
        self._coding_decisions_table()
        for d in self.store.rows("SELECT agent_id FROM coding_decisions WHERE applied_at IS NULL"):
            try:
                self.coding_apply_decision(d["agent_id"])
            except Exception:
                pass

    # ------------------------------------------------------------------ pings from coding agents
    PING_GAP = 60          # seconds between pings from one sender
    PING_DAILY = 30        # per sender per day

    def coding_ping(self, body):
        """A coding agent asks JoeBro to ping the user (MCP `joebro_ping` / `joebro-ping` CLI).
        Rate limited per sender so a looping agent can't spam the phone."""
        self.store.exec("""CREATE TABLE IF NOT EXISTS coding_pings(id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
            title TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL)""")
        message = _clip(body.get("message"), 600)
        if not message:
            raise ValueError("message is required")
        source = str(body.get("source") or "agent").lower()[:20]
        host = str(body.get("host") or "").lower()[:20]
        project = _clip(body.get("project") or os.path.basename(str(body.get("cwd") or "").rstrip("/")), 60)
        sender = ":".join(x for x in (host, source, str(body.get("session_id") or project)[:80]) if x) or "agent"
        now = time.time()
        last = self.store.one("SELECT created_at FROM coding_pings WHERE sender=? ORDER BY created_at DESC LIMIT 1", (sender,))
        if last and now - last["created_at"] < self.PING_GAP:
            return {"ok": False, "delivered": False, "detail": "rate limited: one ping a minute per agent"}
        n = self.store.one("SELECT COUNT(*) AS n FROM coding_pings WHERE sender=? AND created_at>?", (sender, now - 86400))["n"]
        if n >= self.PING_DAILY:
            return {"ok": False, "delivered": False, "detail": "daily ping limit reached for this agent"}
        who = SOURCES.get(source, source.capitalize() if source != "agent" else "A coding agent")
        where = _where(host)
        title = _clip(body.get("title"), 80) or ("%s%s%s" % (who, where, (" · " + project) if project else ""))
        self.store.exec("INSERT INTO coding_pings(sender,title,message,created_at) VALUES(?,?,?,?)", (sender, title, message, now))
        self.store.exec("DELETE FROM coding_pings WHERE created_at<?", (now - 7 * 86400,))
        aid = "code:%s:%s:%s" % (host, source, body.get("session_id")) if body.get("session_id") else ""
        if aid and self.store.one("SELECT 1 AS x FROM coding_agents WHERE id=?", (aid,)):
            row = self.store.one("SELECT detail_json FROM coding_agents WHERE id=?", (aid,))
            detail = (json.loads(row["detail_json"] or "[]") + ["📣 " + message[:200]])[-14:]
            self.store.exec("UPDATE coding_agents SET detail_json=? WHERE id=?", (json.dumps(detail, ensure_ascii=False), aid))
        delivery = self.notify(message, title="🤖 " + title, kind="coding", source=aid or sender)
        return {"ok": True, "delivered": bool(isinstance(delivery, dict) and delivery.get("delivered", True)), "title": title}

    def coding_launch(self, host, harness, cwd="", prompt=""):
        if not HOST_RX.match(str(host or "")) or harness not in ("claude", "codex", "opencode", "agy"):
            raise ValueError("host must be a linked computer name and harness claude|codex|opencode|agy")
        r = self._agent_control("agent_launch_session", {"host": host, "agent_type": harness, "cwd": cwd or "", "initial_prompt": prompt or ""})
        if r.get("error"):
            raise ValueError(str(r["error"])[:300])
        return {"ok": True, "launched": r}


# ---------------------------------------------------------------------- module level
def live_coding_count(store):
    now = time.time()
    r = store.one("SELECT COUNT(*) AS n FROM coding_agents WHERE ((status='running' AND last_activity>?) OR (status='waiting' AND last_activity>?))",
                  (now - STALE_RUNNING, now - STALE_WAITING)) or {}
    return int(r.get("n") or 0)


def island_state(handler):
    """What the Dynamic Island should show, computed server-side with the same
    rules as the app: one entry per piece of work, a chat turn and its registry
    row counted once, briefings excluded, coding agents included."""
    from jb_chat import _ACTIVE_TURNS
    turns = sorted(_ACTIVE_TURNS)
    def chat_title(sid):
        if sid == PHONE_SESSION_ID:
            return handler.phone_agent_name()
        row = handler.store.one("SELECT name FROM sessions WHERE id=?", (sid,))
        return ((row or {}).get("name") or "").strip() or "JoeBro"
    runs = [{"id": "chat-" + s, "title": chat_title(s), "kind": "personal" if s == PHONE_SESSION_ID else "reply", "detail": "", "queued": False} for s in turns]
    seen = set(turns)
    for r in handler.agents_payload(limit=60)["agents"]:
        if not r["live"] or r["kind"] == "briefing":
            continue
        sid = r.get("session_id") or ""
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        runs.append({"id": r["id"], "title": (handler.phone_agent_name() + " · " if r.get("scope") == "personal" else "") + (r["title"] or "Agent"), "kind": "personal" if r.get("scope") == "personal" else r["kind"],
                     "detail": (r.get("progress") or "")[:120], "queued": r["status"] == "queued"})
    for r in handler.coding_agent_records():
        # Subagents are counted on their run, never listed as runs. A run whose own turn has ended stays on
        # the Island while any of its subagents is still working.
        subs = int(r.get("subagent_live") or 0)
        if r["kind"] != "subagent" and (r["live"] or subs):
            detail = (r.get("progress") or "")[:120] if r["live"] else ("%d subagent%s working" % (subs, "" if subs == 1 else "s"))
            runs.append({"id": r["id"], "title": r["title"], "kind": "coding", "detail": detail, "queued": False,
                         "subagents": subs, "waiting": r["live"] and "Needs you" in (r.get("progress") or "")[:10]})
    return runs


def coding_loop(server):
    """Background upkeep for coding agents: phone pings for agents left waiting on
    an approval, reconciling which sessions are still open in a terminal (every
    90 s), and owning the Live Activity (Dynamic Island) while the app is in the
    background: an update push when the set of live work changes (keep-alive every
    2 min so it never reads stale), and an end push the moment nothing is live."""
    import types as _types
    from joebro_backend import Handler
    time.sleep(25)
    threading.Thread(target=island_loop, args=(server,), daemon=True).start()
    last_reconcile = 0.0
    pushed = {}
    while True:
        try:
            h = Handler.__new__(Handler)
            h.server = _types.SimpleNamespace(store=server.store)
            now = time.time()
            try:
                h.coding_approval_tick(pushed)
            except Exception:
                pass
            if len(pushed) > 400:
                pushed.clear()
            try:                   # Night Shift scheduler (its own try: a failure here must not skip the memory upkeep)
                h.night_tick()
            except Exception:
                import traceback
                traceback.print_exc()
            try:
                import jb_sharedmem
                jb_sharedmem.nightly(server.store)
            except Exception:
                pass
            if now - last_reconcile > 90 and server.store.one("SELECT 1 AS x FROM coding_agents WHERE status<>'ended' LIMIT 1"):
                last_reconcile = now
                try:
                    h.coding_reconcile_open()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(20)


def island_loop(server):
    """Owns the Live Activity (Dynamic Island) while the app is in the background: a push-to-start when the
    first agent appears, update pushes when runs / subagents / waiting change, an end push when nothing is
    live. Woken within about a second by every coding event (jb_push.WAKE); rate limits live in the pusher."""
    import types as _types
    from joebro_backend import Handler
    import jb_push
    pusher = jb_push.LiveActivityPusher(server.store)
    time.sleep(5)
    while True:
        try:
            if jb_push.has_activities(server.store) or jb_push.has_start_tokens(server.store):
                h = Handler.__new__(Handler)
                h.server = _types.SimpleNamespace(store=server.store)
                try:
                    pusher.scene = h.pref("island_scene") or None
                except Exception:
                    pass
                pusher.tick(island_state(h))
            else:
                pusher.last_sig, pusher.started = None, None
        except Exception:
            pass
        jb_push.WAKE.wait(2)
        jb_push.WAKE.clear()


def _harness_event(text):
    """Prompts the agent's own harness injected (not typed by a person) as a short
    event line; "" means hide it; None means it's a real message."""
    t = (text or "").strip()
    low = t.lower()
    if t.startswith("⟦answer⟧ "):          # an option picked from the apps (coding_answer)
        return "Answered: " + t[len("⟦answer⟧ "):]
    if t.startswith("<task-notification>"):
        summ = re.search(r"<summary>(.*?)</summary>", t, re.S)
        status = re.search(r"<status>(.*?)</status>", t, re.S)
        what = (summ.group(1).strip() if summ else "Background task").replace('"', "")
        what = re.sub(r"^Agent (.+?) (finished|completed|failed)$", r"\1", what)
        st = status.group(1).strip() if status else "completed"
        return "%s %s" % (what, "finished" if st in ("completed", "done") else st)
    if t.startswith(("<system-reminder>", "<local-command-stdout>", "<local-command-caveat>", "Caveat: The messages below")):
        return ""
    cmd = re.match(r"<command-name>\s*(/?[\w:-]+)", t)
    if cmd:
        return "Ran " + cmd.group(1)
    if t.startswith("[Request interrupted"):
        return "Interrupted"
    if "usage limit has reset" in low:
        return "Usage limit reset — carrying on"
    if t.startswith("This session is being continued from a previous conversation"):
        return "Conversation compacted — carrying on"
    if t.startswith("<"):
        tag = re.match(r"<([a-z][\w-]*)>", t)
        if tag and t.rstrip().endswith("</%s>" % tag.group(1)):
            return ""     # other harness-only blocks
    return None


def _clean_prompt(text):
    """A typed prompt as a person reads it: harness wrappers around pastes and
    attachments become plain text and short placeholders."""
    t = text or ""
    # <pasted_content id=…>…</pasted_content> (and similar wrapper tags): keep what was pasted.
    t = re.sub(r"</?pasted_content[^>]*>", "", t)
    t = re.sub(r"</?(?:system-reminder|user-prompt-submit-hook)[^>]*>.*?(?:</(?:system-reminder|user-prompt-submit-hook)>|$)", "", t, flags=re.S)
    # Claude Code's collapsed pastes / attachments.
    t = re.sub(r"\[Pasted text #\d+(?: \+\d+ lines)?\]", "[pasted text]", t)
    t = re.sub(r"\[Image(?: #\d+)?(?:: [^\]]*)?\]", "[image]", t)
    t = re.sub(r"<image[^>]*>.*?</image>|<image[^>]*/>", "[image]", t, flags=re.S)
    t = re.sub(r"(?m)^\s*\[Image: source: [^\]]+\]\s*$", "[image]", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
