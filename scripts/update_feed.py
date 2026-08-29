#!/usr/bin/env python3
"""Update pulse.json with fresh US data-center buildout news.

Stdlib only. Pulls RSS from Google News + trade press, filters for US
data-center buildout stories, classifies them into DataCentral's PulseEvent
schema, dedupes against existing entries, and rewrites pulse.json.

When a new story has a unique campus name, city, US state, announced MW, and
an RSS sourceURL — and is an announcement or milestone, not policy/grid copy —
a pin is appended to facilities.json. Pulse stays the story hose; the catalog
only grows when the source is enough. Existing seed rows are never rewritten.

Optional: set ANTHROPIC_API_KEY to have new entries cleaned up by an LLM
(better titles, kind/builder/state extraction). Heuristics are used otherwise
and as the fallback on any API failure.

Exit codes: 0 = ran fine (changed or not). Nonzero = hard failure.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

FEED_PATH = os.path.join(os.path.dirname(__file__), "..", "pulse.json")
FACILITIES_PATH = os.path.join(os.path.dirname(__file__), "..", "facilities.json")
MAX_NEW_PER_RUN = 6
MAX_NEW_SITES_PER_RUN = 3
AUTO_ID_START = 3001
MAX_TOTAL_EVENTS = 120
FRESH_WINDOW_DAYS = 10

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
SOURCES = [
    GNEWS.format(q="%22data%20center%22%20(megawatt%20OR%20gigawatt)"),
    GNEWS.format(q="Stargate%20OpenAI%20data%20center"),
    GNEWS.format(q="xAI%20Colossus%20data%20center"),
    GNEWS.format(q="%22AI%20data%20center%22%20grid%20power"),
    # Ratepayer / legislation recall for the 1.3.0 Your State cards. Without
    # these the feed catches buildout announcements but not the bills and rate
    # cases that decide who pays for them.
    GNEWS.format(q="%22data%20center%22%20(ratepayer%20OR%20%22electric%20bill%22%20OR%20%22rate%20increase%22)"),
    GNEWS.format(q="%22data%20center%22%20(moratorium%20OR%20legislation%20OR%20%22state%20bill%22)"),
    "https://www.datacenterknowledge.com/rss.xml",
    "https://www.datacenterdynamics.com/rss/",
]

BUILDERS = [
    "OpenAI", "Oracle", "xAI", "Meta", "Microsoft", "Google", "Amazon", "AWS",
    "Anthropic", "CoreWeave", "Crusoe", "Vantage", "QTS", "Equinix",
    "Digital Realty", "SoftBank", "Nvidia", "Switch", "Aligned", "EdgeConneX",
    "CyrusOne", "NTT", "Lambda", "Nebius", "Apple", "Tesla", "Stack",
]

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

STATE_CODE_TO_NAME = {code: name.title() for name, code in STATES.items()}

# Honesty: never write these on an auto-added row. The iOS decoder defaults
# missing telemetry to 0; putting numbers here would print invented PUE/used-MW.
CATALOG_NEVER_WRITE = (
    "pueRating", "usedCapacityMw", "serverCount", "uptimePercent",
    "gpuUtilizationPercent", "gpuCount", "gpuType", "capacityPercent",
    "powerUsageKw", "source", "sourceURL",
)

CITY_PLACE_TYPES = {
    "city", "town", "village", "hamlet", "suburb", "neighbourhood",
    "quarter", "borough", "municipality", "city_district",
}

_MONTHS = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
CITY_BLOCKLIST = set(STATES) | _MONTHS | {
    "ai", "us", "usa", "america", "united states",
    "north", "south", "east", "west",
    "data", "center", "centre", "campus", "power", "grid",
    "county", "state", "project", "agreement", "partnership",
}

_STATE_NAME_ALT = "|".join(
    sorted((re.escape(n) for n in STATES), key=len, reverse=True)
)
_STATE_CODE_ALT = "|".join(sorted(set(STATES.values()), key=len, reverse=True))

# "in Cheyenne, Wyoming" / "near Cheyenne, WY" / "in Cheyenne"
IN_CITY_RE = re.compile(
    r"\b(?:in|near|at|outside)\s+"
    r"(?P<city>[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,2})"
    r"(?:,\s*(?P<state>" + _STATE_NAME_ALT + r"|" + _STATE_CODE_ALT + r"))?",
    re.I,
)
# "Cheyenne, WY" — 2-letter code only, so "Oracle, Texas" does not count.
CITY_ST_RE = re.compile(
    r"\b(?P<city>[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,2})"
    r",\s*(?P<code>" + _STATE_CODE_ALT + r")\b",
)

REQUIRED_ANY = [
    "data center", "datacenter", "data centre", "stargate", "colossus",
    "hyperscale", "ai infrastructure", "ai campus",
]
EXCLUDE_ANY = [
    "stocks to", "stock to", "price target", "shares of", "buy rating",
    "dividend", "etf", "invest in these", "motley fool", "wall st",
    "top 10 stocks", "analyst", "earnings call",
]

NON_US_RE = re.compile(
    r"\b(canada|canadian|toronto|ontario|quebec|vancouver|alberta|uk|britain|"
    r"london|europe|european|germany|france|spain|italy|netherlands|ireland|"
    r"sweden|norway|finland|denmark|india|mumbai|china|japan|tokyo|korea|"
    r"seoul|singapore|malaysia|indonesia|australia|sydney|brazil|mexico city|"
    r"saudi|uae|dubai|qatar|israel|africa|trinidad|tobago|caribbean)\b", re.I
)

KIND_RULES = [
    # "bill " used to be here and matched "electric bill", pulling ratepayer
    # stories into policy. Legislative bills are now matched explicitly below.
    ("policy", ["permit", "zoning", "moratorium", "regulat", "ordinance",
                "lawsuit", "tax break", "legislat", "county approve",
                "county reject", "city council", "house bill", "senate bill",
                "state bill", "ratepayer protection", "rate case",
                "public utilities commission", "public service commission"]),
    ("grid", ["grid", "ercot", "pjm", "miso", "caiso", "substation",
              "transmission", "utility", "power plant", "nuclear",
              "gas turbine", "electricity price", "electric bill",
              "power demand", "megawatts of power"]),
    ("milestone", ["opens", "goes online", "now online", "goes live",
                   "complete", "energized", "operational", "first power",
                   "begins operating", "opens doors", "ribbon"]),
]

MW_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*[- ]?(gigawatt|megawatt|gw|mw)\b", re.I
)

# Legislative bill identifiers: "HB 1500", "S.B. 32", "House Bill 12", "LD 1234",
# "H.R. 5678". Normalised to a compact "HB 1500" form.
BILL_RE = re.compile(
    r"\b("
    r"(?:H\.?\s?B\.?|S\.?\s?B\.?|A\.?\s?B\.?|H\.?\s?R\.?|S\.?\s?R\.?|L\.?\s?D\.?)"
    r"\s?\d{1,5}"
    r"|(?:House|Senate|Assembly)\s+Bill\s+\d{1,5}"
    r")\b", re.I
)

# Status vocabulary is deliberately neutral — a bill *advanced* or *stalled*, it
# never "threatens" or "protects" anything. The app reports what happened; it
# does not take a side. See docs/RATEPAYER-MVP.md in the DataCentral repo.
#
# Real feed content is mostly *regulatory and local* decisions — "regulators
# reject", "state pauses projects over 50 MW", "county ends a tax break" —
# not statehouse bills moving through committee. An earlier legislature-only
# vocabulary matched 0 of the 8 policy events actually in the feed, so these
# cover both, with verbs that describe the action and nothing more.
STATUS_RULES = [
    ("signed", ["signed into law", "signs into law", "governor signed"]),
    # Stems, so "approve" also catches approves/approved/approval. The "<body>
    # ok" forms are headline style ("Indiana regulators OK ..."); bare "ok" is
    # far too loose to use on its own.
    ("approved", ["passed the", "passes the", "voted to approve", "approve",
                  "cleared the legislature", "ok'd", "regulators ok",
                  "commission ok", "council ok", "board ok", "greenlight",
                  "green-light"]),
    ("rejected", ["voted down", "reject", "denie", "deny", "defeat", "veto",
                  "ends ", "ended", "repeal"]),
    # "moratorium" is deliberately absent: it names a topic, not an action, and
    # matching it marked "plans submitted ahead of moratorium" as stalled — a
    # status the story never reported. Omitting beats misreporting.
    ("stalled", ["stalled", "shelved", "tabled", "postponed", "withdrawn",
                 "held in committee", "pauses", "paused",
                 "halts", "halted", "freeze", "delays"]),
    ("advanced", ["advanced", "advances", "clears committee", "cleared committee",
                  "moves to the", "sent to the senate", "sent to the house",
                  "committee approved"]),
    ("introduced", ["introduced", "filed", "proposed", "unveiled", "submitted"]),
]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "datacentral-feed/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def rss_items(raw):
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate")
        try:
            date = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            date = None
        if title and link:
            yield title, link, re.sub(r"<[^>]+>", " ", desc), date


def norm_title_key(title):
    return hashlib.sha1(re.sub(r"[^a-z0-9]", "", title.lower()).encode()).hexdigest()


def story_key(event):
    """Same builders + MW + state = same story reported by different outlets.
    Returns None when there isn't enough signal to safely merge."""
    if not event["builders"] and event["mw"] is None:
        return None
    return (tuple(sorted(event["builders"])), event["mw"], event["stateCode"])


