"""Importance gate for every proactive push (pulse, heartbeat, proactive).

Default is NOT to ping. Plain code decides first (sender history, labels, keywords,
deadlines, status); a model is only ever asked about items that already passed.
Pref `pulse_ping_level`: "important" (default) or "normal".

Pings: things needing a decision soon (approval, agent waiting/blocked, Night Shift
review ready), time-critical items (event starting soon, reply-by deadline today),
messages from people the user actually corresponds with that sound urgent, and
failures of things the user started. Never: newsletters, receipts, marketing,
notifications, "N new emails" summaries.

Rate limit: a few non-actionable pings a day (pref `pulse_ping_daily_cap`, default 3),
actionable ones are exempt. Similar pings inside 12 hours are dropped.
"""
import re
import time
from datetime import datetime, timedelta

LEVELS = ("important", "normal")
ACTIONABLE = {"approval", "agent_waiting", "night_review", "failure", "calendar_soon", "deadline_today"}

_BULK_SENDER = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|marketing|notifications?@|alerts?@|mailer|"
                          r"updates?@|news@|promo|deals?@|offers?@|billing@|receipts?@|orders?@|support@|info@|hello@|team@)", re.I)
_BULK_TEXT = re.compile(r"(unsubscribe|newsletter|receipt|your order|order confirmation|invoice|payment received|"
                        r"% off|\bsale\b|discount|promo|digest|webinar|weekly roundup|view in browser|"
                        r"verification code|security alert|statement is ready|shipped|delivered)", re.I)
_SUMMARY = re.compile(r"\b(\d+|several|a few|some)\s+(new\s+|unread\s+)?(emails?|messages?|mails?)\b|\bnew (emails?|mail)\b|"
                      r"\binbox\b.*\b(has|have|contains)\b", re.I)
_URGENT = re.compile(r"(urgent|asap|as soon as possible|emergency|time[- ]sensitive|immediately|right away|important)", re.I)
_TODAY = re.compile(r"(\btoday\b|\btonight\b|\bby (eod|end of (the )?day|noon|\d{1,2}(:\d\d)?\s*(am|pm))\b|\bdeadline\b|"
                    r"\bby this (morning|afternoon|evening)\b)", re.I)
_ASK = re.compile(r"(\?|can you|could you|please|need you|let me know|reply|respond|confirm)", re.I)


