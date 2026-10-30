"""Reading prices off a page, and money as integers in minor units (the rule the rest of the hub follows).

`parse_price_text` copes with the ways a shop writes a number: "$1,299.00", "1.299,00 €", "EUR 1 299,00", "A$24.95". The one
rule that cannot be certain is a single separator followed by exactly three digits ("1.299", "1,299"): it is read as a
thousands separator, because a price with three decimals does not exist in the currencies this handles and a four digit price
does. That reading is pinned by a test, so a change to it is visible.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.connectors.shopify import _ZERO_DECIMAL

#: Symbols that name one currency. A bare "$" or "¥" names several, so it returns None and the caller falls back to the
#: currency the page declares (JSON-LD, a meta tag) or the one expected for that competitor.
_SYMBOLS = (("AU$", "AUD"), ("CA$", "CAD"), ("NZ$", "NZD"), ("US$", "USD"), ("A$", "AUD"), ("C$", "CAD"), ("S$", "SGD"),
            ("€", "EUR"), ("£", "GBP"), ("₱", "PHP"), ("₹", "INR"), ("₩", "KRW"))
_CODE_RE = re.compile(r"\b(AUD|USD|EUR|GBP|NZD|CAD|PHP|SGD|JPY|INR|KRW|CHF)\b", re.I)
# a space or apostrophe is a thousands separator only between groups of exactly three digits, so "24.95 2 left" reads as 24.95
_NUMBER_RE = re.compile(r"\d{1,3}(?:[   ']\d{3})+(?:[.,]\d{1,2})?|\d[\d.,]*\d|\d")


def exponent(currency: str) -> int:
    return 0 if currency.upper() in _ZERO_DECIMAL else 2


def to_minor(amount: Decimal | str | int, currency: str) -> int:
    """Exact: a Decimal scaled and rounded half up, never a float."""
    return int((Decimal(str(amount)) * (10 ** exponent(currency))).quantize(Decimal(1), rounding="ROUND_HALF_UP"))


def format_minor(minor: int, currency: str) -> str:
    """49.95 as '49.95'; a zero-decimal currency as '4995'. This is also the string Shopify's Money type takes."""
    e = exponent(currency)
    if e == 0:
        return str(minor)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 10 ** e)
    return "%s%d.%0*d" % (sign, whole, e, frac)


def parse_number(text: str) -> Decimal | None:
    """The first number in `text` as a Decimal, or None. See the module docstring for the one ambiguous case."""
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"[\s  ']", "", m.group(0))
    if "," in raw and "." in raw:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        parts = raw.split(sep)
        # 1,299,000 or 1.299.000: repeated, so thousands. 1,299: one separator and three digits after it, so thousands.
        decimal_sep = sep if len(parts) == 2 and len(parts[1]) != 3 else None
    else:
        decimal_sep = None
    if decimal_sep:
        raw = raw.replace("," if decimal_sep == "." else ".", "").replace(decimal_sep, ".")
    else:
        raw = raw.replace(",", "").replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_price_text(text: str | None) -> tuple[Decimal | None, str | None]:
    """(amount, currency) from visible price text; either may be None. 'Call for price' gives (None, None)."""
    if not text:
        return None, None
    currency = None
    for symbol, code in _SYMBOLS:
        if symbol in text:
            currency = code
            break
    if currency is None:
        m = _CODE_RE.search(text)
        currency = m.group(1).upper() if m else None
    return parse_number(text), currency