def classify(text):
    for kind, needles in KIND_RULES:
        if any(n in text for n in needles):
            return kind
    return "announcement"


def extract_mw(text):
    m = MW_RE.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2).lower().startswith("g"):
        value *= 1000
    return round(value, 1)


def normalize_bill_id(raw):
    """'H.B. 1500' / 'House Bill 1500' -> 'HB 1500'."""
    compact = re.sub(r"[.\s]", "", raw).upper()
    m = re.match(r"^(HOUSEBILL|SENATEBILL|ASSEMBLYBILL|HB|SB|AB|HR|SR|LD)(\d+)$", compact)
    if not m:
        return raw.strip()
    prefix = {"HOUSEBILL": "HB", "SENATEBILL": "SB", "ASSEMBLYBILL": "AB"}.get(
        m.group(1), m.group(1)
    )
    return f"{prefix} {m.group(2)}"


def extract_policy(text):
    """Bill identifier + neutral status for policy-kind events.

    Returns None when neither is present — an empty object would just be noise
    the client has to guard against. Both fields are independently optional:
    a rate-case story has a status and no bill, a bill filing has both.
    """
    bill = BILL_RE.search(text)
    status = None
    for name, needles in STATUS_RULES:
        if any(n in text for n in needles):
            status = name
            break
    if not bill and not status:
        return None
    payload = {}
    if bill:
        payload["billId"] = normalize_bill_id(bill.group(1))
    if status:
        payload["status"] = status
    return payload


