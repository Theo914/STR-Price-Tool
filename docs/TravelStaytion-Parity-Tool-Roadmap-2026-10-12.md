# TravelStaytion Parity Tool — Current Roadmap

Created: 12 October 2026  
Last updated: 13 October 2026

## Current verified position

- Hotfix 40 is installed on Theo's Windows machine.
- Complete Windows test suite: `238 passed`.
- Hotfix 41 is built for the shared three-site stay selector; Windows install
  and live acceptance proof are pending.
- The active scope is TravelStaytion against Booking.com and Airbnb. Other
  OTAs and direct-booking websites are deferred.
- Property identity must be verified before any competitor is priced.
- Live requests are bounded, governed and recordable for offline replay.
- Live check-in dates must satisfy the 30-day lead-time guard.
- Automatic selection uses the operating date and bounded future windows.
- The comparison duration is the longer date-specific minimum stay accepted by
  TravelStaytion and the competitor.
- Dates, guests and currency must match across both quotes.
- The primary verdict compares full guest-visible totals. Captured component
  totals remain audit evidence and cannot override the guest-total verdict.
- Exact price breakdowns are reported only when captured line items reconcile
  to the displayed total.
- Hypothetical discounts are separate from observed prices and are always
  labelled non-bookable.

## Proven comparison examples

| Property | Stay | TravelStaytion | Competitor | Observed verdict |
| --- | --- | ---: | ---: | --- |
| Villa Andrews | 7 nights | EUR 1,480.58 | Booking EUR 1,542.25 | TravelStaytion cheaper by EUR 61.67 (4.17%) |
| Washington Studio | 7 nights | EUR 2,260.00 | Booking EUR 2,106.50 | Booking cheaper by EUR 153.50 (6.79%) |
| Brighton Modern 3BR | 10 nights | GBP 3,863.00 | Booking GBP 5,270.00 | TravelStaytion cheaper by GBP 1,407.00 (36.42%) |
| Tenby Bedford House | 7 nights | GBP 724.52 | Booking GBP 756.00 | TravelStaytion cheaper by GBP 31.48 (4.34%) |
| Hoxton | 3 nights | EUR 796.84 | Airbnb EUR 742.26 | Airbnb cheaper by EUR 54.58 (6.85%) |

## Phases 1–8 — Evidence foundation and Booking coverage (complete)

The completed foundation provides:

1. TravelStaytion subject parsing and candidate discovery.
2. Mandatory identity verification by exact registration identifier or
   one-to-one photo evidence.
3. Persisted verified mappings with expiry and replayable evidence.
4. Governed network budgets, recorded fixtures and offline replay.
5. Structured Airbnb pricing and exact checkout reconciliation.
6. Structured Booking pricing, availability classification and exact checkout
   reconciliation.
7. Safe-date orchestration using the current date and bounded weekly windows.
8. Aligned two-sided guest-total comparison for EUR and GBP properties.

The Booking proof set covers available and unavailable stays, refundable and
non-refundable rates, explicit VAT, city tax, cleaning fees, GBP and EUR, exact
registration identity and photo identity. Rare Booking page shapes remain
stress tests rather than blockers.

Do not repeat the completed live proof requests unless a later regression
requires a fresh capture.

## Phase 9 — Common minimum-stay selection (complete)

Implemented behaviour:

- probe the requested future check-in with a one-night stay;
- read a structured minimum-stay rejection when either site exposes it;
- increase the duration within a strict adjustment budget;
- select the longer minimum duration required across both sites;
- retain date-specific evidence because minimum stays can change by date;
- stop without producing parity when a common priced duration cannot be found.

Final proofs:

- Brighton: TravelStaytion required 10 nights for 17 May 2027; Booking accepted
  the same 10-night stay.
- Hoxton: the selected 1 February 2027 date required three nights in the
  captured TravelStaytion calendar; Airbnb accepted the same three-night stay.
- Booking proof archive SHA-256:
  `5946e40e63afb5af3fbdf381e4cc98241c23b34ad3cd05a66b76576ef840ebb8`.
- Airbnb proof archive SHA-256:
  `0bcd070da8a4b02a9b762fcce706c4b87000aa60b6c2b505ca2af5d73c73f2f4`.

## Phase 10 — Currency and exact price breakdown (complete)

Implemented behaviour:

- require one explicit three-letter currency across both quotes;
- fail closed when a returned currency differs from the requested currency;
- classify a breakdown as `exact` only when its captured components reconcile;
- otherwise label the breakdown unavailable while retaining a safe guest total
  where possible;
- show accommodation, cleaning, fees, taxes and captured discounts when exact;
- preserve the guest-visible total as the parity comparison basis.

