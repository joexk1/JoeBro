"""Calendar: CalDAV sync, ICS, natural-language events."""
from jb_core import *  # noqa: F401,F403
from zoneinfo import ZoneInfo

import calendar as _pycal
from datetime import date
import logging as _logging

_cal_log = _logging.getLogger("joebro.caldav")

RECUR_SEP = "::occ::"          # id suffix of one expanded occurrence of a recurring series
CAL_WINDOW_PAST_DAYS = 30
CAL_WINDOW_FUTURE_DAYS = 365
_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class CalDAVError(RuntimeError):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class CalDAVConflict(CalDAVError):
    """The server copy changed (or exists) - nothing was written."""


class CalDAVReadOnly(CalDAVError):
    """Edit refused because it would be lossy/unsafe (recurring series)."""


class UnsupportedRRule(Exception):
    pass


# ---- ICS parsing ------------------------------------------------------------

def _unfold(text):
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_quoted(s, sep):
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
        if ch == sep and not q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _split_prop(line):
    """'NAME;P=v:value' -> (NAME, {P: v}, value); colons inside quoted params are ok."""
    q = False
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            left, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = _split_quoted(left, ";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def ics_vevents(text):
    """Top-level VEVENTs (VALARM etc. skipped) as [{NAME: [(params, value), ...]}]."""
    events, cur, depth = [], None, 0
    for line in _unfold(text):
        u = line.strip().upper()
        if cur is None:
            if u == "BEGIN:VEVENT":
                cur, depth = {}, 0
            continue
        if u.startswith("BEGIN:"):
            depth += 1
            continue
        if u.startswith("END:"):
            if depth:
                depth -= 1
            elif u == "END:VEVENT":
                events.append(cur)
                cur = None
            continue
        if depth:
            continue
        p = _split_prop(line)
        if p:
            cur.setdefault(p[0], []).append((p[1], p[2]))
    return events


def _zone(tzid, default_tz):
    if not tzid:
        return default_tz
    try:
        return ZoneInfo(tzid)
    except Exception:
        pass
    parts = tzid.strip("/").split("/")          # e.g. /softwarestudio.org/Tzfile/Europe/London
    for i in range(1, len(parts)):
        try:
            return ZoneInfo("/".join(parts[i:]))
        except Exception:
            continue
    raise ValueError(f"unknown TZID {tzid!r}")


def ics_time(value, params, default_tz):
    """-> (date, True) for all-day, else (aware datetime, False). A TZID is honoured,
    Z means UTC, a floating time is read in default_tz (the backend zone)."""
    v = (value or "").strip()
    if (params or {}).get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d").date(), True
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", v)
    if not m:
        raise ValueError(f"bad ICS time {value!r}")
    naive = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2):
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=_zone((params or {}).get("TZID"), default_tz)), False


def _first(ev, name):
    v = ev.get(name)
    return v[0] if v else (None, None)


def _ics_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (v or "").strip().upper())
    if not m:
        raise ValueError(f"bad DURATION {v!r}")
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _event_span(ev, default_tz):
    """-> (start, end, all_day); start/end are date (all-day, end exclusive) or aware datetimes."""
    p, v = _first(ev, "DTSTART")
    if v is None:
        raise ValueError("VEVENT without DTSTART")
    start, all_day = ics_time(v, p, default_tz)
    ep, ev_ = _first(ev, "DTEND")
    if ev_:
        end, _ = ics_time(ev_, ep, default_tz)
    else:
        dp, dv = _first(ev, "DURATION")
        if dv:
            end = start + _ics_duration(dv)
        else:
            end = start + timedelta(days=1) if all_day else start
    return start, end, all_day


def _key(x):
    """Identity of one occurrence: the UTC instant, or the date for all-day."""
    return x.astimezone(timezone.utc) if isinstance(x, datetime) else x


# ---- RRULE ------------------------------------------------------------------

def parse_rrule(text):
    r = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            r[k.upper()] = v
    extra = set(r) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
    if extra:
        raise UnsupportedRRule("unsupported RRULE part(s) " + ",".join(sorted(extra)))
    freq = r.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise UnsupportedRRule(f"unsupported FREQ {freq!r}")
    out = {"freq": freq, "interval": int(r.get("INTERVAL") or 1), "count": int(r["COUNT"]) if r.get("COUNT") else None,
           "until": r.get("UNTIL"), "byday": [], "bymonthday": [], "bymonth": [],
           "wkst": _WD.get((r.get("WKST") or "MO").upper(), 0)}
    if out["interval"] < 1:
        raise UnsupportedRRule("bad INTERVAL")
    for item in filter(None, (r.get("BYDAY") or "").upper().split(",")):
        m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item)
        if not m:
            raise UnsupportedRRule(f"bad BYDAY {item!r}")
        out["byday"].append((int(m.group(1) or 0), _WD[m.group(2)]))
    out["bymonthday"] = [int(x) for x in filter(None, (r.get("BYMONTHDAY") or "").split(","))]
    out["bymonth"] = [int(x) for x in filter(None, (r.get("BYMONTH") or "").split(","))]
    ordinals = any(n for n, _ in out["byday"])
    if freq in ("DAILY", "WEEKLY") and ordinals:
        raise UnsupportedRRule("ordinal BYDAY outside MONTHLY/YEARLY")
    if freq == "WEEKLY" and out["bymonthday"]:
        raise UnsupportedRRule("WEEKLY with BYMONTHDAY")
    if freq == "YEARLY" and (out["byday"] or out["bymonthday"]) and not out["bymonth"]:
        raise UnsupportedRRule("YEARLY BYDAY/BYMONTHDAY without BYMONTH")
    if any(not 1 <= m <= 12 for m in out["bymonth"]) or any(x == 0 or abs(x) > 31 for x in out["bymonthday"]):
        raise UnsupportedRRule("out-of-range BYMONTH/BYMONTHDAY")
    return out


