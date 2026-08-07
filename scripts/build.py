#!/usr/bin/env python3
"""
Weekly build for ragingcunt.com — the local rage wall.

Reads data/sources.yml, pulls events from each configured source,
normalizes them to the front-end schema, buckets them into neighborhoods,
dedupes, drops past events, and writes data/events.json.

RULES BAKED IN (do not remove):
  * NEVER fabricate events. Only emit what a real source actually returned.
  * KEEP-LAST-GOOD: if a source errors, reuse its events from the previous
    events.json instead of blanking the city — EXCEPT the demo seed, which is
    never allowed to leak into real output.
  * Everything before `now` (minus a 6h grace) is dropped.

This is a working SKELETON. The two fetchers (Mobilize, ICS) have real shapes
but VERIFY endpoints/params against the current APIs before trusting them.
Run locally:  python scripts/build.py
"""
from __future__ import annotations
import json, sys, time, hashlib, datetime as dt, pathlib, traceback

import yaml
import requests
from dateutil import parser as dtparse

# Naive/floating times in feeds (esp. ICS) are wall-clock local, NOT UTC.
# The site is California-wide today, so default naive times to Pacific instead
# of fabricating a UTC offset. Make this per-city if/when a non-CA city is added.
try:
    from zoneinfo import ZoneInfo
    DEFAULT_TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - zoneinfo missing / no tzdata
    DEFAULT_TZ = dt.timezone.utc

# optional deps — imported lazily so a partial setup still runs
try:
    from icalendar import Calendar
except ImportError:
    Calendar = None
try:
    from shapely.geometry import shape, Point
except ImportError:
    shape = Point = None

ROOT      = pathlib.Path(__file__).resolve().parent.parent
DATA      = ROOT / "data"
OUT       = DATA / "events.json"
SOURCES   = DATA / "sources.yml"
HOODS_DIR = DATA / "neighborhoods"
NOW       = dt.datetime.now(dt.timezone.utc)

TYPES = {"action", "meeting", "mutualaid", "show", "teachin", "tenant", "kyr"}


def log(*a): print("[build]", *a, file=sys.stderr)


# ───────────────────────── neighborhood bucketing ─────────────────────────
_hood_cache: dict[str, list] = {}

def load_hoods(city_key):
    """Load data/neighborhoods/<city>.geojson -> [(name, polygon), ...]."""
    if city_key in _hood_cache:
        return _hood_cache[city_key]
    path = HOODS_DIR / f"{city_key}.geojson"
    feats = []
    if path.exists() and shape is not None:
        gj = json.loads(path.read_text(encoding="utf-8"))
        for f in gj.get("features", []):
            name = (f.get("properties") or {}).get("name")
            try:
                feats.append((name, shape(f["geometry"])))
            except Exception:
                pass
    _hood_cache[city_key] = feats
    return feats

def bucket(city_key, lat, lng):
    """Point-in-polygon -> neighborhood name, else 'Citywide'."""
    if lat is None or lng is None or Point is None:
        return "Citywide"
    pt = Point(float(lng), float(lat))
    for name, poly in load_hoods(city_key):
        try:
            if poly.contains(pt):
                return name
        except Exception:
            pass
    return "Citywide"


# ───────────────────────────── normalization ──────────────────────────────
def _iso(v):
    if not v:
        return None
    if isinstance(v, str):
        try: v = dtparse.parse(v)
        except Exception: return None
    # All-day date (no time component): anchor to local midnight so the calendar
    # day is preserved. UTC midnight would render as the *previous* evening in PT.
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        v = dt.datetime(v.year, v.month, v.day, tzinfo=DEFAULT_TZ)
    # Floating/naive datetime: it's local wall-clock time. Attaching UTC would
    # shift a 5:30pm rally to 10:30pm. Interpret in the site's default zone.
    elif v.tzinfo is None:
        v = v.replace(tzinfo=DEFAULT_TZ)
    return v.isoformat()

