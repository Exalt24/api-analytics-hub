"""Price watch: reading prices and pages.

Two kinds of test. The pure ones (money parsing, `extract_product` over hand written signals) need nothing. The browser ones serve the
eight invented pages of app/price_watch/fake_site.py from a local HTTP server and read them with real Chromium through Playwright; they
skip cleanly where no Chromium is installed (CI installs one: `python -m playwright install --with-deps chromium`). No real shop is ever contacted.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.price_watch.fake_site import PAGES, FakeSite  # noqa: E402
from app.price_watch.money import format_minor, parse_number, parse_price_text, to_minor  # noqa: E402
from app.price_watch.scraper import PageScraper, browser_available, extract_product  # noqa: E402


# ---- money, no browser ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, amount, currency", [
    ("$24.95", "24.95", None),
    ("A$24.95", "24.95", "AUD"),
    ("US$1,299.00", "1299.00", "USD"),
    ("$1,299.00", "1299.00", None),
    ("1.299,00 €", "1299.00", "EUR"),
    ("39,90 €", "39.90", "EUR"),
    ("EUR 1 299,00", "1299.00", "EUR"),
    ("₱1,299.50", "1299.50", "PHP"),
    ("AUD 1'299.00", "1299.00", "AUD"),
    ("From $24.95 2 left", "24.95", None),          # a lone digit after a space is not a thousands group
    ("1,299", "1299", None),                          # one separator and three digits: thousands (pinned, see money.py)
    ("12.5", "12.5", None),
    ("CA$10.00", "10.00", "CAD"),                     # CA$ must not be read as A$
])
def test_price_text_forms(text, amount, currency):
    got_amount, got_currency = parse_price_text(text)
    assert got_amount == Decimal(amount)
    assert got_currency == currency


def test_text_with_no_number_has_no_price():
    assert parse_price_text("Price on application") == (None, None)
    assert parse_price_text(None) == (None, None)
    assert parse_number("") is None


def test_minor_units_are_exact_and_zero_decimal_currencies_are_not_scaled():
    assert to_minor("19.99", "AUD") == 1999            # float('19.99') * 100 is 1998.9999999999998
    assert to_minor(Decimal("0.285"), "AUD") == 29     # half up
    assert to_minor("4995", "JPY") == 4995
    assert format_minor(4995, "AUD") == "49.95"
    assert format_minor(5, "AUD") == "0.05"
    assert format_minor(4995, "JPY") == "4995"


# ---- extraction over hand written signals, no browser -------------------------------------------------------------------

def signals(**kw):
    base = {"jsonld": [], "title": "A thing", "meta": {}, "candidates": [], "struck": [], "shipping": [], "sku": None, "gtin": None,
            "availability": None, "body_text": ""}
    base.update(kw)
    return base


def test_a_json_ld_block_that_is_not_json_is_skipped_not_fatal():
    got = extract_product(signals(jsonld=["{not json", '{"@type":"Product","name":"X","offers":{"price":"5.00","priceCurrency":"AUD"}}']), "http://x")
    assert got.price_minor == 500 and got.currency == "AUD"


def test_an_assumed_currency_is_used_and_said():
    got = extract_product(signals(candidates=[{"sel": ".price", "text": "$10.00", "content": None}]), "http://x", assume_currency="AUD")
    assert got.currency == "AUD" and got.price_minor == 1000
    assert any("assumed AUD" in p for p in got.problems)


def test_a_price_with_no_currency_anywhere_is_kept_out_of_minor_units():
    got = extract_product(signals(candidates=[{"sel": ".price", "text": "$10.00", "content": None}]), "http://x")
    assert got.price_minor is None and "price has no currency" in got.problems


def test_struck_through_price_is_the_original_never_the_current_one():
    got = extract_product(signals(candidates=[{"sel": ".price", "text": "$64.50", "content": None}], struck=["$89.00"]), "http://x", assume_currency="AUD")
    assert got.price_minor == 6450 and got.original_price_minor == 8900


def test_shipping_words():
    free = extract_product(signals(shipping=["Free shipping on this item"], candidates=[{"sel": ".p", "text": "$5.00", "content": None}]), "http://x", assume_currency="AUD")
    assert free.shipping_minor == 0
    later = extract_product(signals(shipping=["Shipping calculated at checkout"], candidates=[{"sel": ".p", "text": "$5.00", "content": None}]), "http://x", assume_currency="AUD")
    assert later.shipping_minor is None and "checkout" in later.shipping_text
    stated = extract_product(signals(shipping=["Shipping: $7.50"], candidates=[{"sel": ".p", "text": "$5.00", "content": None}]), "http://x", assume_currency="AUD")
    assert stated.shipping_minor == 750


def test_shipping_in_another_currency_than_the_price_is_not_taken_as_theirs():
    got = extract_product(signals(shipping=["Shipping: 6,90 €"], candidates=[{"sel": ".p", "text": "$5.00", "content": None}]), "http://x", assume_currency="AUD")
    assert got.shipping_minor is None


# ---- the browser ------------------------------------------------------------------------------------------------------

needs_browser = pytest.mark.skipif(not (os.environ.get("PRICE_WATCH_REQUIRE_BROWSER") or browser_available()),
                                   reason="no Chromium for Playwright here (python -m playwright install chromium)")


@pytest.fixture(scope="module")
def scraped():
    """Every invented page, read once with real Chromium (so the whole module costs one browser start)."""
    async def go():
        with FakeSite() as site:
            async with PageScraper() as s:
                out = {slug: await s.scrape(site.url(slug), assume_currency="AUD") for slug in PAGES}
                out["broken"] = await s.scrape(site.url("broken"), assume_currency="AUD")
                out["missing"] = await s.scrape(site.url("nothing-here"), assume_currency="AUD")
                return out
    return asyncio.run(go())


@needs_browser
def test_json_ld_price_gtin_sku_availability_and_stated_shipping(scraped):
    p = scraped["jsonld-bottle"]
    assert (p.title, p.sku, p.gtin) == ("Kestrel Insulated Bottle 750ml - Stainless", "KB750-STL", "9300000001016")
    assert (p.price_minor, p.currency, p.availability) == (4100, "AUD", "in_stock")
    assert p.shipping_minor == 695 and p.problems == ()


@needs_browser
def test_a_price_a_script_puts_on_the_page_after_load_is_still_read(scraped):
    p = scraped["span-lamp"]
    assert p.price_minor == 2490 and p.sku == "LMP-210"
    assert p.shipping_minor is None and p.shipping_text is None       # the page says nothing about shipping


@needs_browser
def test_a_thousands_separator_and_shipping_in_a_plain_paragraph(scraped):
    p = scraped["thousands-battery"]
    assert p.price_minor == 124900 and p.shipping_minor == 3900


@needs_browser
def test_sale_price_against_original_and_free_shipping(scraped):
    p = scraped["sale-jumper"]
    assert (p.price_minor, p.original_price_minor) == (4950, 8900)
    assert p.shipping_minor == 0 and p.sku == "HMJ-NVY-M"


@needs_browser
def test_a_page_with_no_price_says_so_and_does_not_invent_one(scraped):
    p = scraped["no-price"]
    assert p.price_minor is None and "no price found" in p.problems


@needs_browser
def test_shipping_details_in_json_ld(scraped):
    p = scraped["jsonld-stove"]
    assert (p.price_minor, p.shipping_minor, p.gtin) == (5400, 1250, "9300000001023")


@needs_browser
def test_a_euro_page_keeps_its_own_currency_and_the_machine_value(scraped):
    p = scraped["euro-gloves"]
    assert (p.price_minor, p.currency) == (3990, "EUR")


@needs_browser
def test_out_of_stock_and_calculated_at_checkout(scraped):
    p = scraped["bottle-500"]
    assert p.availability == "out_of_stock" and p.shipping_minor is None
    assert "checkout" in p.shipping_text.lower()


@needs_browser
def test_a_dead_or_missing_page_is_a_problem_not_an_exception(scraped):
    assert scraped["broken"].problems == ("HTTP 500",)
    assert scraped["missing"].problems == ("HTTP 404",)
    assert scraped["broken"].price_minor is None
