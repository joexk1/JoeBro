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
SOURCES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "agy": "AGY"}
HOST_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")   # the computer name each link reports as
EVENTS = {"start", "prompt", "tool", "tool_done", "waiting", "stop", "end", "subagent_start", "subagent_stop"}
STALE_RUNNING = 25 * 60     # running with no event this long -> assume it died
STALE_WAITING = 3 * 3600
SUB_DONE_SHOW = 30 * 60   # finished subagents stay listed this long
SHOW_IDLE = 6 * 3600        # idle sessions stay listed this long
DROP_ENDED = 15 * 60

CODING_AGENTS_TOOL = {"type": "function", "function": {
    "name": "coding_agents",
    "description": ("See the user's coding agents (Claude Code, Codex, OpenCode) running on their linked computers: "
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
        for col in ("kind TEXT NOT NULL DEFAULT 'coding'", "agent_type TEXT NOT NULL DEFAULT ''"):
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
        text = _clip(ev.get("text"), 300)
        tool = _clip(ev.get("tool"), 60)
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
            if text:
                detail.append("✓ " + text)
        elif kind == "end":
            status = "ended"
        elif kind in ("subagent_start", "subagent_stop"):
            pass
        detail = detail[-14:]
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
        if not is_sub and kind in ("stop", "end"):   # harnesses that send no subagent stop: they end with the parent's turn
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
        for r in self.store.rows("SELECT * FROM coding_agents ORDER BY last_activity DESC LIMIT 120"):
            age = now - r["last_activity"]
            status = r["status"]
            is_sub = r.get("kind") == "subagent"
            if status == "running" and age > STALE_RUNNING:
                status = "stale"
            if status == "waiting" and age > STALE_WAITING:
                status = "idle"
            if status == "ended" or (is_sub and status == "done" and age > SUB_DONE_SHOW) or \
                    (is_sub and status in ("stale", "idle") and age > SUB_DONE_SHOW):
                hidden.add(r["id"])
                continue
            if not is_sub and status in ("idle", "stale") and (age > SHOW_IDLE or not include_idle):
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
            recs.append({
                "id": n["id"], "kind": n["kind"], "title": ("%s · %s" % (label, n["name"])) if not sub else n["name"],
                "agent_type": n["agent_type"], "children": n["children"],
                "subagent_count": n["subagents"]["total"], "subagent_live": n["subagents"]["live"],
                "status": n["status"] if n["status"] != "idle" else "done", "live": live,
                "progress": ("Needs you — " if n["waiting"] else "") + (n["detail"][-1] if n["detail"] else _where(n["host"]).strip()),
                "session_id": "", "model": n["model"], "ref_id": "", "parent_id": n["parent"] or "",
                "created_at": n["started"], "started_at": n["started"],
                "finished_at": "" if live else n["last_activity"], "error": "",
                "cancel_requested": False, "host": n["host"], "source": n["source"], "cwd": n["cwd"],
                "result": "\n".join(n["detail"][-6:]), "tool_calls": n["tool_calls"]})
        return recs

    def coding_agent_detail(self, aid):
        rec = next((r for r in self.coding_agent_records() if r["id"] == aid), None)
        if not rec:
            return None
        node = next(n for n in self.coding_nodes() if n["id"] == aid)
        rec["result"] = "\n".join(node["detail"])
        rec["child_agents"] = [r for r in self.coding_agent_records() if r["parent_id"] == aid]
        rec["messages"] = [{"role": "user" if line.startswith("›") else "assistant", "content": line,
                            "created_at": node["last_activity"]} for line in node["detail"]]
        return rec

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
        self._coding_table()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if not row:
            return {"ok": False, "detail": "no such coding agent"}, 404
        mcp = next((r for r in self.store.rows("SELECT * FROM mcp_servers WHERE enabled=1")
                    if "agent_control" in str(r.get("args") or "") or str(r.get("name") or "").lower() == "agent control"), None)
        if not mcp:
            return {"ok": False, "detail": "Agent Control is not enabled"}, 409
        listed = self.run_mcp_tool(mcp, "agent_list_sessions", {"host": row["host"], "filter_type": "all"})
        try:
            sessions = json.loads(listed.get("output") or "{}").get("sessions") or []
        except ValueError:
            sessions = []
        term = json.loads(row.get("terminal_json") or "{}")
        sock = str(term.get("kitty_listen_on") or "").replace("unix:", "")
        match = [s for s in sessions if s.get("controllable") and (
            (row["source"] == "claude" and s.get("claude_session_id") == row["session_id"]) or
            (sock and str(s.get("socket")) == sock and str(s.get("window_id")) == str(term.get("kitty_window_id"))) or
            (term.get("tmux_pane") and s.get("kind") == "tmux" and
             s.get("session_id") == "tmux:" + str(term["tmux_pane"]) and
             s.get("agent_type") == row["source"]))]
        if len(match) != 1:
            return {"ok": False, "detail": "Couldn't match that agent to exactly one controllable terminal (it must run in Kitty or tmux)."}, 409
        sent = self.run_mcp_tool(mcp, "agent_send_keys", {"session_id": match[0]["session_id"], "host": row["host"], "keys": "escape"})
        if sent.get("exit_code"):
            return {"ok": False, "detail": str(sent.get("output") or "interrupt failed")[:300]}, 502
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
    def _session_matches(row, s):
        term = json.loads(row.get("terminal_json") or "{}")
        sock = str(term.get("kitty_listen_on") or "").replace("unix:", "")
        if row["source"] == "claude" and s.get("claude_session_id") == row["session_id"]:
            return True
        if row.get("pid") and str(s.get("pid")) == str(row["pid"]):
            return True
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
        rows = self.store.rows("SELECT * FROM coding_agents WHERE status<>'ended'")
        ended = adopted = 0
        claimed = set()
        for row in rows:
            if row["host"] in down:
                continue
            hit = next((s for s in sessions if s.get("host") == row["host"] and self._session_matches(row, s)), None)
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
        self._coding_table()
        row = self.store.one("SELECT * FROM coding_agents WHERE id=?", (aid,))
        if not row or row["status"] == "ended":
            raise ValueError("no open coding agent with that id (list them first)")
        return row

    def _coding_terminal(self, row):
        """The exact Agent Control session for this agent, or None."""
        data = self._agent_control("agent_list_sessions", {"host": row["host"], "filter_type": "all"})
        match = [s for s in data.get("sessions") or [] if s.get("controllable") and self._session_matches(row, s)]
        return match[0] if len(match) == 1 else None

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
        if mode in ("auto", "terminal") and not live:
            term = self._coding_terminal(row)
            if term:
                r = self._agent_control("agent_send_input", {"session_id": term["session_id"], "host": row["host"], "text": text, "press_enter": True})
                if not r.get("error"):
                    self.coding_event({"source": row["source"], "host": row["host"], "session_id": row["session_id"],
                                       "event": "prompt", "text": "[from %s] %s" % (sender, text)})
                    return {"ok": True, "delivered": "terminal"}
            if mode == "terminal":
                raise ValueError("this agent isn't in a controllable Kitty/tmux terminal")
        mid = "cin_" + os.urandom(5).hex()
        self.store.exec("INSERT INTO coding_inbox(id,agent_id,text,sender,created_at) VALUES(?,?,?,?,?)",
                        (mid, aid, text[:4000], sender, time.time()))
        return {"ok": True, "delivered": "queued", "id": mid,
                "detail": "Queued: the agent receives it when its current turn ends (or with its next prompt)."}

    def coding_inbox_pull(self, q):
        """Called by the reporter hook (Stop / UserPromptSubmit) of that agent."""
        self._coding_inbox_table()
        aid = "code:%s:%s:%s" % (str(q.get("host") or ""), str(q.get("source") or ""), str(q.get("session_id") or ""))
        rows = self.store.rows("SELECT * FROM coding_inbox WHERE agent_id=? AND delivered_at IS NULL ORDER BY created_at", (aid,))
        for r in rows:
            self.store.exec("UPDATE coding_inbox SET delivered_at=?, via=? WHERE id=?", (time.time(), str(q.get("via") or "hook")[:20], r["id"]))
        return {"ok": True, "messages": [{"from": r["sender"], "text": r["text"]} for r in rows]}

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
            send_push(self.store, title, body[:300], category="CODING_APPROVAL" if actionable else None,
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

    APPROVAL_RX = re.compile(r"Do you want|Would you like|Allow (once|always|this)|Yes, proceed|approv|\(y/n\)|\[y/N\]|Permission required", re.I)

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
    r = store.one("SELECT COUNT(*) AS n FROM coding_agents WHERE kind<>'subagent' AND ((status='running' AND last_activity>?) OR (status='waiting' AND last_activity>?))",
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
        if r["live"] and r["kind"] != "subagent":    # subagents are counted on their run, never listed as runs
            runs.append({"id": r["id"], "title": r["title"], "kind": "coding", "detail": (r.get("progress") or "")[:120], "queued": False,
                         "subagents": r.get("subagent_live", 0), "waiting": "Needs you" in (r.get("progress") or "")[:10]})
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
            try:                   # Night Shift scheduler + the nightly memory consolidation
                h.night_tick()
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
