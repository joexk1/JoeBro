"""Memory, skills, tasks, deep research, JSON serializers."""
from jb_core import *  # noqa: F401,F403


class AssistantMixin:

    def _task_endpoint(self):
        """Pick an endpoint/model to run a scheduled task: the saved default,
        else the most recent chat's, else any configured endpoint."""
        ep, model = self.pref("default_endpoint_id") or "", self.pref("default_model") or ""
        if not ep or not model:
            # Fill the gaps independently — a saved endpoint with no saved model
            # used to fall through as model "" and 404 at the endpoint.
            s = self.store.one("select endpoint_id, model from sessions where endpoint_id != '' order by datetime(created_at) desc limit 1")
            if s:
                ep, model = ep or s.get("endpoint_id") or "", model or s.get("model") or ""
        if not ep:
            e = self.store.one("select id from endpoints order by datetime(created_at) limit 1")
            ep = e["id"] if e else ""
        return ep, model

    def execute_task(self, task_id):
        """Run a saved task through the agent loop (so it can actually use tools —
        e.g. the skill audit calls manage_skills, the email summary reads mail).
        Output lands in a persistent '[TASK] <name>' chat. Returns the reply."""
        row = self.store.one("select * from tasks where id=?", (task_id,))
        if not row:
            return None
        ep, model = self._task_endpoint()
        if not ep:
            return "No model endpoint configured — add one in Settings."
        tsid = "task_session_" + task_id
        if not self.store.one("select id from sessions where id=?", (tsid,)):
            self.store.exec(
                "insert into sessions(id,name,model,endpoint_url,endpoint_id,workdir,created_at) values(?,?,?,?,?,?,?)",
                (tsid, "[TASK] " + (row.get("title") or "Task"), model, "", ep, "", now_iso()))
        prompt = row.get("prompt") or row.get("title") or "Run this task."
        self.add_message(tsid, "user", prompt, {})
        # GUARANTEE: every task runs as an agent. This is the single chokepoint for
        # running a task — both the manual "Run now" route and the scheduler call
        # execute_task — and mode is hardcoded "agent" here, never read from the
        # task row, so no task can run as plain chat. The task's own permission
        # mode only governs file access (default: the safe sandbox).
        pmode = (row.get("permission_mode") or "sandbox").strip() or "sandbox"
        f = {"session": tsid, "message": prompt, "endpoint_id": ep, "model": model,
             "permission_mode": pmode, "max_tokens": "4000", "allow_web_search": "true"}
        f["mode"] = "agent"   # forced last — cannot be overridden
        reply, thinking, events = self.run_agent(f)
        meta = {"model": model}
        if thinking:
            meta["thinking"] = thinking
        if events:
            meta["tool_events"] = [{k: v for k, v in e.items() if k != "doc"} for e in events]
        self.add_message(tsid, "assistant", reply, meta)
        self.store.exec("update tasks set last_run=?, updated_at=? where id=?", (now_iso(), now_iso(), task_id))
        return reply

    def _active_background_plugins(self):
        """Enabled background (guardrail) plugins applied to every turn."""
        return self.store.rows("select * from plugins where kind='background' and is_enabled=1 order by name")

    def _content_words(self, text):
        """Lowercased set of meaningful words (len>3, filler removed) for matching."""
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
                if len(w) > 3 and w not in self._MEMORY_STOPWORDS}

    def _use_relevant_memories(self, message):
        """Inject memories relevant to this request into the agent's context and
        bump their use_count/last_used. Returns (context_string, matched_texts)
        so the caller can report which memories were used (message footer)."""
        rows = self.store.rows("select * from memory")
        if not rows:
            return "", []
        qwords = self._content_words(message)
        if not qwords:
            return "", []
        # Match on EXACT shared content words (not substring — "art" must not
        # match "started"), filler removed. Strict, because loose matching kept
        # pulling in unrelated memories:
        #   1. A word that recurs across many memories is generic, not a topic,
        #      so filter it by document frequency — it can't anchor a match.
        #   2. Keep a memory only on a real signal: two distinctive shared words,
        #      or one long (>=8-char) specific keyword. One coincidental medium
        #      word ("create", "content", "project") is not enough.
        mem_words = [(r, self._content_words(r.get("text") or "")) for r in rows]
        df = {}
        for _, w in mem_words:
            for x in w:
                df[x] = df.get(x, 0) + 1
        generic_cap = max(3, len(rows) // 4)
        scored = []
        for r, w in mem_words:
            shared = {x for x in (qwords & w) if df[x] <= generic_cap}
            if not shared:
                continue
            if len(shared) >= 2 or any(len(x) >= 8 for x in shared):
                scored.append((len(shared), sum(len(x) for x in shared), r))
        if not scored:
            return "", []
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        matched = [r for _, __, r in scored[:3]]
        for r in matched:
            self.store.exec("update memory set use_count=?, last_used=? where id=?",
                            ((r.get("use_count") or 0) + 1, now_iso(), r["id"]))
        lines = "\n".join(f"- {r.get('text')}" for r in matched)
        return "What you know about the user (memory):\n" + lines, [r.get("text") or "" for r in matched]

    # Words too generic to make a skill "relevant" on their own — they show up in
    # lots of skill descriptions and used to drag UI/design skills into plain chat.
    _SKILL_STOPWORDS = frozenset({
        "this", "that", "with", "have", "your", "from", "they", "will", "make",
        "makes", "help", "want", "need", "just", "like", "about", "into", "what",
        "when", "where", "there", "their", "then", "than", "some", "more", "most",
        "also", "been", "being", "does", "done", "could", "would", "should",
        "please", "thanks", "thank", "hello", "give", "gives", "using", "used",
        "over", "very", "much", "many", "good", "well", "here", "know", "look",
        "tell", "show", "find", "take", "them", "time", "work", "stuff", "thing",
    })

    def _use_relevant_skills(self, message):
        """Find saved skills relevant to this request, inject them, and RAISE
        their confidence (frequency of use). Skills that keep getting used climb
        and survive the audit; ones that never match stay low and get pruned —
        the self-improving loop. Matching is whole-word against the CURATED fields
        only (name/description/when-to-use), with a real threshold, so a skill's
        long procedure body no longer pulls it into unrelated chat."""
        rows = self.store.rows("select * from skills where status != 'archived'")
        if not rows:
            return "", []
        words = {w for w in re.findall(r"[a-z0-9]+", (message or "").lower())
                 if len(w) > 3 and w not in self._SKILL_STOPWORDS}
        if not words:
            return "", []
        # Per-skill curated word sets, plus document frequency across the library:
        # a word in many skills (design, image, project) is weak signal; one in
        # just a couple is distinctive enough to match on its own.
        sig = {}
        df = {}
        for r in rows:
            nw = set(re.findall(r"[a-z0-9]+", (r.get("name") or "").lower()))
            sw = nw | set(re.findall(
                r"[a-z0-9]+",
                ((r.get("description") or "") + " " + (r.get("when_to_use") or "")).lower()))
            sig[r["id"]] = (nw, sw)
            for w in sw:
                df[w] = df.get(w, 0) + 1
        scored = []
        for r in rows:
            name_words, signal_words = sig[r["id"]]
            hits = words & signal_words
            if not hits:
                continue
            name_hit = bool(words & name_words)
            # A distinctive word = rare in the library AND a substantial token, so
            # a common short verb that's merely rare here (write, reply) doesn't
            # count, but a real noun (discord, tiktok, exhibition) does.
            rare_hit = any(df.get(w, 0) <= 2 and len(w) >= 6 for w in hits)
            # Qualify on real signal: 2+ matching words, a hit on the skill's own
            # name, or a single DISTINCTIVE word. A lone generic word still won't.
            if len(hits) >= 2 or name_hit or rare_hit:
                rank = len(hits) + (2 if name_hit else 0) + (1 if rare_hit else 0)
                scored.append((rank, len(hits), r))
        if not scored:
            return "", []
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:3]
        matched = [r for _, _, r in top]
        for _, nhits, r in top:
            # Confidence climbs HARSH and incremental: a few on-point uses to clear
            # the 30% audit threshold, not one. Based on distinct matching words
            # (real efficacy), not the match rank — so name/rare bonuses sharpen
            # WHICH skills match without inflating how fast they gain trust.
            bump = min(8, 2 + nhits)
            new_conf = min(100, (r.get("confidence") if r.get("confidence") is not None else 60) + bump)
            self.store.exec("update skills set confidence=?, updated_at=? where id=?", (new_conf, now_iso(), r["id"]))
        lines = "\n".join(f"- {r.get('name')}: {(r.get('content') or '').strip()[:300]}" for r in matched)
        return ("Relevant saved skills you can apply to this request:\n" + lines,
                [r.get("name") for r in matched if r.get("name")])

    def _auto_learn(self, sid, user_msg, reply, endpoint_id, model):
        """After a turn, quietly extract a durable user memory and/or a reusable
        skill and save them. Auto-skills start at low confidence so they must
        prove useful (get used) or get pruned by the skill audit. Best-effort."""
        if not endpoint_id or len((user_msg or "").strip()) < 25:
            return
        try:
            out = self._complete(
                endpoint_id, model,
                "You extract durable learnings from one chat turn. Respond with ONLY JSON: "
                '{"memory": <one stable fact about the USER worth remembering long-term, or null>, '
                '"skill": {"name": <short imperative title>, '
                '"description": <one-line summary of what the skill does>, '
                '"when_to_use": <the trigger: what kind of request should invoke this>, '
                '"content": <a reusable procedure as 3-6 concise numbered markdown steps>} or null}. '
                "Memory = lasting preferences/identity/recurring context, never one-off task details. "
                "Skill = a repeatable workflow the user will likely want again, captured well enough to "
                "follow later WITHOUT this conversation: a real description, a clear trigger, and concrete "
                "steps — never a single vague sentence. If you can't write all three fields properly, return null. "
                "Use null when nothing qualifies. Keep each field tight but complete.",
                f"User: {user_msg}\n\nAssistant: {(reply or '')[:1500]}", max_tokens=900)
            if not out:
                return
            m = re.search(r"\{.*\}", out, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            mem = data.get("memory")
            if isinstance(mem, str) and mem.strip().lower() not in ("", "null", "none"):
                existing = [(e.get("text") or "").lower() for e in self.store.rows("select text from memory")]
                if mem.strip().lower() not in existing:
                    self.store.exec("insert into memory(id,text,category,created_at) values(?,?,?,?)",
                                    ("mem_" + os.urandom(6).hex(), mem.strip(), "auto", now_iso()))
            sk = data.get("skill")
            if isinstance(sk, dict) and (sk.get("name") or "").strip():
                content = sk.get("content") or sk.get("procedure") or ""
                if isinstance(content, list):   # model sometimes returns steps as an array
                    content = "\n".join(f"{i+1}. {s}" for i, s in enumerate(content))
                content = str(content).strip()
                # Skip thin junk: an auto-skill must carry a usable procedure.
                if len(content) >= 25:
                    names = [(e.get("name") or "").lower() for e in self.store.rows("select name from skills")]
                    if (sk.get("name") or "").strip().lower() not in names:
                        self.store.exec(
                            "insert into skills(id,name,description,content,when_to_use,status,confidence,created_at,updated_at)"
                            " values(?,?,?,?,?,?,?,?,?)",
                            ("skill_" + os.urandom(6).hex(), sk.get("name").strip(),
                             (sk.get("description") or "").strip(), content,
                             (sk.get("when_to_use") or "").strip(), "draft", 20, now_iso(), now_iso()))
        except Exception:
            pass

    def upsert_task(self, body, task_id=None):
        tid = task_id or body.get("id") or body.get("task_id") or "task_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from tasks where id=?", (tid,))
        title = body.get("title") or body.get("name") or (existing or {}).get("title") or "Untitled task"
        prompt = body.get("prompt") or body.get("description") or (existing or {}).get("prompt") or ""
        schedule = body.get("schedule") or body.get("rrule") or (existing or {}).get("schedule") or ""
        scheduled_time = body.get("time") or body.get("scheduled_time") or (existing or {}).get("scheduled_time") or ""
        status = body.get("status") or (existing or {}).get("status") or "active"
        pmode = (body.get("permission_mode") or (existing or {}).get("permission_mode") or "sandbox").strip() or "sandbox"
        if pmode not in ("sandbox", "readonly", "full"):
            pmode = "sandbox"
        # repeat_day: weekday a weekly task fires on (0=Mon ... 6=Sun). Default Mon.
        # Accept "weekday" too (the agent tool exposes it under that clearer name).
        rday = body.get("repeat_day")
        if rday is None:
            rday = body.get("weekday")
        if rday is None:
            rday = (existing or {}).get("repeat_day", 0) if existing else 0
        try:
            rday = max(0, min(6, int(rday or 0)))
        except (TypeError, ValueError):
            rday = 0
        if existing:
            self.store.exec(
                "update tasks set title=?,prompt=?,schedule=?,scheduled_time=?,status=?,permission_mode=?,repeat_day=?,updated_at=? where id=?",
                (title, prompt, schedule, scheduled_time, status, pmode, rday, now, tid),
            )
        else:
            self.store.exec(
                "insert into tasks(id,title,prompt,schedule,scheduled_time,status,permission_mode,repeat_day,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                (tid, title, prompt, schedule, scheduled_time, status, pmode, rday, now, now),
            )
        return {"ok": True, "task": self.task_json(self.store.one("select * from tasks where id=?", (tid,)))}

    def upsert_skill(self, body, skill_id=None):
        sid = skill_id or body.get("id") or body.get("skill_id") or "skill_" + os.urandom(6).hex()
        now = now_iso()
        existing = self.store.one("select * from skills where id=?", (sid,))
        name = body.get("name") or body.get("title") or (existing or {}).get("name") or "Untitled skill"
        description = body.get("description") or (existing or {}).get("description") or ""
        content = body.get("procedure") or body.get("content") or body.get("text") or (existing or {}).get("content") or ""
        if isinstance(content, (list, dict)):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        when_to_use = body.get("when_to_use") or (existing or {}).get("when_to_use") or ""
        status = body.get("status") or (existing or {}).get("status") or "draft"
        # Manually-created skills start established (60); auto-created ones pass
        # a low confidence so they must prove useful (via use) or get audited out.
        conf = body.get("confidence")
        conf = int(conf) if conf is not None else ((existing or {}).get("confidence") if existing else 60)
        if conf is None:
            conf = 60
        if existing:
            self.store.exec(
                "update skills set name=?,description=?,content=?,when_to_use=?,status=?,confidence=?,updated_at=? where id=?",
                (name, description, content, when_to_use, status, conf, now, sid),
            )
        else:
            self.store.exec(
                "insert into skills(id,name,description,content,when_to_use,status,confidence,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)",
                (sid, name, description, content, when_to_use, status, conf, now, now),
            )
        return {"ok": True, "skill": self.skill_json(self.store.one("select * from skills where id=?", (sid,)))}

    def create_research(self, query, endpoint_id=None, model=None):
        rid = "research_" + os.urandom(6).hex()
        now = now_iso()
        progress = json.dumps({"phase": "Queued", "message": query})
        self.store.exec(
            "insert into research(id,query,status,content,progress,archived,created_at,updated_at) values(?,?,?,?,?,?,?,?)",
            (rid, query, "running", f"# {query}\n\nResearching…", progress, 0, now, now),
        )
        self.start_research_worker(rid, query, endpoint_id, model)
        return rid

    def start_research_worker(self, rid, query, endpoint_id, model):
        """Spawn the worker for `rid` exactly once. A panel reload / repeat POST
        never starts a second worker, and a finished/cancelled item is never
        re-run (the worker itself bails on a non-running status)."""
        with _RESEARCH_GUARD:
            if rid in _RESEARCH_STARTED:
                return
            _RESEARCH_STARTED.add(rid)
            _RESEARCH_CANCEL.discard(rid)
        threading.Thread(target=self._run_research, args=(rid, query, endpoint_id, model), daemon=True).start()

    def cancel_research(self, rid):
        """Mark a research item cancelled. The worker checks the flag between
        phases and stops; the row's status becomes 'cancelled' so it never
        re-runs or claims to be running."""
        row = self.store.one("select status from research where id=?", (rid,))
        if not row:
            return self.json({"ok": False, "detail": "not found"}, 404)
        with _RESEARCH_GUARD:
            _RESEARCH_CANCEL.add(rid)
        self.store.exec(
            "update research set status='cancelled', progress=?, updated_at=? where id=?",
            (json.dumps({"phase": "Cancelled", "message": ""}), now_iso(), rid))
        return self.json({"ok": True, "status": "cancelled"})

    def _research_cancelled(self, rid):
        with _RESEARCH_GUARD:
            if rid in _RESEARCH_CANCEL:
                return True
        row = self.store.one("select status from research where id=?", (rid,))
        return bool(row) and row.get("status") == "cancelled"

    @staticmethod
    def _report_with_images(content, images):
        """Place up to 2 images inline: one right under the # title, a second
        under the 2nd ## section. Returns content unchanged if no images."""
        images = [u for u in (images or []) if u][:2]
        if not images:
            return content
        out, placed_top, h2 = [], False, 0
        for line in content.split("\n"):
            out.append(line)
            if not placed_top and line.startswith("# "):
                out += ["", f"![]({images[0]})"]
                placed_top = True
            elif len(images) > 1 and line.startswith("## "):
                h2 += 1
                if h2 == 2:
                    out += ["", f"![]({images[1]})"]
        if not placed_top:   # no top-level heading — prepend
            out = [f"![]({images[0]})", ""] + out
        return "\n".join(out)

    def _set_research_progress(self, rid, phase, message=""):
        self.store.exec("update research set progress=?, updated_at=? where id=?",
                        (json.dumps({"phase": phase, "message": message}), now_iso(), rid))

    def _run_research(self, rid, query, endpoint_id, model):
        try:
            if self._research_cancelled(rid):
                return
            self._set_research_progress(rid, "Searching the web", query)
            results = self.search_web(query, count=12)
            if self._research_cancelled(rid):
                return
            self._set_research_progress(rid, "Reading sources", f"{len(results)} sources")
            images = self.search_images(query, count=2)   # best-effort, rendered inline (not in the .md)
            if images:
                self.store.exec("update research set images=? where id=?", (json.dumps(images), rid))
            sources_md = "\n".join(
                f"- [{r['title']}]({r['url']})" + (f"\n  {r['snippet']}" if r.get("snippet") else "")
                for r in results
            ) or "- No sources found."
            report = None
            if endpoint_id and not self._research_cancelled(rid):
                self._set_research_progress(rid, "Writing report", query)
                src_text = "\n\n".join(
                    f"[{i}] {r['title']}\n{r['url']}\n{r.get('snippet', '')}"
                    for i, r in enumerate(results, 1)
                ) or "(no sources retrieved)"
                report = self._complete(
                    endpoint_id, model,
                    "You are a meticulous senior research analyst writing an in-depth briefing for an "
                    "expert reader. Produce a LONG, comprehensive, well-structured Markdown report — aim "
                    "for substantial depth, not a summary. Required structure:\n"
                    "1. A top-level title (single # heading) restating the topic.\n"
                    "2. An '## Executive Summary' of 1-2 dense paragraphs covering the key findings.\n"
                    "3. At least FOUR to SIX themed analysis sections, each a '## heading', that go deep: "
                    "explain mechanisms, compare viewpoints, cite concrete specifics (names, numbers, dates, "
                    "examples). Reference sources inline by bracket number (e.g. [3]) for specific facts.\n"
                    "4. A '## Caveats & Limitations' section.\n"
                    "5. A '## Conclusion' with a clear bottom line.\n"
                    "STYLE: make it SCANNABLE, not a wall of text. Lead each section with one short framing "
                    "sentence, then use BULLET POINTS (and '### subheadings') for the substance — most of the "
                    "detail should be bullets, with bold lead-ins where useful. Keep paragraphs to 1-3 sentences. "
                    "Be thorough in coverage but tight in prose. Use ONLY the supplied sources; never fabricate.",
                    f"Research topic: {query}\n\nNumbered web sources:\n{src_text}\n\n"
                    "Write the complete, in-depth, bullet-driven report now.",
                    max_tokens=8000,
                )
            if self._research_cancelled(rid):
                return
            content = (report.strip() + "\n\n## Sources\n" + sources_md) if report else (
                f"# {query}\n\n## Sources\n{sources_md}")
            self.store.exec("update research set status='completed', content=?, progress=?, updated_at=? where id=?",
                            (content, json.dumps({"phase": "Complete", "message": ""}), now_iso(), rid))
        except Exception as exc:
            if self._research_cancelled(rid):
                return
            self.store.exec("update research set status='completed', content=?, progress=?, updated_at=? where id=?",
                            (f"# {query}\n\nResearch failed: {exc}",
                             json.dumps({"phase": "Complete", "message": "failed"}), now_iso(), rid))
        finally:
            with _RESEARCH_GUARD:
                _RESEARCH_STARTED.discard(rid)
                _RESEARCH_CANCEL.discard(rid)

    @staticmethod
    def loads(s):
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {}

    @staticmethod
    def session_json(r):
        return {
            "id": r["id"],
            "name": r["name"],
            "model": r.get("model") or "",
            "mode": r.get("mode") or "chat",
            "archived": bool(r.get("archived")),
            "is_important": bool(r.get("is_important")),
            "folder": r.get("folder") or None,
            "sort_order": r.get("sort_order"),
            "created_at": r.get("created_at"),
        }

    @staticmethod
    def memory_json(r):
        return {
            "id": r["id"],
            "text": r["text"],
            "category": r.get("category") or "general",
            "pinned": bool(r.get("pinned")),
            "use_count": r.get("use_count") or 0,
            "last_used": r.get("last_used") or "",
            "created_at": r.get("created_at"),
        }

    @staticmethod
    def event_json(r):
        return {
            "uid": r["id"],
            "id": r["id"],
            "summary": r["summary"],
            "dtstart": r["dtstart"],
            "dtend": r["dtend"],
            "all_day": bool(r["all_day"]),
            "location": r["location"],
            "description": r["description"],
        }

    @staticmethod
    def task_json(r):
        title = r.get("title") or "Untitled task"
        return {
            "id": r["id"],
            "name": title,
            "title": title,
            "prompt": r.get("prompt") or "",
            "schedule": r.get("schedule") or "",
            "scheduled_time": r.get("scheduled_time") or "",
            "status": r.get("status") or "active",
            "permission_mode": r.get("permission_mode") or "sandbox",
            "repeat_day": int(r.get("repeat_day") or 0),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    @staticmethod
    def skill_json(r):
        return {
            "id": r["id"],
            "name": r.get("name") or "Untitled skill",
            "description": r.get("description") or "",
            "content": r.get("content") or "",
            "procedure": r.get("content") or "",          # the editor's "Procedure (markdown)"
            "when_to_use": r.get("when_to_use") or "",
            "category": r.get("category") or "general",
            "status": r.get("status") or "draft",
            "confidence": r.get("confidence") if r.get("confidence") is not None else 60,
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    @classmethod
    def research_json(cls, r):
        return {
            "id": r["id"],
            "query": r.get("query") or "",
            "status": r.get("status") or "queued",
            "content": r.get("content") or "",
            "progress": cls.loads(r.get("progress")) or {},
            "archived": bool(r.get("archived")),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