def _byday_match(byday, d, last):
    for n, wd in byday:
        if d.weekday() != wd:
            continue
        if n == 0 or (n > 0 and (d.day - 1) // 7 + 1 == n) or (n < 0 and (last - d.day) // 7 + 1 == -n):
            return True
    return False


def _month_days(r, y, m, d0):
    last = _pycal.monthrange(y, m)[1]
    if r["freq"] == "MONTHLY" and r["bymonth"] and m not in r["bymonth"]:
        return []
    if r["bymonthday"]:
        days = {x if x > 0 else last + x + 1 for x in r["bymonthday"] if abs(x) <= last}
    elif r["byday"]:
        days = set(range(1, last + 1))
    else:
        days = {d0.day} if d0.day <= last else set()
    out = []
    for day in sorted(days):
        if not 1 <= day <= last:
            continue
        d = date(y, m, day)
        if r["byday"] and not _byday_match(r["byday"], d, last):
            continue
        out.append(d)
    return out


def _rrule_dates(r, d0, stop):
    """Ascending dates >= d0 produced by the rule, until periods start after `stop`."""
    freq, iv, n = r["freq"], r["interval"], 0
    wds = {wd for _, wd in r["byday"]}
    while True:
        if freq == "DAILY":
            d = d0 + timedelta(days=iv * n)
            period = d
            ok = ((not wds or d.weekday() in wds) and (not r["bymonth"] or d.month in r["bymonth"])
                  and (not r["bymonthday"] or d.day in [x if x > 0 else _pycal.monthrange(d.year, d.month)[1] + x + 1 for x in r["bymonthday"]]))
            cands = [d] if ok else []
        elif freq == "WEEKLY":
            ws = d0 - timedelta(days=(d0.weekday() - r["wkst"]) % 7) + timedelta(weeks=iv * n)
            period = ws
            want = wds or {d0.weekday()}
            cands = [ws + timedelta(days=i) for i in range(7)
                     if (ws + timedelta(days=i)).weekday() in want
                     and (not r["bymonth"] or (ws + timedelta(days=i)).month in r["bymonth"])]
        elif freq == "MONTHLY":
            y, m0 = divmod(d0.year * 12 + d0.month - 1 + iv * n, 12)
            period = date(y, m0 + 1, 1)
            cands = _month_days(r, y, m0 + 1, d0)
        else:
            y = d0.year + iv * n
            period = date(y, 1, 1) if y <= 9999 else None
            if period is None:
                return
            cands = []
            for m in sorted(r["bymonth"] or [d0.month]):
                cands += _month_days(r, y, m, d0)
        if period > stop:
            return
        for c in cands:
            if c >= d0:
                yield c
        n += 1
        if n > 60000:
            raise UnsupportedRRule("recurrence too long to expand")


def _parse_until(v, all_day, default_tz):
    if not v:
        return None
    if re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v, "%Y%m%d").date()
    return ics_time(v, {}, default_tz)[0]


def _exdates(ev, default_tz):
    out = set()
    for params, value in ev.get("EXDATE", []):
        for v in filter(None, value.split(",")):
            x, is_date = ics_time(v, params, default_tz)
            out.add(x if is_date else _key(x))
    return out


def _rdates(ev, default_tz):
    out = []
    for params, value in ev.get("RDATE", []):
        if params.get("VALUE", "").upper() == "PERIOD":
            raise UnsupportedRRule("RDATE PERIOD")
        for v in filter(None, value.split(",")):
            out.append(ics_time(v, params, default_tz)[0])
    return out


def _occurrence_starts(master, start, all_day, stop_date, default_tz):
    """Original start (date or aware datetime) of every occurrence of a series up to stop_date."""
    out = []
    tz = start.tzinfo if not all_day else None
    d0 = start.date() if not all_day else start
    rr = master.get("RRULE")
    if rr:
        r = parse_rrule(rr[0][1])
        until = _parse_until(r["until"], all_day, default_tz)
        n = 0
        for d in _rrule_dates(r, d0, stop_date):
            if all_day:
                occ = d
                past = until is not None and (occ > (until.date() if isinstance(until, datetime) else until))
            else:
                occ = datetime.combine(d, start.timetz().replace(tzinfo=None)).replace(tzinfo=tz)
                if until is None:
                    past = False
                elif isinstance(until, datetime):
                    past = _key(occ) > _key(until)
                else:
                    past = d > until
            if past:
                break
            n += 1
            out.append(occ)
            if r["count"] and n >= r["count"]:
                break
            if len(out) > 20000:
                raise UnsupportedRRule("too many occurrences")
    else:
        out.append(start)
    for x in _rdates(master, default_tz):
        if x not in out:
            out.append(x)
    return out


def _overlaps(s, e, ws, we, default_tz):
    if not isinstance(s, datetime):
        s = datetime.combine(s, datetime.min.time(), tzinfo=default_tz)
        e = datetime.combine(e, datetime.min.time(), tzinfo=default_tz)
    return s < we and (e > ws or (e <= s and s >= ws))


def _text(ev, name):
    return ics_unescape_text(_first(ev, name)[1] or "")


def ics_unescape_text(v):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v or "")