def extract_state(text):
    for name, code in STATES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return code
    return None


def extract_builders(title):
    found = []
    for b in BUILDERS:
        if re.search(r"\b" + re.escape(b) + r"\b", title, re.I):
            found.append(b)
    return found


def _normalize_state_token(token):
    if not token:
        return None
    raw = token.strip()
    if len(raw) == 2 and raw.upper() in STATE_CODE_TO_NAME:
        return raw.upper()
    return STATES.get(raw.lower())


def _usable_city_name(city):
    if not city:
        return None
    name = re.sub(r"\s+", " ", city).strip(" ,")
    if name.islower():
        name = name.title()
    lowered = name.lower()
    if lowered in CITY_BLOCKLIST:
        return None
    if "county" in lowered:
        return None
    if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
        return None
    return name


def extract_city(title, desc, state_code_hint=None):
    """Return (city, stateCode) from the story text, or (None, None).

    Prefers 'in City, State' / 'in City, ST'. Falls back to 'City, ST' and
    then 'in City' when the event already has a US stateCode. Never treats a
    state name as a city.
    """
    blob = f"{title} {desc}".strip()
    for m in IN_CITY_RE.finditer(blob):
        city = _usable_city_name(m.group("city"))
        if not city:
            continue
        # IN_CITY_RE is case-insensitive for state names; require the city
        # slice to be capitalized in the source so "in the Dallas area" drops.
        start = m.start("city")
        if start >= 0 and blob[start:start + 1] and not blob[start].isupper():
            continue
        code = _normalize_state_token(m.group("state")) or state_code_hint
        if code:
            return city, code
    for m in CITY_ST_RE.finditer(blob):
        city = _usable_city_name(m.group("city"))
        code = _normalize_state_token(m.group("code"))
        if city and code:
            return city, code
    return None, None


