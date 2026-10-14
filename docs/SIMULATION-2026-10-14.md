# Live simulation and fault injection — 14 October 2026

Tool: `python tools/simulate.py --sample 14 --channels airbnb --no-web-search`
(TravelStaytion seeds plus their "similar properties", round-robin by
country). Airbnb only: Booking.com throttles the cloud test IP.

## Before / after (14 real listings: AU, FR, GR, MA, UK)

| ID | Country | Baseline | After fixes | Notes |
| --- | --- | --- | --- | --- |
| 1069699 | AU | PRICED −4.53% | PRICED | exact licence |
| 323069 | GR | PRICED +2.91% | PRICED | |
| 1050999 | UK | PRICED +0.65% | PRICED | |
| 240068 | FR | PRICED +0.97% | PRICED | |
| 845891 | GR | PRICE_FAILED | **PRICED +2.74%** | calendar-aware dates |
| 819062 | FR | PRICE_FAILED | **PRICED +0.18%** | calendar-aware dates |
| 1024819 | GR | PRICE_FAILED | **PRICED +14.48%** | 45-day target lead |
| 820056 | MA | IDENTITY_UNVERIFIED | **PRICED −9.68%** | ring search + card-photo pre-screen + soft bathrooms |
| 337856 | FR | IDENTITY_UNVERIFIED | unverified | known Booking-only listing |
| 718977 | UK | IDENTITY_UNVERIFIED | unverified | no card-photo match in nearest 25 |
| 1048474 | MA | IDENTITY_UNVERIFIED | unverified | photos checked: 0 matches |
| 951550 | GR | IDENTITY_UNVERIFIED | unverified | transient TLS timeout now retried; 0 photo matches |
| 726375 | MA | IDENTITY_UNVERIFIED | unverified | 0 photo matches |
| 993361 | FR | IDENTITY_UNVERIFIED | unverified | 0 photo matches |

Priced 4 → 8 of 14; date failures 3 → 0; crashes 0 throughout. The six
remaining are most likely not listed on Airbnb (no card or gallery photo
match among the nearest candidates); ground truth would confirm.

## Weaknesses found and fixed

| Area | Weakness | Fix |
| --- | --- | --- |
| Dates | 120-day blind weekly probes miss seasonal and short-horizon calendars | windows from TravelStaytion open check-ins; default target 45 days |
| Discovery | dense areas: Airbnb omits a listing 70 m away from a 1 km box | 0.4 → 1.5 → 4 km rings |
| Discovery | candidates ranked only by distance/title | search-card photo pre-screen (+40/photo) |
| Discovery | longitude box shrinks with latitude | 1/cos(lat) widening |
| Identity | one bathroom (or sofa-bed capacity ±2) difference skipped photos | recorded, non-blocking |
| Identity | newer Airbnb listings: 0 photos, date label as title, no coordinates | mediaItems + base64 markers, og: title, latitude/longitude |
| Identity safety | short numeric licence could match any page number | needs licence context below 10 digits |
| Identity safety | sibling units sharing building photos | same-scheme different licence → review only; keypoints need 3 photos |
| Pricing safety | TS quote without currency assumed requested; echoed stay unchecked | fail closed |
| Robustness | one TLS handshake timeout aborted discovery | one retry for idempotent GETs |
| Parsing | `€1.234` read as 1.234 (Guesty/Avantio) | EU thousands grouping |

## Fault injection (tests/test_fault_injection.py)

Real captured Airbnb and TravelStaytion responses are corrupted (currency
swapped, unknown fee, changed amounts, missing totals, truncated/HTML
bodies, rate limits, GraphQL errors, changed dates/guests, string numbers)
and every case must end without a PRICED result. Identity attacks cover
licence substrings, placeholder licences, short numeric ids, sibling units
and timestamp look-alikes.
