"""Long-term memory: Hindsight-style retain / recall / reflect over topic pages.

Every memory is ONE self-contained fact typed by kind —
  world      a lasting fact about the user or their world
  experience what happened / the current state of an ongoing project
  opinion    a judgement or preference the user expressed
— filed on a wiki-like `topic` page (Karpathy's AI-wiki idea: facts cluster on
named pages that get edited over time, not an append-only log).

The protocol against context pollution:
  retain   write typed items; a near-duplicate on the same topic MERGES into
           the existing row instead of piling up
  recall   the only thing a turn ever injects: pinned rows plus at most k
           entries that clear a real relevance bar, one compact line each
  reflect  the weekly "Memory audit" task consolidates pages like a wiki
           editor: merge, rewrite, prune superseded state

All stdlib + sqlite; the Brain tab API (memory_json shape, /api/memory routes)
is unchanged apart from new optional fields.
"""
import math

from jb_core import *  # noqa: F401,F403

MEMORY_KINDS = ("world", "experience", "opinion")


def memory_slug(text):
    """kebab-slug for topic page names."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:48] or "general"


class MemoryMixin:

    # Generic filler that creates false memory matches — conversational glue and
    # doc-editing verbs that say nothing about WHICH memory is relevant.
    _MEMORY_STOPWORDS = frozenset({
        "want", "wants", "need", "needs", "like", "just", "make", "made", "also",
        "this", "that", "with", "from", "into", "your", "you", "have", "here",
        "there", "what", "when", "then", "them", "they", "some", "something",
        "anything", "added", "adding", "always", "about", "okay", "please",
        "could", "would", "should", "really", "thing", "things", "stuff", "write",
        "writing", "develop", "section", "document", "file", "files", "title",
        "page", "ideas", "idea", "good", "going", "gonna", "gonen", "teh", "adn",
        "give", "show", "tell", "help", "user", "down", "bottom", "more", "very",
        "people", "place", "point", "using", "based", "sure", "able", "actually",
        "basically", "probably", "usually", "maybe", "perhaps", "instead", "rather",
        "quite", "pretty", "still", "even", "once", "while", "around", "across",
        "along", "than", "kind", "lots", "much", "many", "most", "such", "each",
        "both", "other", "another", "everything", "nothing", "anyone", "someone",
        "everyone", "never", "often", "sometimes", "thanks",
    })

    def _content_words(self, text):
        """Lowercased set of meaningful words (len>3, filler removed)."""
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
                if len(w) > 3 and w not in self._MEMORY_STOPWORDS}

    def _memory_index_words(self, r):
        """The words a memory is findable by: its text plus the curated
        topic/entities labels (which also earn a scoring boost)."""
        label_words = self._content_words((r.get("topic") or "") + " " + (r.get("entities") or ""))
        return self._content_words(r.get("text") or "") | label_words, label_words

    def _score_memories(self, query, rows):
        """Rank memories against a query: exact shared content words, weighted by
        rarity across the memory set (a word most memories contain can't anchor
        a match) and doubled on curated topic/entity labels. A row only scores
        at all on a real signal — two distinctive shared words, or one long
        (>=8-char) specific one. Returns [(score, row)] best-first."""
        qwords = self._content_words(query)
        if not qwords or not rows:
            return []
        docs = [(r,) + self._memory_index_words(r) for r in rows]
        df = {}
        for _, words, _ in docs:
            for x in words:
                df[x] = df.get(x, 0) + 1
        n = len(docs)
        generic_cap = max(3, n // 4)
        scored = []
        for r, words, label_words in docs:
            shared = {x for x in (qwords & words) if df[x] <= generic_cap}
            if not shared:
                continue
            if len(shared) >= 2 or any(len(x) >= 8 for x in shared):
                score = sum(math.log(1 + n / df[x]) * (2 if x in label_words else 1)
                            for x in shared)
                scored.append((score, r))
        scored.sort(key=lambda t: -t[0])
        return scored

    # ---- recall -------------------------------------------------------------

    def recall_memories(self, message, k=3):
        """The ONE injection point for memory in a turn: every pinned row (the
        user forced those) plus at most k rows that clear the relevance bar.
        An unrelated message injects nothing — that's the point.
        Returns (context_block, used_texts) for the prompt and message footer."""
        rows = self.store.rows("select * from memory")
        if not rows:
            return "", []
        pinned = [r for r in rows if r.get("pinned")]
        pinned.sort(key=lambda r: r.get("last_used") or "", reverse=True)
        scored = self._score_memories(message, [r for r in rows if not r.get("pinned")])
        hits = pinned[:6] + [r for _, r in scored[:k]]
        if not hits:
            return "", []
        now = now_iso()
        for r in hits:
            self.store.exec("update memory set use_count=?, last_used=? where id=?",
                            ((r.get("use_count") or 0) + 1, now, r["id"]))
        block = ("Long-term memory relevant to this request (background knowledge — "
                 "use it, don't recite it):\n" + "\n".join(self._memory_line(r) for r in hits))
        return block, [r.get("text") or "" for r in hits]

    @staticmethod
    def _memory_line(r):
        tag = " · ".join(x for x in ((r.get("topic") or r.get("category") or "").strip(),
                                     (r.get("kind") or "").strip()) if x)
        return (f"- [{tag}] " if tag else "- ") + (r.get("text") or "")

    # ---- retain -------------------------------------------------------------

    def retain_memory(self, item):
        """Write one typed memory. A near-duplicate on the same topic page
        (Jaccard >= 0.6 or containment) merges into the existing row — the page
        gets edited, the log doesn't grow. Returns (id, 'added'|'merged') or
        (None, 'skipped')."""
        text = (item.get("text") or "").strip()
        if not text:
            return None, "skipped"
        kind = item.get("kind") if item.get("kind") in MEMORY_KINDS else "world"
        topic = memory_slug(item.get("topic") or item.get("category") or "general")
        entities = item.get("entities") or ""
        if isinstance(entities, (list, tuple)):
            entities = ", ".join(str(e).strip() for e in entities if str(e).strip())
        words = self._content_words(text)
        now = now_iso()
        for r in self.store.rows("select * from memory where topic=? or category=?", (topic, topic)):
            rw = self._content_words(r.get("text") or "")
            if not words or not rw:
                continue
            j = len(words & rw) / len(words | rw)
            if j >= 0.6 or words <= rw or rw <= words:
                # Same fact, possibly refreshed — keep the fuller phrasing.
                new_text = text if len(text) >= len(r.get("text") or "") else r["text"]
                self.store.exec(
                    "update memory set text=?, kind=?, topic=?, category=?, entities=?, updated_at=? where id=?",
                    (new_text, kind, topic, topic, entities or r.get("entities") or "", now, r["id"]))
                return r["id"], "merged"
        mid = "mem_" + os.urandom(6).hex()
        self.store.exec(
            "insert into memory(id,text,category,kind,topic,entities,pinned,created_at,updated_at)"
            " values(?,?,?,?,?,?,0,?,?)",
            (mid, text, topic, kind, topic, entities, now, now))
        return mid, "added"

    def memory_topics(self):
        """Existing page names, for the extractor prompt (reuse before invent)."""
        rows = self.store.rows("select distinct topic, category from memory")
        topics = {(r.get("topic") or r.get("category") or "").strip() for r in rows}
        return sorted(t for t in topics if t)[:30]

    # ---- HTTP / tool surface (Brain tab contract unchanged) ------------------

    def upsert_memory(self, body, memory_id=None):
        mid = memory_id or body.get("id") or body.get("memory_id") or "mem_" + os.urandom(6).hex()
        now = now_iso()
        ex = self.store.one("select * from memory where id=?", (mid,)) or {}
        text = body.get("text") if "text" in body else ex.get("text")
        # The UI's "category" and the protocol's "topic" are the same page name.
        topic = memory_slug(body.get("topic") or body.get("category")
                            or ex.get("topic") or ex.get("category") or "general")
        kind = body.get("kind") if body.get("kind") in MEMORY_KINDS else (ex.get("kind") or "world")
        entities = body.get("entities", ex.get("entities") or "")
        if isinstance(entities, (list, tuple)):
            entities = ", ".join(str(e).strip() for e in entities if str(e).strip())
        pinned = body.get("pinned")
        pinned = 1 if pinned is True else 0 if pinned is False else (ex.get("pinned") or 0)
        if ex:
            self.store.exec(
                "update memory set text=?, category=?, topic=?, kind=?, entities=?, pinned=?, updated_at=? where id=?",
                (text or "", topic, topic, kind, entities, pinned, now, mid))
        else:
            self.store.exec(
                "insert into memory(id,text,category,kind,topic,entities,pinned,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                (mid, text or "", topic, kind, topic, entities, pinned, now, now))
        return {"ok": True, "id": mid,
                "memory": self.memory_json(self.store.one("select * from memory where id=?", (mid,)))}

    def manage_memory_tool(self, body):
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").lower()
        if action == "add":
            mid, how = self.retain_memory(args)
            if not mid:
                return "Nothing to add — pass `text`."
            return f"Merged into existing memory {mid}" if how == "merged" else f"Added memory {mid}"
        if action == "edit":
            mid = args.get("id") or args.get("memory_id")
            if not mid:
                raise ValueError("memory id required")
            self.upsert_memory(args, mid)
            return f"Edited memory {mid}"
        if action == "delete":
            mid = args.get("id") or args.get("memory_id")
            if not mid:
                raise ValueError("memory id required")
            self.store.exec("delete from memory where id=?", (mid,))
            return f"Deleted memory {mid}"
        rows = self.store.rows("select * from memory order by pinned desc, datetime(created_at) desc")
        q = args.get("text") or args.get("query") or ""
        if action == "search" and q.strip():
            rows = [r for _, r in self._score_memories(q, rows)][:10]
            now = now_iso()
            for r in rows:   # recalled = used
                self.store.exec("update memory set use_count=?, last_used=? where id=?",
                                ((r.get("use_count") or 0) + 1, now, r["id"]))
        return json.dumps([self.memory_json(r) for r in rows], ensure_ascii=False)

    @staticmethod
    def memory_json(r):
        return {
            "id": r["id"],
            "text": r["text"],
            "category": r.get("topic") or r.get("category") or "general",
            "kind": r.get("kind") or "world",
            "topic": r.get("topic") or "",
            "entities": r.get("entities") or "",
            "pinned": bool(r.get("pinned")),
            "use_count": r.get("use_count") or 0,
            "last_used": r.get("last_used") or "",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at") or "",
        }
