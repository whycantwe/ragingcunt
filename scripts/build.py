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
               lat=None, lng=None, url=None, blurb=None, source="?", end=None,
               recurrence=None):
    """Build one event dict in the front-end's exact shape (or None if unusable).
    `recurrence` (optional) is a human label like 'Every Sunday' for a collapsed
    repeating series; the `start` is then that series' NEXT occurrence."""
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
        "recurrence": recurrence,   # None for one-offs; label for collapsed series
    }


# ───────────────────────────── http w/ retry ──────────────────────────────
# Transient statuses worth a retry (rate-limit + gateway/5xx). 4xx (except 429)
# are the caller's fault and fail fast.
_RETRY_STATUS = {429, 500, 502, 503, 504}
# A browser-ish UA: some calendar hosts (e.g. saccenter.org) 403 the default
# python-requests agent.
_UA = "Mozilla/5.0 (compatible; ragingcunt-build/1.0; +https://ragingcunt.com)"

def http_get(url, params=None, *, timeout=30, retries=3, backoff=2.0):
    """GET with a hard timeout and exponential backoff on transient failures.
    Honors Retry-After on 429/503. Raises the last error if all attempts fail."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                              headers={"User-Agent": _UA})
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

def fetch_mobilize(city_key, org_id, default_type="meeting", near_zips=None):
    """
    Mobilize public API. Docs: https://github.com/mobilizeamerica/api
    Endpoint: GET /v1/organizations/:org_id/events  (org-scoped, documented path).
    Pagination is cursor-based: the response envelope's `next` field is a full
    URL to the following page (or null). Follow it until exhausted.
    Verified fields: timeslots[].start_date (unix), location.venue,
    location.location.latitude/longitude, sponsor.name, browser_url, event_type.

    near_zips: optional list of postal-code PREFIXES (e.g. ["956","958"]). Many
    Indivisible/coalition orgs cross-post a national firehose; when set, only
    events whose postal_code starts with one of these prefixes are kept, so the
    feed contributes just its genuinely-local events. Events without a matching
    postal code (incl. virtual/no-location) are dropped.
    """
    # First request seeds per_page; every subsequent `next` URL already carries
    # the cursor + per_page, so params are only sent on the initial call.
    url = f"https://api.mobilize.us/v1/organizations/{org_id}/events"
    params = {"per_page": 50}
    out = []
    dropped = 0
    for page in range(_MOBILIZE_MAX_PAGES):
        r = http_get(url, params=params, timeout=30)
        params = None
        body = r.json()
        for ev in body.get("data", []):
            loc = ev.get("location") or {}
            ll = (loc.get("location") or {})
            if near_zips:
                pc = str(loc.get("postal_code") or "")
                if not any(pc.startswith(z) for z in near_zips):
                    dropped += 1
                    continue                      # non-local coalition cross-post
            # Collapse to the NEXT upcoming timeslot — Mobilize visibility/recurring
            # events can carry dozens of future timeslots; one card each, like ICS.
            starts = sorted(ts["start_date"] for ts in (ev.get("timeslots") or [])
                            if ts.get("start_date"))
            upcoming = [s for s in starts if s >= NOW.timestamp() - 6 * 3600]
            if not upcoming:
                continue
            e = norm_event(
                city_key, title=ev.get("title", ""),
                start=dt.datetime.fromtimestamp(upcoming[0], dt.timezone.utc),
                type_=_guess_type(ev.get("event_type"), default_type),
                org=(ev.get("sponsor") or {}).get("name"),
                venue=loc.get("venue"), lat=ll.get("latitude"), lng=ll.get("longitude"),
                url=ev.get("browser_url"), blurb=(ev.get("description") or "")[:280],
                source="mobilize",
                recurrence=("Recurring" if len(upcoming) > 1 else None))
            if e:
                out.append(e)
        url = body.get("next")
        if not url:
            break
    else:
        log(f"[{city_key}] mobilize org {org_id}: hit {_MOBILIZE_MAX_PAGES}-page cap, "
            f"stopping pagination early")
    if near_zips and dropped:
        log(f"[{city_key}] mobilize org {org_id}: dropped {dropped} non-local events "
            f"(kept postal prefixes {near_zips})")
    return out

# How far ahead we expand repeating events. A weekly rebuild keeps data fresh;
# this only sets how far a visitor can SEE. 35d guarantees the next occurrence of
# a MONTHLY series is always visible and gives a buffer if a weekly build is missed.
RECUR_WINDOW_DAYS = 35
_WD = {"MO":"Monday","TU":"Tuesday","WE":"Wednesday","TH":"Thursday",
       "FR":"Friday","SA":"Saturday","SU":"Sunday"}
_WEEKDAY = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

def rrule_label(comp):
    """Human label for an RRULE, e.g. 'Every Sunday' / 'Every other Tuesday' / 'Monthly'."""
    r = comp.get("rrule")
    if not r:
        return None
    freq = (r.get("FREQ") or [""])[0]
    interval = int((r.get("INTERVAL") or [1])[0])
    byday = r.get("BYDAY") or []
    day = _WD.get(str(byday[0])[-2:].upper()) if byday else None
    if not day:
        ds = comp.get("dtstart")
        if ds is not None and hasattr(ds.dt, "strftime"):
            day = ds.dt.strftime("%A")
    if freq == "WEEKLY":
        if interval == 1: return f"Every {day}" if day else "Weekly"
        if interval == 2: return f"Every other {day}" if day else "Every other week"
        return f"Every {interval} weeks"
    if freq == "DAILY":
        return "Daily" if interval == 1 else f"Every {interval} days"
    if freq == "MONTHLY":
        return "Monthly" if interval == 1 else f"Every {interval} months"
    if freq == "YEARLY":
        return "Yearly"
    return "Recurring"

def _ics_start(comp):
    """Comparable tz-aware start for an ICS component (all-day/naive -> DEFAULT_TZ)."""
    ds = comp.get("dtstart")
    if not ds:
        return None
    v = ds.dt
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        v = dt.datetime(v.year, v.month, v.day, tzinfo=DEFAULT_TZ)
    elif v.tzinfo is None:
        v = v.replace(tzinfo=DEFAULT_TZ)
    return v

def _ics_norm(city_key, comp, org, default_type, recurrence=None):
    """Turn one ICS VEVENT (master, one-off, or expanded occurrence) into an event."""
    lat = lng = None
    geo = comp.get("geo")
    if geo is not None:
        try: lat, lng = float(geo.latitude), float(geo.longitude)
        except Exception: pass
    ds = comp.get("dtstart")
    return norm_event(
        city_key, title=str(comp.get("summary", "")),
        start=ds.dt if ds else None, type_=default_type, org=org,
        venue=str(comp.get("location", "")) or None,
        lat=lat, lng=lng, url=str(comp.get("url", "")) or None,
        blurb=str(comp.get("description", "") or "")[:280],
        source="ics", recurrence=recurrence)

def fetch_ics(city_key, url, org=None, default_type="meeting"):
    """Parse an .ics feed (The Events Calendar, Google Calendar, etc.).
    Param is `url` to match the documented sources.yml schema (kind: ics, url: …).

    Recurring series are COLLAPSED to a single card at their next occurrence within
    the next RECUR_WINDOW_DAYS (labelled e.g. 'Every Sunday'); one-off events are
    emitted with no forward cap, exactly as before."""
    if Calendar is None:
        raise RuntimeError("icalendar not installed (pip install icalendar)")
    cal = Calendar.from_ical(http_get(url, timeout=30).content)

    # Which UIDs are recurring masters? Map each to its human label.
    recurring = {str(c.get("uid")): rrule_label(c)
                 for c in cal.walk("VEVENT") if c.get("rrule")}

    out = []

    # Collapse recurring series -> the earliest upcoming occurrence in the window.
    if recurring:
        try:
            import recurring_ical_events
            occs = recurring_ical_events.of(cal).between(
                NOW - dt.timedelta(hours=6), NOW + dt.timedelta(days=RECUR_WINDOW_DAYS))
        except Exception as ex:
            log(f"[{city_key}] recurrence expansion unavailable ({ex}); skipping repeats")
            occs = []
        best = {}
        for occ in occs:
            uid = str(occ.get("uid"))
            if uid not in recurring:
                continue                      # one-offs handled below (no window cap)
            k = _ics_start(occ)
            if k is not None and (uid not in best or k < best[uid][0]):
                best[uid] = (k, occ, recurring[uid])
        # Second collapse: some orgs model one weekly-ish event as several separate
        # monthly series (e.g. the same reading circle on alternating Tuesdays/venues).
        # Merge same title on the same weekday to the single earliest upcoming card —
        # while keeping genuinely-distinct same-title series (e.g. a Sat and a Sun
        # session) apart, since they fall on different weekdays.
        merged = {}
        for k, occ, label in best.values():
            ck = (str(occ.get("summary", "")).strip().lower(), k.weekday())
            if ck not in merged or k < merged[ck][0]:
                merged[ck] = (k, occ, label)
        for k, occ, label in merged.values():
            e = _ics_norm(city_key, occ, org, default_type, recurrence=label)
            if e:
                out.append(e)

    # One-off events: emit all future ones (no forward cap). Skip recurrence masters
    # and their edited-instance overrides — those are represented by the collapsed card.
    for comp in cal.walk("VEVENT"):
        if comp.get("rrule") or comp.get("recurrence-id") is not None:
            continue
        e = _ics_norm(city_key, comp, org, default_type)
        if e:
            out.append(e)
    return out

def _strip_tags(s):
    """Strip HTML tags and decode entities (Squarespace encodes &amp;, accents, …)."""
    import re, html
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

def _ss_months(start, end):
    """Yield 'MM-YYYY' strings for every month spanned by [start, end] inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{m:02d}-{y}"
        m, y = (1, y + 1) if m == 12 else (m + 1, y)

