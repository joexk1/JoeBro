"""Spend tracking: live provider balances + per-model usage analytics.

GET /api/spend?days=N  (see SpendMixin.spend_payload)

Sources
  balances  OpenRouter GET /api/v1/credits, DeepSeek GET /user/balance, using the
            keys already stored on the `endpoints` rows (never printed/logged).
            Cached BALANCE_TTL seconds.
  usage     the `spend_log` table, written by record_usage() after every model
            call in jb_chat.py. Cost is REAL when the provider reports it
            (OpenRouter usage.cost); otherwise tokens x model price, flagged
            estimated. One-off backfill from old chat metadata is all estimated.
Neither provider exposes per-model activity to a normal key (OpenRouter's
/activity needs a management key; DeepSeek has none), hence the local ledger.

The ledger only sees calls THIS backend made. Spend from anything else on the same
OpenRouter account shows up only in /credits total_usage, so the payload carries an
ADDITIVE `accounts[]` block: account-wide usage vs ledger-attributed vs untracked.
If a management key is present (env OPENROUTER_MANAGEMENT_KEY, or the file
$JOEBRO_DATA_DIR/openrouter-management.key) /api/v1/activity is also read and returned
as `account_usage[]` (true per-model, per-day, all callers, last 30 completed UTC days).
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

BALANCE_TTL = 300          # seconds a good balance is reused
ERROR_TTL = 60             # seconds a failed lookup is reused
HTTP_TIMEOUT = 8
PRICE_TTL = 24 * 3600
ACTIVITY_TTL = 900
_UA = "Mozilla/5.0 JoeBro-spend"

_lock = threading.Lock()
_bal_cache = {}            # provider id -> (expires, dict)
_price_cache = {"exp": 0, "map": {}}
_init_done = set()
_act_cache = {"exp": 0, "val": None}

# USD per token (in, out, cached-in). Fallback when OpenRouter's price list has no match.
STATIC_PRICES = {
    "deepseek-chat": (0.27e-6, 1.10e-6, 0.07e-6),
    "deepseek-reasoner": (0.55e-6, 2.19e-6, 0.14e-6),
}


def provider_for(base_url):
    u = (base_url or "").lower()
    for needle, pid in (("openrouter.ai", "openrouter"), ("api.deepseek.com", "deepseek"),
                        ("anthropic.com", "anthropic"), ("api.openai.com", "openai"),
                        ("groq.com", "groq"), ("cerebras", "cerebras"),
                        ("googleapis.com", "gemini")):
        if needle in u:
            return pid
    return "local" if any(x in u for x in (":11434", "localhost", "127.0.0.1", "100.")) else "other"


def _http_json(url, key=""):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_openrouter(key):
    d = (_http_json("https://openrouter.ai/api/v1/credits", key) or {}).get("data") or {}
    total, used = float(d.get("total_credits") or 0), float(d.get("total_usage") or 0)
    return {"balance": round(total - used, 4), "currency": "USD", "limit": round(total, 4),
            "usage_total": round(used, 4)}


def fetch_deepseek(key):
    d = _http_json("https://api.deepseek.com/user/balance", key) or {}
    infos = d.get("balance_infos") or []
    if not infos:
        raise ValueError("no balance_infos in response")
    pick = next((i for i in infos if i.get("currency") == "USD"), infos[0])
    out = {"balance": round(float(pick.get("total_balance") or 0), 4),
           "currency": pick.get("currency") or "USD", "limit": None, "usage_total": None}
    if d.get("is_available") is False:
        out["error"] = "account reports balance unavailable"
    return out


PROVIDERS = (("openrouter", "OpenRouter", fetch_openrouter), ("deepseek", "DeepSeek", fetch_deepseek))


def _blank(pid, name, **kw):
    r = {"id": pid, "name": name, "balance": None, "currency": "USD", "limit": None,
         "usage_total": None, "ok": False, "error": ""}
    r.update(kw)
    return r


def _model_prices():
    """{openrouter model id: (in, out, cached_in)} from OpenRouter's public list, cached."""
    now = time.time()
    with _lock:
        if _price_cache["map"] and now < _price_cache["exp"]:
            return _price_cache["map"]
    try:
        m = {}
        for e in (_http_json("https://openrouter.ai/api/v1/models") or {}).get("data") or []:
            p = e.get("pricing") or {}
            try:
                pi, po = float(p.get("prompt")), float(p.get("completion"))
            except (TypeError, ValueError):
                continue
            m[e["id"]] = (pi, po, float(p["input_cache_read"]) if p.get("input_cache_read") else pi)
        with _lock:
            _price_cache.update(exp=now + PRICE_TTL, map=m)
        return m
    except Exception:
        with _lock:
            _price_cache["exp"] = now + 300       # retry soon, keep stale map
            return _price_cache["map"]


