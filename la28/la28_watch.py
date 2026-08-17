#!/usr/bin/env python3
"""
LA28 tennis resale ticket watcher — Vivid Seats via ScrapingBee.

Why ScrapingBee: vividseats.com sits behind an Imperva JS challenge and stubhub.com
behind DataDome. Plain HTTP gets a challenge page; headless Chromium cannot reach the
network at all in the Claude Code web sandbox (the egress proxy resets its connections).
ScrapingBee renders JS from a US residential IP and returns the real DOM.

Usage:
    SB_KEY=<scrapingbee key> python3 la28_watch.py            # scrape + print JSON
    SB_KEY=... python3 la28_watch.py --state state.json       # diff against saved state

Notes:
 - Session code is read off each event page and asserted against the expected code, so a
   remapped URL fails loudly instead of reporting another session's prices.
 - Only listings whose purchasable range reaches 2 are kept (Mark needs 2+ together).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TARGETS = ["TEN31", "TEN32", "TEN33", "TEN35", "TEN36", "TEN37", "TEN40"]

# Venue pages that index the tennis sessions (center court + court 1 + court 2)
VENUE_PAGES = [
    "https://www.vividseats.com/dignity-health-tennis-center-tickets/venue/73536",
    "https://www.vividseats.com/dignity-health-tennis-center---court-1-tickets/venue/73538",
    "https://www.vividseats.com/dignity-health-tennis-center---court-2-tickets/venue/73539",
]

# Hand-verified pairs used to prove the code->URL mapping logic still holds.
GROUND_TRUTH = {"6894442": "TEN31", "6894472": "TEN35"}

LISTING_RE = re.compile(
    r'data-testid="(VB\d+)"\s+data-sectionid="([^"]*)"\s+data-rowid="([^"]*)"\s+data-price="\$?([\d,]+)"'
)
QTY_RE = re.compile(r"(\d+)\s*(?:[–—-]\s*(\d+))?\s*ticket", re.I)
CATEGORY_RE = re.compile(
    r"(CATEGORY\s+[A-Z0-9]+|CAT\s+[A-Z0-9]+|GENERAL ADMISSION|[A-Z][A-Za-z ]{0,28}?)\s*\|?\s*Row", re.I
)


def sb_fetch(target, wait=18000, retries=4):
    key = os.environ["SB_KEY"]
    params = {
        "api_key": key,
        "url": target,
        "render_js": "true",
        "country_code": "us",
        "premium_proxy": "true",
        "block_resources": "false",
        "wait": str(wait),
    }
    url = "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=240) as r:
                if r.status == 200:
                    return r.read().decode("utf-8", "replace")
                last = f"http {r.status}"
        except urllib.error.HTTPError as e:
            last = f"http {e.code}: {e.read()[:200]!r}"
        except Exception as e:  # network flake
            last = str(e)
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"fetch failed for {target}: {last}")


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " | ", s)
    return re.sub(r"(\s*\|\s*)+", " | ", s)


def build_session_map(html_pages):
    """Map TENxx -> event URL. The event link precedes its title, so for each
    /production/<id> anchor take the first (TENxx) that follows it."""
    out = {}
    for html in html_pages:
        anchors = list(re.finditer(r'href="(/[^"]*?/production/(\d+))"', html))
        for idx, m in enumerate(anchors):
            path, pid = m.group(1), m.group(2)
            end = anchors[idx + 1].start() if idx + 1 < len(anchors) else len(html)
            seg = html[m.end():end]
            code_m = re.search(r"\((TEN\d{2})\)", seg)
            if not code_m:
                continue
            date_m = re.search(r">([A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}, \d{4})<", seg)
            time_m = re.search(r">(\d{1,2}:\d{2}[AP]M)<", seg)
            venue_m = re.search(r">(Dignity Health Tennis Center[^<&]*)", seg)
            out.setdefault(code_m.group(1), {
                "production_id": pid,
                "url": "https://www.vividseats.com" + path,
                "date": date_m.group(1) if date_m else None,
                "time": time_m.group(1) if time_m else None,
                "venue": venue_m.group(1).strip() if venue_m else None,
            })
    return out


def validate_map(smap):
    problems = []
    for pid, expect in GROUND_TRUTH.items():
        got = next((c for c, r in smap.items() if r["production_id"] == pid), None)
        if got != expect:
            problems.append(f"production/{pid} mapped to {got}, expected {expect}")
    return problems


def parse_listings(html, expect_code):
    codes = sorted(set(re.findall(r"TEN\d{2}", html)))
    if codes != [expect_code]:
        raise ValueError(f"page declares {codes}, expected exactly ['{expect_code}']")

    matches = list(LISTING_RE.finditer(html))
    seen, out = set(), []
    for idx, m in enumerate(matches):
        lid, section, row, price = m.groups()
        if lid in seen:
            continue
        seen.add(lid)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(html), m.end() + 6000)
        seg = strip_tags(html[m.end():end])
        q = QTY_RE.search(seg)
        qmin = int(q.group(1)) if q else None
        qmax = int(q.group(2)) if (q and q.group(2)) else qmin
        cat = CATEGORY_RE.search(seg)
        out.append({
            "listing_id": lid,
            "session": expect_code,
            "section_id": section,
            "category": cat.group(1).strip().title() if cat else None,
            "row": row,
            "price_per_ticket": int(price.replace(",", "")),
            "qty_text": q.group(0) if q else None,
            "two_plus_together": bool(qmax and qmax >= 2),
        })
    return out


def key_of(l):
    """Identity for diffing.

    Keyed on Vivid Seats' own listing id, NOT price: their displayed price drifts by a
    few percent between fetches (dynamic pricing/fees), so a price-based key reports the
    same offer as "new" on every run.
    """
    return f"{l['session']}|{l['listing_id']}"


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="path to JSON state file for new-listing diffing")
    args = ap.parse_args()

    venue_html = [sb_fetch(u, wait=10000) for u in VENUE_PAGES]
    smap = build_session_map(venue_html)
    problems = validate_map(smap)
    if problems:
        print("ABORT: session map failed validation:", *problems, sep="\n  ", file=sys.stderr)
        return 2

    results, errors = {}, {}
    for code in TARGETS:
        rec = smap.get(code)
        if not rec:
            errors[code] = "not found on venue pages"
            continue
        try:
            html = sb_fetch(rec["url"])
            listings = parse_listings(html, code)
        except Exception as e:
            errors[code] = str(e)
            continue
        results[code] = {
            "meta": rec,
            "all": listings,
            "qualifying": [l for l in listings if l["two_plus_together"]],
        }

    current = {key_of(l): l for r in results.values() for l in r["qualifying"]}

    new_keys, price_moves = list(current), []
    if args.state and os.path.exists(args.state):
        prior = json.load(open(args.state)).get("listings", {})
        new_keys = [k for k in current if k not in prior]
        for k, l in current.items():
            was = prior.get(k, {}).get("price_per_ticket")
            if was is not None and was != l["price_per_ticket"]:
                price_moves.append({**l, "previous_price": was})

    payload = {
        "sessions_ok": sorted(results),
        "errors": errors,
        "qualifying_count": len(current),
        "new_count": len(new_keys),
        "new": [current[k] for k in new_keys],
        "price_changes": price_moves,
        "listings": current,
    }
    print(json.dumps(payload, indent=2))

    if args.state:
        json.dump(payload, open(args.state, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(run())
