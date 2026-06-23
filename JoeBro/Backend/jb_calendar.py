"""Calendar: CalDAV sync, ICS, natural-language events."""
from jb_core import *  # noqa: F401,F403


class CalendarMixin:

    def calendar_events(self, q):
        cfg = self.pref("calendar_connection") or {}
        if cfg.get("type") == "caldav":
            try:
                rows = self.caldav_fetch_events(cfg)
                self.replace_cached_caldav_events(rows)
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
        return urllib.parse.urljoin(base, href.lstrip("/"))

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
            raise RuntimeError(f"{method} {target} failed with HTTP {exc.code}: {body[:240]}")

    def validate_caldav(self, cfg):
        body = b'''<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'''
        self.caldav_request(cfg, "PROPFIND", data=body, headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})

    @staticmethod
    def ics_escape(value):
        return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

    @staticmethod
    def ics_unescape(value):
        return (value or "").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

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
    def iso_from_ics(value):
        v = (value or "").strip()
        if re.fullmatch(r"\d{8}", v):
            return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
        if v.endswith("Z") and re.fullmatch(r"\d{8}T\d{6}Z", v):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        if re.fullmatch(r"\d{8}T\d{6}", v):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%S")
            return dt.isoformat()
        return v

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

    def parse_ics_event(self, text, href="", etag=""):
        fields = {}
        params = {}
        in_event = False
        for line in self.unfold_ics(text):
            if line == "BEGIN:VEVENT":
                in_event = True
                continue
            if line == "END:VEVENT":
                break
            if not in_event or ":" not in line:
                continue
            left, value = line.split(":", 1)
            parts = left.split(";")
            key = parts[0].upper()
            fields[key] = value
            params[key] = parts[1:]
        uid = fields.get("UID") or "caldav_" + os.urandom(6).hex()
        dtstart = self.iso_from_ics(fields.get("DTSTART") or "")
        dtend = self.iso_from_ics(fields.get("DTEND") or fields.get("DTSTART") or "")
        all_day = any("VALUE=DATE" in p.upper() for p in params.get("DTSTART", [])) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", dtstart or "") is not None
        return {
            "id": uid,
            "summary": self.ics_unescape(fields.get("SUMMARY") or "Untitled event"),
            "dtstart": dtstart or now_iso(),
            "dtend": dtend or dtstart or now_iso(),
            "all_day": 1 if all_day else 0,
            "location": self.ics_unescape(fields.get("LOCATION") or ""),
            "description": self.ics_unescape(fields.get("DESCRIPTION") or ""),
            "caldav_href": href,
            "caldav_etag": etag,
            "created_at": now_iso(),
        }

    def caldav_fetch_events(self, cfg):
        body = b'''<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>'''
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
                rows.append(self.parse_ics_event(caldata, href=href, etag=etag))
        return rows

    def replace_cached_caldav_events(self, rows):
        self.store.exec("delete from events where caldav_href != ''")
        for r in rows:
            self.store.exec(
                """insert or replace into events(id,summary,dtstart,dtend,all_day,location,description,caldav_href,caldav_etag,created_at)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["summary"], r["dtstart"], r["dtend"], r["all_day"], r["location"],
                 r["description"], r["caldav_href"], r["caldav_etag"], r["created_at"]),
            )

    def caldav_put_event(self, cfg, uid, body, href=""):
        target = self.caldav_url(cfg, href or (urllib.parse.quote(uid, safe="") + ".ics"))
        data = self.event_ics(uid, body).encode("utf-8")
        status, headers, _ = self.caldav_request(
            cfg, "PUT", url=target, data=data,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        return target, headers.get("ETag") or headers.get("Etag") or ""

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
            href, etag = self.caldav_put_event(cfg, uid, body, href=href)
        self.store.exec(
            "update events set summary=?,dtstart=?,dtend=?,all_day=?,location=?,description=?,caldav_href=?,caldav_etag=? where id=?",
            (body.get("summary") or "Event", body.get("dtstart") or now_iso(), body.get("dtend") or now_iso(),
             1 if body.get("all_day") else 0, body.get("location") or "", body.get("description") or "", href, etag, uid),
        )
        return self.json({"ok": True})

    def delete_event(self, uid):
        row = self.store.one("select * from events where id=?", (uid,))
        cfg = self.pref("calendar_connection") or {}
        if row and cfg.get("type") == "caldav" and row.get("caldav_href"):
            self.caldav_request(cfg, "DELETE", url=self.caldav_url(cfg, row["caldav_href"]))
        self.store.exec("delete from events where id=?", (uid,))
        return self.json({"ok": True})