def campus_name(event, city):
    """Stable catalog name. Seed style is '{Builder} {City}'."""
    title = event.get("title") or ""
    builders = event.get("builders") or []
    if re.search(r"\bstargate\b", title, re.I):
        return f"Stargate {city}"
    if re.search(r"\bcolossus\b", title, re.I):
        return f"xAI Colossus — {city}"
    if builders:
        return f"{builders[0]} {city}"
    m = re.match(
        r"^([A-Z][A-Za-z0-9.&'’-]+(?:\s+[A-Z][A-Za-z0-9.&'’-]*){0,3})\s+"
        r"(?:plans|announces|breaks|acquires|secures|opens|to build|will build)",
        title,
    )
    if m:
        who = m.group(1).strip().rstrip("'’s")
        if who.lower() not in CITY_BLOCKLIST and len(who) >= 3:
            return f"{who} {city}"
    return None


def norm_site_key(name, state_code):
    n = (name or "").lower()
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    n = re.sub(
        r"\b(campus|data centres?|data centers?|datacenters?|hyperscale|"
        r"flagship|ai)\b",
        " ",
        n,
    )
    n = re.sub(r"\s+", " ", n).strip()
    return (n, (state_code or "").upper())


def existing_site_keys(catalog):
    keys = set()
    for row in list(catalog.get("sites") or []) + list(catalog.get("aiCampuses") or []):
        name = row.get("name") or ""
        code = row.get("stateCode") or ""
        if name and code:
            keys.add(norm_site_key(name, code))
    return keys


def next_auto_id(catalog):
    ids = [row.get("id") or 0 for row in catalog.get("sites") or []]
    ids += [row.get("id") or 0 for row in catalog.get("aiCampuses") or []]
    taken = [i for i in ids if i >= AUTO_ID_START]
    return max(taken, default=AUTO_ID_START - 1) + 1


def format_as_of(dt):
    """Match FacilityCatalogService.formatAsOf: 'MMMM yyyy' on the 1st."""
    if dt.day == 1:
        return dt.strftime("%B %Y")
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%Y')}"


def propose_campus(event, catalog):
    """Draft a catalog row from a pulse event, or None if the source is thin.

    Does not geocode and does not assign an id. Caller adds those only after
    a named-city geocode succeeds.
    """
    if (event.get("kind") or "") not in ("announcement", "milestone"):
        return None
    if not (event.get("sourceURL") or "").strip():
        return None
    if event.get("mw") is None:
        return None
    title = event.get("title") or ""
    detail = event.get("detail") or ""
    city, state_code = extract_city(title, detail, event.get("stateCode"))
    if not city or not state_code:
        return None
    state_name = STATE_CODE_TO_NAME.get(state_code)
    if not state_name:
        return None
    name = campus_name(event, city)
    if not name:
        return None
    if norm_site_key(name, state_code) in existing_site_keys(catalog):
        return None
    try:
        mw = float(event["mw"])
    except (TypeError, ValueError):
        return None
    row = {
        "name": name,
        "city": city,
        "stateName": state_name,
        "stateCode": state_code,
        "status": "building",
        "provider": " / ".join(event.get("builders") or []),
        "totalCapacityMw": mw,
        "facilityType": "hyperscale",
    }
    for banned in CATALOG_NEVER_WRITE:
        row.pop(banned, None)
    return row


def usable_city_geocode(hit):
    """Accept a Nominatim hit only when it is a named US place, not a state.

    Never returns (0, 0). Never accepts a state/country centroid.
    """
    try:
        lat = float(hit.get("lat"))
        lon = float(hit.get("lon"))
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None
    address = hit.get("address") or {}
    country = (address.get("country_code") or "").lower()
    if country and country != "us":
        return None
    addresstype = (hit.get("addresstype") or hit.get("type") or "").lower()
    if addresstype in {"state", "country", "continent", "region", "county"}:
        return None
    cls = (hit.get("class") or "").lower()
    if cls == "boundary" and addresstype == "administrative":
        if not any(address.get(k) for k in CITY_PLACE_TYPES):
            return None
        if not address.get("city") and not address.get("town") and not address.get("village"):
            return None
    if addresstype not in CITY_PLACE_TYPES and (hit.get("class") or "") != "place":
        if not any(address.get(k) for k in CITY_PLACE_TYPES):
            return None
    return (round(lat, 4), round(lon, 4))


