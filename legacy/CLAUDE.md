# Parity tool — project rules for Claude Code

## What this is
A scaled-down, manual-assisted price parity tool for TravelStaytion. Goal: for each TS listing, capture what a guest pays for the SAME stay on every OTA it appears on (incl. TS), plus a rough market band for similar properties nearby, and produce a per-host call sheet with a recommended TS price.

No scraping, no servers, no databases. The data store is CSV files in `data/`, filled by hand from a logged-in browser. The viewer is one static HTML file in `viewer/`.

## Non-negotiable data rules
- Every price is the **all-in guest total** for the stated stay (nightly x nights + cleaning + service fee + taxes as shown at the final booking step). Never the headline nightly rate.
- One **currency per listing row set**. Note the currency; never mix.
- A price that cannot be captured (sold out, min-stay mismatch, blocked) is left **blank**. Never estimated, never copied from another window.
- Two fixed stay windows per listing: `W3` = first bookable 3-night stay within 30 days; `W7` = first bookable 7-night stay within 30 days. Record the actual dates used.
- Capture whether the OTA applied a length-of-stay/weekly discount (`los_discount` = yes/no/unknown).
- Every price row has a `source_url`, `checked_by`, `checked_at`.

## Comparison rules (viewer)
- Parity gap = TS total minus cheapest other OTA total, in % of the OTA total. Positive = TS is more expensive.
- OTA price colour: **green when higher than TS, red when lower than TS** (red = we are being undercut).
- Market gap = TS total vs comp median for same bedrooms/area/dates.
- Recommended TS price = the lower of (cheapest other OTA) and (comp median), but never below comp P25.
- Blank inputs propagate as blank; never show a number derived from a blank.

## Files
- `data/listings.csv` — one row per TS listing (identity + host).
- `data/prices.csv` — one row per listing x OTA x window.
- `data/comps.csv` — one row per listing x window: area price band.
- `viewer/index.html` — loads the three CSVs (file picker), shows parity table + per-host call sheet.
- `build-report.js` — embeds the CSVs into the viewer and writes `report/index.html` (opens with no file picking). Run after every check.
- `report/` — generated; never edit by hand.
- `RUNBOOK.md` — how a human fills the CSVs.

## Conventions
- Plain HTML/CSS/JS, no build step, no frameworks, no npm dependencies unless asked.
- Keep column names exactly as in the CSV headers; the viewer depends on them.
- When adding a feature, update RUNBOOK.md if it changes what a human has to do.
