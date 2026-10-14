Run the parity check for the TravelStaytion listing at: $ARGUMENTS

Use the **playwright mcp** browser tools for every page. Follow CLAUDE.md data rules exactly. Work through these stages and report after each one; do not skip ahead.

## Stage 1 — Fingerprint
Open the TS URL. Read title, town, bedrooms, sleeps, licence number, coordinates (evaluate `window.property` in the page if present, else read from the page). Add or update the row in `data/listings.csv`.

## Stage 2 — Stay windows
Find on the TS page the first bookable 3-night stay (W3) and 7-night stay (W7) starting from tomorrow within 30 days. Record the exact dates. If a window is not bookable on TS, that window is skipped everywhere and you say so.

## Stage 3 — Discover OTAs
Look for the same unit on: Booking.com, Airbnb, Vrbo, Expedia, Holidu, HomeToGo, Marriott Homes & Villas. Methods, in order: Google search of the exact title + town (look for the Vacation Rentals entity card), licence number in quotes, then map search at the coordinates with the same bedroom count. A match needs coordinates + licence, or coordinates + capacity + matching photos. For every OTA you check, add a row per window to `data/prices.csv` with `found = yes` or `no` and the URL if found. Absence is data.

## Stage 4 — Price the same stay
For each found OTA and each window (TS included): enter the exact dates, proceed to the last step before payment, read the **all-in total**. Fill `total`, `currency`, `los_discount` (yes/no/unknown), `source_url`, `checked_by = playwright`, `checked_at = today`. If sold out or min-stay blocks it: leave `total` blank and write why in `notes`. Never estimate. Booking.com: no more than 5 page loads per run; stop and tell me if you hit a captcha or block.

## Stage 5 — Market band
On Airbnb, map search at the coordinates, radius ~5 km, same bedrooms, entire place, W7 dates. From the first page of results record sample size, lowest total, rough 25th percentile, median, rough 75th percentile. Write one row to `data/comps.csv`. Blank beats guessed.

## Stage 6 — Report
Print a short table: OTA, found, W3 total, W7 total, and flag any OTA cheaper than TS in red terms ("undercut"). Then tell me exactly which rows you appended to which CSVs.

## Stage 7 — Build the report
Run `node build-report.js`. Then tell me to open `report/index.html`. Do not modify `viewer/index.html` or `build-report.js`.
