# TravelStaytion price-parity tool

Checks a TravelStaytion listing's price against the same property on other
channels (Airbnb, the host's own direct site, Vrbo/Expedia, Booking.com,
Guesty, Lodgify, Hostaway), plus a like-for-like Airbnb neighbour comp.
Runs on your own machine, logged out of every site, and never books
anything -- every call is a read-only quote/availability lookup.

Full research and every recipe below came from manually verifying live
prices; see `parity-price-verification-2026-09-22.md` in the TravelStaytion
project for the underlying evidence and decisions this tool implements.

## Status (as of 29 September 2026)

Working now (plain HTTP, no browser needed):
- TravelStaytion, Airbnb, Avantio-based direct sites, Lodgify, Hostaway.

Booking.com, Vrbo and Expedia identity verification have now passed governed
live and offline checkpoints using signed Microsoft Edge on Windows. Their
pricing foundations support strict offline parsing of exact, structured
checkout snapshots. Live pricing remains explicitly locked behind separate
diagnostic flags. Guesty and Homes & Villas by Marriott remain prototypes;
Marriott now has a governed offline-first identity foundation; pricing remains
locked because its end-to-end date-selection flow has not completed reliably
in a live run.

Candidate discovery now supports Airbnb map results and Booking.com public
search results. Both produce unverified candidates; neither claims a candidate
is the same property or prices it until the separate identity gate passes.
The ID-first automatic workflow tests ranked candidates until one is proved
per OTA. It shows the best reviewable links without a price when proof is
insufficient.

## Install

```
pip install -r requirements.txt
```

On Windows, browser-rendered channels use the installed, signed Microsoft
Edge browser, with installed Google Chrome as a fallback. Patchright's bundled
Chromium is deliberately not retried on Windows when code-integrity policy has
rejected it. Each signed browser receives its own logged-out profile under
`.browser-profiles`. No Windows security setting needs to be disabled.

Before the first Booking.com, Vrbo/Expedia or Guesty run, use the harmless
launch check:

```
python tools/test_browser.py
```

It visits only `example.com`. A successful Windows result reports
`Browser channel: msedge` (or `chrome` if Edge is unavailable), loads the
Example Domain page, and closes cleanly.

## Run

```
python -m parity.cli check https://www.travelstaytion.com/stay/210890 \
  --against https://www.airbnb.co.uk/rooms/4554005 \
            https://www.airbnb.co.uk/rooms/12598753 \
  --checkin 2026-10-04 --checkout 2026-10-11 --guests 2
```

If a human has already verified a candidate, record that explicitly rather
than weakening automated thresholds:

```
python -m parity.cli check <subject-url> \
  --verified <candidate-url> \
  --verified-by "Reviewer name" \
  --verification-note "Matched gallery, location and structural attributes" \
  --checkin 2026-10-04 --checkout 2026-10-11
```

`--verified` is deliberately auditable: reviewer and justification are
mandatory. Ordinary `--against` candidates must still satisfy automated
identity verification before they are priced.

Or a batch of properties from a JSON config:

```
python -m parity.cli batch properties.json
```

where `properties.json` is a list of
`{"subject": url, "against": [url, ...], "checkin": "...", "checkout": "...", "guests": 2}`.

## Host portfolio workflow

`batch` is the older identity-and-price workflow. For the bounded operational
price check, use `portfolio`. It accepts one host and no more than ten
TravelStaytion listings, reuses only persisted verified mappings, writes one
quote report per channel, and creates combined JSON and CSV results.

An offline proof config is included as `portfolio-phase12-proof.json`:

```
python -m parity.cli portfolio portfolio-phase12-proof.json \
  --offline \
  --work-dir .parity/phase12-portfolio-proof \
  --output phase12-portfolio-results.json
```

