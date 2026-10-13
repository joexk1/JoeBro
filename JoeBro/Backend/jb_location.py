"""Location + weather for the phone agent. Latest fix only (no history), stdlib.

Opt-in: nothing here talks to the network until the user turns on "Use my phone's
location for weather" on the Mac (pref `location_enabled`, default off). The hosts
it then contacts are api.open-meteo.com and geocoding-api.open-meteo.com (weather
and place search) and nominatim.openstreetmap.org (turning coordinates into a
town name). The fix itself is kept in location.json in the data folder.
"""
import json, os, tempfile, threading, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

from jb_core import data_dir

_LOCK = threading.Lock()
_WX = {}  # (lat2, lon2, hours, days) -> (expiry, payload)
WX_TTL = 600
CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle",
         53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
         67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
         81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorm",
         96: "thunderstorm with hail", 99: "thunderstorm with hail"}
RAIN = {51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _get(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "JoeBro/1.3"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _path():
    return data_dir() / "location.json"


def load_location():
    try:
        return json.loads(_path().read_text())
    except Exception:
        return None


def reverse_place(lat, lon, timeout=6):
    try:
        d = _get("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&lat=%s&lon=%s" % (lat, lon), timeout)
        a = d.get("address") or {}
        city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("suburb") or ""
        return ", ".join(x for x in (city, a.get("country") or "") if x) or (d.get("name") or "")
    except Exception:
        return ""


def save_location(body, geocode=reverse_place):
    lat, lon = float(body["lat"]), float(body["lon"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    old = load_location() or {}
    same = abs(old.get("lat", 99) - lat) < 0.02 and abs(old.get("lon", 99) - lon) < 0.02  # ~2 km: reuse cached name
    place = old.get("place") if same and old.get("place") else geocode(round(lat, 3), round(lon, 3))
    rec = {"lat": lat, "lon": lon, "accuracy": body.get("accuracy"), "timestamp": body.get("timestamp"),
           "received_at": datetime.now().astimezone().isoformat(timespec="seconds"), "place": place or old.get("place", "")}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent))
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, p)
    return rec


def fetch_weather(lat, lon, hours=6, days=0, timeout=6):
    key = (round(lat, 2), round(lon, 2), hours, days)
    hit = _WX.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    q = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_hours": max(1, min(hours, 48)),
         "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m",
         "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code"}
    if days:
        q.update(forecast_days=max(1, min(days, 7)),
                 daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max")
    d = _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout)
    _WX[key] = (time.time() + WX_TTL, d)
    return d


def _cond(code):
    return CODES.get(code, "unknown")


def summarize(d, hours, days):
    c = d.get("current") or {}
    out = {"now": {"temp_c": c.get("temperature_2m"), "feels_c": c.get("apparent_temperature"),
                   "condition": _cond(c.get("weather_code")), "precip_mm": c.get("precipitation"),
                   "wind_kmh": c.get("wind_speed_10m")}}
    h = d.get("hourly") or {}
    if hours:
        out["hourly"] = [{"time": t[11:16], "temp_c": h["temperature_2m"][i], "rain_prob": h["precipitation_probability"][i],
                          "precip_mm": h["precipitation"][i], "condition": _cond(h["weather_code"][i])}
                         for i, t in enumerate(h.get("time", [])[:hours])]
    dl = d.get("daily")
    if days and dl:
        out["daily"] = [{"date": t, "min_c": dl["temperature_2m_min"][i], "max_c": dl["temperature_2m_max"][i],
                         "rain_mm": dl["precipitation_sum"][i], "rain_prob": dl["precipitation_probability_max"][i],
                         "condition": _cond(dl["weather_code"][i])} for i, t in enumerate(dl["time"])]
    return out


def rain_soon(d, hours=2):
    """First rain time within `hours` if it is not raining now, else None."""
    c = d.get("current") or {}
    if (c.get("precipitation") or 0) > 0 or c.get("weather_code") in RAIN:
        return None
    h = d.get("hourly") or {}
    for i, t in enumerate(h.get("time", [])[:hours + 1]):
        if (h["precipitation"][i] or 0) >= 0.2 or h["weather_code"][i] in RAIN or (h["precipitation_probability"][i] or 0) >= 70:
            return t[11:16]
    return None


def now_line(timeout=3):
    loc = load_location()
    if not loc:
        return ""
    try:
        c = fetch_weather(loc["lat"], loc["lon"], 2, 0, timeout).get("current") or {}
        return "Now: %s, %s°C, %s" % (loc.get("place") or "current location", round(c["temperature_2m"]), _cond(c.get("weather_code")))
    except Exception:
        return "Now: %s" % loc["place"] if loc.get("place") else ""


OFF_TEXT = ("Location sharing is off. The user can turn on \"Use my phone's location for weather\" "
            "in the Mac app (Settings > Mobile). Until then, ask them for a place name if you need one.")


class LocationMixin:
    def location_enabled(self):
        return str(self.pref("location_enabled")).strip().lower() in ("1", "true", "yes", "on")

    def location_post(self, body):
        # 200 with ok:false (not an error) so the phone's reporter stops quietly.
        if not self.location_enabled():
            return {"ok": False, "detail": "location sharing is off on the Mac"}
        rec = save_location(body)
        return {"ok": True, "place": rec["place"]}

    def get_location_tool(self, body):
        if not self.location_enabled():
            return json.dumps({"ok": False, "detail": OFF_TEXT})
        loc = load_location()
        return json.dumps(loc or {"ok": False, "detail": "no location reported yet; the phone sends one when it has a fix"})

    def get_weather_tool(self, body):
        if not self.location_enabled():
            return json.dumps({"ok": False, "detail": OFF_TEXT})
        a = self.parse_tool_args(body)
        hours, days = int(a.get("hours") or 6), int(a.get("days") or 0)
        place = (a.get("place") or "").strip()
        if place:
            g = _get("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(place))
            r = (g.get("results") or [None])[0]
            if not r:
                return json.dumps({"ok": False, "detail": "place not found: " + place})
            lat, lon, name = r["latitude"], r["longitude"], ", ".join(x for x in (r.get("name"), r.get("country")) if x)
        else:
            loc = load_location()
            if not loc:
                return json.dumps({"ok": False, "detail": "no location reported yet; pass a place"})
            lat, lon, name = loc["lat"], loc["lon"], loc.get("place") or "current location"
        out = summarize(fetch_weather(lat, lon, hours, days), hours, days)
        out.update(ok=True, place=name)
        return json.dumps(out, ensure_ascii=False)
