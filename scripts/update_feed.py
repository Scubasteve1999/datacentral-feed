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

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} check(s) failed.", file=sys.stderr)
        return 1
    print(f"selftest passed ({len(cases) + len(kinds)} checks).")
    return 0


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
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