Each listing supplies one TravelStaytion URL, currency, guests and a
`channels` array containing Booking and/or Airbnb candidate URLs plus the
database holding the verified mapping. Explicit dates are shared by every
channel under that listing. Automatic minimum-stay selection also supports a
listing mapped to both Airbnb and Booking. It starts with the TravelStaytion
minimum, raises the stay whenever either OTA exposes a longer minimum, and
re-prices all three sites after every adjustment. A comparison is emitted only
when TravelStaytion, Airbnb and Booking all price the exact same dates, guest
count and currency.

Completed `PRICED` and `UNAVAILABLE` jobs are resumed only when the full job
fingerprint and the saved quote file SHA-256 still match. Use `--force` to rerun
them. Failures remain visible as `IDENTITY_FAILED`, `PRICE_FAILED` or
`REVIEW_REQUIRED` rows and are not silently discarded.

## Candidate discovery

Generate unverified Airbnb and Booking candidates from a TravelStaytion ID:

```
python -m parity.cli discover 742313 \
  --channels airbnb booking --limit-per-site 5 --format json \
  --output discovery-742313.json
```

Airbnb discovery uses the subject's coordinates and bedroom count, starts with
a small radius and expands only when the first pass is too sparse. Booking
discovery runs up to four bounded public searches from the current
TravelStaytion title, city, country, postcode and registration identifier,
then deduplicates their property links. Both cheaply rank available structural,
title and location evidence and store candidates in
`.parity/parity.db`. Candidates remain unverified and cannot be priced until
the separate identity gate returns `VERIFIED`.

The report includes provider diagnostics even when no candidates are found,
so an honest zero-result search is distinguishable from an HTTP block, missing
embedded data, parser drift or discarded malformed cards. On Windows, use
`--output` instead of PowerShell `>` redirection so the file is always UTF-8.

Previously verified mappings are reused while fresh. Expedia, Vrbo and
Marriott candidate providers are not implemented in the ID-first product.

## Identity-only verification

Verify a candidate without requesting any prices:

```
python -m parity.cli verify https://www.travelstaytion.com/stay/742313 \
  --against https://www.airbnb.co.uk/rooms/1099791623724969319 \
  --format json --output identity-742313.json
```

This workflow identifies both pages and evaluates registration/photo evidence.
Only a `VERIFIED` result is saved as a reusable mapping. Probable, insufficient
and rejected candidates remain untrusted. Price collection is deliberately not
part of this command.

Before downloading galleries, the verifier blocks candidates whose rentable
configuration conflicts materially with the subject. Two independent conflicts
(for example, both bedrooms and guest capacity) return `REJECTED`; one returns
`PROBABLE_REVIEW_REQUIRED`. Photo galleries are internally deduplicated before
matching, and successful image hashes are cached for 30 days in
`.parity/photo-hashes.db`. Use `--hash-cache` to choose a different cache path.
When `--output` writes a JSON file, the final identity verdict is also printed
directly in the terminal.

## Price-only quote from a verified mapping

After `verify` has saved an unexpired mapping, request one fresh Airbnb quote
without re-identifying either listing, downloading galleries, searching, or
running neighbour comps:

```
python -m parity.cli quote https://www.travelstaytion.com/stay/742313 \
  --channel airbnb --db .parity/parity.db \
  --checkin 2026-10-04 --checkout 2026-10-11 --guests 2 \
  --currency EUR --format json --output quote-742313.json
```

If more than one fresh Airbnb mapping exists for the subject, select the exact
one with `--candidate-url`. The command refuses missing, expired, ambiguous or
unverified mappings before opening an HTTP client.

Airbnb quote parsing is fail-closed. It accepts only itemized checkout data
anchored below `structuredDisplayPrice`, preserves accommodation, discount,
cleaning, service fee, other mandatory fee, tax and deposit lines, and checks
that their signed sum matches the displayed total within two cents. Unknown
monetary lines, missing itemization, missing totals or arithmetic mismatches
produce `ERROR` with no comparable price. A blended headline-total fallback is
deliberately forbidden.

