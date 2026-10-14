"""Regression test for the shared money-parsing helper (parity/core/
textparse.py), used by Avantio and all 3 browser adapters to read prices
out of visible page text rather than a clean API response."""
from parity.core.textparse import parse_money


def test_eu_thousands_format():
    assert parse_money("Total: €3.640,00") == 3640.0


def test_us_thousands_format():
    assert parse_money("Total: €3,640.00") == 3640.0


def test_ambiguous_comma_as_thousands():
    assert parse_money("€3,640") == 3640.0


def test_ambiguous_comma_as_decimal():
    assert parse_money("€45,50") == 45.5


def test_plain_integer():
    assert parse_money("€624") == 624.0


def test_no_money_in_text():
    assert parse_money("no price here") is None


def test_amount_before_symbol():
    # Real Vrbo /it-it/ page text has no leading currency symbol at all:
    # "Il prezzo attuale è 3843 €" -- confirmed live 22 Sep.
    assert parse_money("Il prezzo attuale è 3843 €") == 3843.0


def test_amount_before_symbol_no_thousands():
    assert parse_money("549 € a notte") == 549.0