def ics_occurrences(vevents, win_start, win_end, default_tz, log=None):
    """Expand parsed VEVENTs into the occurrences overlapping [win_start, win_end).

    Handles RRULE (DAILY/WEEKLY/MONTHLY/YEARLY + INTERVAL/COUNT/UNTIL/BYDAY/
    BYMONTHDAY/BYMONTH/WKST), RDATE, EXDATE and RECURRENCE-ID overrides. A series
    using anything else is skipped and logged - never emitted with guessed dates.
    Each result: dict(uid, start, end, all_day, summary, location, description, recurring)."""
    log = log or _cal_log
    groups = {}
    for ev in vevents:
        groups.setdefault(_first(ev, "UID")[1] or "", []).append(ev)
    out = []

    def emit(uid, ev, s, e, all_day, recurring):
        if _first(ev, "STATUS")[1] and _first(ev, "STATUS")[1].strip().upper() == "CANCELLED":
            return
        if _overlaps(s, e, win_start, win_end, default_tz):
            out.append({"uid": uid, "start": s, "end": e, "all_day": all_day, "recurring": recurring,
                        "summary": _text(ev, "SUMMARY"), "location": _text(ev, "LOCATION"),
                        "description": _text(ev, "DESCRIPTION")})

    for uid, evs in groups.items():
        masters = [e for e in evs if "RECURRENCE-ID" not in e]
        overrides = [e for e in evs if "RECURRENCE-ID" in e]
        try:
            ovr = {}
            for o in overrides:
                p, v = _first(o, "RECURRENCE-ID")
                if (p.get("RANGE") or "").upper() == "THISANDFUTURE":
                    raise UnsupportedRRule("RECURRENCE-ID RANGE=THISANDFUTURE")
                x, is_date = ics_time(v, p, default_tz)
                ovr[x if is_date else _key(x)] = o
            consumed = set()
            for m in masters:
                start, end, all_day = _event_span(m, default_tz)
                if "RRULE" not in m and "RDATE" not in m:
                    emit(uid, m, start, end, all_day, False)
                    continue
                dur = end - start
                ex = _exdates(m, default_tz)
                stop = (win_end + timedelta(days=2)).date()
                for occ in _occurrence_starts(m, start, all_day, stop, default_tz):
                    k = occ if all_day else _key(occ)
                    if k in ex or (not all_day and occ.date() in ex):
                        continue
                    o = ovr.get(k)
                    if o is not None:
                        consumed.add(k)
                        s2, e2, ad2 = _event_span(o, default_tz)
                        emit(uid, o, s2, e2, ad2, True)
                    else:
                        emit(uid, m, occ, occ + dur, all_day, True)
            for k, o in ovr.items():                     # moved into the window from outside it
                if k not in consumed:
                    s2, e2, ad2 = _event_span(o, default_tz)
                    emit(uid, o, s2, e2, ad2, True)
        except (UnsupportedRRule, ValueError) as exc:
            log.warning("caldav: skipped series %s: %s", uid or "?", exc)
    return out


# ---- ICS patching (edit without destroying anything we don't touch) -----------