As of 29 September 2026, the server-rendered Hyperloop room page contains the
booking inputs but leaves all price fields null. `quote` therefore makes one
logged-out `StaysPdpSections` persisted-query POST directly, rather than
pretending the listing-page HTML is a quote. The operation hash and public
browser API key are intentionally treated as expiring integration details:
schema errors return `ERROR` and are never retried automatically.

The request is deliberately booking-scoped. It asks for the six sections used
by Airbnb checkout (`BOOK_IT_CALENDAR_SHEET`, `POLICIES_DEFAULT`,
`BOOK_IT_SIDEBAR`, `BOOK_IT_NAV`, `BOOK_IT_FLOATING_FOOTER` and
`CANCELLATION_POLICY_PICKER_MODAL`) and uses the current `includeGp...`
fragment family. A broader PDP request can return HTTP 200 while marking the
booking sections `NOT_COMPLETE`; that response is incomplete quote evidence
and must remain an `ERROR`.

The successful governed Hoxton CLI response is retained as an inert 30-day
fixture under `examples/phase5-pricing-api-fixtures`. For the exact 1–9
February 2027, two-guest, EUR request, offline replay returns EUR 2,091.27
accommodation, EUR 59.68 discount and EUR 2,031.59 reconciled total. The live
capture and its zero-network replay passed on 29 September 2026, completing the
Airbnb Phase 5 pricing proof.

When Airbnb renders a rounded whole-dollar headline but also supplies an
explicitly labelled checkout `Total` with cents inside its anchored
`BasicPriceDetail`, the exact checkout total takes precedence. Its currency
must match the request and the itemised lines must still reconcile within two
cents. Conflicting totals or genuine arithmetic differences remain errors.

Booking.com quotes use the same verified-mapping gate, but the rollout remains
offline-first. An exact structured pricing snapshot can be replayed with
`--channel booking --offline`. The parser requires the canonical property URL,
dates, guests and currency to match exactly; accepts only explicit
`available`, `unavailable` or minimum-stay evidence; requires every monetary
component to have a known category, signed amount, currency and displayed-total
inclusion flag; and reconciles included lines to the displayed total within two
cents. Tax and an explicitly refundable deposit are retained as evidence but
excluded from the comparable total. Unknown lines, ambiguous deposits, missing
itemisation and arithmetic mismatches return `ERROR` with no price.

An ordinary live Booking quote is still blocked before a browser opens. The
separate `--enable-booking-pricing-capture` flag is diagnostic only and also
requires `--record-fixtures`; it permits one governed Booking pricing
navigation and saves only bounded structured evidence, never raw HTML, scripts,
cookies, request headers or session identifiers. Until Booking's current
checkout shape is approved, visible money text is diagnostic evidence only and
cannot become a quote.

Vrbo and Expedia quotes use the same verified-mapping and offline-first safety
boundary. Use `--channel vrbo` or `--channel expedia`; the CLI canonicalises
regional or descriptive property URLs back to the exact saved mapping. Pricing
snapshots must match the channel, canonical property URL, quote URL, dates,
guests and currency exactly. Explicit accommodation, discount, cleaning,
service-fee, mandatory-fee, tax and refundable-deposit lines must reconcile to
the displayed total within two cents. Tax and refundable deposits are retained
but excluded from the comparable amount. Unknown lines or ambiguous evidence
return `ERROR` with no comparable price.

Ordinary live Vrbo and Expedia pricing is blocked before a browser opens.
`--enable-expedia-group-pricing-capture` requires `--record-fixtures` and allows
one governed diagnostic navigation. The initial capture saves only bounded,
allow-listed public price elements; it does not infer itemisation from visible
money text, and therefore cannot produce `PRICED` until a known checkout shape
is explicitly supported.

## Offline-first fixtures and request governor

