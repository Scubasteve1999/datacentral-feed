#!/usr/bin/env python3
"""Update pulse.json with fresh US data-center buildout news.

Stdlib only. Pulls RSS from Google News + trade press, filters for US
data-center buildout stories, classifies them into DataCentral's PulseEvent
schema, dedupes against existing entries, and rewrites pulse.json.

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
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

FEED_PATH = os.path.join(os.path.dirname(__file__), "..", "pulse.json")
MAX_NEW_PER_RUN = 6
MAX_TOTAL_EVENTS = 120
FRESH_WINDOW_DAYS = 10

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
SOURCES = [
    GNEWS.format(q="%22data%20center%22%20(megawatt%20OR%20gigawatt)"),
    GNEWS.format(q="Stargate%20OpenAI%20data%20center"),
    GNEWS.format(q="xAI%20Colossus%20data%20center"),
    GNEWS.format(q="%22AI%20data%20center%22%20grid%20power"),
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
    ("policy", ["permit", "zoning", "moratorium", "regulat", "ordinance",
                "lawsuit", "tax break", "legislat", "county approve",
                "county reject", "city council", "bill "]),
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
    return {
        "id": hashlib.sha1(link.encode()).hexdigest()[:16],
        "date": (date or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
        "kind": classify(text),
        "title": clean_title[:140],
        "detail": "",
        "builders": extract_builders(clean_title),
        "stateCode": extract_state(text),
        "mw": extract_mw(text),
        "facilityId": None,
        "sourceName": source,
        "sourceURL": link,
    }


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
        "`date`, `sourceName`, `sourceURL`, `facilityId` unchanged. Return "
        "ONLY a JSON array with the same length and order.\n\n"
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


def main():
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

    if not new_events:
        print("No new events.")
        return

    merged = new_events + existing
    merged.sort(key=lambda e: e["date"], reverse=True)
    feed["events"] = merged[:MAX_TOTAL_EVENTS]
    feed["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(FEED_PATH, "w") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Added {len(new_events)} event(s); total {len(feed['events'])}.")


if __name__ == "__main__":
    main()
