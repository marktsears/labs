# LA28 Tennis Ticket Watch

Checks StubHub and Vivid Seats for LA28 Olympics tennis resale listings on sessions
TEN31, TEN32, TEN33, TEN35, TEN36, TEN37, TEN40 with 2+ tickets available together,
dedupes likely cross-listed offers, and diffs against `state.json` — the sole
source of truth for this routine — to find genuinely new listings.

## Why this exists / how it works

- **StubHub is the primary source.** It sits behind DataDome, but that only blocks
  individual *event* pages, not search-result pages. Event titles on StubHub embed
  our `TENxx` session codes directly (e.g. "Tennis - Semifinal - TEN31 - ..."), and
  the dates for our 7 target sessions cross-validate exactly against what Vivid
  Seats independently shows.
- **Vivid Seats** sits behind an easier Imperva JS challenge and is the second
  source.
- **SeatGeek is not included.** Its search endpoint returns a genuine DataDome
  CAPTCHA most of the time, and even when it doesn't, a search for "olympics
  tennis" matches the 2004 Athens venue, not LA28 — there is no real LA28
  inventory there yet. Worth an occasional manual spot-check, not worth building
  a scraper for.
- **Headless Chromium doesn't work in a Claude Code web sandbox** — the sandbox's
  egress proxy resets outbound connections for browser traffic even to trivial
  domains, while curl/urllib through ScrapingBee works fine.