def geocode_city(city, state_name, state_code=None):
    """Geocode the named city only. None if Nominatim cannot pin a settlement."""
    query = f"{city}, {state_name}, USA"
    params = {
        "q": query,
        "format": "json",
        "addressdetails": 1,
        "limit": 5,
        "countrycodes": "us",
        "featuretype": "settlement",
    }
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    try:
        raw = fetch(url, timeout=20)
        hits = json.loads(raw.decode())
    except Exception as e:  # noqa: BLE001 — one bad geocode must not kill the run
        print(f"geocode failed ({query}): {e}", file=sys.stderr)
        return None
    if not isinstance(hits, list):
        return None
    for hit in hits:
        coords = usable_city_geocode(hit)
        if coords:
            return coords
    return None


def append_sourced_campuses(catalog, events, geocode=None, now=None, dry_run=False,
                            max_new=MAX_NEW_SITES_PER_RUN):
    """Append at most max_new sourced sites. Never mutates existing rows.

    `geocode` is (city, state_name, state_code) -> (lat, lon) | None.
    Dry run still geocodes so the printed list is real, but does not write
    into `catalog`.
    """
    geocode = geocode or geocode_city
    now = now or datetime.now(timezone.utc)
    added = []
    next_id = next_auto_id(catalog)
    # Copy of keys so same-run duplicates skip even on dry_run.
    keys = existing_site_keys(catalog)
    for event in events:
        if len(added) >= max_new:
            break
        row = propose_campus(event, catalog)
        if not row:
            continue
        key = norm_site_key(row["name"], row["stateCode"])
        if key in keys:
            continue
        coords = geocode(row["city"], row["stateName"], row["stateCode"])
        if geocode is geocode_city:
            time.sleep(1.1)
        if not coords:
            print(
                f"skip campus {row['name']}: no city geocode",
                file=sys.stderr,
            )
            continue
        lat, lon = coords
        if (lat, lon) == (0, 0):
            continue
        site = {
            "id": next_id,
            "name": row["name"],
            "city": row["city"],
            "stateName": row["stateName"],
            "stateCode": row["stateCode"],
            "status": "building",
            "provider": row["provider"],
            "totalCapacityMw": row["totalCapacityMw"],
            "latitude": lat,
            "longitude": lon,
            "facilityType": "hyperscale",
        }
        for banned in CATALOG_NEVER_WRITE:
            site.pop(banned, None)
        added.append(site)
        keys.add(key)
        next_id += 1
        if not dry_run:
            catalog.setdefault("sites", []).append(site)
    if added and not dry_run:
        catalog["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        catalog["asOfLabel"] = format_as_of(now)
    return added


def split_gnews_title(title):
    """Google News titles end with ' - Source Name'."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and 2 <= len(tail) <= 40:
            return head.strip(), tail.strip()
    return title, None


def heuristic_event(title, link, desc, date):
    clean_title, source = split_gnews_title(title)
    text = (clean_title + " " + desc).lower()
    kind = classify(text)
    event = {
        "id": hashlib.sha1(link.encode()).hexdigest()[:16],
        "date": (date or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
        "kind": kind,
        "title": clean_title[:140],
        "detail": "",
        "builders": extract_builders(clean_title),
        "stateCode": extract_state(text),
        "mw": extract_mw(text),
        "facilityId": None,
        "sourceName": source,
        "sourceURL": link,
    }
    # Only policy events carry this, and only when there is something to carry.
    # Absent rather than null, so older clients decoding a fixed key set are
    # unaffected — the field is purely additive.
    if kind == "policy":
        policy = extract_policy(text)
        if policy:
            event["policy"] = policy
    return event


def llm_polish(events):
    """Optionally rewrite/classify new entries via the Anthropic API."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not events:
        return events
    prompt = (
        "You maintain a US data-center buildout news feed. For each item, "
        "rewrite `title` as a concise factual headline (max 90 chars, no "
        "source name, no clickbait), write a one-sentence `detail`, and "
        "correct `kind` (announcement|milestone|grid|policy), `builders` "
        "(company names), `stateCode` (2-letter US state or null), and `mw` "
        "(number or null) using only information in the item. Keep `id`, "
        "`date`, `sourceName`, `sourceURL`, `facilityId` unchanged.\n\n"
        "For `kind: \"policy\"` items only, you may set `policy` to an object "
        "with `billId` (e.g. \"HB 1500\", omit when no bill is named) and/or "
        "`status`, one of: introduced, advanced, stalled, approved, rejected, "
        "signed. These cover regulatory and local decisions too — a commission "
        "rejecting a permit is `rejected`, a state pausing projects is "
        "`stalled`. Omit the `policy` key entirely when neither is stated. "
        "Never infer a status that is not reported.\n\n"
        "Stay neutral and factual throughout. A bill advanced, stalled or "
        "passed — it never threatens, protects, or saves anything. Do not "
        "characterise data centers or legislation as good or bad.\n\n"
        "Return ONLY a JSON array with the same length and order.\n\n"
        + json.dumps(events)
    )
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
        text = payload["content"][0]["text"]
        text = text[text.index("["): text.rindex("]") + 1]
        polished = json.loads(text)
        if (
            isinstance(polished, list)
            and len(polished) == len(events)
            and all(p.get("id") == e["id"] for p, e in zip(polished, events))
        ):
            return polished
    except Exception as e:  # noqa: BLE001 — LLM polish is best-effort
        print(f"LLM polish skipped: {e}", file=sys.stderr)
    return events


def selftest():
    """`python3 scripts/update_feed.py --selftest` — no network, no writes.

    Pins the policy extraction so a regex tweak can't silently stop populating
    the field, and pins the classification split that ratepayer stories depend on.
    """
    cases = [
        ("ohio hb 15 advanced out of committee on data center rates",
         {"billId": "HB 15", "status": "advanced"}),
        ("lawmakers filed senate bill 1200 on data center power use",
         {"billId": "SB 1200", "status": "introduced"}),
        ("governor signed into law new data center tax rules",
         {"status": "signed"}),
        # Regulatory and local decisions — the shape most feed policy items
        # actually take. A legislature-only vocabulary matched none of these.
        ("new mexico regulators reject natural gas pipeline for oracle",
         {"status": "rejected"}),
        ("new york pauses data center projects over 50 mw",
         {"status": "stalled"}),
        ("indiana regulators ok energy project tied to google data center",
         {"status": "approved"}),
        ("north carolina ends data center power tax break",
         {"status": "rejected"}),
        ("virginia data center campus opens next year", None),
    ]
    failures = []
    for text, expected in cases:
        got = extract_policy(text)
        if got != expected:
            failures.append(f"extract_policy({text!r}) -> {got!r}, want {expected!r}")

    kinds = [
        # "electric bill" must not read as a legislative bill.
        ("data center blamed for higher electric bill in georgia", "grid"),
        ("county council votes on data center zoning", "policy"),
        ("ohio house bill 15 targets data center rates", "policy"),
        ("meta data center goes online in iowa", "milestone"),
    ]
    for text, expected in kinds:
        got = classify(text)
        if got != expected:
            failures.append(f"classify({text!r}) -> {got!r}, want {expected!r}")

    def announcement(**kwargs):
        event = {
            "kind": "announcement",
            "title": "Meta announces 500 MW data center campus in Cheyenne, Wyoming",
            "detail": "",
            "builders": ["Meta"],
            "stateCode": "WY",
            "mw": 500,
            "sourceURL": "https://example.com/cheyenne",
        }
        event.update(kwargs)
        return event

    empty_cat = {"sites": [], "aiCampuses": []}
    campus_checks = 0

    campus_checks += 1
    if propose_campus(
        announcement(
            mw=None,
            title="Meta announces a data center campus in Cheyenne, Wyoming",
        ),
        empty_cat,
    ) is not None:
        failures.append("skip-without-MW: proposed a campus when mw is missing")

    campus_checks += 1
    dup_cat = {
        "sites": [{"name": "Meta Altoona Campus", "stateCode": "IA", "city": "Altoona"}],
        "aiCampuses": [{"name": "CoreWeave Austin GPU Hub", "stateCode": "TX"}],
    }
    if propose_campus(
        announcement(
            title="Meta announces 200 MW data center campus in Altoona, Iowa",
            stateCode="IA",
            mw=200,
            sourceURL="https://example.com/altoona",
        ),
        dup_cat,
    ) is not None:
        failures.append("skip-duplicate: proposed a campus already in sites (name+state)")

    campus_checks += 1
    if propose_campus(announcement(kind="policy"), empty_cat) is not None:
        failures.append("skip-policy: proposed a campus from a policy story")

    campus_checks += 1
    if propose_campus(announcement(kind="grid"), empty_cat) is not None:
        failures.append("skip-grid: proposed a campus from a grid story")

    campus_checks += 1
    row = propose_campus(announcement(), empty_cat)
    if row is None:
        failures.append("sourced announcement with city/state/MW should propose a campus")
    else:
        if row.get("name") != "Meta Cheyenne":
            failures.append(f"name {row.get('name')!r} != 'Meta Cheyenne'")
        if row.get("city") != "Cheyenne" or row.get("stateCode") != "WY":
            failures.append(f"place {row.get('city')!r}, {row.get('stateCode')!r}")
        if row.get("status") != "building" or row.get("facilityType") != "hyperscale":
            failures.append("status/facilityType mismatch")
        if row.get("provider") != "Meta" or row.get("totalCapacityMw") != 500:
            failures.append("provider/MW mismatch")
        leaked = [k for k in CATALOG_NEVER_WRITE if k in row]
        if leaked:
            failures.append(f"honesty leak on propose: {leaked}")
        if "id" in row or "latitude" in row or "longitude" in row:
            failures.append("propose_campus must not assign id or coordinates")

    campus_checks += 1
    if usable_city_geocode({
        "lat": "0", "lon": "0", "addresstype": "city", "class": "place",
        "type": "city",
        "address": {"city": "Cheyenne", "state": "Wyoming", "country_code": "us"},
    }) is not None:
        failures.append("geocode accepted 0,0")

    campus_checks += 1
    if usable_city_geocode({
        "lat": "31.0", "lon": "-99.9", "addresstype": "state",
        "class": "boundary", "type": "administrative",
        "address": {"state": "Texas", "country_code": "us"},
    }) is not None:
        failures.append("geocode accepted a state centroid")

    campus_checks += 1

    def boom_geocode(*_a, **_k):
        raise AssertionError("geocode must not run for an incomplete story")

    dry_cat = {"version": 1, "sites": [], "aiCampuses": []}
    dry_added = append_sourced_campuses(
        dry_cat,
        [announcement(
            mw=None,
            title="Meta announces a data center campus in Cheyenne, Wyoming",
        )],
        geocode=boom_geocode,
        dry_run=True,
    )
    if dry_added or dry_cat["sites"]:
        failures.append("dry run invented a site without MW")

    campus_checks += 1
    seed_row = {"id": 1001, "name": "Meta Altoona", "stateCode": "IA"}
    dry_cat2 = {
        "version": 1,
        "updated": "2026-06-01T00:00:00Z",
        "asOfLabel": "June 2026",
        "sites": [dict(seed_row)],
        "aiCampuses": [{"id": 2001, "name": "CoreWeave Austin GPU Hub", "stateCode": "TX"}],
    }
    dry_added2 = append_sourced_campuses(
        dry_cat2,
        [announcement()],
        geocode=lambda *_a, **_k: (41.1398, -104.8203),
        now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        dry_run=True,
    )
    if dry_cat2["sites"] != [seed_row] or dry_cat2["asOfLabel"] != "June 2026":
        failures.append("dry run mutated facilities.json in memory")
    if not dry_added2 or dry_added2[0].get("id") != 3001:
        failures.append(f"dry run should propose id 3001, got {dry_added2!r}")

    campus_checks += 1
    write_cat = {
        "version": 1,
        "updated": "2026-06-01T00:00:00Z",
        "asOfLabel": "June 2026",
        "sites": [dict(seed_row)],
        "aiCampuses": [{"id": 2001, "name": "CoreWeave Austin GPU Hub", "stateCode": "TX"}],
    }
    written = append_sourced_campuses(
        write_cat,
        [announcement()],
        geocode=lambda *_a, **_k: (41.1398, -104.8203),
        now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        dry_run=False,
    )
    if write_cat["sites"][0] != seed_row:
        failures.append("rewrote an existing seed row")
    if (
        not written
        or written[0]["id"] != 3001
        or write_cat["sites"][-1]["id"] != 3001
        or write_cat["asOfLabel"] != "Aug 29, 2026"
        or write_cat["updated"] != "2026-08-29T00:00:00Z"
    ):
        failures.append(
            f"write path id/asOf mismatch: {written!r} {write_cat.get('asOfLabel')!r}"
        )
    leaked_write = [k for k in CATALOG_NEVER_WRITE if k in write_cat["sites"][-1]]
    if leaked_write:
        failures.append(f"honesty leak on write: {leaked_write}")

    campus_checks += 1
    four = [
        announcement(),
        announcement(
            title="Microsoft announces 400 MW data center campus in Cheyenne, Wyoming",
            builders=["Microsoft"],
            sourceURL="https://example.com/msft-cheyenne",
        ),
        announcement(
            title="Google announces 300 MW data center campus in Cheyenne, Wyoming",
            builders=["Google"],
            sourceURL="https://example.com/goog-cheyenne",
        ),
        announcement(
            title="Amazon announces 250 MW data center campus in Cheyenne, Wyoming",
            builders=["Amazon"],
            sourceURL="https://example.com/amzn-cheyenne",
        ),
    ]
    capped = append_sourced_campuses(
        {"sites": [], "aiCampuses": []},
        four,
        geocode=lambda *_a, **_k: (41.1398, -104.8203),
        dry_run=True,
    )
    if len(capped) != MAX_NEW_SITES_PER_RUN:
        failures.append(f"max new sites: got {len(capped)}, want {MAX_NEW_SITES_PER_RUN}")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} check(s) failed.", file=sys.stderr)
        return 1
    print(f"selftest passed ({len(cases) + len(kinds) + campus_checks} checks).")
    return 0


