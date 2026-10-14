"""Pins the Marriott adapter's price math against the real graphql quote
response Theo captured live (22 Sep) via his own browser's DevTools
Network tab -- not a synthetic fixture. Confirms comparable_total() nets
out correctly from Marriott's field names: baseNightlyRate is actually
the whole-stay accommodation subtotal (not per-night, despite the name),
and cleaningFee/taxTotal/securityDeposit come from feesAndTaxDetails."""
from parity.core.models import PriceBreakdown
from parity.core.pricing import comparable_total

# Verbatim numbers from the real captured response (data.quote / data.quote.feesAndTaxDetails).
REAL_QUOTE = {
    "total": 2594.27,
    "baseNightlyRate": 2123.87,
    "feesAndTaxDetails": {
        "cleaningFee": 470.4,
        "taxTotal": 0,
        "originalTaxTotal": 0,
        "securityDeposit": 0,
        "otherFees": 0,
    },
}


def test_real_quote_comparable_matches_total_when_no_tax_or_deposit():
    fees = REAL_QUOTE["feesAndTaxDetails"]
    breakdown = PriceBreakdown(
        nightly_total=REAL_QUOTE["baseNightlyRate"],
        cleaning=fees["cleaningFee"],
        other_mandatory_fees=fees["otherFees"],
        tourist_tax=fees["taxTotal"],
        deposit=fees["securityDeposit"],
    )
    # No tax or deposit in this example, so comparable should equal the
    # site's own headline total exactly.
    assert comparable_total(breakdown) == REAL_QUOTE["total"]


def test_tax_and_deposit_are_excluded_when_present():
    breakdown = PriceBreakdown(
        nightly_total=2123.87,
        cleaning=470.4,
        other_mandatory_fees=0,
        tourist_tax=42.0,       # would be folded into "total" on the real site but must not appear in comparable
        deposit=1500.0,         # refundable deposit -- must never appear in comparable
    )
    # comparable = nightly_total + cleaning + other_mandatory_fees only.
    assert comparable_total(breakdown) == 2594.27