def fetch_squarespace(city_key, url, org=None, default_type="meeting"):
    """Pull events from a Squarespace 7.1 Events collection. Squarespace dropped the
    ICS export, but the collection's OWN data API still serves structured JSON — this
    is an automated feed of the org's published events, just a different transport.

    The generic ?format=json view is unreliable (paginates oddly), so we use the
    collection's `/api/open/GetItemsByMonth` endpoint, month by month across the
    window. startDate is a unix-ms UTC timestamp. Recurring events come pre-expanded
    into instances, so we collapse same-title-same-weekday to the next upcoming card
    (labelling weekly ones 'Every <day>')."""
    from urllib.parse import urlsplit
    base = urlsplit(url)
    origin = f"{base.scheme}://{base.netloc}"
    sep = "&" if "?" in url else "?"
    meta = http_get(url + sep + "format=json", timeout=30).json() or {}
    cid = (meta.get("collection") or {}).get("id")
    if not cid:
        raise RuntimeError("squarespace: could not resolve collection id")

    floor = NOW - dt.timedelta(hours=6)
    horizon = NOW + dt.timedelta(days=RECUR_WINDOW_DAYS)
    counts, earliest = {}, {}
    for mon in _ss_months(NOW, horizon):
        api = f"{origin}/api/open/GetItemsByMonth?month={mon}&collectionId={cid}"
        items = http_get(api, timeout=30).json() or []
        for it in items:
            sd = it.get("startDate")
            if not sd:
                continue
            start = dt.datetime.fromtimestamp(sd / 1000, dt.timezone.utc)
            if start < floor or start > horizon:
                continue
            wd = start.astimezone(DEFAULT_TZ).weekday()
            key = (str(it.get("title", "")).strip().lower(), wd)
            counts[key] = counts.get(key, 0) + 1
            if key not in earliest or start < earliest[key][0]:
                earliest[key] = (start, it, wd)

    import html
    out = []
    for key, (start, it, wd) in earliest.items():
        loc = it.get("location") or {}
        venue = loc.get("addressTitle") or loc.get("addressLine1") or None
        full = it.get("fullUrl") or ""
        e = norm_event(
            city_key, title=html.unescape(str(it.get("title", ""))), start=start,
            type_=default_type, org=org, venue=html.unescape(venue) if venue else None,
            url=(origin + full) if full.startswith("/") else (full or None),
            blurb=_strip_tags(it.get("excerpt") or it.get("body") or "")[:280],
            source="squarespace",
            recurrence=(f"Every {_WEEKDAY[wd]}" if counts[key] > 1 else None))
        if e:
            out.append(e)
    return out