Development reruns should use saved fixtures rather than repeatedly contacting
an OTA.  After a governed live checkpoint has been deliberately approved, add
`--record-fixtures` to save successful HTTP page/search/quote responses:

```
python -m parity.cli verify https://www.travelstaytion.com/stay/742313 \
  --against https://www.airbnb.com.au/rooms/1099791623724969319 \
  --record-fixtures --format json --output identity-live.json
```

Then replay the identical request with zero network access:

```
python -m parity.cli verify https://www.travelstaytion.com/stay/742313 \
  --against https://www.airbnb.com.au/rooms/1099791623724969319 \
  --offline --format json --output identity-offline.json
```

`--offline` is fail-closed. A missing, expired, unsupported or checksum-failed
fixture stops the command rather than silently reaching the internet. Fixtures
are inert response bytes plus checksummed metadata under `.parity/fixtures`;
they are parsed as data and never opened as webpages. Request/response cookies
and request headers are not stored, safe response headers are allow-listed, and
common token fields in textual bodies are redacted.

Gallery image bytes are deliberately not stored as fixtures. Successful image
fingerprints remain in `.parity/photo-hashes.db` for 30 days, so an offline
identity replay must use the same fresh hash cache. This keeps normal offline
verification network-free without retaining an unnecessary copy of an OTA
gallery.

Every CLI HTTP request also passes through a persistent governor. Its default
Airbnb per-run budgets are two search pages, three identity/listing pages, one
independently classified pricing request and forty image requests. Cache and
fixture hits use zero network budget. HTTP 429 honours
`Retry-After` when supplied and otherwise opens a six-hour persistent cooldown;
HTTP 401/403 or a recognised challenge page opens a longer circuit. A cooling
platform returns `DEFERRED`, prints the exact expiry and does not retry
automatically. Other platforms may continue independently. Telemetry and
circuits default to `.parity/parity.db` and survive PowerShell closing.

The governor currently wraps the HTTP adapters used by TravelStaytion and
Airbnb plus the Phase 6 Booking.com identity and pricing paths. Booking.com
receives one governed top-level listing navigation, one independently governed
pricing navigation and up to forty governed CDN image downloads per run. A
recognised browser challenge opens a persistent Booking cooldown and is not
saved as evidence.

Booking.com identity supports offline replay through a separate structured
browser snapshot. The tool strips all advertising, referral and session query
parameters from the supplied property URL before navigation. It stores only
the extracted public property fields needed for matching: canonical URL,
title, registration identifiers, location, structure, amenities and gallery
URLs. Raw HTML, scripts, cookies, request headers and session identifiers are
never retained. Offline verification loads that snapshot and the existing
photo-hash cache without launching Chrome.

Booking.com live pricing remains deliberately locked. The old prototype could
mistake arbitrary page text containing “price” for a checkout total and did
not reconcile itemised charges. The new offline parser and CLI path are
fail-closed, but live acceptance will not be enabled until a real checkout
shape has been captured, classified, reconciled and proved by exact replay.

### Booking.com identity checkpoint

The first governed Booking.com positive completed on 29 September 2026 for
TravelStaytion `742313` and Booking's Villa Sa Planeta page. It used one of one
permitted Booking page navigations and twenty of forty Booking image requests.
After deduplication, four independent photo pairs matched and the result was
`VERIFIED`. The exact structured page snapshot and all forty subject/candidate
photo hashes then replayed offline with zero network requests and the same
decision.

A known-different Cala Jondal villa provided the negative control. Booking
exposed four bedrooms against the subject's three, but no second independently
reliable structural conflict. The verifier therefore returned
`PROBABLE_REVIEW_REQUIRED`, skipped gallery downloads, saved no mapping and
blocked pricing. Its offline replay reproduced that outcome with zero network
requests. This boundary is intentional: one host-supplied bedroom discrepancy
may reflect stale or inconsistent room counting and is not sufficient for an
automatic `REJECTED` decision.

