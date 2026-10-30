"""Invented data for the price watch demo and for tests: a small catalogue, the competitor pages that go with it (fake_site.py),
and a shipping rate table. Every name, price, id and GTIN here is made up (the GTINs carry a correct check digit so the matcher
accepts them). The Shopify and Zoho ids look like the vendors' but belong to nothing.

The catalogue is built so the loop shows each case once:
  KTB-750    competitor page states shipping, matched by GTIN, cheaper on landed price -> a proposal from a STATED figure
  LMP-210    competitor page states no shipping -> our rate table estimates it, the line says ESTIMATED
  MER-48V    "$1,249.00" with a thousands separator, matched by title only
  HMJ-NVY-M  sale price against a struck-through original, free shipping, the proposal is held at our floor
  TWF-2      the competitor page has no price -> excluded
  PCS-1      competitor is dearer once its stated shipping is added -> compared, not lower
  ASG-L      competitor prices in euros; its title only scores 0.80 against ours, so it waits for a person (confirm-match), and once
             confirmed it is excluded for the currency, there is no exchange rate
and one competitor page (Kestrel 500ml) that must NOT match the 750ml bottle.
"""
from __future__ import annotations

from typing import Any

import asyncpg

from . import store

DEMO_BASE = "http://127.0.0.1:8791"
CURRENCY = "AUD"

PRODUCTS: list[dict[str, Any]] = [
    dict(sku="KTB-750", title="Kestrel Insulated Bottle 750ml", brand="Kestrel", gtin="9300000001016", price_minor=4995, floor_minor=3800,
         weight_grams=480, shopify_product_gid="gid://shopify/Product/1001", shopify_variant_gid="gid://shopify/ProductVariant/2001",
         zoho_item_id="4815000000010001", zoho_item_name="Kestrel Insulated Bottle 750ml"),
    dict(sku="LMP-210", title="Lumen Pocket Lamp 210 lumen", brand="Lumen", price_minor=3450, floor_minor=2200, weight_grams=150,
         shopify_product_gid="gid://shopify/Product/1002", shopify_variant_gid="gid://shopify/ProductVariant/2002"),
    dict(sku="MER-48V", title="Meridian E-Bike Battery 48V 14Ah", brand="Meridian", price_minor=134900, floor_minor=110000, weight_grams=3200,
         shopify_product_gid="gid://shopify/Product/1003", shopify_variant_gid="gid://shopify/ProductVariant/2003",
         zoho_item_id="4815000000010003", zoho_item_name="Meridian E-Bike Battery 48V 14Ah"),
    dict(sku="HMJ-NVY-M", title="Harbour Merino Jumper Navy M", brand="Harbour", price_minor=7900, floor_minor=5500, weight_grams=600,
         shopify_product_gid="gid://shopify/Product/1004", shopify_variant_gid="gid://shopify/ProductVariant/2004",
         zoho_item_id="4815000000010004", zoho_item_name="Harbour Merino Jumper Navy M"),
    dict(sku="TWF-2", title="Tidewell Filter Cartridge 2 Pack", brand="Tidewell", price_minor=2400, floor_minor=1500, weight_grams=200,
         shopify_product_gid="gid://shopify/Product/1005", shopify_variant_gid="gid://shopify/ProductVariant/2005"),
    dict(sku="PCS-1", title="Pinecone Camp Stove", brand="Pinecone", gtin="9300000001023", price_minor=5900, floor_minor=4200, weight_grams=900,
         zoho_item_id="4815000000010006", zoho_item_name="Pinecone Camp Stove"),
    dict(sku="ASG-L", title="Alpine Ski Gloves L", brand="Alpine", price_minor=6900, floor_minor=4500, weight_grams=350,
         shopify_product_gid="gid://shopify/Product/1007", shopify_variant_gid="gid://shopify/ProductVariant/2007"),
]

#: (competitor, page slug in fake_site.PAGES)
LISTINGS: list[tuple[str, str]] = [
    ("Outdoor Depot (invented)", "jsonld-bottle"),
    ("Outdoor Depot (invented)", "span-lamp"),
    ("Gearhouse (invented)", "thousands-battery"),
    ("Gearhouse (invented)", "sale-jumper"),
    ("Gearhouse (invented)", "no-price"),
    ("Outdoor Depot (invented)", "jsonld-stove"),
    ("Alpen Store (invented)", "euro-gloves"),
    ("Alpen Store (invented)", "bottle-500"),
]

#: zone, from g, to g (exclusive), cost in minor units
RATES: list[tuple[str, int, int, int]] = [
    ("domestic", 0, 500, 850),
    ("domestic", 500, 2000, 1200),
    ("domestic", 2000, 5000, 1850),
    ("domestic", 5000, 20000, 3200),
]


async def load_demo(conn: asyncpg.Connection, tenant_id: str, base: str = DEMO_BASE) -> dict[str, int]:
    for p in PRODUCTS:
        await store.add_product(conn, tenant_id, currency=CURRENCY, **p)
    for competitor, slug in LISTINGS:
        await store.add_listing(conn, tenant_id, competitor=competitor, url="%s/p/%s" % (base, slug))
    for zone, lo, hi, cost in RATES:
        await store.add_rate(conn, tenant_id, zone=zone, weight_from_g=lo, weight_to_g=hi, cost_minor=cost, currency=CURRENCY)
    return {"products": len(PRODUCTS), "listings": len(LISTINGS), "rates": len(RATES)}
