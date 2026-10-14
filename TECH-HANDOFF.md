# TravelStaytion Parity Desk — technical handoff

## Handoff state

- Release: Hotfix 49
- Product surface: one TravelStaytion listing per search
- Regression suite: 281 tests passing on Python 3.12
- Primary operating system: Windows 10/11
- Browser automation: Patchright using installed Microsoft Edge, with Google
  Chrome as fallback
- Run command: `python -m parity.cli app`

This archive is the complete current development tree. It contains the
application, adapters, discovery and verification logic, pricing logic,
tests, inert fixtures, utilities, configuration examples, historic hotfix
notes and the current roadmap.

Generated or machine-bound directories are intentionally absent:

- `.venv` and Python caches — platform-specific and recreated by setup;
- `.parity` — run output, local databases and captured public evidence;
- browser profiles — local state which must not be transferred;
- test caches — generated and irrelevant.

Nothing in those excluded directories is required to reconstruct or run the
application. `SETUP-WINDOWS.ps1` creates the environment from the supplied
dependency files.

## Start from a clean Windows machine

Requirements:

- Python 3.11 or newer; Python 3.12 is the known-good version;
- Microsoft Edge or Google Chrome installed;
- internet access to TravelStaytion, Airbnb and Booking.com.

From PowerShell in this directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\SETUP-WINDOWS.ps1
powershell -ExecutionPolicy Bypass -File .\RUN-TESTS.ps1
powershell -ExecutionPolicy Bypass -File .\RUN-APP.ps1
```

The application binds only to `127.0.0.1`, selects a free port, and opens the
local launcher. Keep PowerShell open while it runs. Press `Ctrl+C` to stop it.

## Operator flow

1. The operator enters one numeric TravelStaytion ID or full `/stay/<id>` URL.
2. The tool identifies the TravelStaytion listing and extracts identity data.
3. Candidate discovery searches Airbnb and Booking.com.
4. Candidate identity is checked using registration/licence evidence,
   structural attributes and photos.
5. Only a verified candidate is priced.
6. TravelStaytion and OTA totals are compared for the same dates, guests and
   currency.
7. Unverified candidate links remain visible for manual review without prices.

The UI entry point is `parity/app.py`. It starts the `auto` CLI workflow in
`parity/cli.py`. The automatic orchestration helpers are in
`parity/automatic.py`; discovery providers are in
`parity/discovery/providers.py`; identity scoring is in
`parity/core/matching.py`; channel adapters live under `parity/adapters/`.

## Current critical defect: live discovery is incomplete

A manual reverse search of the TravelStaytion licence number found the Airbnb
and Booking listings, while the application failed to surface them. This is a
real product failure. The passing regression suite does not prove that current
live OTA search pages can still be discovered or parsed.

Current behaviour:

- Airbnb discovery is primarily a coordinate/bedroom map search.
- Booking discovery runs bounded searches inside Booking.com using title,
  city, country, postcode and sometimes the registration identifier.
- Registration/licence evidence is strong during identity verification.
- There is no first-class exact web reverse-search provider using the licence
  number to generate Airbnb and Booking candidate URLs.
- Provider diagnostics are not exposed clearly in the single-search result,
  so a block, parser drift and an honest zero-result search can look alike.

Required correction:

1. Extract and normalise every TravelStaytion registration/licence identifier.
2. Run exact-match reverse searches for each useful variant, restricted to
   canonical Airbnb room pages and Booking hotel pages.
3. Collect multiple canonical candidates from both channels.
4. Feed every candidate through the existing identity gate; a search-engine
   result is candidate evidence, never proof of identity.
5. Fall back to title/location and map discovery only after licence-first
   discovery.
6. Show the attempted queries, candidate counts, blocks, parser failures and
   verification outcomes in the diagnostic result.
7. Add controlled tests for licence query generation, URL extraction,
   deduplication, misleading search results and provider failures.
8. Pass a governed live acceptance case where manual exact licence search is
   already known to locate both OTA listings.

Do not weaken the verified-only pricing boundary to disguise discovery
failure. A candidate may be displayed for review, but it must not be priced
until identity is proven.

## What the tests establish

The 281-test suite covers, among other things:

- single-search request validation and local application endpoints;
- candidate ranking, provider error handling and offline discovery parsing;
- strict identity decisions and persisted verified mappings;
- Airbnb, Booking and Expedia-family price parsing boundaries;
- currency, fee, tax, discount and total reconciliation;
- minimum-stay and shared-date selection;
- portfolio execution, resume rules and result dashboards;
- network governance and bounded diagnostic snapshots.

It does not establish that Airbnb or Booking's current live pages still match
the parsers. Live discovery and live pricing require separate governed checks.

## Runtime output and diagnostics

The local app writes beneath `.parity/app/`:

- `latest-results.json` and `latest-results.csv`;
- `latest-dashboard.html`;
- `run/verified-mappings.db` and `run/photo-hashes.db`;
- bounded fixtures and per-channel quote evidence under `run/`.

After reproducing a failure, stop the app and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\COLLECT-DIAGNOSTICS.ps1
```

The script creates a ZIP on the current user's Desktop containing the local
app evidence. Review it before modifying discovery. The application is
designed not to retain raw HTML, scripts, cookies, request headers or session
identifiers in its bounded fixtures.

## Key files

| Area | Files |
| --- | --- |
| Local single-search UI | `parity/app.py` |
| CLI orchestration | `parity/cli.py`, `parity/automatic.py` |
| Discovery | `parity/discovery/engine.py`, `parity/discovery/providers.py` |
| Identity | `parity/core/matching.py`, `parity/storage.py` |
| Date and minimum-stay policy | `parity/core/date_policy.py` |
| Price comparison | `parity/core/pricing.py`, `parity/core/discounts.py` |
| TravelStaytion and Airbnb HTTP adapters | `parity/adapters/http/` |
| Booking and browser adapters | `parity/adapters/browser/` |
| Network controls | `parity/http_control.py`, `parity/browser_control.py` |
| Dashboard | `parity/dashboard.py` |
| Regression suite | `tests/` |
| Current roadmap | `docs/TravelStaytion-Parity-Tool-Roadmap-2026-10-12.md` |

## Acceptance standard

A successful technical fix must demonstrate all of the following on Windows:

- clean setup succeeds using the supplied scripts;
- all 281 existing tests remain green, plus new licence-discovery tests;
- an unseen TravelStaytion ID triggers fresh candidate discovery;
- exact licence reverse search surfaces the known Airbnb and Booking pages;
- those pages pass or fail the existing identity gate honestly;
- unverified pages remain visible without prices;
- verified pages are priced only for matching dates, guests and currency;
- diagnostic output distinguishes blocking, parser drift, zero results and
  identity rejection.