def _parse(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if "T" in str(v) else None
    except ValueError:
        return None


def level_of(value):
    v = str(value or "").strip().lower()
    return v if v in LEVELS else "important"


def is_bulk(item):
    sender = str(item.get("from") or "")
    text = f"{item.get('title') or ''} {item.get('text') or ''}"
    if item.get("bulk") or item.get("labels") and {"promotions", "social", "updates", "forums", "category_promotions"} & {
            str(x).lower() for x in item["labels"]}:
        return True
    return bool(_BULK_SENDER.search(sender) or _BULK_TEXT.search(text) or _SUMMARY.search(str(item.get("title") or "")))


def classify(item, now=None, level="important", known=()):
    """(ping, category, reason) for one candidate item. Pure. Default: no."""
    level = level_of(level)
    now = now or datetime.now().astimezone()
    item = dict(item)
    for k in ("start",):
        if isinstance(item.get(k), str):
            item[k] = _parse(item[k])
    if "start" not in item and item.get("start_iso"):
        item["start"] = _parse(item["start_iso"])
    src = str(item.get("source") or "").lower()
    status = str(item.get("status") or "").lower()
    if src in ("approval",) or status in ("awaiting_approval", "needs_approval", "pending_approval"):
        return True, "approval", "waiting for a decision"
    if src in ("agent", "agents") and status in ("waiting", "blocked", "needs_input", "awaiting_input"):
        return True, "agent_waiting", "an agent is waiting"
    if src == "night" and status == "review":
        return True, "night_review", "Night Shift review ready"
    if status == "failed" and item.get("started_by_user", True) and src in ("agent", "agents", "night", "tasks", "task", "scheduled"):
        return True, "failure", "something you started failed"
    if src in ("calendar", "event"):
        start = item.get("start")
        if isinstance(start, datetime) and not item.get("all_day"):
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            mins = (start - now).total_seconds() / 60
            if -5 <= mins <= (30 if level == "important" else 90):
                return True, "calendar_soon", "event starting soon"
        return False, "", "not soon"
    if src in ("reminder", "reminders"):
        due = item.get("start")
        if isinstance(due, datetime):
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
            if (due - now).total_seconds() / 60 <= 30:
                return True, "deadline_today", "reminder due"
        return False, "", "not due"
    if src in ("email", "mail", "whatsapp", "message", "messages", "telegram"):
        if is_bulk(item):
            return False, "", "bulk"
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        sender = str(item.get("from") or "").lower()
        is_known = bool(item.get("known_sender")) or any(k and k.lower() in sender for k in known)
        urgent, today = bool(_URGENT.search(text)), bool(_TODAY.search(text))
        if is_known and (urgent or (today and _ASK.search(text))):
            return True, "deadline_today" if today else "personal_urgent", "known contact, time-sensitive"
        if level == "normal" and urgent and today:
            return True, "personal_urgent", "urgent and due today"
        return False, "", "routine message"
    return False, "", "not a ping category"


def filter_data(data, now=None, level="important", known=()):
    """Reduce a bg_collect dict to the items worth a model call. In 'normal' level
    non-message sources are kept whole (old behaviour), messages still get screened."""
    level = level_of(level)
    out, hits = {}, []
    for source, items in (data or {}).items():
        keep = []
        for it in items:
            probe = dict(it, source=it.get("kind_source") or _SRC.get(source, source))
            ping, cat, _ = classify(probe, now, level, known)
            if ping:
                keep.append(dict(it, category=cat))
        if keep:
            out[source] = keep
            hits += keep
    return out, hits


_SRC = {"calendar": "calendar", "reminders": "reminders", "tasks": "tasks", "attention": "attention",
        "whatsapp": "whatsapp", "email": "email", "bank": "bank"}


def looks_like_summary(text):
    t = str(text or "")
    return bool(_SUMMARY.search(t)) and not _URGENT.search(t)


def _norm(text):
    return set(w for w in re.findall(r"[a-z0-9]{3,}", str(text or "").lower()))


def allow_ping(handler, text, category="", now=None):
    """Rate limit + dedupe. Records the ping if allowed. `handler` needs pref/set_pref."""
    now_ts = (now.timestamp() if isinstance(now, datetime) else None) or time.time()
    log = handler.pref("ping_log")
    log = [e for e in (log if isinstance(log, list) else []) if now_ts - float(e.get("t", 0)) < 86400]
    words = _norm(text)
    for e in log:
        if now_ts - float(e.get("t", 0)) < 12 * 3600:
            old = set(e.get("w") or [])
            if words and old and len(words & old) / max(1, len(words | old)) >= 0.5:
                handler.set_pref("ping_log", log)
                return False, "duplicate"
    cap = handler.pref("pulse_ping_daily_cap")
    cap = 3 if cap in (None, "") else int(cap)
    if category not in ACTIONABLE and sum(1 for e in log if e.get("c") not in ACTIONABLE) >= cap:
        handler.set_pref("ping_log", log)
        return False, "daily cap"
    log.append({"t": now_ts, "c": category, "w": sorted(words)[:40]})
    handler.set_pref("ping_log", log[-60:])
    return True, "ok"


def attention_items(handler, now=None):
    """Plain-code read of things waiting on the user: agents waiting/blocked, Night Shift
    reviews, recent failures. Returns bg_collect-style items."""
    items = []
    try:
        for r in handler.store.rows("select id,title,status from agents where status in "
                                    "('waiting','blocked','needs_input','awaiting_input','awaiting_approval') limit 10"):
            items.append({"id": r["id"], "title": str(r["title"] or "")[:100], "text": r["status"],
                          "status": r["status"], "kind_source": "agent"})
        since = ((now or datetime.now().astimezone()) - timedelta(hours=6)).isoformat()
        for r in handler.store.rows("select id,title from agents where status='failed' and kind='task' and notify=1 "
                                    "and coalesce(finished_at,'')>=? limit 5", (since,)):
            items.append({"id": r["id"], "title": str(r["title"] or "")[:100], "text": "failed",
                          "status": "failed", "kind_source": "agent"})
    except Exception:
        pass
    try:
        for r in handler.store.rows("select id,title from night_tasks where status='review' limit 5"):
            items.append({"id": "night:" + r["id"], "title": str(r["title"] or "")[:100], "text": "review ready",
                          "status": "review", "kind_source": "night"})
    except Exception:
        pass
    return items
