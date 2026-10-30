"""A stand-in competitor shop on 127.0.0.1, with eight product pages in eight different layouts. All of it is invented.

Used by the scraper tests and by the demo (scripts/price_watch.py demo-server), so a stranger can run the whole loop without
touching a real shop. Each page exists to show one way a shop writes a price or a shipping line:

  jsonld-bottle    price, currency, GTIN and availability in JSON-LD; shipping in a marked element
  span-lamp        price put on the page by JavaScript after load; SKU as a "SKU:" label; no shipping line at all
  thousands-battery  "$1,249.00" in a plain span; shipping in an ordinary paragraph with no marker
  sale-jumper      a struck-through original price beside the sale price; "Free shipping"; SKU as microdata
  no-price         "Price on application", no price anywhere
  jsonld-stove     price and shippingDetails both in JSON-LD
  euro-gloves      microdata with a machine value 39.90 and display text "39,90 €" (a different currency from ours)
  bottle-500       same brand and name as our 750ml bottle but 500ml (the near miss), out of stock, "calculated at checkout"
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HEAD = "<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>{head}</head><body>"


def _page(title: str, body: str, head: str = "") -> str:
    return _HEAD.format(title=title, head=head) + "<nav>Home / Shop</nav><h1>%s</h1>%s<footer>Invented shop, not real.</footer></body></html>" % (title, body)


PAGES: dict[str, str] = {
    "jsonld-bottle": _page(
        "Kestrel Insulated Bottle 750ml - Stainless",
        '<p class="blurb">Keeps drinks cold for 24 hours.</p><div data-shipping>Shipping: A$6.95</div>',
        head='<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Kestrel Insulated Bottle 750ml - Stainless",'
             '"sku":"KB750-STL","gtin13":"9300000001016","offers":{"@type":"Offer","price":"41.00","priceCurrency":"AUD",'
             '"availability":"https://schema.org/InStock"}}</script>'),
    "span-lamp": _page(
        "Lumen Pocket Lamp 210 lumen",
        '<p class="sku">SKU: LMP-210</p><span class="price" id="p"></span>'
        '<script>setTimeout(function(){document.getElementById("p").textContent="$24.90"},150)</script>'
        '<p class="availability">In stock</p>'),
    "thousands-battery": _page(
        "Meridian 48V 14Ah E-Bike Battery",
        '<span class="product-price">$1,249.00</span><p>Delivery: $39.00 flat rate to most addresses.</p><p class="availability">In stock</p>'),
    "sale-jumper": _page(
        "Harbour Merino Wool Jumper (Navy, M)",
        '<div class="price"><del class="price-was">$89.00</del> <ins class="price-now">$49.50</ins></div>'
        '<span itemprop="sku">HMJ-NVY-M</span><div class="shipping">Free shipping</div><p class="availability">In stock</p>'),
    "no-price": _page(
        "Tidewell Filter Cartridge 2 Pack",
        '<p class="no-price">Price on application</p><p class="availability">In stock</p>'),
    "jsonld-stove": _page(
        "Pinecone Camp Stove",
        "<p>Folds flat. Burns gas canisters.</p>",
        head='<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"Invented Outdoors"},'
             '{"@type":"Product","name":"Pinecone Camp Stove","gtin13":"9300000001023","offers":{"@type":"Offer","price":"54.00",'
             '"priceCurrency":"AUD","availability":"https://schema.org/InStock","shippingDetails":{"@type":"OfferShippingDetails",'
             '"shippingRate":{"@type":"MonetaryAmount","value":12.5,"currency":"AUD"}}}}]}</script>'),
    "euro-gloves": _page(
        "Alpine Ski Gloves - Size L",
        '<span itemprop="price" content="39.90">39,90 &euro;</span><meta itemprop="priceCurrency" content="EUR">'
        '<p class="availability">In stock</p><div class="shipping">Shipping: 6,90 &euro;</div>'),
    "bottle-500": _page(
        "Kestrel Insulated Bottle 500ml",
        '<span class="price">$39.00</span><p class="availability">Out of stock</p><div class="shipping">Shipping calculated at checkout</div>'),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
        slug = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if slug == "broken":
            self.send_error(500, "invented server error")
            return
        html = PAGES.get(slug)
        if html is None:
            self.send_error(404, "no such invented page")
            return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass


class FakeSite:
    """`with FakeSite() as site: site.url('span-lamp')`. Port 0 picks a free port; the demo passes a fixed one."""

    def __init__(self, port: int = 0):
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def url(self, slug: str) -> str:
        return "%s/p/%s" % (self.base, slug)

    def __enter__(self) -> "FakeSite":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
