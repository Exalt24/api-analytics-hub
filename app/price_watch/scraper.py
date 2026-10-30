"""Read one competitor product page in a real browser and pull out what the report needs.

Two layers, kept apart on purpose. `PageScraper` drives Playwright (async Chromium) and runs one script in the page that
collects raw SIGNALS: the JSON-LD blocks, meta tags, the text of every element that looks like a price, the availability,
the SKU and the shipping lines. `extract_product` is pure Python over those signals, so every rule below is testable
without a browser, and the browser tests only prove that the signals are collected from a page that builds itself with JavaScript.

WHERE EACH FIELD COMES FROM, best source first:
  price         JSON-LD offers.price, then microdata / product meta tags, then the visible price element. On a sale page the
                struck-through element is the ORIGINAL price and is never used as the current one.
  currency      JSON-LD priceCurrency or a meta tag, then a symbol or code in the visible text, then the currency the caller
                expects for that competitor (`assume_currency`). A price with no currency at all is kept but flagged, and the
                report refuses to compare it.
  availability  JSON-LD offers.availability, then microdata, then the words in an availability element.
  SKU / GTIN    JSON-LD (sku, mpn, gtin8 to gtin14), then microdata, then a "SKU: ..." label. A GTIN is never guessed from free text.
  shipping      JSON-LD shippingDetails, then an element that states it. "Free shipping" is 0. "Calculated at checkout" and a
                page that says nothing are None: the report estimates those from the tenant's rate table and labels the figure.

The scraper is polite by construction (one page at a time, a declared user agent) but it has only ever been pointed at the
invented pages in fake_site.py. Pointing it at a real shop is the operator's decision, and that shop's terms and robots.txt are theirs to read.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import parse_number, parse_price_text, to_minor

USER_AGENT = "price-watch-lab/1.0 (+https://github.com/Exalt24/api-analytics-hub)"


class BrowserUnavailable(RuntimeError):
    """No Chromium could be started: not installed, or PLAYWRIGHT_BROWSERS_PATH points somewhere without one."""


@dataclass(frozen=True)
class ScrapedProduct:
    url: str
    title: str | None
    sku: str | None
    gtin: str | None
    price_minor: int | None
    original_price_minor: int | None
    currency: str | None
    availability: str            # in_stock, out_of_stock or unknown
    shipping_minor: int | None   # None: the page did not say. 0: free.
    shipping_text: str | None
    problems: tuple[str, ...] = field(default_factory=tuple)


# Runs inside the page. It returns plain data and decides nothing.
COLLECT_SIGNALS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const first = (sel) => document.querySelector(sel);
  const attr = (sel, a) => { const e = first(sel); return e ? e.getAttribute(a) : null; };
  // text of an element with its struck-through children removed, so "$89.00 $64.50" reads as the current price only
  const textWithoutStruck = (el) => {
    const c = el.cloneNode(true);
    c.querySelectorAll('del, s, strike, .price-was, .was-price, .price--original, .compare-at').forEach(n => n.remove());
    return clean(c.textContent);
  };
  const priceSelectors = ['[itemprop=price]', '.price-now', '.sale-price', '.price--sale', 'ins', '[data-price]', '.product-price', '.price'];
  const candidates = [];
  for (const sel of priceSelectors) {
    for (const el of document.querySelectorAll(sel)) {
      candidates.push({sel, text: textWithoutStruck(el), content: el.getAttribute('content')});
    }
  }
  const struck = Array.from(document.querySelectorAll('del, s, strike, .price-was, .was-price, .price--original, .compare-at')).map(e => clean(e.textContent));
  const shipping = [];
  for (const e of document.querySelectorAll('[data-shipping], .shipping, .delivery, .shipping-info')) shipping.push(clean(e.textContent));
  const sku = first('[itemprop=sku]') || first('.sku');
  const avail = first('[itemprop=availability]') || first('.availability, .stock');
  const gtinEl = first('[itemprop^=gtin]');
  return {
    jsonld: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => s.textContent),
    title: clean((first('h1') || {}).textContent) || attr('meta[property="og:title"]', 'content') || clean(document.title),
    meta: {
      amount: attr('meta[property="product:price:amount"]', 'content') || attr('meta[itemprop=price]', 'content'),
      currency: attr('meta[property="product:price:currency"]', 'content') || attr('meta[itemprop=priceCurrency]', 'content'),
    },
    candidates, struck, shipping,
    sku: sku ? clean(sku.getAttribute('content') || sku.textContent) : null,
    gtin: gtinEl ? clean(gtinEl.getAttribute('content') || gtinEl.textContent) : null,
    availability: avail ? clean(avail.getAttribute('href') || avail.getAttribute('content') || avail.textContent) : null,
  };
}
"""

