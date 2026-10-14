# Runbook — one listing, start to finish (target: 10–15 min)

## 0. Pick the stay windows once per session
- W3: first date from tomorrow where a 3-night stay is bookable on TS.
- W7: first date where a 7-night stay is bookable on TS.
Write the actual check-in/check-out dates into prices.csv for every row. If a window is not bookable on TS at all, leave that window blank everywhere.

## 1. Identify (from the TS page)
Open https://www.travelstaytion.com/stay/<id>. In DevTools console type `window.property` and copy: coordinates, licence number, bedrooms, sleeps, photo count. Fill one row in listings.csv.

## 2. Discover the OTAs
Order of attack, stop when you have enough:
1. Google "<exact listing title>" + town -> look for the Google Vacation Rentals entity card; it lists OTAs carrying the unit.
2. Reverse-image search the cover photo (Google Lens).
3. Search the licence number in quotes.
4. Booking.com / Airbnb map search at the coordinates, same bedroom count.
Confirm a match on coordinates + licence or coordinates + capacity + photos. Record the URL per OTA in prices.csv. If an OTA is checked and the unit is NOT there, still add a row with `found = no` — absence is data.

## 3. Price the same stay everywhere (incl. TS)
For each OTA row and each window: enter the dates, go to the final step before payment, copy the total. Fill `total`, `currency`, `los_discount`, `source_url`, `checked_by`, `checked_at`. Booking.com: max ~5 loads per session, then pause.
Blank if sold out or min-stay blocks the window. Note why in `notes`.

## 4. Market band (comps.csv)
Airbnb map search at the coordinates, same bedrooms, same W7 dates, "entire place". Sort by price. Record: number of results on page 1, lowest, ~25th percentile, median, ~75th percentile of the totals shown. One row per listing per window. Crude is fine; blank is better than guessed.

## 5. View
Open viewer/index.html, load the three CSVs, filter by host. Export the call sheet.

## Weekly cadence
5–10 hosts with upcoming calls, 3–5 listings each. One person ≈ 2 hours; a team ≈ minutes each.
