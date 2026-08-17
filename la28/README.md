# LA28 Tennis Ticket Watch

Checks Vivid Seats for LA28 Olympics tennis resale listings on sessions TEN31, TEN32,
TEN33, TEN35, TEN36, TEN37, TEN40 with 2+ tickets available together, and diffs
against the last run to find genuinely new listings.

## Why this exists / how it works

- **StubHub and SeatGeek are not usable.** StubHub sits behind DataDome and blocks
  everything, including through ScrapingBee. SeatGeek renders fine but currently has
  **zero** LA28 tennis events listed. Vivid Seats is the only workable source.
- **Vivid Seats sits behind an Imperva JS challenge** — plain HTTP fetches (curl,
  simple `requests`) get a "Challenge Validation" page, not real content.
- **Headless Chromium doesn't work in a Claude Code web sandbox** — the sandbox's
  egress proxy resets outbound connections for browser traffic even to trivial
  domains like example.com, while curl through the same proxy works fine. So a
  local Playwright/Puppeteer approach is a dead end in that environment.
- **The fix: [ScrapingBee](https://www.scrapingbee.com/)** (`render_js=true`,
  `premium_proxy=true`, `country_code=us`) — renders real JS from a US residential
  IP and returns the actual DOM. Sprout already has an account (used elsewhere for
  CC's data pipeline) — get `SCRAPINGBEE_API_KEY` from the CC `.env` / 1Password,
  do not commit it here.

## Setup

```bash
export SB_KEY=<scrapingbee api key>
python3 la28_watch.py --state state.json
```

No dependencies beyond the Python standard library.

`state.json` in this repo is the running state for the automated check — each run
reads it, diffs, and overwrites it. Commit the updated `state.json` after real runs
so history isn't lost, or point `--state` at a path outside git if you'd rather not
track it.

## Output

Prints a JSON payload to stdout:

```json
{
  "sessions_ok": ["TEN31", ...],
  "errors": {},
  "qualifying_count": 18,
  "new_count": 0,
  "new": [...],
  "price_changes": [...],
  "listings": {...}
}
```

- `new` — listings not seen in the previous `state.json` (by Vivid Seats listing ID,
  **not price** — see below). This is what should trigger a Slack alert.
- `price_changes` — same listing, price moved since last run. Informational only.

### Important: the diff key is the listing ID, not price

Vivid Seats' displayed prices drift a few percent between fetches (dynamic
pricing/fees). Keying the new-vs-seen diff on price caused every listing to be
reported "new" on every single run. Fixed by keying on Vivid Seats' own
`data-testid="VBxxxxx"` listing ID instead. If you ever rewrite the diff logic,
keep this in mind.

### Self-validation

Vivid Seats doesn't expose the session code (TEN31, TEN32, etc.) directly by URL —
the code has to be scraped from a title string next to each event link, and the
link/title pairing in the DOM is easy to get backwards (an earlier version of this
script silently paired every evening session with the wrong day). To guard against
that:

1. The code→URL map is checked against two hand-verified pairs
   (`production/6894442` = TEN31, `production/6894472` = TEN35) before anything
   else runs. If that check fails, the whole run aborts — it will not report
   prices under the wrong session.
2. Each event page declares its own session code; the scraper asserts the fetched
   page's code matches what was expected before parsing it.

## Current state (2026-08-17, 2nd run)

18 listings across the 7 target sessions. 7 are new since the initial baseline:
TEN31 x2, TEN35 x2, TEN36 x2, TEN37 x1 — all Category A/B, $7,022–$10,742/ticket.
TEN31 and TEN35 previously had zero/limited listings; now covered at the high end.
Cheapest entry point remains TEN36/TEN37 Category D at ~$594/ticket.
Full detail + run log lives in Notion: "LA28 Tennis Ticket Watch — State".

## Caveats

LA28 has publicly stated that any resale listings before their verified resale
program launches in 2027 are "speculative and unverified" — these are broker
listings, not necessarily backed by real tickets yet.

## Next steps

- Wire this into a scheduled task that runs the script and Slacks Mark only the
  `new` listings.
- Consider re-checking Ticketmaster's Discovery API and SeatGeek periodically —
  both currently have zero LA28 inventory but that will change as sales open.
