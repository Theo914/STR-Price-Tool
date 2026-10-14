"""Maps a registry site key to a live adapter instance.

The orchestrator calls `get_http_adapter`/`get_browser_adapter` rather than
importing adapter classes directly, so adding a new site is a one-line
change here plus one new entry in registry._HOST_RULES.
"""
from __future__ import annotations

import httpx

from parity.adapters.base import Adapter
from parity.adapters.browser.booking import BookingAdapter
from parity.adapters.browser.expedia import ExpediaAdapter
from parity.adapters.browser.guesty import GuestyAdapter
from parity.adapters.browser.marriott import MarriottAdapter
from parity.adapters.browser.vrbo_expedia import VrboExpediaAdapter
from parity.adapters.http.airbnb import AirbnbAdapter
from parity.adapters.http.avantio import AvantioAdapter
from parity.adapters.http.hostaway import HostawayAdapter
from parity.adapters.http.lodgify import LodgifyAdapter
from parity.adapters.http.travelstaytion import TravelStaytionAdapter

_HTTP_ADAPTERS = {
    "travelstaytion": TravelStaytionAdapter,
    "airbnb": AirbnbAdapter,
    "avantio": AvantioAdapter,
    "lodgify": LodgifyAdapter,
    "hostaway": HostawayAdapter,
}

_BROWSER_ADAPTERS = {
    "vrbo_expedia": VrboExpediaAdapter,
    "expedia": ExpediaAdapter,
    "booking": BookingAdapter,
    "guesty": GuestyAdapter,
    "marriott": MarriottAdapter,
}


def get_http_adapter(site: str, client: httpx.Client | None = None) -> Adapter:
    cls = _HTTP_ADAPTERS.get(site)
    if cls is None:
        raise ValueError(f"No HTTP adapter registered for site {site!r}")
    return cls(client=client)


def get_browser_adapter(site: str, session) -> Adapter:
    cls = _BROWSER_ADAPTERS.get(site)
    if cls is None:
        raise ValueError(f"No browser adapter registered for site {site!r}")
    return cls(session=session)


def known_sites() -> set[str]:
    return set(_HTTP_ADAPTERS) | set(_BROWSER_ADAPTERS)