The final evidence archive contains two structured Booking snapshots, twenty-
one HTTP fixtures, forty photo hashes and four live/offline reports. Every
fixture byte count and SHA-256 checksum validates. The positive mapping store
contains exactly one Booking mapping; the negative store contains none. The
governor recorded two successful Booking navigations, twenty successful Booking
image requests, no challenge circuit and no retries. Phase 6 identity is signed
off.

### Booking.com pricing foundation checkpoint

The next foundation is implemented and regression-tested without contacting
Booking.com. `quote --channel booking` now loads only an unexpired verified
mapping and supports exact offline structured-snapshot replay without launching
a browser. The request identity includes the canonical listing, check-in,
check-out, guests and currency. Availability, minimum-stay and priced outcomes
are distinct, and a priced result requires complete, typed component evidence
whose signed arithmetic reconciles to the displayed total.

Live capture remains a separate explicit diagnostic mode. It is locked unless
both `--enable-booking-pricing-capture` and `--record-fixtures` are supplied,
and it has a one-navigation budget. The initial capture deliberately records
only bounded checkout diagnostics and may return `ERROR`; that is the expected
safe result whenever Booking's current structured price shape has not yet been
mapped. No arbitrary visible currency string is promoted into a total.

The first controlled diagnostic for Villa Sa Planeta, 19–25 October 2026 and
two guests, used exactly one pricing navigation and correctly returned `ERROR`.
Its valid structured snapshot retained only the generic “We Price Match”
elements because Booking's visible rates were rendered in the older room-table
markup rather than the watched `data-testid` elements. The next diagnostic
capture therefore records bounded public availability-table evidence: at most
forty rows, sixteen cells per row, limited text/class/test-id fields and limited
room-selection metadata. It still records no HTML and cannot convert those
diagnostics into a quote. A further parser change requires review of that exact
captured structure first.

The second controlled diagnostic successfully captured the live room-table
structure. Booking exposed an exact six-night accommodation amount of
EUR 2,352.16, a Booking-funded reduction of EUR 150.96 and a displayed total
of EUR 2,201.20; the signed arithmetic reconciles. The rate was non-refundable
and payable online. However, Booking labelled the total as including taxes and
charges without quantifying those components. The tool therefore still refuses
to call EUR 2,201.20 comparable. The next bounded diagnostic records allow-
listed price/tax/fee attributes and briefly reveals public tax/charge popovers
by hover. Those fields remain diagnostic only and cannot emit `PRICED`.

The final tax-disclosure diagnostic confirmed that Booking supplies no separate
tax amount for this rate: the public exclusion attribute is present but empty,
and no tax/charge popover is revealed. The parser now preserves the exact
accommodation, Booking-funded reduction, displayed total and zero arithmetic
difference while returning `INCOMPLETE_EVIDENCE` with `comparable_total` left
blank. This distinguishes a valid but non-comparable rate from an unexpected
adapter error. The exact live fixture replays offline with zero network access
and the same outcome. Villa Sa Planeta requires no further Booking requests.

The final proof archive validates the complete boundary: one verified Booking
mapping, three authorised pricing navigations, no retry/challenge/circuit, one
checksummed structured pricing fixture and a zero-network replay. Phase 6
pricing is signed off for this case as `INCOMPLETE_EVIDENCE`; it is not a
successful comparable Booking price and cannot affect parity ranking.

### Phase 7 Vrbo/Expedia identity foundation

Phase 7 begins with identity, not pricing. The previous Vrbo and Expedia
adapters returned only a page title and used a broad visible-text price
heuristic. That was insufficient identity evidence and could mistake a nightly
or partial amount for a complete quote. Both pricing paths were locked until a
strict offline pricing boundary could be built and tested.

