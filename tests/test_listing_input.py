import pytest

from parity.listing_input import (
    normalise_listing_inputs,
    normalise_travelstaytion_listing,
)


def test_bare_travelstaytion_id_becomes_canonical_stay_url():
    assert normalise_travelstaytion_listing("1069699") == (
        "1069699",
        "https://www.travelstaytion.com/stay/1069699",
    )


def test_travelstaytion_url_is_canonicalised_and_tracking_is_removed():
    assert normalise_travelstaytion_listing(
        "http://travelstaytion.com/stay/1069699/?source=test"
    ) == (
        "1069699",
        "https://www.travelstaytion.com/stay/1069699",
    )


@pytest.mark.parametrize("value", ["", "0", "abc", "https://example.com/stay/1"])
def test_invalid_travelstaytion_input_fails_closed(value):
    with pytest.raises(ValueError):
        normalise_travelstaytion_listing(value)


def test_portfolio_inputs_are_deduplicated_and_limited():
    assert normalise_listing_inputs([
        "1069699",
        "https://www.travelstaytion.com/stay/1069699",
        "1069700",
    ]) == [
        ("1069699", "https://www.travelstaytion.com/stay/1069699"),
        ("1069700", "https://www.travelstaytion.com/stay/1069700"),
    ]
    with pytest.raises(ValueError, match="maximum is 10"):
        normalise_listing_inputs([str(value) for value in range(1, 12)])