FETCHERS = {"mobilize": fetch_mobilize, "ics": fetch_ics, "squarespace": fetch_squarespace}


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

# ─────────────────────────── SEO: SSR + JSON-LD + sitemap ───────────────────
# The front end renders events client-side, which crawlers index poorly. Each
# build also bakes the current events into index.html as static HTML + schema.org
# JSON-LD and refreshes sitemap.xml, so search engines and no-JS clients see real
# content. The runtime JS overwrites the SSR block on load with the same content.
SITE_URL     = "https://ragingcunt.com"
DEFAULT_CITY = "sacramento"
INDEX_HTML   = ROOT / "index.html"
SITEMAP_XML  = ROOT / "sitemap.xml"
TYPE_META = {
    "action":   ("ACTION",         "t-action"),
    "meeting":  ("MEETING",        "t-meeting"),
    "mutualaid":("MUTUAL AID",     "t-mutualaid"),
    "show":     ("SHOW",           "t-show"),
    "teachin":  ("TEACH-IN",       "t-teachin"),
    "tenant":   ("TENANT",         "t-tenant"),
    "kyr":      ("KNOW UR RIGHTS", "t-kyr"),
}

def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace(
        "<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _pt(iso):
    """Parse an event start ISO into a DEFAULT_TZ-local datetime (or None)."""
    try:
        d = dtparse.parse(iso)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=DEFAULT_TZ)
    return d.astimezone(DEFAULT_TZ)

