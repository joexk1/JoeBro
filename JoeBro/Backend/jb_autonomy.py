"""Autonomous routines: small scheduled checks that run with NO model call and
file a short report in the Inbox (or notify, if the user opted in).

Core ships the machinery and no routines. An extension supplies them through
the `autonomy_routines` hook, which returns a dict:

    {routine_id: {"title": str, "enabled": bool, "notify": bool, "every_days": int,
                  "at": "HH:MM", "timezone": IANA id (default: this machine's),
                  "quiet_hours": "HH:MM-HH:MM", "topics": [], "channel_ids": [],
                  "report": fn(cfg, previous, read) -> {"status": "ok"|..., "body": str, ...}}}

`report` runs at most once per slot; `previous` is the last good result, `read`
is None in production (the routine fetches for itself) or a test double.
The tick is `run_autonomy_due`, called from the pulse loop once a minute.
"""
import hashlib
import json
import re
import secrets
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jb_ext

LOCK = threading.Lock()
PREF = "autonomy_routines_v1"
ALLOWED = {"enabled", "notify", "every_days", "at", "timezone", "quiet_hours", "topics", "channel_ids"}
BASE = {"title": "", "enabled": False, "notify": False, "every_days": 1, "at": "09:00",
        "quiet_hours": "23:00-07:30", "topics": [], "channel_ids": []}


class AutonomyError(ValueError):
    pass


def _local_tz_name():
    try:
        name = open("/etc/timezone").read().strip()
        ZoneInfo(name)
        return name
    except Exception:
        pass
    try:
        return datetime.now().astimezone().tzinfo.key
    except Exception:
        return "UTC"


def routines():
    """{id: template} merged from every extension (later extensions win)."""
    out = {}
    for group in jb_ext.call("autonomy_routines"):
        for rid, tpl in group.items():
            cfg = deepcopy(BASE)
            cfg["timezone"] = _local_tz_name()
            cfg.update({k: v for k, v in tpl.items() if k != "report"})
            cfg["title"] = cfg["title"] or rid
            out[rid] = cfg
    return out


def _reporter(routine):
    for group in jb_ext.call("autonomy_routines"):
        if routine in group and group[routine].get("report"):
            return group[routine]["report"]
    return None


def local_now(cfg, epoch):
    return datetime.fromtimestamp(epoch, ZoneInfo(cfg["timezone"]))


def in_quiet(cfg, epoch):
    start, end = cfg["quiet_hours"].split("-")
    current = local_now(cfg, epoch).strftime("%H:%M")
    return False if start == end else (start <= current < end if start < end else current >= start or current < end)


def next_due(cfg, epoch):
    local = local_now(cfg, epoch)
    hour, minute = map(int, cfg["at"].split(":"))
    return (local + timedelta(days=cfg["every_days"])).replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def due(cfg, epoch):
    if not cfg["enabled"] or in_quiet(cfg, epoch):
        return False
    if cfg.get("next_due_at"):
        return epoch >= cfg["next_due_at"]
    return local_now(cfg, epoch).strftime("%H:%M") >= cfg["at"]


def _clock(v):
    return isinstance(v, str) and bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v))


def validated(routine, settings, known=None):
    if routine not in (known if known is not None else routines()):
        raise AutonomyError("Unknown routine")
    if not isinstance(settings, dict) or set(settings) - ALLOWED:
        raise AutonomyError("Unsupported routine settings")
    cfg = deepcopy(settings)
    for key in ("enabled", "notify"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise AutonomyError(key + " must be boolean")
    if "every_days" in cfg and (isinstance(cfg["every_days"], bool) or not isinstance(cfg["every_days"], int)
                                or not 1 <= cfg["every_days"] <= 30):
        raise AutonomyError("every_days must be 1 to 30")
    if "at" in cfg and not _clock(cfg["at"]):
        raise AutonomyError("at must be HH:MM")
    if "quiet_hours" in cfg and (not isinstance(cfg["quiet_hours"], str) or len(cfg["quiet_hours"].split("-")) != 2
                                 or not all(_clock(v) for v in cfg["quiet_hours"].split("-"))):
        raise AutonomyError("quiet_hours must be HH:MM-HH:MM")
    if "timezone" in cfg:
        try:
            ZoneInfo(cfg["timezone"])
        except Exception:
            raise AutonomyError("timezone must be an IANA timezone") from None
    for key in ("topics", "channel_ids"):
        if key in cfg:
            values = cfg[key]
            if not isinstance(values, list) or len(values) > 3 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 150 for v in values):
                raise AutonomyError(key + " must contain up to 3 nonempty strings")
            cfg[key] = list(dict.fromkeys(v.strip() for v in values))
    return cfg


def _paused(handler):
    return str(handler.pref("personal_automation_paused")).lower() in ("true", "1", "yes", "on")