def price_for(provider, model):
    """(in, out, cached_in) per token, or None. For non-OpenRouter providers this
    borrows OpenRouter's price for the same model id, so it is an ESTIMATE."""
    if provider == "local":
        return (0.0, 0.0, 0.0)
    m = (model or "").lower().lstrip("~")
    if m in STATIC_PRICES:
        return STATIC_PRICES[m]
    prices = _model_prices()
    cands = [m, "deepseek/" + m, "deepseek/" + m + "-latest", "anthropic/" + m, "openai/" + m]
    if "deepseek" in m and "flash" in m:
        cands.append("deepseek/deepseek-v4-flash")
    if "deepseek" in m and "pro" in m:
        cands.append("deepseek/deepseek-v4-pro")
    for c in cands:
        if c in prices:
            return prices[c]
    return None


def management_key():
    k = (os.environ.get("OPENROUTER_MANAGEMENT_KEY") or "").strip()
    if k:
        return k
    try:
        from jb_core import data_dir; root = str(data_dir())
        with open(os.path.join(root, "openrouter-management.key")) as f:
            return f.read().strip()
    except Exception:
        return ""


def fetch_activity(mkey):
    """OpenRouter GET /api/v1/activity -> rows shaped like usage[] (all callers on the account)."""
    rows = []
    for e in (_http_json("https://openrouter.ai/api/v1/activity", mkey) or {}).get("data") or []:
        rows.append({"date": e.get("date"), "provider": "openrouter", "model": e.get("model") or "",
                     "requests": int(e.get("requests") or 0),
                     "tokens_in": int(e.get("prompt_tokens") or 0),
                     "tokens_out": int(e.get("completion_tokens") or 0),
                     "cost": round(float(e.get("usage") or 0) + float(e.get("byok_usage_inference") or 0), 6),
                     "estimated": False})
    rows.sort(key=lambda r: (r["date"] or "", r["cost"]), reverse=True)
    return rows


def _day(ts=None):
    return (ts or datetime.now()).strftime("%Y-%m-%d")