Acceptance proofs:

- Airbnb Hoxton: exact EUR 742.26 total from EUR 762.00 accommodation and a
  EUR 19.74 long-stay discount.
- Booking Brighton: exact GBP 5,270.00 total from GBP 4,170.00 accommodation
  and GBP 1,100.00 cleaning.
- Windows suite after Phase 10: `230 passed`.
- Final proof archive SHA-256:
  `c8524d2b84b1c8d4fef84940a740597fb02fb1732ac376d6576417bc3c75d6bf`.

## Phase 11 — Hypothetical discount calculators (complete)

The calculator is deliberately separate from captured parity evidence.

Implemented behaviour:

- Airbnb exposes a discreet 10% scenario.
- Booking exposes discreet 10%, 15% and 20% scenarios.
- The percentage applies only to captured accommodation.
- Fixed cleaning fees and fixed charges remain unchanged.
- A tax is reduced only when its captured label explicitly states a percentage
  rate.
- An equivalent already-captured discount is not stacked again.
- Scenarios are disabled unless the OTA breakdown is exact and reconciled.
- Each adjusted total is compared with the aligned TravelStaytion guest total
  in the same currency.
- Every scenario is explicitly hypothetical and non-bookable.

Final proof results:

| Channel | Scenario | Adjusted guest total | Difference from TravelStaytion |
| --- | ---: | ---: | ---: |
| Airbnb Hoxton | 10% | EUR 666.06 | EUR 130.78 cheaper (16.41%) |
| Booking Brighton | 10% | GBP 4,853.00 | GBP 990.00 more expensive (25.63%) |
| Booking Brighton | 15% | GBP 4,644.50 | GBP 781.50 more expensive (20.23%) |
| Booking Brighton | 20% | GBP 4,436.00 | GBP 573.00 more expensive (14.83%) |

- Windows suite after Phase 11: `234 passed`.
- Hotfix 39 SHA-256:
  `ee7dbd6134c4b530c310246577ccf46caca222d85099e6aeb1f23ae1f11d01e7`.
- Final proof archive contains 18 intact files and passed exact JSON arithmetic
  validation. SHA-256:
  `915079509b3868317b2237965573ebff0e0b4d2d7149630c4aa8eeaaed132cde`.

## Phase 12 — Portfolio workflow for up to ten listings (complete)

### Foundation — complete

Hotfix 40 adds a separate `portfolio` workflow rather than weakening the old
batch command. Proven capabilities:

1. Accept one host with a hard limit of ten TravelStaytion listings.
2. Accept verified Airbnb and/or Booking mappings for each listing.
3. Keep persisted identity verification mandatory before pricing.
4. Use one common explicit date range when supplied for multiple channels.
5. Reuse automatic minimum-stay selection for a single-channel listing.
6. Return combined JSON and CSV plus one full quote report per comparison.
7. Preserve status, dates, nights, guests, currency, both guest totals,
   difference, percentage, verdict, breakdown status, listing links, discount
   scenarios and evidence paths.
8. Preserve explicit `PRICED`, `UNAVAILABLE`, `IDENTITY_FAILED`,
   `PRICE_FAILED` and `REVIEW_REQUIRED` outcomes.
9. Resume completed `PRICED` and `UNAVAILABLE` jobs only when both the complete
   job fingerprint and saved quote SHA-256 match.
10. Refuse to mark failures or review-required work as completed.
11. Regenerate the combined report offline without repeating completed checks.

Foundation proof:

- two existing verified listings were replayed without live requests;
- Hoxton/Airbnb and Brighton/Booking both returned `PRICED`;
- combined JSON and CSV retained the exact guest totals and discount scenarios;
- the second run resumed both jobs without network or fixture replay;
- all 23 proof files passed compressed-data validation;
- both state records matched the exact SHA-256 of their saved quote reports;
- Windows suite: `238 passed`;
- Hotfix 40 SHA-256:
  `928e11aee6bcbaa5ba05a846eea0503e268348ff35c3bd87cebb973e3318f3e5`;
- final proof archive SHA-256:
  `02f300efbde7d008b9a698625e244bf7cbbe68ab0e653ab4170119244c286367`.

### Shared automatic stay selector — complete

Hotfix 41 implements one bounded automatic date and minimum-stay decision
shared by TravelStaytion, Airbnb and Booking when the same listing has both OTA
mappings. It raises the duration to the longest exact minimum exposed by any
site, then re-prices every site for the expanded stay. Generic unavailability
moves the whole group to the next weekly window; uncertain errors stop the
comparison. The same dates, guests and currency are mandatory.

