"""Given a URL (and optionally the page HTML we already fetched), decide
which adapter should handle it.

Signatures below come straight from parity-price-verification-2026-09-22.md.
Add new ones there in prose first, then mirror them here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class SiteMatch:
    site: str            # canonical adapter key, e.g. "guesty"
    needs_browser: bool


# (regex on hostname, adapter key, needs_browser) — order matters, first match wins
_HOST_RULES: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"(^|\.)travelstaytion\.com$"), "travelstaytion", False),
    (re.compile(r"(^|\.)airbnb\.[a-z.]+$"), "airbnb", False),
    (re.compile(r"(^|\.)vrbo\.com$"), "vrbo_expedia", True),
    # Expedia is its own real distribution channel per Theo (22 Sep), priced
    # independently -- it used to be routed to the Vrbo adapter as a bare
    # alias, but VrboExpediaAdapter only ever builds vrbo.com URLs, so an
    # Expedia URL routed there would have silently been re-priced via Vrbo
    # rather than via the actual Expedia page. Now has its own adapter.
    (re.compile(r"(^|\.)expedia\.[a-z.]+$"), "expedia", True),
    (re.compile(r"(^|\.)booking\.com$"), "booking", True),
    (re.compile(r"\.guestybookings\.com$"), "guesty", True),
    (re.compile(r"(^|\.)holidayfuture\.com$"), "hostaway", False),
    # Homes & Villas by Marriott Bonvoy -- confirmed real TS distribution
    # partner (see Marriott Exhibit docs / BDDSales), added per Theo (22
    # Sep). Casa Tatano's real listing confirmed live: /en/properties/
    # 78114516 has licence 15063044EXT0379, an exact match. The adapter
    # adapter now emits governed structured identity; pricing is locked.
    (re.compile(r"(^|\.)homes-and-villas\.marriott\.com$"), "marriott", True),
]


def match_by_host(url: str) -> SiteMatch | None:
    host = urlparse(url).netloc.lower()
    for pattern, site, needs_browser in _HOST_RULES:
        if pattern.search(host):
            return SiteMatch(site, needs_browser)
    return None


def match_by_html(html: str) -> SiteMatch | None:
    """Fallback for direct sites on a custom domain, sniffed by the scripts
    they load rather than by hostname."""
    if "crs.avantio.com" in html:
        return SiteMatch("avantio", False)
    if "websites-static.lodgify.com" in html or "checkout.lodgify.com" in html:
        return SiteMatch("lodgify", False)
    if "static-production-nextjs.hostaway.com" in html or "booking-engine.hostaway.com" in html:
        return SiteMatch("hostaway", False)
    if "app.guesty.com/api/pm-websites-backend" in html:
        return SiteMatch("guesty", True)
    return None


def resolve(url: str, html: str | None = None) -> SiteMatch | None:
    m = match_by_host(url)
    if m:
        return m
    if html is not None:
        return match_by_html(html)
    return None