#: Waits (up to a few seconds) until the page has something that looks like a price or a product block. A page that builds
#: itself after load is why this is a browser and not a plain HTTP fetch.
PAGE_READY = r"""
() => document.querySelector('script[type="application/ld+json"], .no-price, meta[itemprop=price]') ||
      Array.from(document.querySelectorAll('[itemprop=price], .price, .price-now, .sale-price, [data-price], .product-price'))
           .some(e => /\d/.test(e.textContent || ''))
"""


# ----------------------------------------------------------------------------------------------- pure extraction

_SKU_LABEL_RE = re.compile(r"(?:sku|item code|part no\.?|mpn)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{1,40})", re.I)
_SKU_PREFIX_RE = re.compile(r"^\s*(?:sku|item code|part no\.?|mpn)\s*[:#]?\s*", re.I)
_FREE_RE =re.compile(r"\bfree\b", re.I)
_CHECKOUT_RE = re.compile(r"calculated at checkout|at checkout|enter your postcode|varies", re.I)


def _flatten_jsonld(blocks: list[str]) -> list[dict[str, Any]]:
    """Every object in every JSON-LD block, @graph and lists included. A block that is not valid JSON is skipped, not fatal."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            out.append(node)
            walk(node.get("@graph", []))

    for raw in blocks:
        try:
            walk(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return out


def _has_type(node: dict[str, Any], name: str) -> bool:
    t = node.get("@type")
    return name in t if isinstance(t, list) else t == name


def _first_offer(product: dict[str, Any]) -> dict[str, Any] | None:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict) and isinstance(offers.get("offers"), list) and offers["offers"]:  # AggregateOffer
        offers = offers["offers"][0]
    return offers if isinstance(offers, dict) else None


def _availability(raw: str | None) -> str:
    if not raw:
        return "unknown"
    t = raw.lower()
    if "outofstock" in t or "out of stock" in t or "sold out" in t or "soldout" in t or "discontinued" in t:
        return "out_of_stock"
    if "instock" in t or "in stock" in t or "limitedavailability" in t or "available" in t:
        return "in_stock"
    return "unknown"


def _shipping_from_text(texts: list[str], currency: str | None) -> tuple[int | None, str | None]:
    """First usable line wins. 'Free' is 0; 'calculated at checkout' is not a figure."""
    for text in texts:
        if not text:
            continue
        if _CHECKOUT_RE.search(text):
            return None, text
        if _FREE_RE.search(text):
            return 0, text
        amount, cur = parse_price_text(text)
        if amount is not None and (cur is None or currency is None or cur == currency):
            return to_minor(amount, currency or cur or "AUD"), text
    return None, texts[0] if texts else None


def extract_product(signals: dict[str, Any], url: str, *, assume_currency: str | None = None) -> ScrapedProduct:
    problems: list[str] = []
    nodes = _flatten_jsonld(signals.get("jsonld") or [])
    product = next((n for n in nodes if _has_type(n, "Product")), None)
    offer = _first_offer(product) if product else None

    title = (product or {}).get("name") or signals.get("title")
    sku = (product or {}).get("sku") or (product or {}).get("mpn") or signals.get("sku")
    if isinstance(sku, str):
        sku = _SKU_PREFIX_RE.sub("", sku)  # an element's text reads "SKU: LMP-210"; the value is what follows the label
    if not sku:
        m = _SKU_LABEL_RE.search(signals.get("body_text") or "")
        sku = m.group(1) if m else None
    gtin = next((str((product or {})[k]) for k in ("gtin", "gtin14", "gtin13", "gtin12", "gtin8") if (product or {}).get(k)), None) or signals.get("gtin")

    price: Decimal | None = None
    currency: str | None = None
    if offer and offer.get("price") not in (None, ""):
        price = parse_number(str(offer["price"]))
        currency = (offer.get("priceCurrency") or "").upper() or None
    if price is None and signals.get("meta", {}).get("amount"):
        price = parse_number(str(signals["meta"]["amount"]))
        currency = (signals["meta"].get("currency") or "").upper() or None
    if price is None:
        for c in signals.get("candidates") or []:
            # a machine readable attribute beats the display text, which may use a comma for the decimal point
            shown_amount, shown_cur = parse_price_text(c.get("text"))
            machine = parse_number(c["content"]) if c.get("content") else None
            amount = machine if machine is not None else shown_amount
            if amount is not None:
                price = amount
                currency = shown_cur or (signals.get("meta", {}).get("currency") or "").upper() or None
                break

    if currency is None and price is not None:
        currency = assume_currency
        if currency is not None:
            problems.append("currency not on the page, assumed %s" % currency)
    if price is None:
        problems.append("no price found")
    elif currency is None:
        problems.append("price has no currency")

    original = None
    for text in signals.get("struck") or []:
        amount, _ = parse_price_text(text)
        if amount is not None and price is not None and amount > price:
            original = amount
            break

    availability = _availability((offer or {}).get("availability") or signals.get("availability"))

    shipping_minor: int | None = None
    shipping_text: str | None = None
    details = (offer or {}).get("shippingDetails")
    if isinstance(details, list):
        details = details[0] if details else None
    if isinstance(details, dict) and isinstance(details.get("shippingRate"), dict):
        rate = details["shippingRate"]
        value = parse_number(str(rate.get("value", ""))) if rate.get("value") not in (None, "") else None
        if value is not None:
            shipping_minor = to_minor(value, (rate.get("currency") or currency or "AUD").upper())
            shipping_text = "shippingDetails %s %s" % (rate.get("value"), rate.get("currency") or "")
    if shipping_minor is None and shipping_text is None:
        lines = list(signals.get("shipping") or [])
        if not lines:  # no element marked as shipping: short lines of the page text that mention it
            lines = [ln.strip() for ln in (signals.get("body_text") or "").splitlines()
                     if re.search(r"shipping|delivery", ln, re.I) and len(ln.strip()) <= 120]
        shipping_minor, shipping_text = _shipping_from_text(lines, currency)

    return ScrapedProduct(
        url=url,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        sku=str(sku).strip() if sku else None,
        gtin=str(gtin).strip() if gtin else None,
        price_minor=to_minor(price, currency) if price is not None and currency else None,
        original_price_minor=to_minor(original, currency) if original is not None and currency else None,
        currency=currency,
        availability=availability,
        shipping_minor=shipping_minor,
        shipping_text=shipping_text,
        problems=tuple(problems),
    )


# ----------------------------------------------------------------------------------------------- the browser

class PageScraper:
    """`async with PageScraper() as s: await s.scrape(url)`. One browser, one page at a time."""

    def __init__(self, *, headless: bool = True, timeout_ms: int = 10_000, ready_timeout_ms: int = 3_000):
        self._headless = headless
        self._timeout = timeout_ms
        self._ready_timeout = ready_timeout_ms
        self._pw = None
        self._browser = None

    async def __aenter__(self) -> "PageScraper":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        except Exception as exc:  # playwright raises its own Error for a missing executable
            await self._pw.stop()
            self._pw = None
            raise BrowserUnavailable("could not start Chromium: %s" % str(exc).splitlines()[0]) from exc
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def scrape(self, url: str, *, assume_currency: str | None = None) -> ScrapedProduct:
        """One page. A page that fails to load comes back as a ScrapedProduct with a problem, not an exception, so one dead
        link does not end a scan of fifty."""
        if self._browser is None:
            raise RuntimeError("use PageScraper as an async context manager")
        context = await self._browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        page.set_default_timeout(self._timeout)
        try:
            try:
                response = await page.goto(url, wait_until="load")
            except Exception as exc:
                return _failed(url, "page did not load: %s" % str(exc).splitlines()[0])
            if response is None or response.status >= 400:
                return _failed(url, "HTTP %s" % (response.status if response else "no response"))
            try:
                await page.wait_for_function(PAGE_READY, timeout=self._ready_timeout)
            except Exception:
                pass  # nothing price-like appeared; extraction reports "no price found" with what it did get
            signals = await page.evaluate(COLLECT_SIGNALS)
            signals["body_text"] = await page.evaluate("() => (document.body.innerText || '').slice(0, 4000)")
            return extract_product(signals, url, assume_currency=assume_currency)
        finally:
            await context.close()

    async def scrape_many(self, urls: list[str], *, assume_currency: str | None = None) -> list[ScrapedProduct]:
        """Sequential on purpose: one page at a time keeps the load on a competitor's site low."""
        return [await self.scrape(u, assume_currency=assume_currency) for u in urls]


def _failed(url: str, problem: str) -> ScrapedProduct:
    return ScrapedProduct(url=url, title=None, sku=None, gtin=None, price_minor=None, original_price_minor=None, currency=None,
                          availability="unknown", shipping_minor=None, shipping_text=None, problems=(problem,))


def browser_available() -> bool:
    """True when Chromium starts here. Used by the tests to skip cleanly where no browser is installed."""

    async def probe() -> bool:
        try:
            async with PageScraper():
                return True
        except Exception:
            return False

    try:
        return asyncio.run(probe())
    except Exception:
        return False