Local regression suite after this change: `240 passed` (232 standard and eight
image-matching tests). Hotfix 41 SHA-256:
`c3e965551a4c0ba2f87926dceb2ad154b4c5343e90461d0221d1a4239f06e8bf`.

The first live three-site run correctly stopped when Airbnb exposed a rounded
AUD 1,585 headline beside an exact AUD 1,584.17 checkout total. The diagnostic
proved the exact `BasicPriceDetail` total and itemised accommodation both equal
AUD 1,584.17. Hotfix 42 now prefers that explicitly labelled exact total while
retaining the existing two-cent reconciliation tolerance. The captured payload
parses as `PRICED` with a zero difference.

The next live run passed Airbnb and then stopped on Booking's previously
unknown `Air conditioning fee`. The captured selected rate proved that this was
an included AUD 59 mandatory fee: AUD 1,292 accommodation + AUD 249 cleaning +
AUD 59 air conditioning + AUD 48 government tax = the exact AUD 1,648 guest
total. Hotfix 43 classifies only that named fee while preserving the unknown-line
failure gate. The final governed live run and its exact offline replay return:

- TravelStaytion guest total: AUD 1,563.57;
- Airbnb guest total: AUD 1,584.17 (AUD 20.60 / 1.32% higher);
- Booking guest total: AUD 1,649.24 (AUD 85.67 / 5.48% higher).

The common stay is 15–18 February 2027 for two guests in AUD. Both OTA
identities were verified by the exact registration identifier
`STRA606076CE4Z3E`. The second live portfolio invocation resumed both completed
jobs without launching a browser or making a pricing request. A fresh explicit-
date offline portfolio replay then returned `PRICED=2` with zero network
requests and totals identical to the live capture.

Regression suite: `242 passed`. The 29-file final proof archive passed compressed
data validation; every saved quote SHA-256 matches its resume-state record.
Final proof archive SHA-256:
`c809381046dd040f735ea9e30976d71f4107defadcb90faf25fcf03b0d827b31`.

## Phase 13 — Compact single-listing interface

Once Phase 12 produces stable structured output, build the small desktop-style
interface:

- a host/listing selector for no more than ten listings;
- the selected TravelStaytion guest total at the top;
- Booking and Airbnb guest totals beneath it;
- absolute and percentage differences from TravelStaytion;
- direct links to each OTA listing;
- exact price-breakdown expansion when evidence exists;
- discreet Airbnb 10% and Booking 10/15/20 what-if controls;
- clear unavailable, failed and review-required states;
- no fabricated price component when the source did not expose it.

### ID-first compact app — implemented, Windows live proof pending

Hotfix 44 adds a `dashboard` command which turns saved portfolio and quote JSON
into one self-contained HTML file. It uses no remote scripts, fonts, images or
analytics and never requests a price.

Hotfix 45 adds the localhost-only `app` launcher and the ID-first `auto`
workflow. A bare ID such as `1069699` becomes the canonical TravelStaytion
`/stay/1069699` URL. Single mode accepts one ID and portfolio mode accepts no
more than ten. The workflow reuses fresh verified mappings, discovers Airbnb
map candidates and Booking public-search candidates, verifies only the
best-ranked candidate per OTA, and prices only `VERIFIED` mappings. A likely
but unproven candidate is displayed without pricing; a listing with only one
verified OTA remains usable. The result screen has restrained TS, Airbnb and
Booking accent colours while red/green are reserved for the price verdict,
and the Edge app window is reduced to 540 x 760. The launcher is bound to
`127.0.0.1`, uses a per-launch request token and accepts no command or path
input from the page. Local regression suite: `268 passed`; governed Windows
proof of the new Booking discovery path and full one-click live flow is still
required before Phase 13 is complete.

Hotfix 46 strengthens continuity and the operator interface after the first
ID-first trial found Airbnb but missed a Booking listing that an earlier run had
already discovered. The automatic workflow now scans the existing local
`.parity` evidence tree for prior verified mappings and discovery candidates.
A prior verified URL receives discovery priority but is deliberately treated as
a candidate, not current proof: it must pass fresh licence/photo verification
before pricing. Any likely or alternative OTA URL remains available for manual
review when identity is unproven. The compact interface adds Clear, New check,
copy-link and CSV/JSON export controls, remembers an unfinished input locally,
collapses the technical log, and uses a dark forest-green TravelStaytion accent.
Local regression suite: `273 passed`. A governed Windows run for `1069699` is
the remaining live proof. This continuity work is retained, but it is not
accepted as the primary discovery fix because a never-before-seen ID cannot
depend on historical local evidence.