class SpendMixin:
    # ---- storage -------------------------------------------------------
    def _spend_init(self):
        key = str(self.store.db_path)
        if key in _init_done:
            return
        self.store.exec(
            "create table if not exists spend_log(id integer primary key autoincrement, ts text, day text, "
            "provider text, endpoint_id text, model text, tokens_in integer default 0, "
            "tokens_out integer default 0, cost real default 0, estimated integer default 0, source text default '')")
        self.store.exec("create index if not exists spend_log_day on spend_log(day)")
        _init_done.add(key)
        try:
            self._spend_backfill()
        except Exception:
            pass

    def _spend_backfill(self):
        """One-off: turn old assistant-message metadata into ESTIMATED usage rows."""
        if self.store.one("select value from prefs where key='spend_backfilled'"):
            return
        self.store.exec("insert or replace into prefs(key,value) values('spend_backfilled','true')")
        eps = {e["id"]: e["base_url"] for e in self.store.rows("select id,base_url from endpoints")}
        rows = self.store.rows(
            "select m.metadata, m.created_at, s.endpoint_id from messages m join sessions s on s.id=m.session_id "
            "where m.role='assistant' and m.metadata like '%\"model\"%'")
        for r in rows:
            try:
                d = json.loads(r["metadata"] or "{}")
            except Exception:
                continue
            model, base = d.get("model"), eps.get(r["endpoint_id"] or "")
            if not model or not base:
                continue
            window = self._context_window(model)
            if d.get("output_tokens") is not None:
                tin, tout = int(d.get("input_tokens") or 0), int(d.get("output_tokens") or 0)
            else:
                tout = int((d.get("tokens_per_second") or 0) * (d.get("response_time") or 0))
                tin = max(0, int((d.get("context_percent") or 0) / 100.0 * window) - tout)
            prov = provider_for(base)
            self._spend_insert(r["created_at"][:10], r["created_at"], prov, r["endpoint_id"], model,
                               tin, tout, None, "backfill")

    def _spend_insert(self, day, ts, prov, ep_id, model, tin, tout, cost, source, cached=0):
        est = 0
        if cost is None:
            est = 1
            p = price_for(prov, model)
            cost = 0.0 if p is None else (max(0, tin - cached) * p[0] + cached * p[2] + tout * p[1])
        elif source != "provider":
            est = 1
        self.store.exec(
            "insert into spend_log(ts,day,provider,endpoint_id,model,tokens_in,tokens_out,cost,estimated,source) "
            "values(?,?,?,?,?,?,?,?,?,?)",
            (ts, day, prov, ep_id or "", model or "", int(tin), int(tout), float(cost), est, source))

    def record_usage(self, endpoint_id, model, usage=None, est_in_chars=0, est_out_chars=0):
        """Log one model call. `usage` is the provider's usage dict (OpenAI shape);
        without it tokens are estimated from char counts (~4 chars/token). Never raises."""
        try:
            self._spend_init()
            ep = self.store.one("select base_url from endpoints where id=?", (endpoint_id or "",))
            if not ep or not model:
                return
            prov = provider_for(ep["base_url"])
            u = usage or {}
            now = datetime.now()
            if u.get("prompt_tokens") is not None or u.get("completion_tokens") is not None:
                tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
                cached = int(u.get("prompt_cache_hit_tokens")
                             or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                cost = u.get("cost")
                if cost is not None:
                    self._spend_insert(_day(now), now.isoformat(), prov, endpoint_id, model, tin, tout,
                                       float(cost), "provider")
                else:
                    self._spend_insert(_day(now), now.isoformat(), prov, endpoint_id, model, tin, tout,
                                       None, "tokens", cached)
            else:
                self._spend_insert(_day(now), now.isoformat(), prov, endpoint_id, model,
                                   est_in_chars // 4, est_out_chars // 4, None, "chars")
        except Exception:
            pass

    # ---- balances ------------------------------------------------------
    def _provider_key(self, pid):
        needle = {"openrouter": "openrouter.ai", "deepseek": "api.deepseek.com"}[pid]
        for e in self.store.rows("select api_key,base_url from endpoints where is_enabled=1 "
                                 "and coalesce(api_key,'')!='' order by created_at"):
            if needle in (e["base_url"] or "").lower():
                return e["api_key"]
        return ""

    def spend_providers(self, force=False):
        out = []
        for pid, name, fn in PROVIDERS:
            now = time.time()
            with _lock:
                hit = _bal_cache.get(pid)
            if hit and now < hit[0] and not force:
                out.append(dict(hit[1])); continue
            key = self._provider_key(pid)
            if not key:
                res = _blank(pid, name, error="no API key configured")
            else:
                try:
                    res = _blank(pid, name, ok=True, **fn(key))
                except Exception as exc:
                    # type only: urllib errors can embed request detail
                    code = getattr(exc, "code", "")
                    res = _blank(pid, name, error=f"{type(exc).__name__} {code}".strip())
            with _lock:
                _bal_cache[pid] = (now + (BALANCE_TTL if res["ok"] else ERROR_TTL), res)
            out.append(dict(res))
        return out

    def spend_activity(self, force=False):
        """(rows, status) for account-wide per-model usage; status in no_key|ok|error:<Type> <code>."""
        mkey = management_key()
        if not mkey:
            return [], "no_management_key"
        now = time.time()
        with _lock:
            if _act_cache["val"] is not None and now < _act_cache["exp"] and not force:
                return _act_cache["val"]
        try:
            val = (fetch_activity(mkey), "ok")
            ttl = ACTIVITY_TTL
        except Exception as exc:
            val = ([], f"error: {type(exc).__name__} {getattr(exc, 'code', '')}".strip())
            ttl = ERROR_TTL
        with _lock:
            _act_cache.update(exp=now + ttl, val=val)
        return val

    def spend_accounts(self, providers, act_status):
        out = []
        for p in providers:
            if p["id"] != "openrouter":
                continue
            mine = (self.store.one("select coalesce(sum(cost),0) c from spend_log where provider='openrouter'") or {}).get("c") or 0
            real = (self.store.one("select coalesce(sum(cost),0) c from spend_log where provider='openrouter' and estimated=0") or {}).get("c") or 0
            acct = p.get("usage_total")
            row = {"provider": "openrouter", "name": p["name"], "ok": p["ok"],
                   "account_usage": acct, "joebro_attributed": round(mine, 4),
                   "joebro_attributed_exact": round(real, 4),
                   "untracked": None if acct is None else round(max(0.0, acct - mine), 4),
                   "breakdown_available": act_status == "ok", "breakdown_status": act_status,
                   "note": ("Account-wide usage (all apps on this key's account, lifetime) vs calls made through "
                            "JoeBro. 'untracked' is spend from other apps, which has no per-model detail "
                            "unless a management key is configured.")}
            out.append(row)
        return out

    # ---- payload -------------------------------------------------------
    def spend_payload(self, days=30, force=False):
        self._spend_init()
        try:
            days = max(1, min(int(days), 3650))
        except (TypeError, ValueError):
            days = 30
        today = datetime.now().date()
        since = (today - timedelta(days=days - 1)).isoformat()
        usage = [{"date": r["day"], "provider": r["provider"], "model": r["model"], "requests": r["n"],
                  "tokens_in": r["ti"], "tokens_out": r["to"], "cost": round(r["c"], 6),
                  "estimated": bool(r["e"])}
                 for r in self.store.rows(
                     "select day,provider,model,count(*) n,sum(tokens_in) ti,sum(tokens_out) 'to',sum(cost) c,"
                     "max(estimated) e from spend_log where day>=? group by day,provider,model "
                     "order by day desc, c desc", (since,))]

        def total(frm):
            return round((self.store.one("select coalesce(sum(cost),0) c from spend_log where day>=?", (frm,)) or {}).get("c") or 0, 4)
        first = self.store.one("select min(day) d from spend_log") or {}
        providers = self.spend_providers(force)
        act, act_status = self.spend_activity(force)
        act = [r for r in act if (r["date"] or "") >= since]
        return {"ok": True, "providers": providers, "usage": usage,
                "accounts": self.spend_accounts(providers, act_status), "account_usage": act,
                "totals": {"today": total(today.isoformat()),
                           "week": total((today - timedelta(days=6)).isoformat()),
                           "month": total(today.replace(day=1).isoformat()),
                           "all_time": total("0000-00-00"), "currency": "USD"},
                "tracking_since": first.get("d")}