# Calendar-blocking placeholders that show up in shared Google Calendars but
# aren't real public events. Matched case-insensitively against the whole title.
_SKIP_TITLES = {"space reserved", "reserved", "hold", "placeholder", "busy", "tbd", "n/a"}

def norm_event(city_key, *, title, start, type_, org=None, venue=None,
               lat=None, lng=None, url=None, blurb=None, source="?", end=None):
    """Build one event dict in the front-end's exact shape (or None if unusable)."""
    start_iso = _iso(start)
    if not title or not start_iso:
        return None
    if title.strip().lower() in _SKIP_TITLES:  # drop venue-hold placeholders
        return None
    eid = hashlib.sha1(f"{title}|{start_iso}|{venue or ''}".encode()).hexdigest()[:12]
    return {
        "id": eid,
        "title": title.strip(),
        "type": type_ if type_ in TYPES else "meeting",
        "org": org,
        "start": start_iso,
        "end": _iso(end),
        "venue": venue,
        "neighborhood": bucket(city_key, lat, lng),
        "lat": lat, "lng": lng,
        "url": url,
        "blurb": (blurb or "").strip(),
        "source": source,
    }


# ───────────────────────────── http w/ retry ──────────────────────────────
# Transient statuses worth a retry (rate-limit + gateway/5xx). 4xx (except 429)
# are the caller's fault and fail fast.
_RETRY_STATUS = {429, 500, 502, 503, 504}