def _clock(d):
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d}{'AM' if d.hour < 12 else 'PM'} PT"

def render_ssr(result):
    """Static HTML of the default city's events (mirrors the JS render markup)."""
    city = (result.get("cities") or {}).get(DEFAULT_CITY)
    if not city:
        return ""
    evs = sorted((e for e in city.get("events", []) if _pt(e.get("start"))),
                 key=lambda e: _pt(e["start"]))
    n = len(evs)
    html = (f'<div class="ihead"><span class="eyebrow">ISSUE #01</span><h1>CITYWIDE</h1>'
            f'<div class="where">{_esc((city.get("label") or "").upper())} · '
            f'<span class="count">{n} DISPATCH{"" if n == 1 else "ES"}</span></div></div>'
            f'<div class="feed">')
    last_day = None
    for e in evs:
        d = _pt(e["start"])
        daykey = d.date().isoformat()
        if daykey != last_day:
            if last_day is not None:
                html += "</div>"
            wd = d.strftime("%a").upper()
            md = d.strftime("%b").upper() + " " + str(d.day)
            html += (f'<div class="daydiv"><div class="d"><b>{wd}</b> {md}</div></div>'
                     f'<div class="grid">')
            last_day = daykey
        tlabel, tcls = TYPE_META.get(e.get("type"), (str(e.get("type", "")).upper(), ""))
        rec = e.get("recurrence")
        recpill = f'<span class="recur">↻ {_esc(rec)}</span>' if rec else ""
        blurb = _strip_tags(e.get("blurb") or "")
        if len(blurb) > 168:
            blurb = blurb[:168].rsplit(" ", 1)[0] + "…"
        blurb_html = f'<div class="blurb">{_esc(blurb)}</div>' if blurb else ""
        url = e.get("url")
        link = (f'<a class="flyer-link" href="{_esc(url)}" target="_blank" rel="noopener">'
                f'DETAILS / RSVP →</a>' if url and url != "#" else "")
        html += (
            f'<article class="flyer"><div class="tagrow">'
            f'<span class="tag {tcls}">{_esc(tlabel)}</span>{recpill}</div>'
            f'<h3 class="ftitle">{_esc(e.get("title"))}</h3><div class="meta">'
            f'<div><span class="k">{"NEXT" if rec else "WHEN"}</span><span>{_clock(d)}</span></div>'
            f'<div><span class="k">WHERE</span><span>{_esc(e.get("venue") or "TBA — RSVP")}</span></div>'
            f'<div><span class="k">WHO</span><span>{_esc(e.get("org") or "—")}</span></div></div>'
            f'{blurb_html}{link}'
            f'<div class="src"><span>src: {_esc(e.get("source") or "—")}</span></div></article>')
    if last_day is not None:
        html += "</div>"
    return html + "</div>"

