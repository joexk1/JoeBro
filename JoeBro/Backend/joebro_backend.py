"""JoeBro backend — entry point. Shared code: jb_core; feature
methods: the jb_* mixins composed into Handler below."""
from jb_core import *  # noqa: F401,F403
from jb_chat import ChatMixin
from jb_email import _bridge_types
from jb_tools import ToolsMixin
from jb_email import EmailMixin
from jb_docs import DocsMixin
from jb_calendar import CalendarMixin
from jb_models import ModelsMixin
from jb_assistant import AssistantMixin
from jb_memory import MemoryMixin
from jb_files import FilesMixin
from jb_agents import AgentsMixin
from jb_fleet import FleetMixin, pulse_loop
from jb_briefing import BriefingMixin, briefing_loop
from jb_autonomy import AutonomyMixin
from jb_reach import ReachMixin
from jb_spend import SpendMixin
from jb_updates import PersonalUpdatesMixin, ensure_tables, claim_scheduled_run, finish_scheduled_run, personal_reply_loop
from jb_personal import PersonalMixin, reminder_loop
import jb_usermem
from jb_muse import MuseMixin, muse_name
from jb_ptasks import PersonalTasksMixin
from jb_heartbeat import HeartbeatMixin
from jb_location import LocationMixin
from jb_prompt import PromptMixin
from jb_mobile import MobileMixin, lan_denied, clamp_form, start_lan_if_enabled, LAN
from jb_push import register_device, push_status, PushError, register_activity, list_devices, remove_device
from jb_janitor import janitor_loop
from jb_xlsx import xlsx_to_csv, csv_to_xlsx
from jb_proactive import ProactiveMixin, proactive_loop
from jb_desktop import DesktopUseMixin
from jb_coding import CodingAgentsMixin, coding_loop
from jb_plugins import PluginsMixin, PluginError
from jb_nightshift import NightShiftMixin
import jb_nightshift
import jb_sharedmem
import jb_ext
import traceback
import gzip
import hmac

jb_ext.load()      # private add-ons, if any: after the core modules, before Handler


# Prefs that must never leave the process in clear — see the /api/prefs handler.
SECRET_PREFS = {"bot_token"}
SECRET_MASK = "••••••••"
_AUTH_FAILS = {"n": 0}      # a count in memory only: how many bad tokens this run has seen


