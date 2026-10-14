"""Governed Marriott identity adapter with pricing deliberately locked."""
from __future__ import annotations

import time
from datetime import date

from parity.adapters.base import NeedsBrowser
from parity.adapters.browser.browser_session import BrowserSession
from parity.adapters.browser.marriott_identity import (
    build_marriott_identity_snapshot,
    canonical_marriott_url,
    property_from_marriott_snapshot,
)
from parity.core.models import Outcome, PriceResult, PropertyRef
from parity.http_control import RequestDeferred


class MarriottAdapter(NeedsBrowser):
    site = "marriott"

    def __init__(self, session: BrowserSession):
        self._session = session

    def identify(self, url: str) -> PropertyRef:
        canonical_url = canonical_marriott_url(url)
        if self._session.offline:
            return property_from_marriott_snapshot(
                self._session.load_snapshot(self.site, "page", canonical_url)
            )

        self._session.before_navigation(self.site, "page")
        started = time.monotonic()
        page = self._session.new_page()
        recorded = False
        try:
            response = page.goto(canonical_url, wait_until="domcontentloaded", timeout=25000)
            BrowserSession.decline_cookies(page)
            page.wait_for_timeout(1800)
            title = page.title()
            body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}"):
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_challenge",
                    challenge=True,
                )
                recorded = True
                raise RequestDeferred(
                    "DEFERRED — Marriott challenge detected; persistent cooldown opened"
                )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_http_{status}",
                )
                recorded = True
                raise ValueError(f"Marriott property page returned HTTP {status}")
            try:
                headings = page.locator("h1, h2").all_text_contents()
            except Exception:
                headings = []
            try:
                json_ld_texts = page.locator(
                    'script[type="application/ld+json"]'
                ).all_text_contents()
            except Exception:
                json_ld_texts = []
            try:
                dom_image_values = page.locator("img").evaluate_all(
                    r"""els => els.flatMap(el => [
                        el.currentSrc || '', el.src || '', el.getAttribute('data-src') || '',
                        ...(el.srcset || '').split(',').map(part => part.trim().split(/\s+/)[0])
                    ]).filter(Boolean)"""
                )
            except Exception:
                dom_image_values = []
            try:
                meta_image_values = page.locator(
                    'meta[property="og:image"], meta[name="twitter:image"]'
                ).evaluate_all("els => els.map(el => el.content || '').filter(Boolean)")
            except Exception:
                meta_image_values = []
            snapshot = build_marriott_identity_snapshot(
                canonical_url=canonical_url,
                page_title=title,
                headings=headings,
                body_text=body_text,
                json_ld_texts=json_ld_texts,
                dom_image_values=dom_image_values,
                meta_image_values=meta_image_values,
            )
            ref = property_from_marriott_snapshot(snapshot)
            self._session.record_navigation(
                self.site, "page", (time.monotonic() - started) * 1000
            )
            recorded = True
            self._session.save_snapshot(self.site, "page", canonical_url, snapshot)
            return ref
        except (RequestDeferred, ValueError):
            raise
        except Exception as error:
            if not recorded:
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_error:{type(error).__name__}",
                )
            raise
        finally:
            page.close()

    def get_price(
        self,
        ref: PropertyRef,
        checkin: date,
        checkout: date,
        guests: int,
    ) -> PriceResult:
        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.ERROR,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            notes=(
                "Marriott pricing is locked until structured identity has passed "
                "governed live and offline checkpoints"
            ),
        )