def http_get(url, params=None, *, timeout=30, retries=3, backoff=2.0):
    """GET with a hard timeout and exponential backoff on transient failures.
    Honors Retry-After on 429/503. Raises the last error if all attempts fail."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in _RETRY_STATUS:
                raise requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            if attempt == retries - 1:
                break
            # exponential backoff: 1s, 2s, 4s … unless the server named a delay
            wait = backoff ** attempt
            resp = getattr(e, "response", None)
            if resp is not None:
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, int(ra))
            log(f"GET {url} failed ({e}); retry {attempt + 1}/{retries - 1} in {wait:.0f}s")
            time.sleep(wait)
    raise last


# ─────────────────────── fetchers (VERIFY before trusting) ─────────────────
def _guess_type(raw, default):
    m = {"RALLY": "action", "CANVASS": "action", "MEETING": "meeting",
         "COMMUNITY": "mutualaid", "FUNDRAISER": "show"}
    return m.get((raw or "").upper(), default)

_MOBILIZE_MAX_PAGES = 40  # safety valve: ~40 * per_page events before we stop

def fetch_mobilize(city_key, org_id, default_type="meeting"):
    """
    Mobilize public API. Docs: https://github.com/mobilizeamerica/api
    Endpoint: GET /v1/organizations/:org_id/events  (org-scoped, documented path).
    Pagination is cursor-based: the response envelope's `next` field is a full
    URL to the following page (or null). Follow it until exhausted.
    Verified fields: timeslots[].start_date (unix), location.venue,
    location.location.latitude/longitude, sponsor.name, browser_url, event_type.
    """
    # First request seeds per_page; every subsequent `next` URL already carries
    # the cursor + per_page, so params are only sent on the initial call.
    url = f"https://api.mobilize.us/v1/organizations/{org_id}/events"
    params = {"per_page": 50}
    out = []
    for page in range(_MOBILIZE_MAX_PAGES):
        r = http_get(url, params=params, timeout=30)
        params = None
        body = r.json()
        for ev in body.get("data", []):
            loc = ev.get("location") or {}
            ll = (loc.get("location") or {})
            for ts in (ev.get("timeslots") or [{}]):
                start = (dt.datetime.fromtimestamp(ts["start_date"], dt.timezone.utc)
                         if ts.get("start_date") else None)
                e = norm_event(
                    city_key, title=ev.get("title", ""), start=start,
                    type_=_guess_type(ev.get("event_type"), default_type),
                    org=(ev.get("sponsor") or {}).get("name"),
                    venue=loc.get("venue"), lat=ll.get("latitude"), lng=ll.get("longitude"),
                    url=ev.get("browser_url"), blurb=(ev.get("description") or "")[:280],
                    source="mobilize")
                if e:
                    out.append(e)
        url = body.get("next")
        if not url:
            break
    else:
        log(f"[{city_key}] mobilize org {org_id}: hit {_MOBILIZE_MAX_PAGES}-page cap, "
            f"stopping pagination early")
    return out

def fetch_ics(city_key, url, org=None, default_type="meeting"):
    """Parse an .ics feed (The Events Calendar, Google Calendar, etc.).
    Param is `url` to match the documented sources.yml schema (kind: ics, url: …)."""
    if Calendar is None:
        raise RuntimeError("icalendar not installed (pip install icalendar)")
    r = http_get(url, timeout=30)
    cal = Calendar.from_ical(r.content)
    out = []
    for comp in cal.walk("VEVENT"):
        ds = comp.get("dtstart")
        start = ds.dt if ds else None
        lat = lng = None
        geo = comp.get("geo")
        if geo is not None:
            try: lat, lng = float(geo.latitude), float(geo.longitude)
            except Exception: pass
        e = norm_event(
            city_key, title=str(comp.get("summary", "")), start=start,
            type_=default_type, org=org,
            venue=str(comp.get("location", "")) or None,
            lat=lat, lng=lng, url=str(comp.get("url", "")) or None,
            blurb=str(comp.get("description", "") or "")[:280], source="ics")
        if e:
            out.append(e)
    return out

FETCHERS = {"mobilize": fetch_mobilize, "ics": fetch_ics}


# ─────────────────────────────── main loop ────────────────────────────────
def load_prev():
    if OUT.exists():
        try: return json.loads(OUT.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def drop_past(evs):
    keep = []
    for e in evs:
        try:
            if dtparse.parse(e["start"]) >= NOW - dt.timedelta(hours=6):
                keep.append(e)
        except Exception:
            keep.append(e)
    return keep

def dedupe(evs):
    seen, out = set(), []
    for e in evs:
        if e["id"] in seen:
            continue
        seen.add(e["id"]); out.append(e)
    return out

def main():
    cfg  = yaml.safe_load(SOURCES.read_text(encoding="utf-8")) or {}
    prev = load_prev()
    # keep-last-good source = previous REAL output only. Never resurrect the demo seed.
    prev_events = {}
    if not prev.get("demo"):
        prev_events = {c: (prev.get("cities", {}).get(c, {}) or {}).get("events", [])
                       for c in (cfg.get("cities") or {})}

    out_cities = {}
    for city_key, city in (cfg.get("cities") or {}).items():
        label = (city or {}).get("label", city_key.replace("-", " ").title())
        collected = []
        for src in (city or {}).get("sources", []) or []:
            kind = (src or {}).get("kind")
            fn = FETCHERS.get(kind)
            if not fn:
                log(f"[{city_key}] unknown source kind {kind!r}")
                continue
            try:
                args = {k: v for k, v in src.items() if k != "kind"}
                collected += fn(city_key, **args)
            except Exception as e:
                log(f"SOURCE FAILED [{city_key}] {src}: {e}")
                traceback.print_exc()
        # keep-last-good: nothing came back but we had real events before -> reuse
        if not collected and prev_events.get(city_key):
            log(f"[{city_key}] using last-good ({len(prev_events[city_key])} events)")
            collected = prev_events[city_key]
        collected = dedupe(drop_past(collected))
        out_cities[city_key] = {"label": label, "events": collected}
        log(f"[{city_key}] {len(collected)} events")

    result = {"generated": NOW.isoformat(), "demo": False, "cities": out_cities}
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(len(c["events"]) for c in out_cities.values())
    log(f"wrote {OUT} — {total} events across {len(out_cities)} cities")


if __name__ == "__main__":
    main()