Vrbo and Expedia now canonicalise property URLs, remove tracking parameters,
use independently governed one-page navigation budgets and save only bounded
structured identity snapshots. The retained fields are limited to title,
registration identifiers, location, structural attributes, amenities and
public property-gallery URLs. Raw HTML, scripts, cookies, request headers,
tokens and session identifiers are not persisted. A snapshot is rejected
unless it contains a real title plus either an explicit registration identifier
or at least two property-gallery images.

The verifier now permits these structured adapters to use the normal identity
gate: exact registration match first, material structural conflicts next and
one-to-one deduplicated photo hashing when registration evidence is absent.
Expedia remains a separate distribution surface from Vrbo and must be observed
independently. The Phase 7 foundation passes 126 offline tests. The first
controlled live checkpoint is TravelStaytion `319567`, Casetta Borsa by
Wonderful Italy, against Vrbo property `11180096`; historical operator evidence
records the exact Italian registration `IT082053C2UUYPDYCD` on both listings.
The live checkpoint is not part of this offline foundation and must stop after
one navigation if the listing is stale, challenged or structurally changed.

The first Vrbo checkpoint completed on 29 September 2026. The current Vrbo
page did not expose a registration identifier matching TravelStaytion's local
licence, so the verifier used the allowed gallery fallback. Eleven unique
TravelStaytion photos and five successfully decoded unique Vrbo photos produced
four one-to-one pHash matches and `VERIFIED` after one governed Vrbo page
navigation. One sixth captured value resolved to a normal `www.vrbo.com` page
rather than image bytes and failed decoding. Hotfix 17 narrows the gallery
allow-list to the real `media.vrbo.com` image host, classifies those downloads
under the bounded Expedia Group image budget and re-filters saved snapshots on
offline load. The invalid URL is therefore removed without another live page
request. The suite passes 127 tests; the next gate is Windows installation and
then replay of the existing capture with zero network requests.

The submitted Vrbo/Expedia identity proof validates both decisions. All sixteen
fixture manifests match their byte counts and SHA-256 hashes. The mapping
stores contain one verified Vrbo mapping and one verified Expedia mapping; the
shared hash cache contains eleven TravelStaytion and five Vrbo hashes. The
governors record one Vrbo and one Expedia browser navigation, no challenges,
no retries and no open circuit. Vrbo's offline report removes the invalid logo
URL and reproduces four photo matches; Expedia reproduces the exact national
registration match.

That audit also found two pre-existing evidence-boundary issues that do not
invalidate either decision. Expedia's structured snapshot held five property
photos followed by ten recommendation-card photos. Hotfix 18 now accepts only
Expedia image paths containing the exact numeric hotel id. TravelStaytion CDN
photos were classified as ordinary page requests, so their bytes were retained
and they had no explicit image budget. They are now classified as images,
bounded to forty per run and excluded from future fixture storage. Existing
hashes remain valid and no new live request is required. The complete suite
passes 129 tests.

The Phase 7 pricing foundation is now in place for both Vrbo and Expedia. It
adds deterministic quote URLs, exact request-identity validation, explicit
line classification, two-cent arithmetic reconciliation, honest unavailable
and minimum-stay outcomes, zero-network structured replay and independent
one-navigation pricing budgets. Live access remains diagnostic-only and must be
explicitly enabled with fixture recording. Arbitrary rendered money text is
never accepted as a quote. The complete offline suite passes 160 tests. The
next approved step is one controlled Vrbo pricing diagnostic capture; Expedia
will be tested independently afterwards. Marriott remains planned as a separate
channel and will not be inferred from either Expedia Group surface.

The first Vrbo pricing diagnostic completed with one governed navigation and
replayed with zero network access. Vrbo rendered “$466 for 8 nights, all fees
included” despite the exact request specifying EUR. The dollar symbol is not an
unambiguous ISO currency, and the bundled presentation does not quantify fees
or taxes. The tool correctly withheld every price field. Hotfix 20 now reports
that precise evidence failure on replay and extends the next live diagnostic to
click at most one price-details control, retaining only bounded dialog/sheet
text and explicit currency indicators. It still cannot promote visible text to
`PRICED`. The complete suite passes 163 tests; no Expedia or Marriott pricing
request has been made.

