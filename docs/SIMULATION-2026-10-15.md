# Stress test — 15 October 2026

`python tools/simulate.py --sample 15 --channels airbnb --no-web-search --currencies GBP USD EUR`
on 15 listings the tool had not seen before (GR, US, FR, UK, MA). Airbnb only:
Booking.com throttles the cloud test IP. Crashes: 0.

| ID | Country | Currency | First run | After fixes | Notes |
| --- | --- | --- | --- | --- | --- |
| 1024817 | GR | GBP | PRICED +3.63% | | |
| 707942 | US | USD | PRICED +13.41% | | bare "$" currency now accepted |
| 756432 | FR | EUR | PRICE_FAILED | stops plainly | TravelStaytion calendar has no open dates |
| 1045512 | UK | GBP | PRICED −3.80% | | |
| 707941 | US | USD | PRICED +13.15% | | |
| 758836 | FR | EUR | unverified | unverified | nearby Airbnbs show other Paris licences, 0 photo matches |
| 814642 | MA | GBP | unverified | unverified | riad 20 m away: different licence, 0/48 photos match |
| 756421 | FR | USD | unverified | unverified | other licences, 0 photo matches |
| 726363 | MA | EUR | unverified | **PRICED +0.02%** | Airbnb thin page (2 of 61 photos) — headers fix |
| 758849 | FR | GBP | unverified | unverified | other licences |
| 726391 | MA | USD | PRICED −23.82% | | Airbnb nightly-only pricing; worth a manual look |
| 756427 | FR | EUR | PRICE_FAILED | stops plainly | TravelStaytion calendar has no open dates |
| 965991 | MA | GBP | PRICED −17.12% | | Airbnb nightly-only pricing; worth a manual look |
| 827409 | FR | USD | unverified | unverified | other licences, 0 photo matches |
| 1048472 | MA | EUR | unverified | unverified | capacity 2 vs 6 nearby; 0 photo matches |

Re-running the 10 unmatched listings (these 6 plus 718977, 993361, 1048474,
951550 from 14 October) after the header fix loaded full galleries for every
candidate and still found no photo or licence match, so they are most likely
not on Airbnb.

## Weaknesses found and fixed

| Area | Weakness | Fix |
| --- | --- | --- |
| Identity | Airbnb room page fetched without browser headers returned a thin shell (2 photos) | navigation headers on identify(); retry thin shell twice |
| Identity | Airbnb `PageGenerateAtToken` timestamp read as a 13-digit French licence | strip JSON timestamp fields before scanning |
| Pricing | Airbnb USD shown as bare "$" was rejected | "$" confirms a requested dollar currency when nothing contradicts it |
| Pricing | Booking "6 % Tourism fee" (Miami-Dade) unknown line | classified as tax |
| Dates | closed TravelStaytion calendar probed 8 unbookable weeks | stop at once with a plain reason |
