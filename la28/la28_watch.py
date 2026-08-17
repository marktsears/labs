#!/usr/bin/env python3
"""
LA28 tennis resale ticket watcher — Vivid Seats + StubHub, via ScrapingBee.

Sources:
 - Vivid Seats: sits behind an Imperva JS challenge. ScrapingBee's render_js +
   premium_proxy gets through fine.
 - StubHub: sits behind DataDome. Search-result pages load fine with
   premium_proxy, but individual event pages (where ticket listings live) get
   a hard DataDome CAPTCHA under premium_proxy and need ScrapingBee's more
   expensive stealth_proxy instead.
 - SeatGeek: not included. Its search endpoint gets a genuine DataDome CAPTCHA
   most of the time, and even when it doesn't, "olympics tennis" matches the
   2004 Athens venue, not LA28 — there is no real LA28 inventory there yet.
   Worth a manual spot-check occasionally, not worth building a scraper for.

Headless Chromium is not an option in the Claude Code web sandbox — the egress
proxy resets its connections for every host, while curl/urllib through
ScrapingBee works fine.

Event URLs are hardcoded below (EVENTS) rather than rediscovered by crawling a
venue/search page each run — they're stable per session and don't change, so
paying for a crawl every run is wasted cost. If a venue is added or an event
page 404s, re-derive its URL (see README) and update EVENTS.

Usage:
    SB_KEY=<scrapingbee key> python3 la28_watch.py --state state.json

Notes:
 - Each event page is asserted to declare its own expected session code before
   being parsed, so a stale/wrong URL fails loudly instead of reporting
   another session's prices under the wrong code.
 - Only listings whose purchasable range reaches 2 are kept (Mark needs 2+
   together).
 - Cross-site dedup: the same physical ticket can't appear on two sites, but a
   broker can list the same seats on both. Listings in the same (session,
   category) from different sources within ~2% price of each other are
   treated as the same offer — see dedup().
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

# Stable per-session event page URLs. Discovered once (see README "Finding
# event URLs"), don't change run to run — no need to re-crawl a venue/search
# page every time.
EVENTS = {
    "vividseats": {
        "TEN31": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---center-court-7-25-2028--sports-other-sports/production/6894442",
        "TEN32": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---court-1-7-25-2028--sports-other-sports/production/6894461",
        "TEN33": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---court-2-7-25-2028--sports-other-sports/production/6894467",
        "TEN35": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---center-court-7-25-2028--sports-other-sports/production/6894472",
        "TEN36": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---center-court-7-26-2028--sports-other-sports/production/6894474",
        "TEN37": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---center-court-7-26-2028--sports-other-sports/production/6894476",
        "TEN40": "https://www.vividseats.com/summer-games---tennis-tickets-dignity-health-tennis-center---center-court-7-28-2028--sports-other-sports/production/6894482",
    },
    "stubhub": {
        "TEN31": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-25-2028/event/160846088/",
        "TEN32": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-25-2028/event/160846821/",
        "TEN33": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-25-2028/event/160846919/",
        "TEN35": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-25-2028/event/160846523/",
        "TEN36": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-26-2028/event/160846091/",
        "TEN37": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-26-2028/event/160846520/",
        "TEN40": "https://www.stubhub.com/los-angeles-2028-summer-games-carson-tickets-7-28-2028/event/160846096/",
    },
}

VS_LISTING_RE = re.compile(
    r'data-testid="(VB\d+)"\s+data-sectionid="([^"]*)"\s+data-rowid="([^"]*)"\s+data-price="\$?([\d,]+)"'
)
VS_QTY_RE = re.compile(r"(\d+)\s*(?:[–—-]\s*(\d+))?\s*ticket", re.I)
VS_CATEGORY_RE = re.compile(
    r"(CATEGORY\s+[A-Z0-9]+|CAT\s+[A-Z0-9]+|GENERAL ADMISSION|[A-Z][A-Za-z ]{0,28}?)\s*\|?\s*Row", re.I
)

SH_LISTING_RE = re.compile(
    r'data-listing-id="(\d+)" data-feature-id="[^"]*" data-is-sold="0" data-price="\$([\d,]+)"(.*?)(?=data-listing-id="|\Z)',
    re.S,
)
SH_CATEGORY_RE = re.compile(r'data-custom-color="false">((?:Category|Field|Court|Sec)[^<]*)</h3>', re.I)
SH_QTY_RE = re.compile(r'listingCardTicketDetails__label"[^>]*>(\d+)\s*tickets?\s*together</p>', re.I)


def sb_fetch(target, wait=10000, retries=4, stealth=False):
    key = os.environ["SB_KEY"]
    params = {
        "api_key": key,
        "url": target,
        "render_js": "true",
        "country_code": "us",
        "wait": str(wait),
    }
    if stealth:
        params["stealth_proxy"] = "true"
    else:
        params["premium_proxy"] = "true"
        params["block_resources"] = "false"
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


def parse_vividseats(html, expect_code, url):
    codes = sorted(set(re.findall(r"TEN\d{2}", html)))
    if codes != [expect_code]:
        raise ValueError(f"page declares {codes}, expected exactly ['{expect_code}']")

    matches = list(VS_LISTING_RE.finditer(html))
    seen, out = set(), []
    for idx, m in enumerate(matches):
        lid, section, row, price = m.groups()
        if lid in seen:
            continue
        seen.add(lid)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(html), m.end() + 6000)
        seg = strip_tags(html[m.end():end])
        q = VS_QTY_RE.search(seg)
        qmin = int(q.group(1)) if q else None
        qmax = int(q.group(2)) if (q and q.group(2)) else qmin
        cat = VS_CATEGORY_RE.search(seg)
        out.append({
            "source": "vividseats",
            "listing_id": lid,
            "session": expect_code,
            "section_id": section,
            "category": cat.group(1).strip().title() if cat else None,
            "row": row,
            "price_per_ticket": int(price.replace(",", "")),
            "qty_text": q.group(0) if q else None,
            "two_plus_together": bool(qmax and qmax >= 2),
            "url": url,
        })
    return out


def parse_stubhub(html, expect_code, url):
    codes = set(re.findall(r"TEN\d{2}", html))
    if expect_code not in codes:
        raise ValueError(f"page doesn't declare {expect_code} (found: {sorted(codes)})")

    out = []
    for m in SH_LISTING_RE.finditer(html):
        lid, price, seg = m.group(1), m.group(2), m.group(3)
        cat_m = SH_CATEGORY_RE.search(seg)
        qty_m = SH_QTY_RE.search(seg)
        qty = int(qty_m.group(1)) if qty_m else None
        out.append({
            "source": "stubhub",
            "listing_id": lid,
            "session": expect_code,
            "section_id": None,
            "category": cat_m.group(1).strip().title() if cat_m else None,
            "row": None,
            "price_per_ticket": int(price.replace(",", "")),
            "qty_text": f"{qty} tickets together" if qty else None,
            "two_plus_together": bool(qty and qty >= 2),
            "url": url,
        })
    return out


def normalize_category(cat):
    """'CATEGORY B', 'Cat B', 'Category B' -> 'B', for cross-site comparison."""
    if not cat:
        return None
    return re.sub(r"^(CATEGORY|CAT)\s*", "", cat.upper()).strip()


def dedup(listings):
    """Collapse same (session, category) listings from different sources that
    are within ~2% price of each other — likely the same broker's seats
    cross-listed, not two independent offers. Keeps the cheaper one as
    canonical; the other is recorded under cross_listed_on rather than
    dropped.
    """
    groups = {}
    for l in listings:
        groups.setdefault((l["session"], normalize_category(l["category"])), []).append(l)

    out = []
    for group in groups.values():
        used = [False] * len(group)
        for i, a in enumerate(group):
            if used[i]:
                continue
            cluster = [a]
            used[i] = True
            for j in range(i + 1, len(group)):
                b = group[j]
                if used[j] or b["source"] == a["source"]:
                    continue
                if abs(b["price_per_ticket"] - a["price_per_ticket"]) / a["price_per_ticket"] <= 0.02:
                    cluster.append(b)
                    used[j] = True
            canonical = min(cluster, key=lambda x: x["price_per_ticket"])
            others = [c for c in cluster if c is not canonical]
            out.append({
                **canonical,
                "cross_listed_on": [
                    {"source": o["source"], "listing_id": o["listing_id"], "price_per_ticket": o["price_per_ticket"]}
                    for o in others
                ],
            })
    return out


def key_of(l):
    """Identity for diffing: source + session + the site's own listing id.

    NOT price: displayed prices drift a few percent between fetches (dynamic
    pricing/fees), so a price-based key reports the same offer as "new" on
    every run.
    """
    return f"{l['source']}|{l['session']}|{l['listing_id']}"


def migrate_prior_key(entry):
    """Older state.json versions keyed listings as 'session|listing_id' with
    no source prefix (single-source era). Re-derive today's key format from
    the entry's own fields so those don't all show up as spuriously 'new'."""
    return key_of(entry) if "source" in entry else None


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="path to JSON state file for new-listing diffing")
    args = ap.parse_args()

    all_listings, errors = [], {}
    parsers = {"vividseats": parse_vividseats, "stubhub": parse_stubhub}
    stealth = {"vividseats": False, "stubhub": True}
    sessions_ok = {"vividseats": [], "stubhub": []}

    for source, events in EVENTS.items():
        for code in TARGETS:
            url = events.get(code)
            if not url:
                errors[f"{source}:{code}"] = "no known event URL"
                continue
            try:
                html = sb_fetch(url, stealth=stealth[source])
                listings = parsers[source](html, code, url)
            except Exception as e:
                errors[f"{source}:{code}"] = str(e)
                continue
            sessions_ok[source].append(code)
            all_listings.extend(listings)

    qualifying = [l for l in all_listings if l["two_plus_together"]]
    current_list = dedup(qualifying)
    current = {key_of(l): l for l in current_list}

    new_keys, price_moves = list(current), []
    if args.state and os.path.exists(args.state):
        prior_raw = json.load(open(args.state)).get("listings", {})
        prior = {}
        for entry in prior_raw.values():
            k = migrate_prior_key(entry)
            if k:
                prior[k] = entry
        new_keys = [k for k in current if k not in prior]
        for k, l in current.items():
            was = prior.get(k, {}).get("price_per_ticket")
            if was is not None and was != l["price_per_ticket"]:
                price_moves.append({**l, "previous_price": was})

    payload = {
        "sessions_ok": sessions_ok,
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