### Phase 8 Marriott identity foundation

Marriott work starts with identity. The former adapter was not safe to run: it
returned only a page title, had no browser request budget or structured offline
snapshot, and its pricing flow could fall back to the first rendered line
containing “total” or “price”. That pricing path has been removed and is now
explicitly locked.

Homes & Villas property URLs are canonicalised to their numeric property id and
stripped of slugs, tracking and query parameters. One governed Marriott listing
navigation is permitted per run. A recognised challenge opens the normal
persistent cooldown without retry. Live pages are reduced to bounded structured
identity evidence: title, registration identifiers, location, structural
attributes, property type and allow-listed Marriott gallery URLs. Raw HTML,
scripts, cookies, request headers, tokens and session identifiers are never
saved. A title plus either an explicit registration identifier or at least two
gallery images is required.

The normal identity gate now accepts this structured Marriott evidence. The
first controlled candidate is the historically confirmed Casa Tatano mapping:
TravelStaytion `210890` against Marriott property `78114516`, with exact
registration `15063044EXT0379`. Offline tests prove URL cleaning, structured
extraction, failure without evidence, registration-only identity, browser-free
snapshot replay, exact-registration verification, Marriott image governance,
one-page request limits and the locked pricing boundary. The complete suite
passes 173 tests. No Marriott request has been made by this foundation.

### Booking source-response fee extraction

Booking's dated public listing HTML contains exact structured rate objects even
when the rendered availability table says only “includes taxes and charges”.
Hotfix 25 parses the original navigation response in memory, retains only
bounded allow-listed amounts and discards the source text. This is the same
underlying response exposed by Ctrl+U, without opening `view-source:`, clicking
Reserve or entering the pre-payment checkout flow. The rendered-script parser
remains a fallback when the navigation response is unavailable.

The observed Villa Andrews flexible rate reconciles exactly from EUR 760.0909
net accommodation, EUR 76.0091 VAT, EUR 120 cleaning and EUR 28 city tax to a
EUR 984.10 pre-discount total. In the governed live observation Booking also
funded EUR 60.81, producing a EUR 923.29 guest-payable total and EUR 819.28
tax-normalised comparable. The structured rate is joined to its bounded table
row by the exact subtotal, then receives that row's stable block id and
cancellation policy; this remains reliable when Booking reorders fields around
the embedded breakdown. The discount is accepted only when its rate-row
disclosure reconciles exactly to both the structured subtotal and final total.
Booking's raw component values may be JSON
numbers or strict plain decimal strings; currency-formatted or arbitrary text
is rejected. The extractor selects a single free-cancellation rate when one is
present, removes duplicate hydration copies and refuses multiple distinct
flexible rates rather than choosing arbitrarily. Unknown non-zero charges,
excluded or conditional charges, mixed inclusion semantics and arithmetic
disagreement remain hard failures. Successful CLI quotes print every component,
its category and displayed-total inclusion, followed by displayed and comparable
totals.

Every structured rejection now records an explicit bounded reason instead of
only returning a null selected block. A network-free fallback command parses a
locally saved Ctrl+U file through the same source extractor:

```
python -m parity.cli import-booking-source \
  --source villa-andrews-source.html \
  --candidate-url https://www.booking.com/hotel/it/villa-andrews-by-wonderful-italy.en-gb.html \
  --checkin 2026-10-12 --checkout 2026-10-19 --guests 2 --currency EUR
```

