"""The one interface every adapter implements, HTTP or browser.

The orchestrator never knows or cares which kind it's talking to. This is
the load-bearing decision in the whole design: keep it this thin.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from parity.core.models import PriceResult, PropertyRef


@runtime_checkable
class Adapter(Protocol):
    site: str  # e.g. "travelstaytion", "airbnb", "guesty"

    def identify(self, url: str) -> PropertyRef:
        """Turn a listing URL into a PropertyRef (fetches the page/API once)."""
        ...

    def get_price(
        self, ref: PropertyRef, checkin: date, checkout: date, guests: int
    ) -> PriceResult:
        """Fetch and normalize the comparable price for one stay."""
        ...


class NeedsBrowser:
    """Marker mixin: adapters that need a real rendered page inherit this,
    so the orchestrator can decide whether to launch Chrome at all this run.
    """

    needs_browser = True


class HttpOnly:
    needs_browser = False
