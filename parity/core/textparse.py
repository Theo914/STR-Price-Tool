"""Shared "find a price in visible page text" helpers, used by every
adapter that has to read a rendered page's text instead of a clean API
response (Avantio direct sites, and the 3 browser-rendered OTAs). Was
duplicated across 4 files during the initial build; centralized here so a
fix to the money-parsing logic doesn't need copying 4 times.
"""
from __future__ import annotations

import re

# Symbol-before-amount ("€3.640,00", "$1,200") is the common US/generic
# form; amount-before-symbol ("3843 €", "549 €") is how Italian-locale
# pages write it (confirmed live on Vrbo's /it-it/ pages: "Il prezzo
# attuale è 3843 €" has no leading currency symbol at all). A regex that
# only looked for symbol-then-digits silently failed to match these lines
# even though the keyword search found them -- so this matches either
# ordering, preferring symbol-before-amount when both appear in a line.
MONEY_RE = re.compile(r"[€$£]\s*([\d.,]+)|([\d][\d.,]*)\s*[€$£]")


def parse_money(text: str) -> float | None:
    """Extracts the first currency amount from a line of text, handling
    both EU (3.640,00) and US (3,640.00) thousands/decimal conventions,
    and both symbol-before-amount and amount-before-symbol ordering."""
    m = MONEY_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        # ambiguous: "3,640" (thousands) vs "3,64" (decimal) -- treat a
        # 3-digit group after the comma as thousands, matching EU sites.
        parts = raw.split(",")
        raw = raw.replace(",", "") if len(parts[-1]) == 3 else raw.replace(",", ".")
    elif raw.count(".") >= 1 and all(len(part) == 3 for part in raw.split(".")[1:]):
        # "1.234" / "12.500" / "1.234.567": EU thousands grouping, not decimals.
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None