def ics_patch_event(text, changes):
    """Apply `changes` {PROP: None | 'PROP[;params]:value'} to the first VEVENT's own
    properties (nested VALARM etc. untouched) and bump SEQUENCE / LAST-MODIFIED /
    DTSTAMP. Every other line - RRULE, alarms, attendees, VTIMEZONE, X- props -
    is carried over byte for byte."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while raw and raw[-1] == "":
        raw.pop()
    groups = []
    for l in raw:
        if l.startswith((" ", "\t")) and groups:
            groups[-1].append(l)
        else:
            groups.append([l])
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    changes = dict(changes)
    seq = 0
    in_ev, done, depth, seen = False, False, 0, set()
    out = []
    for g in groups:
        head = g[0]
        u = head.strip().upper()
        if not in_ev:
            if u == "BEGIN:VEVENT" and not done:
                in_ev = True
            out.append(g)
            continue
        if u.startswith("BEGIN:"):
            depth += 1
            out.append(g)
            continue
        if u.startswith("END:"):
            if depth:
                depth -= 1
            elif u == "END:VEVENT":
                for name, line in [("SEQUENCE", f"SEQUENCE:{seq + 1}"), ("LAST-MODIFIED", f"LAST-MODIFIED:{now}"),
                                   ("DTSTAMP", f"DTSTAMP:{now}")] + list(changes.items()):
                    if name not in seen and line is not None:
                        out.append([line])
                        seen.add(name)
                in_ev, done = False, True
            out.append(g)
            continue
        if depth:
            out.append(g)
            continue
        name = head.split(":", 1)[0].split(";")[0].strip().upper()
        if name == "SEQUENCE":
            try:
                seq = int("".join(g).split(":", 1)[1].strip())
            except Exception:
                seq = 0
            out.append(["SEQUENCE:@@SEQ@@"])
            seen.add("SEQUENCE")
            continue
        if name in ("LAST-MODIFIED", "DTSTAMP"):
            out.append([f"{name}:{now}"])
            seen.add(name)
            continue
        if name in changes:
            if name not in seen and changes[name] is not None:
                out.append([changes[name]])
            seen.add(name)
            continue
        out.append(g)
    body = "\r\n".join("\r\n".join(g) for g in out) + "\r\n"
    return body.replace("SEQUENCE:@@SEQ@@", f"SEQUENCE:{seq + 1}")


def _etag_norm(e):
    e = (e or "").strip()
    return e[2:] if e.startswith("W/") else e




class CalendarMixin:

    @staticmethod
    def backend_zone_name():
        """IANA id of the backend's own configured zone (the fallback when a
        caller supplies no timezone). Never a bare offset."""
        try:
            name = open("/etc/timezone").read().strip()
            if name:
                ZoneInfo(name)
                return name
        except Exception:
            pass
        return "UTC"

    @staticmethod
    def resolve_zone(name):
        """Validate an IANA zone id. Raises ValueError for anything unknown, so
        a typo can never silently turn into some other zone."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a timezone is required (IANA id, e.g. Europe/Berlin)")
        try:
            return name, ZoneInfo(name)
        except Exception:
            raise ValueError(f"unknown timezone {name!r}; use an IANA id such as Europe/Berlin or Europe/London")

    def localize_event_time(self, value, zone_name):
        """ISO string -> ISO-8601 WITH a UTC offset. A naive time is read as wall
        clock in `zone_name`; a time that already carries an offset/Z is an
        absolute instant and is kept as-is. All-day dates pass through."""
        raw = (value or "").strip()
        if not raw or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
        _, zi = self.resolve_zone(zone_name)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zi)
        return dt.isoformat(timespec="seconds")

    def calendar_events(self, q):
        cfg = self.pref("calendar_connection") or {}
        if cfg.get("type") == "caldav":
            try:
                now = datetime.now(timezone.utc)
                zone = ZoneInfo(self.backend_zone_name())

                def _bound(key, default):
                    raw = str((q or {}).get(key) or "").strip()
                    if not raw:
                        return default
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=zone)
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return dt if dt.tzinfo else dt.replace(tzinfo=zone)
                ws = _bound("start", now - timedelta(days=CAL_WINDOW_PAST_DAYS))
                we = _bound("end", now + timedelta(days=CAL_WINDOW_FUTURE_DAYS))
                rows = self.caldav_fetch_events(cfg, ws, we)
                self.replace_cached_caldav_events(rows, ws, we)
                return self.json({"events": [self.event_json(r) for r in rows]})
            except Exception as exc:
                return self.json({"detail": f"CalDAV sync failed: {exc}"}, 502)
        rows = self.store.rows("select * from events order by dtstart")
        return self.json({"events": [self.event_json(r) for r in rows]})

    def caldav_url(self, cfg, href=""):
        base = (cfg.get("url") or "").strip()
        if not base:
            raise ValueError("CalDAV URL is missing")
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if not base.endswith("/"):
            base += "/"
        # A server-absolute href ("/1200/calendars/x/ev.ics", what REPORT returns)
        # replaces the base path; a bare name ("ev.ics") is relative to the collection.
        return urllib.parse.urljoin(base, href if href.startswith("/") else href.lstrip("/"))

    def caldav_request(self, cfg, method, url=None, data=None, headers=None):
        target = url or self.caldav_url(cfg)
        req = urllib.request.Request(target, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        principal = cfg.get("principal") or ""
        password = cfg.get("password") or ""
        if principal or password:
            token = base64.b64encode(f"{principal}:{password}".encode("utf-8")).decode("ascii")
            req.add_header("Authorization", "Basic " + token)
        req.add_header("User-Agent", "JoeBro/1.0")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CalDAVError(f"{method} {target} failed with HTTP {exc.code}: {body[:240]}", exc.code)

    def validate_caldav(self, cfg):
        body = b'''<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'''
        self.caldav_request(cfg, "PROPFIND", data=body, headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})

    @staticmethod
    def ics_escape(value):
        return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

    @staticmethod
    def ics_unescape(value):
        return ics_unescape_text(value)

    @staticmethod
    def ics_datetime(value, all_day=False):
        raw = value or now_iso()
        if all_day:
            return raw[:10].replace("-", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def fold_ics_line(line):
        out = []
        raw = line.encode("utf-8")
        while len(raw) > 75:
            cut = 75
            while cut > 1 and (raw[cut] & 0xC0) == 0x80:
                cut -= 1
            out.append(raw[:cut].decode("utf-8", errors="ignore"))
            raw = b" " + raw[cut:]
        out.append(raw.decode("utf-8", errors="ignore"))
        return "\r\n".join(out)

    def event_ics(self, uid, body):
        all_day = bool(body.get("all_day"))
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//JoeBro//Local Calendar//EN",
            "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            ("DTSTART;VALUE=DATE:" if all_day else "DTSTART:") + self.ics_datetime(body.get("dtstart"), all_day),
            ("DTEND;VALUE=DATE:" if all_day else "DTEND:") + self.ics_datetime(body.get("dtend"), all_day),
            "SUMMARY:" + self.ics_escape(body.get("summary") or "New event"),
        ]
        if body.get("location"):
            lines.append("LOCATION:" + self.ics_escape(body.get("location")))
        if body.get("description"):
            lines.append("DESCRIPTION:" + self.ics_escape(body.get("description")))
        lines += ["END:VEVENT", "END:VCALENDAR"]
        return "\r\n".join(self.fold_ics_line(line) for line in lines) + "\r\n"

    @staticmethod
    def unfold_ics(text):
        lines = []
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.startswith((" ", "\t")) and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        return lines

    @staticmethod
    def iso_from_ics(value, params=None, default_tz=None):
        """ICS date/date-time -> ISO. All-day -> 'YYYY-MM-DD'. Everything else is an
        absolute instant in UTC ('...Z'), honouring TZID; a floating time is read in
        `default_tz` (the backend zone)."""
        v = (value or "").strip()
        if not v:
            return ""
        try:
            x, is_date = ics_time(v, params or {}, default_tz or ZoneInfo("UTC"))
        except ValueError:
            return v
        if is_date:
            return x.isoformat()
        return x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _occ_iso(x):
        if isinstance(x, datetime):
            return x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return x.isoformat()

    def parse_ics_events(self, text, href="", etag="", win_start=None, win_end=None):
        """Every occurrence in [win_start, win_end) of the VEVENTs in one ICS resource
        as cache rows. Recurring occurrences get id '<uid>::occ::<start>' so each is
        its own row; a plain event keeps its UID."""
        zone = ZoneInfo(self.backend_zone_name())
        now = datetime.now(timezone.utc)
        ws = win_start or now - timedelta(days=CAL_WINDOW_PAST_DAYS)
        we = win_end or now + timedelta(days=CAL_WINDOW_FUTURE_DAYS)
        rows = []
        for o in ics_occurrences(ics_vevents(text), ws, we, zone):
            start_iso, end_iso = self._occ_iso(o["start"]), self._occ_iso(o["end"])
            uid = o["uid"] or "caldav_" + os.urandom(6).hex()
            rows.append({
                "id": f"{uid}{RECUR_SEP}{start_iso}" if o["recurring"] else uid,
                "summary": o["summary"] or "Untitled event",
                "dtstart": start_iso, "dtend": end_iso,
                "all_day": 1 if o["all_day"] else 0,
                "location": o["location"], "description": o["description"],
                "caldav_href": href, "caldav_etag": etag, "created_at": now_iso(),
            })
        return rows

    def parse_ics_event(self, text, href="", etag=""):
        """First occurrence of the resource (compat wrapper; use parse_ics_events)."""
        rows = self.parse_ics_events(text, href, etag, datetime(1970, 1, 1, tzinfo=timezone.utc),
                                     datetime(2100, 1, 1, tzinfo=timezone.utc))
        return rows[0] if rows else None

    def caldav_fetch_events(self, cfg, win_start=None, win_end=None):
        now = datetime.now(timezone.utc)
        ws = win_start or now - timedelta(days=CAL_WINDOW_PAST_DAYS)
        we = win_end or now + timedelta(days=CAL_WINDOW_FUTURE_DAYS)
        fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = f'''<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{fmt(ws)}" end="{fmt(we)}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>'''.encode("utf-8")
        _status, _headers, data = self.caldav_request(
            cfg, "REPORT", data=body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        root = ET.fromstring(data)
        ns = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
        rows = []
        for resp in root.findall(".//d:response", ns):
            href = (resp.findtext("d:href", default="", namespaces=ns) or "").strip()
            etag = (resp.findtext(".//d:getetag", default="", namespaces=ns) or "").strip()
            caldata = resp.findtext(".//c:calendar-data", default="", namespaces=ns) or ""
            if "BEGIN:VEVENT" in caldata:
                try:
                    rows += self.parse_ics_events(caldata, href, etag, ws, we)
                except Exception as exc:
                    _cal_log.warning("caldav: skipped unparsable resource %s: %s", href, exc)
        rows.sort(key=lambda r: r["dtstart"])
        return rows

    def replace_cached_caldav_events(self, rows, win_start=None, win_end=None):
        """Refresh the cache for the fetched window only, so a narrow fetch can't wipe
        events outside it (the briefing reads this table)."""
        if win_start and win_end:
            lo = win_start.astimezone(timezone.utc).strftime("%Y-%m-%d")
            hi = win_end.astimezone(timezone.utc).strftime("%Y-%m-%d")
            self.store.exec("delete from events where caldav_href != '' and substr(dtstart,1,10) between ? and ?", (lo, hi))
        else:
            self.store.exec("delete from events where caldav_href != ''")
        for r in rows:
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["summary"], r["dtstart"], r["dtend"], r["all_day"], r["location"],
                 r["description"], r["caldav_href"], r["caldav_etag"], r["created_at"]),
            )

    @staticmethod
    def _hdr(headers, name):
        for k, v in (headers or {}).items():
            if k.lower() == name.lower():
                return v
        return ""

    def _body_time(self, value, all_day, zone):
        """Request time -> comparable date / aware datetime; naive is the backend zone."""
        raw = str(value or "").strip()
        if all_day or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=zone)

    def _ics_time_line(self, name, x):
        if isinstance(x, datetime):
            return f"{name}:{x.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        return f"{name};VALUE=DATE:{x.strftime('%Y%m%d')}"

    def caldav_patch_changes(self, orig_text, body):
        """{PROP: line|None} for just the fields `body` actually changes vs the server copy."""
        zone = ZoneInfo(self.backend_zone_name())
        evs = ics_vevents(orig_text)
        if len(evs) != 1 or any(k in evs[0] for k in ("RRULE", "RDATE", "RECURRENCE-ID")):
            raise CalDAVReadOnly("this is a recurring event; editing a series or one occurrence from here is not "
                                 "supported (it would lose recurrence data) - change it in the Calendar app")
        ev = evs[0]
        start, end, was_all_day = _event_span(ev, zone)
        has_end = "DTEND" in ev
        changes = {}
        for key, name in (("summary", "SUMMARY"), ("location", "LOCATION"), ("description", "DESCRIPTION")):
            if key in body and body[key] is not None:
                if (body[key] or "") != _text(ev, name):
                    changes[name] = ("%s:%s" % (name, self.ics_escape(body[key]))) if body[key] else None
        all_day = bool(body["all_day"]) if "all_day" in body else was_all_day
        new_start = self._body_time(body["dtstart"], all_day, zone) if body.get("dtstart") else start
        new_end = self._body_time(body["dtend"], all_day, zone) if body.get("dtend") else None
        if all_day != was_all_day and new_end is None:
            new_end = new_start + timedelta(days=1) if all_day else new_start
        if new_end is None and new_start != start and has_end:
            new_end = new_start + (end - start)
        if new_start != start or all_day != was_all_day:
            changes["DTSTART"] = self._ics_time_line("DTSTART", new_start)
        if new_end is not None and (new_end != end or all_day != was_all_day):
            changes["DTEND"] = self._ics_time_line("DTEND", new_end)
        return {k: (self.fold_ics_line(v) if v else v) for k, v in changes.items()}

    def caldav_put_event(self, cfg, uid, body, href="", etag=""):
        """Create (no href) or edit one event. An edit GETs the real resource, changes
        only the fields that differ, and PUTs with If-Match, so RRULE, alarms,
        attendees and every other property survive. A changed server copy is a
        CalDAVConflict - never retried blindly. Returns (url, new etag)."""
        if RECUR_SEP in (uid or ""):
            raise CalDAVReadOnly("recurring occurrences can't be edited from here - change them in the Calendar app")
        ct = {"Content-Type": "text/calendar; charset=utf-8"}
        if not href:
            target = self.caldav_url(cfg, urllib.parse.quote(uid, safe="") + ".ics")
            try:
                _s, headers, _ = self.caldav_request(cfg, "PUT", url=target, data=self.event_ics(uid, body).encode("utf-8"),
                                                     headers=dict(ct, **{"If-None-Match": "*"}))
            except CalDAVError as exc:
                if exc.status == 412:
                    raise CalDAVConflict(f"an event with UID {uid} already exists on the server; nothing was written", 412)
                raise
        else:
            target = self.caldav_url(cfg, href)
            _s, gheaders, raw = self.caldav_request(cfg, "GET", url=target)
            server_etag = self._hdr(gheaders, "ETag")
            if etag and server_etag and _etag_norm(etag) != _etag_norm(server_etag):
                raise CalDAVConflict("this event changed on the server since it was last synced; "
                                     "refresh the calendar and redo the edit (nothing was written)", 412)
            orig = raw.decode("utf-8", errors="replace")
            changes = self.caldav_patch_changes(orig, body)
            if not changes:
                return target, server_etag or etag
            data = ics_patch_event(orig, changes).encode("utf-8")
            match = server_etag or etag
            try:
                _s, headers, _ = self.caldav_request(cfg, "PUT", url=target, data=data,
                                                     headers=dict(ct, **({"If-Match": match} if match else {})))
            except CalDAVError as exc:
                if exc.status == 412:
                    raise CalDAVConflict("the event was changed on the server while editing; "
                                         "refresh the calendar and redo the edit (nothing was written)", 412)
                raise
        new_etag = self._hdr(headers, "ETag")
        if not new_etag:
            try:
                _s, h2, _ = self.caldav_request(cfg, "HEAD", url=target)
                new_etag = self._hdr(h2, "ETag")
            except Exception:
                new_etag = ""
        return target, new_etag

    def parse_natural_calendar_event(self, text):
        raw = (text or "New event").strip()
        lower = raw.lower()
        base = datetime.now().astimezone()
        day = base.replace(hour=0, minute=0, second=0, microsecond=0)
        if "tomorrow" in lower:
            day += timedelta(days=1)
        else:
            weekdays = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
            }
            for name, target in weekdays.items():
                if name in lower:
                    delta = (target - day.weekday()) % 7 or 7
                    day += timedelta(days=delta)
                    break

        def _hm(num, mins, suffix):
            h = int(num); mi = int(mins or 0); suf = (suffix or "").lower()
            if suf.startswith("p") and h < 12:
                h += 12
            if suf.startswith("a") and h == 12:
                h = 0
            return max(0, min(h, 23)), max(0, min(mi, 59))

        hour, minute, end_hm = 9, 0, None
        # A time RANGE first: "14:00-17:00", "2-5pm", "9 to 10:30am".
        rng = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|—|to|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower, re.I)
        if rng:
            hour, minute = _hm(rng.group(1), rng.group(2), rng.group(3) or rng.group(6))
            end_hm = _hm(rng.group(4), rng.group(5), rng.group(6))
        else:
            # Single time: "at 14", "at 2:30pm", bare "14:00", or "2pm".
            m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b|\b(\d{1,2}):(\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b", lower, re.I)
            if m:
                if m.group(1):
                    hour, minute = _hm(m.group(1), m.group(2), m.group(3))
                elif m.group(4):
                    hour, minute = _hm(m.group(4), m.group(5), m.group(6))
                elif m.group(7):
                    hour, minute = _hm(m.group(7), None, m.group(8))
        start = day.replace(hour=hour, minute=minute)
        end = start.replace(hour=end_hm[0], minute=end_hm[1]) if end_hm else start + timedelta(hours=1)
        if end <= start:
            end = start + timedelta(hours=1)

        summary = re.sub(r"(?i)^\s*(add|schedule|create|book)\s+", "", raw)
        summary = re.sub(r"(?i)\s+(to|on)\s+(my\s+)?calendar.*$", "", summary)
        summary = re.sub(r"(?i)\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.*$", "", summary)
        summary = re.sub(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:-|–|—|to|until)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?", "", summary, flags=re.I)
        summary = re.sub(r"(?i)\s+at\s+\d{1,2}(:\d{2})?\s*(am|pm|a|p)?\b.*$", "", summary)
        summary = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", "", summary, flags=re.I).strip()
        if not summary:
            summary = "New event"
        return {
            "summary": summary,
            "dtstart": start.isoformat(),
            "dtend": end.isoformat(),
            "all_day": False,
            "location": "",
            "description": "",
        }

    def create_event_result(self, body):
        cfg = self.pref("calendar_connection") or {}
        if not cfg.get("type"):
            raise RuntimeError("No calendar is connected. Open Settings > General to connect one.")
        if cfg.get("type") == "macos":
            raise RuntimeError("macOS Calendar writes are handled by the native app so EventKit permissions are used.")
        uid = body.get("uid") or "ev_" + os.urandom(6).hex()
        href = ""
        etag = ""
        if cfg.get("type") == "caldav":
            href, etag = self.caldav_put_event(cfg, uid, body)
        self.store.exec(
            """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
               values(?,?,?,?,?,?,?,?,?,?)""",
            (uid, body.get("summary") or "New event", body.get("dtstart") or now_iso(), body.get("dtend") or now_iso(),
             1 if body.get("all_day") else 0, body.get("location") or "", body.get("description") or "", href, etag, now_iso()),
        )
        return {"ok": True, "uid": uid}

    def create_event(self, body):
        try:
            return self.json(self.create_event_result(body))
        except Exception as exc:
            return self.json({"detail": str(exc)}, 500)

    def update_event(self, uid, body):
        row = self.store.one("select * from events where id=?", (uid,))
        if not row:
            return self.json({"detail": "Event not found"}, 404)
        cfg = self.pref("calendar_connection") or {}
        href = row.get("caldav_href") or ""
        etag = row.get("caldav_etag") or ""
        if cfg.get("type") == "caldav":
            try:
                href, etag = self.caldav_put_event(cfg, uid, body, href=href, etag=etag)
            except CalDAVConflict as exc:
                return self.json({"detail": str(exc), "conflict": True}, 409)
            except CalDAVReadOnly as exc:
                return self.json({"detail": str(exc), "read_only": True}, 422)
        self.store.exec(
            "update events set summary=?,dtstart=?,dtend=?,all_day=?,location=?,description=?,caldav_href=?,caldav_etag=? where id=?",
            (body.get("summary") or "Event", body.get("dtstart") or now_iso(), body.get("dtend") or now_iso(),
             1 if body.get("all_day") else 0, body.get("location") or "", body.get("description") or "", href, etag, uid),
        )
        return self.json({"ok": True})

    def delete_event(self, uid):
        row = self.store.one("select * from events where id=?", (uid,))
        try:
            self.delete_event_row(row, uid)
        except CalDAVConflict as exc:
            return self.json({"detail": str(exc), "conflict": True}, 409)
        except CalDAVReadOnly as exc:
            return self.json({"detail": str(exc), "read_only": True}, 422)
        return self.json({"ok": True})

    def delete_event_row(self, row, uid=None):
        uid = uid or (row or {}).get("id")
        cfg = self.pref("calendar_connection") or {}
        if row and cfg.get("type") == "caldav" and row.get("caldav_href"):
            if RECUR_SEP in (row.get("id") or ""):
                raise CalDAVReadOnly("recurring occurrences can't be deleted from here (that would delete the whole "
                                     "series) - delete them in the Calendar app")
            etag = row.get("caldav_etag") or ""
            try:
                self.caldav_request(cfg, "DELETE", url=self.caldav_url(cfg, row["caldav_href"]),
                                    headers={"If-Match": etag} if etag else None)
            except CalDAVError as exc:
                if exc.status == 412:
                    raise CalDAVConflict("this event changed on the server since it was last synced; refresh and retry "
                                         "(nothing was deleted)", 412)
                if exc.status != 404:
                    raise
        self.store.exec("delete from events where id=?", (uid,))

    def queue_calendar_ops(self, ops):
        """Durable queue of EventKit writes for the Mac app to apply. The agent
        can run from any client (iOS, Telegram, a background task) with no Mac
        chat stream open, so the tool result alone can't carry the write —
        the Mac drains this via /api/calendar/pending whenever it is running."""
        queue = self.pref("calendar_pending_ops") or []
        for op in ops:
            queue.append(dict(op, id="op_" + os.urandom(5).hex(), queued_at=now_iso()))
        self.set_pref("calendar_pending_ops", queue[-500:])

    def calendar_pending(self, body):
        """Mac app: ack the ops it applied, get back whatever is still queued."""
        done = set(str(i) for i in (body.get("ack") or []))
        queue = [o for o in (self.pref("calendar_pending_ops") or []) if o.get("id") not in done]
        if done:
            self.set_pref("calendar_pending_ops", queue)
        return self.json({"ok": True, "ops": queue})

    def sync_calendar_mirror(self, body):
        """Replace the local mirror of the device's own calendar.

        For a macOS calendar EventKit is the truth and the backend can't read
        it, so the app pushes the current events here. That mirror is what lets
        manage_events see (and move) events it didn't create."""
        events = body.get("events")
        if not isinstance(events, list):
            return self.json({"detail": "events must be a list"}, 400)
        # Only local rows — CalDAV rows are owned by the CalDAV sync.
        pending = [str(o.get("uid")) for o in (self.pref("calendar_pending_ops") or []) if o.get("uid")]
        self.store.exec("delete from events where caldav_href = '' and id not in (%s)" % ",".join("?" * len(pending)), tuple(pending))
        kept = 0
        for e in events:
            uid = str((e or {}).get("uid") or "").strip()
            if not uid:
                continue
            start = e.get("dtstart") or now_iso()
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,'','',?)""",
                (uid, e.get("summary") or "Untitled event", start, e.get("dtend") or start,
                 1 if e.get("all_day") else 0, e.get("location") or "", e.get("description") or "", now_iso()),
            )
            kept += 1
        self.set_pref("calendar_mirror_pushed_at", now_iso())
        return self.json({"ok": True, "count": kept, "pushed_at": now_iso()})

    # ---- batch event management (list / shift / set / delete) ---------------

    @staticmethod
    def _event_dt(value):
        """ISO string -> (datetime, was_date_only). All-day events are stored as
        a bare date and must come back out as one."""
        raw = (value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.strptime(raw, "%Y-%m-%d"), True
        return datetime.fromisoformat(raw.replace("Z", "+00:00")), False

    @staticmethod
    def event_brief(r):
        all_day = bool(r.get("all_day"))
        return {"uid": r.get("uid") or r.get("id") or "", "summary": r.get("summary") or "",
                "dtstart": event_time_out(r.get("dtstart"), all_day),
                "dtend": event_time_out(r.get("dtend"), all_day),
                "all_day": all_day, "location": r.get("location") or "",
                "description": r.get("description") or ""}

    def match_events(self, args):
        """The events a batch op applies to. Refuses to match everything unless
        the caller passes all=true — a typo'd filter would otherwise silently
        rewrite the whole calendar in one call."""
        ids = args.get("ids") or args.get("uid") or args.get("id") or ""
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        ids = [str(i) for i in ids]
        query = (args.get("query") or "").strip().lower()
        day_from = str(args.get("from") or "")[:10]
        day_to = str(args.get("to") or "")[:10]
        if not (ids or query or day_from or day_to or args.get("all")):
            raise ValueError("manage_events needs ids, query, from/to or all=true — "
                             "refusing to touch every event by accident")
        out = []
        for r in self.store.rows("select * from events order by dtstart"):
            day = (r.get("dtstart") or "")[:10]
            if ids and r["id"] not in ids:
                continue
            if query and query not in (r.get("summary") or "").lower():
                continue
            if day_from and day < day_from:
                continue
            if day_to and day > day_to:
                continue
            out.append(dict(r))
        return out

    def manage_events_tool(self, body):
        """List / batch-move / batch-edit / batch-delete calendar events.

        Writes go to the mirror table AND to CalDAV when that's the connection.
        For a macOS calendar the app owns EventKit, so every mutation is also
        returned under `apply` for the app to write through — the same bridge
        create_event already uses."""
        args = self.parse_tool_args(body)
        action = (args.get("action") or "list").strip().lower()
        if action not in ("list", "shift", "set", "delete"):
            raise ValueError(f"unknown manage_events action '{action}' (list, shift, set, delete)")
        rows = self.match_events(args)
        if action == "list":
            return json.dumps({"ok": True, "count": len(rows),
                               "events": [self.event_brief(r) for r in rows]}, ensure_ascii=False)
        if not rows:
            return json.dumps({"ok": True, "count": 0, "events": [], "apply": []}, ensure_ascii=False)
        mac = (self.pref("calendar_connection") or {}).get("type") == "macos"
        if action == "delete":
            for r in rows:
                self.delete_event_row(r)
            if mac:
                self.queue_calendar_ops([{"op": "delete", "uid": r["id"]} for r in rows])
            return json.dumps({"ok": True, "deleted": len(rows),
                               "events": [self.event_brief(r) for r in rows],
                               "apply": [{"op": "delete", "uid": r["id"]} for r in rows]}, ensure_ascii=False)

        minutes = (int(args.get("minutes") or 0) + 60 * int(args.get("hours") or 0)
                   + 1440 * int(args.get("days") or 0))
        fields = {k: args[k] for k in ("summary", "location", "description") if args.get(k) is not None}
        for arg_key, col in (("start", "dtstart"), ("end", "dtend")):
            if args.get(arg_key):
                if len(rows) > 1:
                    raise ValueError(f"'{arg_key}' sets one absolute time but {len(rows)} events matched — "
                                     "use action=shift to move them all by an offset")
                # Absolute times get an explicit zone: the caller's `timezone`, else the
                # backend zone (naive input is never left for a device to guess).
                v = str(args[arg_key])
                if not args.get("timezone") and not re.search(r"(Z|[+-]\d{2}:?\d{2})$", v) and "T" in v:
                    raise ValueError(f"'{arg_key}' has no UTC offset: pass `timezone` (IANA id) or an ISO time with an offset")
                fields[col] = self.localize_event_time(v, args.get("timezone") or self.backend_zone_name())
                if args.get("timezone"):
                    fields["tz"] = str(args["timezone"]).strip()
        if "all_day" in args:
            fields["all_day"] = 1 if args["all_day"] else 0
        if action == "shift" and not minutes:
            raise ValueError("shift needs a non-zero minutes/hours/days (negative moves events earlier)")
        if action == "set" and not fields and not minutes:
            raise ValueError("set needs at least one of summary/start/end/location/description/all_day")

        cfg = self.pref("calendar_connection") or {}
        delta = timedelta(minutes=minutes)
        updated = []
        for r in rows:
            ev = dict(r)
            ev.update(fields)
            if minutes:
                for col in ("dtstart", "dtend"):
                    try:
                        dt, date_only = self._event_dt(ev.get(col))
                    except ValueError:
                        continue          # unparseable timestamp: leave it alone
                    moved = dt + delta
                    ev[col] = moved.strftime("%Y-%m-%d") if date_only else moved.isoformat()
            href, etag = ev.get("caldav_href") or "", ev.get("caldav_etag") or ""
            if cfg.get("type") == "caldav" and href:
                href, etag = self.caldav_put_event(cfg, r["id"], ev, href=href, etag=etag)
            self.store.exec(
                "update events set summary=?,dtstart=?,dtend=?,all_day=?,location=?,description=?,caldav_href=?,caldav_etag=? where id=?",
                (ev.get("summary") or "Event", ev.get("dtstart") or now_iso(), ev.get("dtend") or now_iso(),
                 1 if ev.get("all_day") else 0, ev.get("location") or "", ev.get("description") or "",
                 href, etag, r["id"]),
            )
            brief = self.event_brief(ev)
            if fields.get("tz"):
                brief["tz"] = fields["tz"]
            updated.append(brief)
        if mac:
            self.queue_calendar_ops([dict(e, op="update") for e in updated])
        return json.dumps({"ok": True, "updated": len(updated), "events": updated,
                           "apply": [dict(e, op="update") for e in updated]}, ensure_ascii=False)