Hotfix 47 replaces the single-candidate Booking path with independent,
multi-candidate discovery. For a new TravelStaytion ID it now builds up to four
bounded Booking public searches from the current title, city, country, postcode
and published registration identifier, merges and deduplicates their property
links, and sends the ranked candidates through the normal identity gate. The
gate continues until one candidate is `VERIFIED` for that OTA, then stops to
control requests. A high-ranked but unproven result can no longer block a
lower-ranked proven result. Historical evidence remains available as a small
cache signal but no longer overwhelms a strong fresh title match. All probable,
insufficient, rejected and unchecked candidates remain unpriced and are kept as
manual-review links with an explicit identity status.

Fresh public TravelStaytion subjects used to stress the current identity inputs
were `819062`, `820056`, `845891`, `240068`, `323069`, `718977` and `993361`,
covering France, Morocco, Greece and the United Kingdom; both licence-bearing
and photo-only properties were present. A direct public Booking search for the
new subject `820056` returned `Villa Amelya - Amelkis Golf` as a real candidate,
while another title query degraded to a broad country result. That contrast is
why several bounded searches and multi-candidate verification are required.
These observations are discovery diagnostics, not governed parity proof.

Local regression suite after Hotfix 47: `279 passed`. The runtime used for this
build could not install a local Chromium binary, so Booking/Airbnb live pricing,
date selection, minimum stays, currencies and fee/tax totals were not re-run
here. Their existing governed proof remains intact, and the full local suite
continues to cover offline replay, portfolio resume, common-stay selection,
exact totals, breakdown reconciliation, discounts and compact UI states. A
fresh governed Windows run remains required before claiming live Hotfix 47
acceptance.

Hotfix 47 archive SHA-256:
`aa4140b4eb16d968d0cca507882b296f0da7fb7818b18b6224808ec4cb892322`.

Hotfix 48 narrows the operator product to the single-listing workflow. The
localhost app now accepts exactly one TravelStaytion ID or `/stay/` URL and
rejects portfolio-mode requests at its API boundary. The launcher removes the
mode switch and multi-ID textarea, validates the input before starting, shows
the canonical TravelStaytion URL, supports Enter-to-run and Clear, and presents
four compact progress stages: TravelStaytion, Discover, Verify and Price. The
results retain New check, copy/open links, exports, exact breakdowns, discount
scenarios and every unverified manual-review candidate. Portfolio internals
remain unchanged as the proven execution layer, but no portfolio controls are
exposed in Parity Desk.

Hotfix 48 includes all Hotfix 47 multi-candidate discovery changes. Local
regression suite: `281 passed`. A governed Windows run for `1069699` remains
the live acceptance gate; local regression success is not evidence that the
current Airbnb or Booking pages returned usable candidates.

Hotfix 48 archive SHA-256:
`9baa9dcd08d9e426d3c27ad86b642bdfa2856583219610d31932a24d9aa2e23b`.

Hotfix 49 corrects one Windows-only regression-test fixture in
`test_local_app_serves_result_downloads`. On Windows, `Path.write_text`
translated the fixture's LF line endings to CRLF, so the byte-preserving
download endpoint returned different bytes from the test's LF-only expected
value. The fixture now uses `Path.write_bytes`, making the test deterministic
across platforms. No application, discovery, verification, pricing or UI
behaviour changed. Full local regression suite: `281 passed`.

Hotfix 49 archive SHA-256:
`076486109375df4e6497d1fd40be08e2370e72882f18dec090136a6441b02aa3`.

## Phase 14 — Booking and Airbnb stress coverage

Add difficult but non-blocking cases after the portfolio foundation works:

- several genuinely selectable Booking rate rows;
- no separately itemised cleaning fee;
- an OTA-funded or equivalent captured discount that must not be stacked;
- explicit percentage-tax recalculation under a hypothetical discount;
- different date-specific minimum stays across the two sites;
- unavailable dates, currency mismatch and incomplete breakdown behaviour;
- additional Airbnb and Booking page shapes.

## Phase 15 — Packaging and routine operation

- installable Windows launch flow;
- saved host portfolios;
- controlled refresh and offline replay;
- exportable CSV/JSON results;
- clear evidence retention and expiry;
- concise operator instructions and recovery steps.

Broader OTAs can be reconsidered only after the Booking/Airbnb product is
stable. Direct-booking websites are outside the current product scope.

## Finished-state rule

The tool may rank prices only when property identity, dates, guests, currency,
minimum stay and guest-total treatment are aligned. Exact component claims
require reconciled evidence. Any unknown or contradictory evidence must remain
visible and fail closed rather than being guessed.