def render_jsonld(result):
    """schema.org Event JSON-LD for every event (search rich results)."""
    events = []
    for city in (result.get("cities") or {}).values():
        cityname = (city.get("label") or "").split(",")[0] or "Sacramento"
        for e in city.get("events", []):
            if not e.get("start"):
                continue
            obj = {
                "@context": "https://schema.org", "@type": "Event",
                "name": e.get("title") or "", "startDate": e["start"],
                "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
                "eventStatus": "https://schema.org/EventScheduled",
                "location": {"@type": "Place", "name": e.get("venue") or f"{cityname}, CA",
                             "address": {"@type": "PostalAddress",
                                         "streetAddress": e.get("venue") or "",
                                         "addressLocality": cityname, "addressRegion": "CA"}},
                "organizer": {"@type": "Organization", "name": e.get("org") or ""},
                "url": e.get("url") or SITE_URL,
            }
            if e.get("end"):
                obj["endDate"] = e["end"]
            blurb = _strip_tags(e.get("blurb") or "")
            if blurb:
                obj["description"] = blurb[:300]
            events.append(obj)
    if not events:
        return ""
    payload = json.dumps(events, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'

def _replace_between(text, start, end, inner):
    i, j = text.find(start), text.find(end)
    if i == -1 or j == -1 or j < i:
        raise RuntimeError(f"SEO markers missing: {start!r}..{end!r}")
    return text[:i + len(start)] + inner + text[j:]

def write_sitemap():
    day = NOW.date().isoformat()
    SITEMAP_XML.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{SITE_URL}/</loc><lastmod>{day}</lastmod>'
        '<changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
        '</urlset>\n', encoding="utf-8")

def inject_seo(result):
    """Bake SSR HTML + JSON-LD into index.html and refresh sitemap.xml. Best-effort:
    never aborts the build — events.json is the authoritative output."""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
        html = _replace_between(html, "<!--SSR:START-->", "<!--SSR:END-->", render_ssr(result))
        html = _replace_between(html, "<!--JSONLD:START-->", "<!--JSONLD:END-->", render_jsonld(result))
        INDEX_HTML.write_text(html, encoding="utf-8")
        write_sitemap()
        log("SEO: baked SSR + JSON-LD into index.html, refreshed sitemap.xml")
    except Exception as e:
        log(f"SEO injection skipped: {e}")


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
    inject_seo(result)   # bake crawlable HTML + JSON-LD into index.html, refresh sitemap


if __name__ == "__main__":
    main()