- **The fix: [ScrapingBee](https://www.scrapingbee.com/).** Vivid Seats needs only
  `premium_proxy` (`render_js=true`, `country_code=us`). StubHub's search pages
  also work with `premium_proxy`, but its individual event pages return a hard
  DataDome CAPTCHA under `premium_proxy` and need the pricier `stealth_proxy`
  instead. Sprout already has a ScrapingBee account — get `SCRAPINGBEE_API_KEY`
  from the CC `.env` / 1Password, do not commit it here.

## Setup

```bash
export SB_KEY=<scrapingbee api key>
python3 la28_watch.py --state state.json
```

No dependencies beyond the Python standard library.

## Finding event URLs

Both sources use hardcoded, stable per-session event URLs (`EVENTS` in
`la28_watch.py`) instead of re-crawling a venue/search page every run — the
mapping doesn't change over time, so paying for a crawl on every run is wasted
cost. If a venue is ever added, or an event URL starts 404ing:

- **Vivid Seats**: fetch the venue index pages (see `build_session_map`-style
  logic in git history) and look for `href="/.../production/<id>"` anchors
  followed by `(TENxx)` in the title text.
- **StubHub**: fetch `https://www.stubhub.com/secure/search?q=<query terms>`
  (e.g. "TEN31 tennis olympics") with `render_js=true&premium_proxy=true` — the
  search page itself isn't DataDome-blocked, and a single broad query reliably
  returns the *entire* tennis schedule (TEN01 through TEN41) with session codes
  embedded in each result's `title="..."` attribute, right next to its
  `href="https://www.stubhub.com/.../event/<id>/"`.

## Output

Prints a JSON payload to stdout:

```json
{
  "sessions_ok": {"vividseats": ["TEN31", ...], "stubhub": ["TEN31", ...]},
  "errors": {},
  "qualifying_count": 31,
  "new_count": 0,
  "new": [...],
  "notable_new_count": 0,
  "notable_new": [...],
  "price_changes": [...],
  "listings": {...},
  "current_best": [...],
  "category_stats": {...},
  "session_activity": {...}
}
```

- `new` — listings not seen in the previous `state.json` (by
  `source|session|listing_id`, **not price** — see below). Kept for the record
  and for computing `notable_new`, but on its own this is *not* what should
  trigger a Slack alert — see below.
- `notable_new` — the subset of `new` worth actually alerting on (see "Worth
  showing" below). This is what should trigger a Slack alert.
- `price_changes` — same listing, price moved since last run. Informational only.
- `current_best` — the cheapest live listing per (session, normalized
  category), regardless of whether it's new. This is the "if you were buying
  today" board — the input to the daily digest (see below).
- `category_stats` — running history per `session|category` key: lowest price
  ever recorded (`best_price`) and how many times a listing has been seen
  there (`times_seen`). Persisted across runs so tomorrow's run can tell a
  genuine new low from just another listing.
- `session_activity` — running history per session: `runs_total` (times we
  successfully checked it) and `runs_with_listings` (times it had any
  qualifying listing at all). A session with `runs_with_listings: 0` has
  never had inventory — its first-ever listing is always notable.
- Each listing carries `source` (`"vividseats"` or `"stubhub"`) and `url` (the
  session's event page — neither site exposes a stable per-listing deep link,
  only a per-event one).

### Worth showing: rarity + value filter

Not every new listing deserves a Slack ping. A session that's had essentially
no inventory ever (e.g. TEN40) makes its first listing newsworthy no matter
the price. A session that's regularly flooded with $4,000+ listings (e.g.
TEN32) makes another $4,000+ listing unremarkable — it's "new" by listing ID
but not actionable. `classify_notable()` decides which is which, comparing
each new listing against `category_stats`/`session_activity` **as they stood
before this run** (never against itself):

- `rare_session` — the session has never had a qualifying listing in any
  tracked run. Always notable.
- `new_category` — first time this (session, category) pair has been seen.
  No price history to compare against, so it's surfaced.
- `good_value` — priced at or within 5% of the lowest price ever recorded for
  that (session, category) — a genuine new low or close to it.
- Anything else — a new listing ID, but priced well above the established
  floor for that session/category. Recorded in `listings`/`category_stats`
  for future comparisons, but not alerted.

`update_stats()` then rolls this run's results into `category_stats` and
`session_activity` for next time. Because there's no run history yet right
after this feature ships, the first run or two will classify almost
everything as notable (no established "normal" to compare against) — it
quiets down as `state.json` accumulates history.

### Digest mode (no scraping, no cost)

```bash
python3 la28_watch.py --state state.json --digest-only
```

Skips ScrapingBee entirely and just prints `current_best` /
`session_activity` from the existing `state.json` — the board of what's
currently the best available per session/category. Meant to be run once a
day (e.g. a morning digest) independent of the hourly alert run, since it
needs no fresh fetch — the last hourly run already populated `current_best`.

### Cross-site dedup

`dedup()` groups qualifying listings by `(session, normalized category)` and
collapses any pair from *different* sources within ~2% price of each other into
one canonical entry (the cheaper one), recording the other under
`cross_listed_on` rather than dropping it. This catches a broker cross-listing
the same seats on both sites without collapsing two genuinely different offers
that happen to share a category. The first real run with both sources caught
exactly one such pair: StubHub's TEN37 Category A listing at $2,382 and Vivid
Seats' TEN37 Cat A listing at $2,390 — 0.3% apart, almost certainly the same
broker's seats on both platforms. Every other listing across both sites has
been a distinct, differently-priced offer.

### Important: the diff key is source + session + listing ID, not price

Displayed prices drift a few percent between fetches (dynamic pricing/fees).
Keying the new-vs-seen diff on price reports the same offer as "new" on every
single run. `key_of()` uses each site's own listing ID instead, prefixed with
`source` so StubHub's and Vivid Seats' numeric/VB-prefixed IDs can never
collide. `migrate_prior_key()` re-derives this format from older `state.json`
entries' own fields, so a key-format change (like adding the source prefix)
doesn't make every existing listing spuriously reappear as "new".

### Self-validation

Neither site exposes the session code (TEN31, TEN32, etc.) directly by URL — it
has to be scraped from page/title text, and that pairing is easy to get backwards
(an earlier version of the Vivid Seats scraper silently paired every evening
session with the wrong day). To guard against that, each event page's HTML is
asserted to declare its own expected session code before being parsed — a
stale/wrong URL fails loudly instead of reporting another session's prices under
the wrong code.

### Known limitation: non-deterministic partial rendering

Both sites' event pages can come back from ScrapingBee with only *some* listing
cards hydrated — not zero (which would be easy to detect and retry), just a
silently incomplete subset with no signal anything is missing. StubHub shows
this often; a manual spot-check also caught Vivid Seats dropping one real,
still-active listing in a single run. `fetch_and_parse()` mitigates this by
always fetching every event page **twice** and taking the union of listings by
`listing_id` — but two consecutive misses is still possible (confirmed live: it
happened to both a Vivid Seats and a StubHub listing in the same testing
session). If `new`/`qualifying_count` swings in a way that looks like a listing
vanished, don't trust it blindly — a quick manual re-fetch of that one event
page is cheap and will confirm one way or the other before alerting Mark to a
"removed" listing that never actually left.

If this proves to be a recurring problem, a next step would be hysteresis
across runs (only treat a listing as truly gone after it's missing from 2
consecutive runs), rather than relying solely on redundant fetches within a
single run.

## Current state (2026-08-17)

31 qualifying listings: 18 on Vivid Seats, 13 on StubHub (StubHub's first real
check). No cross-site duplicates found. `state.json` in this repo is the sole
source of truth — there is no separate dashboard; Slack alerts and this file
are it.

## Caveats

LA28 has publicly stated that any resale listings before their verified resale
program launches in 2027 are "speculative and unverified" — these are broker
listings, not necessarily backed by real tickets yet.

## Next steps

- Wire this into a scheduled task that runs the script and Slacks Mark only the
  `new` listings. `SB_KEY` needs to be set as a persistent env var in whatever
  environment actually executes the scheduled run.
- Consider re-checking Ticketmaster's Discovery API and SeatGeek periodically —
  both currently have zero LA28 inventory but that will change as sales open.
- If the partial-render issue keeps costing real "new" listings across multiple
  runs, consider cross-run hysteresis (see above) instead of just doubling
  fetches within a single run.