def main(dry_run=False):
    with open(FEED_PATH) as f:
        feed = json.load(f)
    existing = feed.get("events", [])
    seen_ids = {e["id"] for e in existing}
    seen_titles = {norm_title_key(e["title"]) for e in existing}
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_WINDOW_DAYS)
    recent = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    seen_stories = {
        story_key(e) for e in existing
        if e["date"] >= recent and story_key(e) is not None
    }

    candidates = []
    for url in SOURCES:
        try:
            for title, link, desc, date in rss_items(fetch(url)):
                if date and date < cutoff:
                    continue
                text = (title + " " + desc).lower()
                if not any(k in text for k in REQUIRED_ANY):
                    continue
                if any(k in text for k in EXCLUDE_ANY):
                    continue
                event = heuristic_event(title, link, desc, date)
                # US-only feed: drop foreign-location stories unless a US
                # state was positively identified.
                if event["stateCode"] is None and NON_US_RE.search(text):
                    continue
                tkey = norm_title_key(event["title"])
                skey = story_key(event)
                if event["id"] in seen_ids or tkey in seen_titles:
                    continue
                if skey is not None and skey in seen_stories:
                    continue
                seen_ids.add(event["id"])
                seen_titles.add(tkey)
                if skey is not None:
                    seen_stories.add(skey)
                candidates.append(event)
        except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
            print(f"source failed ({url}): {e}", file=sys.stderr)

    # Prefer items with concrete signals (MW figure, known builder, state).
    candidates.sort(
        key=lambda e: (
            (e["mw"] is not None) * 2 + bool(e["builders"]) + bool(e["stateCode"]),
            e["date"],
        ),
        reverse=True,
    )
    new_events = llm_polish(candidates[:MAX_NEW_PER_RUN])

    new_sites = []
    catalog = None
    if new_events:
        try:
            with open(FACILITIES_PATH) as f:
                catalog = json.load(f)
        except OSError as e:
            print(f"facilities.json unread ({e}); skip campus add", file=sys.stderr)
        else:
            new_sites = append_sourced_campuses(
                catalog, new_events, dry_run=dry_run,
            )

    if not new_events:
        print("No new events.")
        return

    if dry_run:
        print(
            f"Dry run: would add {len(new_events)} event(s); "
            f"{len(new_sites)} campus(es)."
        )
        for site in new_sites:
            print(
                f"  campus {site['id']}: {site['name']} — {site['city']}, "
                f"{site['stateCode']} {site['totalCapacityMw']} MW "
                f"({site['latitude']}, {site['longitude']})"
            )
        return

    merged = new_events + existing
    merged.sort(key=lambda e: e["date"], reverse=True)
    feed["events"] = merged[:MAX_TOTAL_EVENTS]
    feed["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(FEED_PATH, "w") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Added {len(new_events)} event(s); total {len(feed['events'])}.")

    if new_sites and catalog is not None:
        with open(FACILITIES_PATH, "w") as f:
            json.dump(catalog, f, indent=2)
            f.write("\n")
        print(
            f"Added {len(new_sites)} campus(es); "
            f"total sites {len(catalog.get('sites') or [])}."
        )


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main(dry_run="--dry-run" in sys.argv)