class AutonomyMixin:
    def autonomy_config(self):
        saved = self.pref(PREF) or {}
        result = routines()
        for key in result:
            if isinstance(saved.get(key), dict):
                result[key].update(deepcopy(saved[key]))
        return result

    def autonomy_facts(self):
        return "\n".join(
            f"{cfg['title']}: {cfg.get('latest', {}).get('body') or cfg.get('last_status', 'not run')}"
            for cfg in self.autonomy_config().values() if cfg["enabled"])

    def _autonomy_save(self, config):
        self.set_pref(PREF, config)

    def manage_autonomy_tool(self, body):
        args = self.parse_tool_args(body)
        action = args.get("action", "list")
        routine = args.get("routine")
        known = self.autonomy_config()
        if action == "list":
            return json.dumps({"ok": True, "routines": {k: {x: y for x, y in v.items()} for k, v in known.items()},
                               "llm_calls_per_check": 0}, ensure_ascii=False)
        if routine not in known:
            raise AutonomyError("Choose one of: " + ", ".join(known) if known else "No routines are installed")
        if action == "run":
            return json.dumps(self.run_autonomy(routine, force=True), ensure_ascii=False)
        if action not in ("set", "enable", "pause"):
            raise AutonomyError("action must be list, set, enable, pause or run")
        with LOCK:
            config = self.autonomy_config()
            cfg = config[routine]
            settings = validated(routine, args.get("settings", {}) if action == "set" else {"enabled": action == "enable"}, known)
            cfg.update(settings)
            if set(settings) & {"topics", "channel_ids"}:
                cfg.pop("latest", None)
                cfg.pop("fingerprint", None)
            if set(settings) & {"at", "timezone", "every_days", "enabled"}:
                cfg.pop("next_due_at", None)
            self._autonomy_save(config)
            return json.dumps({"ok": True, "routine": routine, "config": cfg}, ensure_ascii=False)

    def _autonomy_publish(self, routine, cfg, body):
        if _paused(self):
            return {"ok": True, "published": False, "reason": "personal automation paused"}
        if cfg["notify"]:
            return self.notify(body[:1500], title=cfg["title"], kind="autonomy", source=routine)
        nid = "inb_" + secrets.token_hex(6)
        self.store.exec("insert into inbox(id,kind,title,body,source,read,created_at) values(?,?,?,?,?,0,?)",
                        (nid, "autonomy", cfg["title"], body[:1500], routine, datetime.now(timezone.utc).isoformat()))
        return {"ok": True, "id": nid, "external_notification": False}

    def run_autonomy(self, routine, force=False, epoch=None, read=None):
        if _paused(self):
            return {"ok": True, "ran": False, "reason": "personal automation paused"}
        report = _reporter(routine)
        if report is None:
            raise AutonomyError("Unknown routine")
        if not LOCK.acquire(blocking=False):
            return {"ok": True, "ran": False, "reason": "routine check already running"}
        epoch = time.time() if epoch is None else epoch
        try:
            config = self.autonomy_config()
            cfg = config[routine]
            if not force and not due(cfg, epoch):
                return {"ok": True, "ran": False, "reason": "not due"}
            missing = jb_ext.first("autonomy_precheck", routine, cfg)    # e.g. "topics required"
            if missing:
                return {"ok": False, "ran": False, "reason": missing}
            # Claim before the network: a failure or restart must not cause a retry storm.
            cfg.update(last_attempt_at=epoch, next_due_at=next_due(cfg, epoch))
            self._autonomy_save(config)
            previous = cfg.get("latest") or {}
            try:
                result = report(cfg, previous, read)
            except Exception:
                result = {"status": "unavailable", "reason": "Check unavailable; retrying at the next scheduled slot."}
            cfg["last_status"] = result["status"]
            cfg["last_checked_at"] = epoch
            if result["status"] != "ok":
                cfg["last_error"] = result.get("reason")
                self._autonomy_save(config)
                return {"ok": True, "ran": True, "routine": routine, "status": result["status"],
                        "reason": result.get("reason"), "notified": False}
            stable = {k: v for k, v in result.items() if k not in ("snapshot_checked_at", "seen_video_ids")}
            fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
            changed = fingerprint != cfg.get("fingerprint")
            cfg.update(latest=result, fingerprint=fingerprint, last_success_at=epoch)
            cfg.pop("last_error", None)
            config[routine] = cfg
            self._autonomy_save(config)
            publish = changed and not result.get("baseline_only") and result.get("publish", True)
            delivery = self._autonomy_publish(routine, cfg, result["body"]) if publish else None
            return {"ok": True, "ran": True, "routine": routine, "status": "ok", "changed": changed, "report": result,
                    "published": bool(delivery and delivery.get("published", True)),
                    "notified": bool(delivery and cfg["notify"] and delivery.get("delivered")), "llm_calls": 0}
        finally:
            LOCK.release()

    def run_autonomy_due(self):
        if _paused(self):
            return []
        epoch = time.time()
        try:
            self.rain_alert_check(epoch)
        except Exception:
            pass
        return [self.run_autonomy(r, epoch=epoch) for r, cfg in self.autonomy_config().items() if due(cfg, epoch)]