The command never contacts Booking and prints every accepted fee directly in
the terminal. Against the exact bounded Villa Andrews source supplied during
development it produces EUR 760.09 accommodation, EUR 76.01 VAT, EUR 120
cleaning, EUR 28 city tax, EUR 984.10 displayed and EUR 880.09 comparable. That
source excerpt does not contain the later EUR 60.81 Booking-funded reduction;
the live route applies it only when the selected rendered row reconciles to the
same structured subtotal, producing EUR 923.29 displayed and EUR 819.28
comparable. The complete suite passes 185 tests.

## What "comparable price" means

Accommodation minus discounts plus mandatory fees (including cleaning and
service fees), excluding tourist/city tax
and any refundable deposit. Headline totals are NOT comparable across
sites -- see the research doc for why (cleaning is bundled five different
ways, Hostaway folds a refundable damage deposit into its headline, etc).
The tool always surfaces the most competitive comparable price and flags
it plainly when that price has no refundable rate.

## The playbook

`playbook.json` is the human-maintained record of per-site recipes and gotchas.
SQLite under `.parity/` separately keeps discovery candidates, expiring verified
mappings, request/circuit evidence and expiring photo hashes. Price observations
are still requested fresh for the supplied dates and occupancy.

## Tests

```
python -m pytest tests/
```

These pin parsing, normalization, offline replay, request governance, identity gating and matching behaviour
against known fixtures and exact figures
verified live during research (e.g. Hostaway's damage-deposit strip,
TravelStaytion's tourist-tax math). They don't hit real sites -- there's
no substitute for running a real `check` against a live listing before
trusting a new site.

Candidates with `probable_review_required`, `insufficient_evidence` or
`rejected` identity decisions are deliberately not priced and
cannot affect the cheapest-channel conclusion. They are reported with the
`identity_unverified` outcome until stronger evidence or a controlled manual
verification mechanism is available.
### Hotfix 43: Booking air-conditioning fee

An explicitly itemised Booking `Air conditioning fee` is now treated as a
mandatory fee when Booking states that it is included in the displayed total.
Currency, inclusion and exact total reconciliation remain compulsory; unknown
monetary labels still fail closed.

### Phase 13 compact dashboard

Launch the localhost-only ID-first interface:

```powershell
.\.venv\Scripts\python.exe -m parity.cli app
```

Enter one bare TravelStaytion ID such as `1069699`, or paste its full
`travelstaytion.com/stay/...` URL. One button runs candidate discovery,
identity verification, same-currency automatic minimum-stay pricing for every
verified OTA, and the compact result dashboard. A plausible candidate that cannot be
proved remains visible as `IDENTITY_UNVERIFIED` and is never priced. The OTA
link and other plausible links remain available for manual review with their
identity status. For a new ID, Booking discovery does not depend on old local
evidence: it runs several current identity searches and tests multiple ranked
candidates until one proves itself. Previous `.parity` evidence remains a
small cache signal for repeat checks, and every stale candidate must still pass
the current identity gate before pricing. If only one OTA verifies, that OTA
is still priced. The launcher deliberately accepts exactly one property so
discovery failures and evidence remain clear. **Clear** resets the launcher,
**New check** returns from
results, and CSV/JSON exports are available from the result screen. The
TravelStaytion accent uses dark forest green. The app listens only on
`127.0.0.1`; keep PowerShell open while using it and press Ctrl+C to stop it.

The same workflow can be run without the launcher:

```powershell
.\.venv\Scripts\python.exe -m parity.cli auto 1069699 --compact
```

Build a portable dashboard from any saved portfolio result:

```powershell
.\.venv\Scripts\python.exe -m parity.cli dashboard `
  ".\phase12-a-sepal-offline-results.json" `
  --output "parity-dashboard.html" `
  --compact
```

`--compact` opens the HTML in a 540 x 760 Microsoft Edge app window on
Windows. Use `--open` for the default browser, or omit both flags to create the
HTML without opening it. The dashboard never requests prices. It displays
saved guest totals, verified links, exact captured components and the existing
hypothetical discount scenarios.