class Handler(*jb_ext.mixins(), PersonalUpdatesMixin, BriefingMixin, AutonomyMixin, SpendMixin, ReachMixin, PromptMixin, ChatMixin, ToolsMixin, EmailMixin, DocsMixin, CalendarMixin, ModelsMixin,
              AssistantMixin, MemoryMixin, FilesMixin, FleetMixin, PersonalMixin, MuseMixin,
              PersonalTasksMixin, AgentsMixin, HeartbeatMixin, LocationMixin, MobileMixin,
              ProactiveMixin, DesktopUseMixin, CodingAgentsMixin, PluginsMixin, NightShiftMixin,
              BaseHTTPRequestHandler):
    server_version = "JoeBroLocal/1.0"

    @property
    def store(self) -> Store:
        return self.server.store

    def log_message(self, fmt, *args):
        if os.environ.get("JOEBRO_BACKEND_LOGS") == "1":
            super().log_message(fmt, *args)

    def _path(self):
        return urllib.parse.urlparse(self.path)

    def _query(self):
        return dict(urllib.parse.parse_qsl(self._path().query))

    def _json_or_form_body(self):
        raw = self._body_bytes()
        if not raw:
            return {}
        if "application/json" in (self.headers.get("content-type") or "").lower():
            return json.loads(raw.decode("utf-8"))
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"))
        return {k: v[-1] for k, v in parsed.items()}

    def _body_bytes(self):
        n = int(self.headers.get("content-length") or 0)
        self._body_drained = True
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._body_bytes()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _form_body(self):
        raw = self._body_bytes().decode("utf-8")
        # keep_blank_values so an explicitly-empty field (e.g. folder= to remove a
        # chat from its project folder) arrives as "" instead of being dropped.
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    def _send(self, status=200, ctype="application/json", encoding=None):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Content-Type", ctype)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()

    def json(self, obj, status=200):
        if not getattr(self, "_body_drained", False):
            self._body_drained = True       # don't reset a client that is still sending
            try:
                n = min(int(self.headers.get("content-length") or 0), 1 << 20)
                if n:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # gzip larger payloads — chat history over Tailscale was ~1MB/10s
        # uncompressed; gzip cuts the transfer several-fold. URLSession sends
        # Accept-Encoding: gzip and decompresses transparently.
        if len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            self._send(status, encoding="gzip")
        else:
            self._send(status)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _trim_for_history(meta):
        """Shrink the bulky, collapsed secondary detail in a history reload —
        tool outputs and thinking — to previews. The live run already showed them
        in full; trimming keeps reopening a long chat fast over a slow link."""
        if not isinstance(meta, dict):
            return meta
        think = meta.get("thinking")
        if isinstance(think, str) and len(think) > 800:
            meta["thinking"] = think[:800] + " … (truncated)"
        evs = meta.get("tool_events")
        if isinstance(evs, list):
            for e in evs:
                out = e.get("output")
                if isinstance(out, str) and len(out) > 1500:
                    e["output"] = out[:1500] + " … (truncated)"
        return meta

    def not_found(self):
        self.json({"detail": "Not found"}, 404)

    def _refuse(self, payload, code):
        self.json(payload, code)

    def _guard(self):
        """Refuse requests a browser made on someone else's behalf.

        This API runs shell commands and writes files with no login, so any page
        the user visits could POST a form at it — a form POST is a CORS "simple
        request", so the browser sends it and only hides the *reply*, which an
        attacker doesn't need. Every browser marks such a request with `Origin`
        or `Sec-Fetch-Site`; the app's URLSession sends neither, so rejecting
        both costs our clients nothing. The Host check blocks DNS rebinding
        (an attacker's domain re-pointed at 127.0.0.1 to become same-origin)."""
        if self.headers.get("Origin") or (self.headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
            self._refuse({"detail": "cross-site request blocked"}, 403)
            return False
        hosts = getattr(self.server, "allowed_hosts", None)
        if hosts and (self.headers.get("Host") or "").lower() not in hosts:
            self._refuse({"detail": "unexpected Host header"}, 403)
            return False
        # Bearer auth: always on for the phone listener, opt-in (JOEBRO_TOKEN) otherwise.
        token = getattr(self.server, "auth_token", "")
        # /api/health is the only unauthenticated route (liveness probes).
        if token and self.command == "GET" and self.path.split("?", 1)[0] == "/api/health":
            return True
        # Compare bytes: a str compare raises TypeError on a non-ASCII header (a 500).
        if token and not hmac.compare_digest((self.headers.get("Authorization") or "").encode(), ("Bearer " + token).encode()):
            _AUTH_FAILS["n"] += 1
            time.sleep(0.3)         # makes guessing a token slow
            self._refuse({"detail": "unauthorized"}, 401)
            return False
        # What the phone listener refuses even with a valid token (see jb_mobile).
        if getattr(self.server, "is_lan", False):
            denied = lan_denied(self.command, self._path().path)
            if denied:
                self._refuse({"detail": denied[1]}, denied[0])
                return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        if not self._guard():
            return
        path = self._path().path
        if jb_ext.route(self, "GET", path, self._query()):
            return
        path = self._path().path          # an extension may have rewritten an alias to its canonical route
        if jb_sharedmem.route(self, "GET", path, self._query()) or jb_nightshift.route(self, "GET", path, self._query()):
            return
        q = self._query()
        if path in ("/api/ping", "/api/health"):
            return self.json({"ok": True, "local": True})
        if path == "/api/mobile/hello":
            return self.json(self.mobile_hello())
        if path == "/api/mobile":
            return self.json(self.mobile_status())
        if path == "/api/mobile/devices":
            return self.json({"devices": list_devices(self.store)})
        if path == "/api/push/status":
            return self.json(push_status(self.store))
        if path == "/api/muse":
            return self.json({"ok": True, "files": self.muse_list()})
        if path.startswith("/api/muse/"):
            try:
                text = self.muse_read(path.split("/")[3])
            except ValueError as e:
                return self.json({"ok": False, "detail": str(e)}, 400)
            if text is None:
                return self.json({"ok": False, "detail": "memory file not found"}, 404)
            return self.json({"ok": True, "name": path.split("/")[3], "content": text})
        if path == "/api/joebro":
            return self.json(self.phone_payload())
        if path == "/api/inbox":
            return self.json({"inbox": self.inbox_rows(unread_only=q.get("unread") == "1")})
        if path == "/api/pulse":
            return self.json({"pulse": self.pulse_config()})
        if path == "/api/heartbeat":
            out = self.heartbeat_config()
            try:      # what the phone's heartbeat card also shows: proactive mode + recent suggested actions
                pro = self.proactive_status()
                out["proactive_mode"] = pro["mode"]
                out["suggestions"] = [a for a in pro["recent_actions"] if a.get("status") == "suggested"]
            except Exception:
                pass
            return self.json(out)
        if path == "/api/agents":
            payload = self.agents_payload(
                kind=(q.get("kind") or "").strip(), status=(q.get("status") or "").strip(),
                limit=int(q.get("limit") or 100))
            try:   # chat turns live in this process: lets a client drop a turn whose stream it lost
                from jb_chat import _ACTIVE_TURNS
                payload["active_turns"] = sorted(_ACTIVE_TURNS)
            except Exception:
                pass
            # Opt-in, so clients that collect coding agents themselves never double-count.
            if "coding" in (q.get("include") or "").split(",") and (q.get("kind") or "coding") == "coding":
                try:
                    coding = self.coding_agent_records()
                except Exception:
                    coding = []
                payload["agents"] = coding + payload.get("agents", [])
                payload["count"] = payload.get("count", 0) + len(coding)
                live = sum(1 for r in coding if r.get("live") and r["kind"] == "coding")
                for k in ("active", "live"):
                    if k in payload:
                        payload[k] += live
                payload.setdefault("by_kind", {})["coding"] = sum(1 for r in coding if r["kind"] == "coding")
                payload["by_kind"]["subagent"] = sum(1 for r in coding if r["kind"] == "subagent")
            return self.json(payload)
        if path == "/api/coding-agents":
            if (q.get("tree") or "") in ("1", "true"):
                return self.json({"ok": True, "nodes": self.coding_nodes(), "tree": self.coding_nodes(tree=True)})
            return self.json({"ok": True, "nodes": self.coding_nodes()})
        if path == "/api/coding-agents/inbox":
            return self.json(self.coding_inbox_pull(q))
        if path.startswith("/api/coding-agents/") and path.endswith("/screen"):
            try:
                return self.json(self.coding_screen(urllib.parse.unquote(path.split("/")[3]), q.get("lines") or 60))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/proactive":
            return self.json(self.proactive_status())
        if path == "/api/shopping":
            return self.json(self.shopping_items())
        if path == "/api/personal/health":
            return self.json({"ok": True, "summary": self.health_summary(int(q.get("days") or 14)),
                              "days": self.health_rows(int(q.get("days") or 14))})
        if path.startswith("/api/agents/code:"):
            rec = self.coding_agent_detail(path[len("/api/agents/"):].strip("/"))
            return self.json({"ok": True, "agent": rec} if rec else {"ok": False, "detail": "no such agent"}, 200 if rec else 404)
        # Exact match above, so this only catches /api/agents/<id>: the detail
        # view, with the FULL result text and the session_id to open its transcript.
        if path.startswith("/api/agents/"):
            aid = path[len("/api/agents/"):].strip("/")
            row = self.store.one("select * from agents where id=?", (aid,))
            if not row:
                return self.json({"ok": False, "detail": "no such agent"}, 404)
            rec = self.agent_record(row, full=True)
            rec["messages"] = self.store.rows(
                "select role, content, created_at from messages where session_id=? order by id",
                (rec["session_id"],)) if rec["session_id"] else []
            return self.json({"ok": True, "agent": rec})
        if path == "/api/reminders":
            return self.json({"reminders": self.store.rows(
                "select * from reminders where status='pending' order by due_at")})
        if path == "/api/plan":
            sid = q.get("session") or ""
            return self.json({"session": sid, "steps": self.plan_get(sid)})
        if path == "/api/fs/browse":
            if str(self.pref("mobile_allow_browse")).lower() in ("0", "false", "no", "off") and getattr(self.server, "is_lan", False):
                return self.json({"detail": "folder browsing from the phone is off"}, 403)
            return self.browse_host(q)
        if path == "/api/sessions":
            # bot_tg_* are the Telegram bot's own per-chat sessions and ptask_* the phone
            # agent's silent task runs: never shown in the sidebar. The phone chat
            # itself is hidden unless the user asked to see it (show_phone_session).
            show_phone = str(self.pref("show_phone_session")).lower() in ("1", "true", "yes", "on")
            rows = self.store.rows(
                "select id,name,model,mode,archived,is_important,host_id,workdir,folder,sort_order,created_at from sessions "
                "where id not like 'bot\\_tg\\_%' escape '\\' and id not like 'task\\_session\\_ptask\\_%' escape '\\' "
                "and (id != ? or ?) order by coalesce(sort_order, -julianday(created_at)) asc",
                (PHONE_SESSION_ID, 1 if show_phone else 0))
            device = (q.get("device") or q.get("host_id") or "").strip()
            if device:          # a client on a linked device sees its own chats plus this host's
                rows = [r for r in rows if (r.get("host_id") or "") in ({""} | LOCAL_HOST_IDS | {device})]
            return self.json([self.session_json(r) for r in rows])
        if path.startswith("/api/session/") and path.endswith("/tip"):
            # Cheap freshness probe for the open chat — the app polls this and
            # only reloads full history when last_id changes.
            sid = path.split("/")[3]
            row = self.store.one("select max(id) m from messages where session_id=?", (sid,))
            sess = self.store.one("select mode, model from sessions where id=?", (sid,)) or {}
            return self.json({"last_id": int((row or {}).get("m") or 0),
                              "mode": sess.get("mode") or "chat", "model": sess.get("model") or ""})
        if path.startswith("/api/history/"):
            sid = path.rsplit("/", 1)[-1]
            session = self.store.one("select * from sessions where id=?", (sid,))
            msgs = self.store.rows(
                "select id,role,content,metadata from messages where session_id=? order by id", (sid,)
            )
            return self.json({
                "history": [
                    {"role": m["role"], "content": m["content"],
                     "metadata": self._trim_for_history(self.loads(m["metadata"])) | {"_db_id": m["id"]}}
                    for m in msgs
                ],
                "model": (session or {}).get("model"),
                "name": (session or {}).get("name"),
                "mode": (session or {}).get("mode") or "chat",
                "permission_mode": (session or {}).get("permission_mode") or "sandbox",
                "endpoint_id": (session or {}).get("endpoint_id") or "",
                "endpoint_url": (session or {}).get("endpoint_url") or "",
            })
        if path == "/api/spend":
            return self.json(self.spend_payload(q.get("days") or 30, str(q.get("refresh") or "") in ("1", "true")))
        if path in ("/api/endpoints", "/api/model-endpoints/config"):
            return self.json({"endpoints": self.endpoint_config_rows(q)})
        if path in ("/api/client-settings", "/api/settings/sync"):
            return self.json({"ok": True, "settings": self.client_settings()})
        if path in ("/api/briefing", "/api/briefing/latest"):
            return self.json(self.latest_briefing_payload())
        if path == "/api/devices":
            rows = self.store.rows("select * from devices order by is_self desc, name")
            return self.json({"devices": [{
                "id": r["id"], "name": r["name"], "is_self": bool(r.get("is_self")),
                "remote": bool((r.get("bridge_url") or "").strip()) and not r.get("is_self"),
            } for r in rows]})
        if path.startswith("/api/email/attachment/"):
            uid = path.rsplit("/", 1)[-1]
            try:
                data, mime, filename = self.email_attachment(uid, q.get("index"), q.get("folder") or "INBOX")
                encoded = urllib.parse.quote(str(filename or "attachment"), safe="")
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + encoded)
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                return self.json({"detail": str(exc)}, 400)
            return
        if path == "/api/models":
            refresh = str(q.get("refresh") or "").lower() in ("1", "true", "yes")
            return self.json({"models": [], "items": self.model_items(refresh=refresh)})
        if path == "/api/model-endpoints":
            eps = self.store.rows("select id,name,base_url,is_enabled from endpoints order by created_at")
            return self.json([
                {"id": e["id"], "name": e["name"], "base_url": e["base_url"], "model_type": "local", "is_enabled": bool(e["is_enabled"])}
                for e in eps
            ])
        if path.startswith("/api/model-endpoints/") and path.endswith("/models"):
            ep_id = path.split("/")[-2]
            return self.json(self.endpoint_models(ep_id))
        if path == "/api/prefs":
            # Prefs is a bulk dump, so it also carried the Telegram bot token and
            # the CalDAV password in clear. Hand back a mask instead: the UI only
            # needs to show that a secret is set, and PUT ignores the mask so a
            # form that round-trips it can't wipe the real value.
            out = {}
            for r in self.store.rows("select key,value from prefs"):
                v = self.loads(r["value"])
                if r["key"] in SECRET_PREFS and v:
                    v = SECRET_MASK
                elif r["key"] == "calendar_connection" and isinstance(v, dict) and v.get("password"):
                    v = dict(v, password=SECRET_MASK)
                out[r["key"]] = v
            return self.json(out)
        if path == "/api/email/status":
            sources = self.email_sources()
            first = sources[0] if sources else {}
            return self.json({
                "configured": bool(sources),
                "type": first.get("type") or "",
                "display": first.get("display") or "",
                "sources": [self.email_source_json(a) for a in sources],
            })
        if path == "/api/calendar/status":
            cfg = self.pref("calendar_connection") or {}
            return self.json({
                "configured": bool(cfg.get("type")),
                "type": cfg.get("type") or "",
                "display": cfg.get("display") or "",
                "macos_authorized": bool(cfg.get("macos_authorized")),
            })
        if path == "/api/memory":
            return self.json({"memory": [self.memory_json(r) for r in jb_usermem.mem_rows(self.store)]})
        if path == "/api/calendar/events":
            return self.calendar_events(q)
        if path == "/api/documents/library":
            return self.documents_library(q)
        if path.startswith("/api/document/") and path.endswith("/export-pdf"):
            return self.export_document(path.split("/")[3])
        if path.startswith("/api/document/"):
            return self.get_document(path.rsplit("/", 1)[-1])
        if path.startswith("/api/upload/"):
            return self.serve_upload(path.rsplit("/", 1)[-1])
        if path.startswith("/api/session/") and "/workdir" in path:
            return self.workdir_get(path, q)
        if path == "/api/email/list":
            return self.email_list(q)
        if path == "/api/email/folders":
            return self.email_folders()
        if path.startswith("/api/email/read/"):
            return self.email_read(path.rsplit("/", 1)[-1])
        if path == "/api/research/active":
            rows = self.store.rows("select id,query from research where archived=0 and status in ('queued','running') order by datetime(created_at) desc")
            return self.json({"active": [{"session_id": r["id"], "id": r["id"], "query": r["query"]} for r in rows]})
        if path == "/api/research/library":
            rows = self.store.rows("select * from research where archived=0 order by datetime(created_at) desc")
            items = [self.research_json(r) for r in rows]
            return self.json({"research": items, "items": items})
        if path.startswith("/api/research/status/"):
            row = self.store.one("select * from research where id=?", (path.rsplit("/", 1)[-1],))
            if not row:
                return self.json({"status": "not_found", "progress": {}}, 404)
            status = row.get("status") or "running"
            # Honest progress: report whatever phase the worker actually reached,
            # not a fixed duration it can't promise.
            prog = self.loads(row.get("progress")) or {}
            if not prog.get("phase"):
                prog = {"phase": ("Complete" if status == "completed" else
                                  "Cancelled" if status == "cancelled" else "Searching the web"),
                        "message": row.get("query") or ""}
            return self.json({"status": status, "progress": prog})
        if path.startswith("/api/research/detail/"):
            row = self.store.one("select * from research where id=?", (path.rsplit("/", 1)[-1],))
            if not row:
                return self.json({"detail": "not found"}, 404)
            d = self.research_json(row)
            # Inline images go into the RENDERED report only — the .md download
            # (raw content) stays image-free.
            d["report"] = self._report_with_images(row.get("content") or "", self.loads(row.get("images")) or [])
            return self.json(d)
        if path == "/api/notes":
            return self.json({"notes": []})
        if path == "/api/tasks":
            return self.json({"tasks": self.tasks_for_scope(q.get("scope"))})   # default general; ?scope=personal
        if path == "/api/skills/learn-status":
            return self.json(self.learn_skills_status(q.get("job") or ""))
        if path == "/api/skills":
            rows = self.store.rows("select * from skills order by datetime(created_at) desc")
            return self.json({"skills": [self.skill_json(r) for r in rows]})
        if path == "/api/tools":
            rows = self.store.rows("select * from api_tools order by datetime(created_at) desc")
            return self.json({"tools": [self.api_tool_json(r) for r in rows]})
        if path == "/api/mcp":
            # plugin-owned servers are managed (and switched) through their plugin
            rows = self.store.rows("select * from mcp_servers where id not like 'mcp\\_plugin\\_%' escape '\\' order by datetime(created_at) desc")
            return self.json({"servers": [self.mcp_server_json(r) for r in rows]})
        if path == "/api/plugins":
            return self.json(self.plugins_list())
        if path == "/api/plugins/spec":
            return self.json({"ok": True, "schema": "joebro.plugin/1", "spec": self.plugin_spec()})
        if path.startswith("/api/plugins/") and path.endswith("/source"):
            try:
                return self.json(self.plugin_source(urllib.parse.unquote(path.split("/")[3])))
            except PluginError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 404)
        if path.startswith("/api/plugins/") and path.split("/")[4:5] == ["ui"]:
            parts = path.split("/")
            got = self.plugin_ui_file(urllib.parse.unquote(parts[3]), urllib.parse.unquote("/".join(parts[5:])))
            if not got:
                return self.json({"ok": False, "detail": "not found (or plugin is off)"}, 404)
            data, mime = got
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline' data: blob: joebro-plugin:; connect-src 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return None
        return self.not_found()

    def do_POST(self):
        if not self._guard():
            return
        path = self._path().path
        if jb_ext.route(self, "POST", path, self._query()):
            return
        path = self._path().path          # an extension may have rewritten an alias to its canonical route
        if jb_sharedmem.route(self, "POST", path, self._query()) or jb_nightshift.route(self, "POST", path, self._query()):
            return
        if path == "/api/mobile/enable":
            out, code = self.mobile_enable(self._json_body() or {})
            return self.json(out, code)
        if path == "/api/mobile/rotate":
            return self.json(self.mobile_rotate())
        if path == "/api/mobile/apns":
            out, code = self.mobile_apns_save()
            return self.json(out, code)
        if path == "/api/personal/location":
            try:
                return self.json(self.location_post(self._json_body()))
            except (KeyError, ValueError, TypeError):
                return self.json({"ok": False, "detail": "lat and lon required"}, 400)
        if path in ("/api/push/activity", "/api/push/register_activity"):
            try:
                return self.json(register_activity(self.store, self._json_body()))
            except (PushError, ValueError, TypeError):
                return self.json({"ok": False, "detail": "Invalid activity token"}, 400)
        if path == "/api/push/register":
            try:
                return self.json(register_device(self.store, self._json_body()))
            except (PushError, ValueError, TypeError) as exc:
                return self.json({"ok": False, "detail": str(exc) if isinstance(exc, PushError) else "Invalid push registration"}, 400)
        if path == "/api/joebro/session":
            return self.json(self.phone_payload(create=True))
        if path in ("/api/joebro/compact", "/api/joebro/clear"):
            if self.phone_busy():
                return self.json({"detail": "Wait for JoeBro to finish this reply."}, 409)
            if path.endswith("/compact"):
                return self.json(self.manual_compact(PHONE_SESSION_ID))
            self.store.exec("delete from messages where session_id=?", (PHONE_SESSION_ID,))
            self.store.exec("delete from prefs where key=?", ("compact_" + PHONE_SESSION_ID,))
            return self.json({"ok": True})
        if path.startswith("/api/session/" + PHONE_SESSION_ID + "/"):
            return self.json({"detail": "The phone chat is managed by the server"}, 403)
        if path == "/api/inbox/read":
            return self.json(self.mark_inbox_read((self._json_body() or {}).get("id")))
        if path == "/api/inbox/delete":
            b = self._json_body() or {}
            ids = b.get("ids", [])
            if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(n, str) or len(n) > 100 for n in ids):
                return self.json({"ok": False, "detail": "Invalid notification selection"}, 400)
            if b.get("all") is True:
                self.store.exec("delete from inbox")
            elif ids:
                self.store.exec("delete from inbox where id in (" + ",".join("?" for _ in ids) + ")", tuple(ids))
            return self.json({"ok": True})
        if path.startswith("/api/inbox/delete/"):
            self.store.exec("delete from inbox where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path == "/api/notify":
            b = self._json_body() or {}
            try:
                return self.json(self.notify(b.get("message") or b.get("body") or "", b.get("title") or "",
                                             kind=b.get("kind") or "note", channel=b.get("channel") or "auto"))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/heartbeat":
            try:
                return self.json(self.heartbeat_control(self._json_body() or {}))
            except (ValueError, TypeError) as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/pulse":
            b = self._json_body() or {}
            if (b.get("action") or "set").lower() == "run":
                return self.json(self.run_pulse(force=True))
            for key in ("enabled", "interval_minutes", "checklist", "quiet_hours", "model", "endpoint_id"):
                if key in b:
                    self.set_pref("pulse_" + key, b[key])
            return self.json({"ok": True, "pulse": self.pulse_config()})
        if path == "/api/agents":
            try:
                self._turn_form = {"session": PHONE_SESSION_ID} if getattr(self.server, "is_lan", False) else {}
                return self.json({"ok": True, "agents": self.spawn_agents(self._json_body() or {})})
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        # /api/agents/<id>/cancel and /stop: checked before any bare /api/agents/
        # prefix so the suffix can't be swallowed.
        if path.startswith("/api/agents/") and path.rsplit("/", 1)[-1] in ("cancel", "stop"):
            out, code = self.agent_cancel(path.split("/")[3])
            return self.json(out, code)
        if path.startswith("/api/chat/stop/"):
            out, code = self.cancel_session_run(path[len("/api/chat/stop/"):].strip("/"))
            return self.json(out, code)
        if path == "/api/reminders":
            try:
                return self.json(json.loads(self.remind_me_tool(json.dumps(self._json_body() or {}))))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/muse":
            b = self._json_body() or {}
            try:
                n = muse_name(b.get("name"))
                if self.muse_read(n) is not None:
                    return self.json({"ok": False, "detail": "that memory file already exists"}, 409)
                self.muse_write(n, b.get("content") or "# " + n.replace("-", " ").title() + "\n")
            except ValueError as e:
                return self.json({"ok": False, "detail": str(e)}, 400)
            return self.json({"ok": True, "name": n})
        if path == "/api/plan":
            b = self._json_body() or {}
            self._plan_session = b.get("session") or ""
            try:
                return self.json(json.loads(self.manage_plan_tool(json.dumps(b))))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/tasks/starters":
            try:
                return self.json(self.ptask_starter((self._json_body() or {}).get("id")))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/session":
            f = self._form_body()
            sid = "s_" + os.urandom(8).hex()
            self.store.exec(
                "insert into sessions(id,name,model,endpoint_url,endpoint_id,mode,created_at) values(?,?,?,?,?,?,?)",
                (sid, f.get("name") or "New Chat", f.get("model") or "", f.get("endpoint_url") or "",
                 f.get("endpoint_id") or "", "chat", now_iso()),
            )
            # New chats start at the top of the list (smallest sort_order).
            self.store.exec("update sessions set sort_order=-julianday(created_at) where id=? and sort_order is null", (sid,))
            return self.json(self.session_json(self.store.one("select * from sessions where id=?", (sid,))))
        if path.startswith("/api/permission/"):
            rid = path.rsplit("/", 1)[-1]
            decision = (self._json_body().get("decision") or "deny").lower()
            with _PERMISSION_GUARD:
                entry = _PERMISSION_REGISTRY.get(rid)
                if entry:
                    entry["decision"] = decision
                    entry["event"].set()
            return self.json({"ok": True})
        if path.startswith("/api/flashcards/to_text"):
            # apkg bytes in -> Q/A text out.
            from jb_apkg import apkg_to_text
            self._send(200, "text/plain")
            self.wfile.write(apkg_to_text(self._body_bytes()).encode("utf-8"))
            return
        if path.startswith("/api/flashcards/to_apkg"):
            # Q/A text in -> apkg bytes out (?name= names the deck).
            from urllib.parse import parse_qs
            from jb_apkg import text_to_apkg
            name = (parse_qs(self._path().query).get("name") or ["Flashcards"])[0]
            self._send(200, "application/octet-stream")
            self.wfile.write(text_to_apkg(self._body_bytes().decode("utf-8"), name))
            return
        if path == "/api/spreadsheet/to_csv":
            # xlsx bytes in -> CSV text out (first sheet, values only).
            self._send(200, "text/csv")
            self.wfile.write(xlsx_to_csv(self._body_bytes()).encode("utf-8"))
            return
        if path == "/api/spreadsheet/to_xlsx":
            # CSV text in -> xlsx bytes out.
            self._send(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.wfile.write(csv_to_xlsx(self._body_bytes().decode("utf-8")))
            return
        if path == "/api/chat_stream":
            return self.chat_stream()
        if path == "/api/chat":
            # Background AI jobs (skill/task generators) post message+session
            # only — the session row carries which model to use.
            f = self._form_body()
            if getattr(self.server, "is_lan", False):
                clamp_form(f, str(self.pref("mobile_allow_full")).lower() in ("1", "true", "yes", "on"))
            sess = self.store.one("select * from sessions where id=?", (f.get("session") or "",)) or {}
            f.setdefault("model", sess.get("model") or "")
            f.setdefault("endpoint_id", sess.get("endpoint_id") or "")
            return self.json({"response": self.chat_reply_with_tools(f)})
        if path.endswith("/move") and path.startswith("/api/session/"):
            # Drag-to-reorder / drag into-or-out-of a project folder. Sets the
            # chat's folder and/or its sidebar sort_order (kept numeric).
            sid = path.split("/")[3]
            f = self._form_body()
            sets, vals = [], []
            if "folder" in f:
                sets.append("folder=?"); vals.append((f.get("folder") or "").strip() or None)
            so = f.get("sort_order")
            if so not in (None, ""):
                try:
                    sets.append("sort_order=?"); vals.append(float(so))
                except ValueError:
                    pass
            if sets:
                vals.append(sid)
                self.store.exec(f"update sessions set {','.join(sets)} where id=?", vals)
            return self.json({"ok": True})
        if path == "/api/folder/rename":
            # Rename a project folder = re-tag every chat under the old name.
            f = self._form_body()
            old = (f.get("old") or "").strip()
            new = (f.get("new") or "").strip()
            if old and new and old != new:
                self.store.exec("update sessions set folder=? where folder=?", (new, old))
            return self.json({"ok": True})
        if path.endswith("/message") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            body = self._json_body()
            self.add_message(sid, body.get("role", "assistant"), body.get("content", ""), body.get("metadata") or {})
            return self.json({"ok": True})
        if path.endswith("/important") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            val = 1 if self._form_body().get("important") == "true" else 0
            self.store.exec("update sessions set is_important=? where id=?", (val, sid))
            return self.json({"ok": True})
        if path.endswith("/archive") and path.startswith("/api/session/"):
            sid = path.split("/")[3]
            self.store.exec("update sessions set archived=1 where id=?", (sid,))
            return self.json({"ok": True})
        if path.endswith("/compact") and path.startswith("/api/session/"):
            return self.json(self.manual_compact(path.split("/")[3]))
        if path.endswith("/delete-messages") and path.startswith("/api/session/"):
            body = self._json_body()
            for mid in body.get("msg_ids", []):
                self.store.exec("delete from messages where id=?", (str(mid),))
            return self.json({"ok": True})
        if path.endswith("/mark-stopped"):
            return self.json({"ok": True})
        if path in ("/api/model-endpoints", "/api/endpoints"):
            f = self._json_or_form_body()
            ep_id = "ep_" + os.urandom(5).hex()
            self.store.exec(
                "insert into endpoints(id,name,base_url,api_key,is_enabled,created_at) values(?,?,?,?,?,?)",
                (ep_id, f.get("name") or "Local endpoint", f.get("base_url") or f.get("url") or "",
                 f.get("api_key") or "", 1 if self.bool_body(f, "is_enabled", self.bool_body(f, "enabled", True)) else 0, now_iso()),
            )
            return self.json({"ok": True, "id": ep_id,
                              "endpoint": self.endpoint_config_json(self.store.one("select * from endpoints where id=?", (ep_id,)), include_key=True)})
        if path == "/api/joebro/reply" or path == "/api/phone/reply":
            try:
                return self.json(self.enqueue_personal_reply(self._json_body()), 202)
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path in ("/api/briefing/run", "/api/briefing"):
            return self.json(self.run_briefing(force=True))
        if path == "/api/briefing/schedule":
            b = self._json_body() or {}
            for key in ("enabled", "interval_hours", "quiet_hours", "notify"):
                if key in b:
                    self.set_pref("briefing_" + key, b[key])
            return self.json({"ok": True, "schedule": self.briefing_config()})
        if path == "/api/calendar/pending":
            return self.calendar_pending(self._json_body())
        if path == "/api/calendar/mirror":
            return self.sync_calendar_mirror(self._json_body())
        if path == "/api/email/connect":
            body = self._json_body()
            account = self.email_account_from_login(body.get("email") or "", body.get("password") or "")
            if account["type"] in _bridge_types():
                err = self.ensure_mail_bridge()
                if err:
                    return self.json({"ok": False, "error": err}, 400)
            account_id = self.upsert_email_account(account["type"], account)
            return self.json({"ok": True, "configured": True, "type": account["type"], "id": account_id})
        if path == "/api/email/connect-imap":
            body = self._json_body()
            self.upsert_email_account("imap", body)
            return self.json({"ok": True, "configured": True, "type": "imap"})
        if path == "/api/email/disconnect":
            account_id = (self._query().get("id") or "").strip()
            if account_id:
                self.store.exec("delete from email_sources where id=?", (account_id,))
            else:
                self.store.exec("delete from email_sources")
            return self.json({"ok": True, "configured": False})
        if path == "/api/calendar/connect-caldav":
            body = self._json_body()
            cfg = {
                "type": "caldav",
                "display": body.get("principal") or body.get("url") or "CalDAV source",
                "url": body.get("url") or "",
                "principal": body.get("principal") or "",
                "password": body.get("password") or "",
                "calendar_names": body.get("calendar_names") or [],
                "updated_at": now_iso(),
            }
            try:
                self.validate_caldav(cfg)
            except Exception as exc:
                return self.json({"detail": f"CalDAV connection failed: {exc}"}, 400)
            self.set_pref("calendar_connection", cfg)
            return self.json({"ok": True, "configured": True, "type": "caldav"})
        if path == "/api/calendar/connect-macos":
            body = self._json_body()
            cfg = {
                "type": "macos",
                "display": "macOS Calendar",
                "macos_authorized": bool(body.get("authorized")),
                "updated_at": now_iso(),
            }
            self.set_pref("calendar_connection", cfg)
            return self.json({"ok": True, "configured": True, "type": "macos"})
        if path == "/api/calendar/disconnect":
            self.set_pref("calendar_connection", {})
            return self.json({"ok": True, "configured": False})
        if path == "/api/memory/add":
            return self.json(self.upsert_memory(self._json_body()))
        if path == "/api/document":
            return self.create_document(self._json_body())
        if path.endswith("/pin") and path.startswith("/api/memory/"):
            mid = path.split("/")[3]
            jb_usermem.mem_pin(self.store, mid)
            return self.json({"ok": True})
        if path == "/api/calendar/events":
            return self.create_event(self._json_body())
        if path == "/api/calendar/quick-parse":
            return self.json({"ok": True, "event": self.parse_natural_calendar_event(self._json_body().get("text", "New event"))})
        if path == "/api/upload":
            return self.handle_upload("files")
        if path == "/api/email/compose-upload":
            return self.handle_upload("file", for_email=True)
        if path == "/api/email/send":
            return self.email_send(self._json_body())
        if path.startswith("/api/email/mark-read/"):
            return self.email_set_read(path.rsplit("/", 1)[-1], True)
        if path.startswith("/api/email/mark-unread/"):
            return self.email_set_read(path.rsplit("/", 1)[-1], False)
        if path.startswith("/api/email/archive/"):
            return self.email_archive(path.rsplit("/", 1)[-1])
        if path.startswith("/api/session/") and "/workdir" in path:
            return self.workdir_post(path)
        if path == "/api/stt/transcribe":
            return self.json({"text": ""})
        if path == "/api/ai-check":
            return self.ai_check(self._json_body())
        if path == "/api/research/start":
            body = self._json_body()
            q = body.get("query") or body.get("topic") or "Untitled research"
            rid = self.create_research(q, body.get("endpoint_id"), body.get("model"))
            return self.json({"ok": True, "session_id": rid, "id": rid,
                              "active": [{"session_id": rid, "id": rid, "query": q}]})
        if path.startswith("/api/research/cancel/"):
            return self.cancel_research(path.rsplit("/", 1)[-1])
        if path.startswith("/api/research/"):
            return self.json({"ok": True})
        if path.startswith("/api/email/"):
            return self.json({"success": True, "summary": "", "reply": ""})
        if path.startswith("/api/tasks/") and path.rsplit("/", 1)[-1] in ("pause", "resume"):
            parts = path.split("/")
            tid = parts[3] if len(parts) > 3 else ""
            action = parts[4] if len(parts) > 4 else ""
            status = "paused" if action == "pause" else "active"
            self.store.exec("update tasks set status=?, updated_at=? where id=?", (status, now_iso(), tid))
            row = self.store.one("select * from tasks where id=?", (tid,))
            return self.json({"ok": bool(row), "task": self.task_json(row) if row else None})
        if path.startswith("/api/tasks/") and path.endswith("/run"):
            tid = path.split("/")[3] if len(path.split("/")) > 3 else ""
            if not self.store.one("select id from tasks where id=?", (tid,)):
                return self.json({"ok": False, "detail": "Task not found"}, 404)
            result = self.execute_task(tid)
            return self.json({"ok": True, "result": result or "Couldn't run — add a model endpoint in Settings first."})
        if path == "/api/tasks" or path == "/api/tasks/add":
            return self.json(self.upsert_task(self._json_body()))
        if path == "/api/skills/learn":
            b = self._json_body()
            job = self.learn_skills_start(b.get("path") or "", b.get("device") or "")
            return self.json({"ok": True, "job": job})
        if path == "/api/skills" or path == "/api/skills/add":
            return self.json(self.upsert_skill(self._json_body()))
        if path == "/api/tools":
            return self.json(self.upsert_api_tool(self._form_body()))
        if path == "/api/mcp":
            return self.json(self.upsert_mcp_server(self._form_body()))
        if path in ("/api/plugins/install", "/api/plugins/reload") or (
                path.startswith("/api/plugins/") and path.rsplit("/", 1)[-1] in ("call", "rollback")):
            try:
                b = self._json_body() or {}
                if path == "/api/plugins/install":
                    return self.json(self.plugin_install(b))
                if path == "/api/plugins/reload":
                    return self.json(self.plugin_reload())
                ident = urllib.parse.unquote(path.split("/")[3])
                if path.endswith("/rollback"):
                    return self.json(self.plugin_rollback(ident))
                return self.json(self.plugin_call(ident, b.get("tool"), b.get("arguments") or {}))
            except (PluginError, ValueError) as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path.startswith("/api/coding-agents/") and path.rsplit("/", 1)[-1] in ("send", "keys", "reconcile", "decide") \
                or path == "/api/coding-agents/launch":
            try:
                b = self._json_body() or {}
                if path == "/api/coding-agents/launch":
                    return self.json(self.coding_launch(b.get("host"), b.get("harness"), b.get("cwd") or "", b.get("prompt") or ""))
                if path.endswith("/reconcile"):
                    return self.json(self.coding_reconcile_open())
                aid = urllib.parse.unquote(path.split("/")[3])
                if path.endswith("/send"):
                    return self.json(self.coding_send(aid, b.get("text"), b.get("sender") or "joebro", b.get("mode") or "auto"))
                if path.endswith("/decide"):
                    return self.json(self.coding_decide(aid, b.get("decision")))
                return self.json(self.coding_keys(aid, b.get("keys")))
            except ValueError as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/coding-agents/start":     # the apps' Resume button (and "start a session"): start_coding_session
            try:
                b = self._json_body() or {}
                msg = self.start_coding_session_tool(b)
                ok = msg.startswith(("Started", "Resumed", "Queued"))
                return self.json({"ok": ok, "detail": msg, "started": msg.startswith(("Started", "Resumed"))}, 200 if ok else 400)
            except (ValueError, TypeError) as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path in ("/api/coding-agents/event", "/api/coding-agents/ping", "/api/proactive", "/api/shopping",
                    "/api/personal/health") or path.startswith(("/api/proactive/undo/", "/api/proactive/approve/")):
            try:
                b = self._json_body() or {}
                if path == "/api/coding-agents/event":
                    return self.json(self.coding_event(b))
                if path == "/api/coding-agents/ping":
                    return self.json(self.coding_ping(b))
                if path == "/api/proactive":
                    return self.json(self.set_proactive_mode(b.get("mode")))
                if path.startswith("/api/proactive/undo/"):
                    return self.json(self.proactive_undo(path.rsplit("/", 1)[-1]))
                if path.startswith("/api/proactive/approve/"):
                    return self.json(self.proactive_approve(path.rsplit("/", 1)[-1]))
                if path == "/api/shopping":
                    return self.json(self.shopping_update(b))
                return self.json(self.health_ingest(b))
            except (ValueError, KeyError) as exc:
                return self.json({"ok": False, "detail": str(exc)}, 400)
        if path == "/api/plugins":
            return self.json(self.upsert_plugin(self._form_body()))
        return self.not_found()

    def do_PUT(self):
        if not self._guard():
            return
        path = self._path().path
        if jb_ext.route(self, "PUT", path, self._query()):
            return
        path = self._path().path          # an extension may have rewritten an alias to its canonical route
        if jb_sharedmem.route(self, "PUT", path, self._query()) or jb_nightshift.route(self, "PUT", path, self._query()):
            return
        if path.startswith("/api/muse/"):
            try:
                self.muse_write(path.split("/")[3], (self._json_body() or {}).get("content") or "")
            except ValueError as e:
                return self.json({"ok": False, "detail": str(e)}, 400)
            return self.json({"ok": True})
        if path == "/api/joebro/soul":
            soul = (self._json_body() or {}).get("soul")
            if not isinstance(soul, str) or len(soul) > 4000:
                return self.json({"detail": "soul must be a string of at most 4000 characters"}, 400)
            self.set_pref("phone_soul", soul)
            return self.json({"ok": True, "soul": soul})
        if path.startswith("/api/session/" + PHONE_SESSION_ID):
            return self.json({"detail": "The phone chat is managed by the server"}, 403)
        if path.startswith("/api/session/") and path.endswith("/workdir"):
            return self.workdir_post(path)
        if path.startswith("/api/session/"):
            sid = path.split("/")[3]
            f = self._form_body()
            fields = []
            vals = []
            for k in ("name", "model", "endpoint_url", "endpoint_id", "mode", "folder"):
                if k in f:
                    fields.append(f"{k}=?")
                    vals.append(f[k])
            if fields:
                vals.append(sid)
                self.store.exec(f"update sessions set {','.join(fields)} where id=?", vals)
            return self.json({"ok": True})
        if path.startswith("/api/prefs/"):
            key = path.rsplit("/", 1)[-1]
            val = self._json_body().get("value")
            if key in SECRET_PREFS and val == SECRET_MASK:
                return self.json({"ok": True})   # the UI echoed the mask back — keep the real value
            self.store.exec("insert or replace into prefs(key,value) values(?,?)", (key, json.dumps(val)))
            return self.json({"ok": True})
        if path.startswith("/api/memory/"):
            return self.json(self.upsert_memory(self._form_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/tasks/"):
            return self.json(self.upsert_task(self._json_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/skills/"):
            return self.json(self.upsert_skill(self._json_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/tools/"):
            return self.json(self.upsert_api_tool(self._form_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/mcp/"):
            return self.json(self.upsert_mcp_server(self._form_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/plugins/"):
            return self.json(self.upsert_plugin(self._form_body(), path.rsplit("/", 1)[-1]))
        if path.startswith("/api/research/") and path.endswith("/archive"):
            rid = path.split("/")[3]
            self.store.exec("update research set archived=1, updated_at=? where id=?", (now_iso(), rid))
            return self.json({"ok": True})
        if path.startswith("/api/calendar/events/"):
            uid = path.rsplit("/", 1)[-1]
            return self.update_event(uid, self._json_body())
        if path.startswith("/api/document/"):
            return self.update_document(path.rsplit("/", 1)[-1], self._json_body())
        if path.startswith("/api/model-endpoints/") and path.endswith("/models"):
            ep_id = path.split("/")[-2]
            hidden = self._json_body().get("hidden") or []
            self.set_pref("hidden_models_" + ep_id, hidden)
            return self.json({"ok": True})
        if path in ("/api/client-settings", "/api/settings/sync"):
            return self.json({"ok": True, "settings": self.set_client_settings(self._json_body() or {})})
        if path.startswith("/api/model-endpoints/") or path.startswith("/api/endpoints/"):
            return self.json(self.upsert_endpoint(self._json_or_form_body(), path.rsplit("/", 1)[-1]))
        return self.not_found()

    do_PATCH = do_PUT

    def do_DELETE(self):
        if not self._guard():
            return
        path = self._path().path
        if jb_ext.route(self, "DELETE", path, self._query()):
            return
        path = self._path().path          # an extension may have rewritten an alias to its canonical route
        if jb_sharedmem.route(self, "DELETE", path, self._query()) or jb_nightshift.route(self, "DELETE", path, self._query()):
            return
        if path.startswith("/api/mobile/devices/"):
            return self.json(remove_device(self.store, path.rsplit("/", 1)[-1]))
        if path == "/api/mobile/apns":
            return self.json(self.mobile_apns_delete())
        if path.startswith("/api/muse/"):
            try:
                if not self.muse_delete(path.split("/")[3]):
                    return self.json({"ok": False, "detail": "memory file not found"}, 404)
            except ValueError as e:
                return self.json({"ok": False, "detail": str(e)}, 400)
            return self.json({"ok": True})
        if path.startswith("/api/agents/"):
            aid = path[len("/api/agents/"):].strip("/")
            row = self.store.one("select * from agents where id=?", (aid,))
            if not row:
                return self.json({"ok": False, "detail": "no such agent"}, 404)
            if self.agent_status(row) in ("queued", "running"):
                return self.json({"ok": False, "detail": "stop it first"}, 409)
            self.store.exec("delete from agents where id=?", (aid,))
            return self.json({"ok": True})
        if path.startswith("/api/reminders/"):
            self.store.exec("update reminders set status='cancelled' where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/session/" + PHONE_SESSION_ID):
            return self.json({"detail": "The phone chat is managed by the server"}, 403)
        if path.startswith("/api/session/"):
            self.delete_session_cascade(path.split("/")[3])      # the chat AND its agent records
            return self.json({"ok": True})
        if path.startswith(("/api/model-endpoints/", "/api/endpoints/")):
            self.store.exec("delete from endpoints where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/memory/"):
            jb_usermem.mem_delete(self.store, path.rsplit("/", 1)[-1])
            return self.json({"ok": True})
        if path.startswith("/api/tasks/"):
            self.store.exec("delete from tasks where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/skills/"):
            self.store.exec("delete from skills where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/tools/"):
            self.store.exec("delete from api_tools where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/mcp/"):
            self.store.exec("delete from mcp_servers where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/plugins/"):
            self.plugin_delete(urllib.parse.unquote(path.rsplit("/", 1)[-1]))
            return self.json({"ok": True})
        if path.startswith("/api/research/"):
            self.store.exec("delete from research where id=?", (path.rsplit("/", 1)[-1],))
            return self.json({"ok": True})
        if path.startswith("/api/calendar/events/"):
            return self.delete_event(path.rsplit("/", 1)[-1])
        if path.startswith("/api/document/"):
            self.store.exec("update documents set archived=1, updated_at=? where id=?", (now_iso(), path.rsplit("/", 1)[-1]))
            return self.json({"ok": True})
        if path.startswith("/api/email/delete/"):
            return self.email_delete(path.rsplit("/", 1)[-1])
        return self.not_found()

    # The agent never holds more than this many images in context at once. Older
    # ones are summarised to text, so a long image task is sent in chunks and no
    # single request balloons (which is what got rejected before).
    MAX_CONTEXT_IMAGES = 4

    _AICHECK_CHUNK = 9000   # ZeroGPT truncates around ~10k; stay under
    _AICHECK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # Compaction tuning. We don't have a real tokenizer in the stdlib, so we
    # budget by characters (~4 chars/token). When history exceeds the budget we
    # summarize the older turns into one dense system note and keep the recent
    # turns verbatim — far better recall than the old `msgs[-30:]` front-chop.
    COMPACT_KEEP_RECENT = 12       # most recent turns always sent verbatim
    COMPACT_CHAR_BUDGET = 48000    # ~12k tokens of history before we compact

    # A tool's reply is fed back to the model and then RE-SENT on every later round
    # of the same turn, so one `read_file` on a big source file or a chatty `bash`
    # used to multiply across the whole loop. Compaction can't save us — it only
    # ever sees persisted user/assistant rows, never live tool replies. Keep the
    # head and the tail: errors and summaries live at the ends, filler in the middle.
    MAX_TOOL_OUTPUT_CHARS = 24000

    COMPACT_SYSTEM_PROMPT = (
        "You are summarizing a conversation to preserve context after compaction. "
        "Produce a dense, structured summary that lets the conversation continue "
        "seamlessly. Use these sections:\n\n"
        "### User Goal\nOne sentence on what the user is trying to accomplish.\n\n"
        "### What Was Done\n- Completed actions, decisions, and key outputs. "
        "Include specific file paths, names, URLs, and values. Note errors and how they were resolved.\n\n"
        "### Current State\nWhat the task/code state is right now and what was last discussed.\n\n"
        "### Pending / Next Steps\n- What remains, open questions, blockers.\n\n"
        "### Key Context\n- Constraints, preferences, decisions that must not be forgotten; "
        "specific values (models, ports, paths, versions).\n\n"
        "Keep it under 1000 tokens. Every token should carry information. No pleasantries.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("JOEBRO_PORT", DEFAULT_PORT)))
    parser.add_argument("--data-dir", default="")
    # The phone listener is a SECOND server (see jb_mobile). Normally the Mac app
    # drives it through mobile.json; these flags are for running it by hand.
    parser.add_argument("--lan-host", default="", help="also listen for the phone on this address (token required)")
    parser.add_argument("--lan-port", type=int, default=8766)
    args = parser.parse_args()
    root = Path(args.data_dir) if args.data_dir else app_support_dir()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = Store(root)
    # joebro.plugin/1: installed plugins and their private data live in the app's data folder.
    os.environ.setdefault("JOEBRO_PLUGINS_DIR", str(Path(root) / "plugins"))
    os.environ.setdefault("JOEBRO_PLUGIN_DATA_ROOT", str(Path(root) / "plugin-data"))
    os.environ.setdefault("JOEBRO_URL", "http://127.0.0.1:%d" % args.port)
    # Anti-rebinding: on the normal loopback bind, only loopback Hosts are real.
    # A non-loopback bind (Tailscale) legitimately sees any Host, so it swaps the
    # Host check for an opt-in bearer token instead — see Handler._guard.
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    server.allowed_hosts = ({f"127.0.0.1:{args.port}", f"localhost:{args.port}",
                             f"[::1]:{args.port}"} if loopback else None)
    server.auth_token = os.environ.get("JOEBRO_TOKEN", "")
    if not loopback and not server.auth_token:
        print(f"WARNING: bound to {args.host} with no JOEBRO_TOKEN — anyone who can "
              "reach this port can run tools as you.", flush=True)
    # A run marked live in the database has no thread behind it after a restart: say so
    # instead of showing a spinner forever, and never replay a half-done scheduled run.
    try:
        _boot = Handler.__new__(Handler)
        _boot.server = server
        ensure_tables(server.store)
        server.store.exec("UPDATE personal_reply_jobs SET status='failed',error='Interrupted by backend restart; open chat to continue' WHERE status='running'")
        server.store.exec("UPDATE scheduled_runs SET status='failed',error='Interrupted by backend restart; not replayed automatically' WHERE status='running'")
        _boot.ptask_seed()
    except Exception as exc:
        print("boot reconcile failed:", exc, flush=True)
    threading.Thread(target=personal_reply_loop, args=(server,), daemon=True).start()
    threading.Thread(target=briefing_loop, args=(server,), daemon=True).start()
    threading.Thread(target=_task_scheduler, args=(server,), daemon=True).start()
    threading.Thread(target=reminder_loop, args=(server,), daemon=True).start()
    threading.Thread(target=pulse_loop, args=(server,), daemon=True).start()   # heartbeat + pulse, both off until enabled
    threading.Thread(target=janitor_loop, args=(server,), daemon=True).start()  # deletes only if enabled
    threading.Thread(target=proactive_loop, args=(server,), daemon=True).start()  # off until enabled
    threading.Thread(target=coding_loop, args=(server,), daemon=True).start()     # linked coding agents
    jb_ext.start_loops(server)                                                   # private add-ons, if any
    try:
        _h = Handler.__new__(Handler); _h.server = server
        _h.reconcile_agents_on_boot()      # a restart killed any run still marked live
    except Exception:
        pass
    if args.lan_host:
        from jb_mobile import load_mobile
        cfg = dict(load_mobile(server.store), host=args.lan_host, port=args.lan_port)
        cfg["token"] = os.environ.get("JOEBRO_LAN_TOKEN") or cfg["token"]
        try:
            LAN.start(Handler, server.store, cfg)       # refuses to start without a token
            print(f"Phone listener on http://{args.lan_host}:{args.lan_port} (token required)", flush=True)
        except ValueError as exc:
            print("Phone listener NOT started:", exc, flush=True)
    else:
        start_lan_if_enabled(Handler, server.store)   # only if the user left it on
    from jb_telegram import telegram_bot_loop
    threading.Thread(target=telegram_bot_loop, args=(server,), daemon=True).start()
    # Boot the local searxng meta-search alongside the app (best-effort).
    def _boot_searxng():
        h = Handler.__new__(Handler); h.server = server
        try:
            h.ensure_searxng()
        except Exception:
            pass
    threading.Thread(target=_boot_searxng, daemon=True).start()
    # Warm the model picker cache at boot so the first /api/models is instant
    # instead of waiting on every endpoint's live fetch.
    def _warm_models():
        h = Handler.__new__(Handler); h.server = server
        try:
            h.model_items(refresh=True)
        except Exception:
            pass
    threading.Thread(target=_warm_models, daemon=True).start()
    # Hold a warm IMAP session per account so the first inbox open is instant.
    def _warm_email():
        h = Handler.__new__(Handler); h.server = server
        try:
            h.email_keepalive_start()
        except Exception:
            pass
    threading.Thread(target=_warm_email, daemon=True).start()
    print(f"JoeBro backend listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


def _local_date(iso):
    """The LOCAL calendar date of a stored timestamp. last_run is written in UTC
    (now_iso), so comparing its first 10 characters against a local date is wrong
    by a day for part of every evening — which is how a task could fire twice
    just after midnight."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


def _task_due(row, now, today):
    """Due = its slot has passed today and it has not run today.

    Deliberately NOT an exact HH:MM match. The old code fired only if a tick
    landed inside the target minute, so any restart near the slot — or any task
    sharing a slot with a slower one — silently skipped the whole day."""
    st = (row.get("scheduled_time") or "").strip()[:5]
    if len(st) < 4 or ":" not in st:
        return False
    try:
        hh, mm = int(st.split(":")[0]), int(st.split(":")[1])
    except (ValueError, IndexError):
        return False
    sched = (row.get("schedule") or "daily").strip().lower()
    if sched == "once" and row.get("scheduled_date") != today:
        return False
    if sched == "weekly" and now.weekday() != int(row.get("repeat_day") or 0):
        return False
    if sched == "monthly" and now.day != 1:
        return False
    if now.hour * 60 + now.minute < hh * 60 + mm:
        return False                       # slot hasn't arrived yet today
    # Creating or changing a 09:00 task at 15:00 must schedule tomorrow's
    # 09:00, not launch it immediately. A task that already existed before
    # today's slot can still catch up after a backend restart.
    changed = row.get("updated_at") or row.get("created_at") or ""
    if _local_date(changed) == today:
        try:
            changed_at = datetime.fromisoformat(str(changed).replace("Z", "+00:00"))
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=timezone.utc)
            local_changed = changed_at.astimezone()
            if (local_changed.hour * 3600 + local_changed.minute * 60 + local_changed.second) > hh * 3600 + mm * 60:
                return False
        except ValueError:
            pass
    return _local_date(row.get("last_run")) != today


def _run_task_thread(server, task_id, slot=None):
    import types as _types
    h = Handler.__new__(Handler)
    h.server = _types.SimpleNamespace(store=server.store)
    try:
        result = h.execute_task(task_id)
        if slot:
            if getattr(h, "_task_skipped", False):
                finish_scheduled_run(server.store, task_id, slot, "Background budget unavailable", retry_safe=True)
            else:
                finish_scheduled_run(server.store, task_id, slot)
    except Exception as exc:
        if slot:
            finish_scheduled_run(server.store, task_id, slot, str(exc),
                                 retry_safe=not getattr(h, "_task_write_performed", True))
        traceback.print_exc()


def _task_scheduler(server):
    """Fire active scheduled tasks once a day, at or after their slot.

    Each run goes on its own thread, so one slow task can no longer hide every
    other task due at the same time (the memory and skill audits are both 07:00).
    The slot is claimed in the database before the thread starts, so a second
    tick during a long run cannot start it twice."""
    while True:
        try:
            now = datetime.now().astimezone()
            today = now.strftime("%Y-%m-%d")
            for row in server.store.rows("select * from tasks where status='active'"):
                if not _task_due(row, now, today):
                    continue
                if (row.get("scope") or "general") == "personal":
                    paused = server.store.one("SELECT value FROM prefs WHERE key='personal_automation_paused'")
                    if paused and str(paused["value"]).lower() in ("true", "1", "yes", "on"): continue
                slot = claim_scheduled_run(server.store, row, now)
                if slot:
                    threading.Thread(target=_run_task_thread, args=(server, row["id"], slot), daemon=True).start()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


if __name__ == "__main__":
    main()
