"""Regression test for hostname routing (parity/adapters/registry.py).
Added 22 Sep when Expedia was split out from the Vrbo adapter into its
own real channel -- pins that an expedia.* URL now routes to "expedia",
not "vrbo_expedia" (which only ever builds vrbo.com URLs and would have
silently re-priced an Expedia listing via Vrbo instead)."""
from parity.adapters.registry import match_by_host


def test_vrbo_routes_to_vrbo_expedia():
    m = match_by_host("https://www.vrbo.com/6909273ha")
    assert m.site == "vrbo_expedia"
    assert m.needs_browser


def test_expedia_com_routes_to_expedia():
    m = match_by_host("https://www.expedia.com/Massa-Lubrense-Hotels-Casa-Tatano.h31843761.Hotel-Information")
    assert m.site == "expedia"
    assert m.needs_browser


def test_expedia_it_routes_to_expedia():
    m = match_by_host("https://www.expedia.it/x.h31843761.Informazioni-Hotel")
    assert m.site == "expedia"


def test_marriott_routes_to_marriott():
    m = match_by_host("https://homes-and-villas.marriott.com/en/properties/40197969-massa-lubrense-resort-ravenna-in-massa-lubrense")
    assert m.site == "marriott"
    assert m.needs_browser


def test_booking_still_routes_to_booking():
    m = match_by_host("https://www.booking.com/hotel/it/villa-tatano-marciano.en-gb.html")
    assert m.site == "booking"
