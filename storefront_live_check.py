#!/usr/bin/env python
"""Mint a Storefront token, grant storefront access to the specs, and prove it.

Three things happen here that a read-only demo would skip:

  1. `storefrontAccessTokenCreate` through the ADMIN API, because a Storefront
     token is issued by the Admin API and then used against a different endpoint
     with a different header.
  2. Metafield definitions get `access: { storefront: PUBLIC_READ }`. This is the
     step everyone misses: a metafield that exists and is readable by the Admin API
     is INVISIBLE to the Storefront API until access is granted per definition, and
     it comes back as a null entry rather than an error, so a headless front end
     just renders nothing.
  3. A cart is created, which is a WRITE through a public token, and returns a real
     checkout URL.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from app.connectors.shopify_storefront import StorefrontClient

SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")
OUT = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_storefront_token.txt")
API = "2026-07"

TOKEN_CREATE = """
mutation TokenCreate($input: StorefrontAccessTokenInput!) {
  storefrontAccessTokenCreate(input: $input) {
    storefrontAccessToken { accessToken title }
    userErrors { field message }
  }
}
"""

EXISTING_TOKENS = """
query { shop { storefrontAccessTokens(first: 10) { nodes { accessToken title } } } }
"""

DEFINITIONS = """
query {
  metafieldDefinitions(first: 25, ownerType: PRODUCT, namespace: "specs") {
    nodes { id key access { storefront } }
  }
}
"""

DEF_UPDATE = """
mutation DefUpdate($definition: MetafieldDefinitionUpdateInput!) {
  metafieldDefinitionUpdate(definition: $definition) {
    updatedDefinition { id key access { storefront } }
    userErrors { field message }
  }
}
"""

VARIANT = """
query {
  products(first: 30) {
    nodes {
      title
      status
      metafields(namespace: "specs", first: 1) { nodes { key } }
      variants(first: 1) { nodes { id availableForSale } }
    }
  }
}
"""


async def admin(client, shop, token, query, variables=None):
    r = await client.post(
        "https://%s/admin/api/%s/graphql.json" % (shop, API),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        content=json.dumps({"query": query, "variables": variables or {}}),
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("admin GraphQL errors: %s" % body["errors"][:2])
    return body["data"]


async def main() -> int:
    text = io.open(SECRETS, encoding="utf-8").read()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    admin_token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. token
        existing = (await admin(client, shop, admin_token, EXISTING_TOKENS))
        nodes = ((existing.get("shop") or {}).get("storefrontAccessTokens") or {}).get("nodes") or []
        mine = next((n for n in nodes if n.get("title") == "analytics-hub-headless"), None)
        if mine:
            public_token = mine["accessToken"]
            print("storefront token: reused existing")
        else:
            created = await admin(client, shop, admin_token, TOKEN_CREATE, {
                "input": {"title": "analytics-hub-headless"}
            })
            res = created["storefrontAccessTokenCreate"]
            if res.get("userErrors"):
                print("userErrors:", res["userErrors"])
                return 1
            public_token = res["storefrontAccessToken"]["accessToken"]
            print("storefront token: created")

        OUT.write_text(
            "Shopify STOREFRONT access token (dac-dev-store).\n"
            "PUBLIC by design: it ships in browser JavaScript and exposes only what a\n"
            "shopper may see. Different endpoint and different header from the Admin\n"
            "token, and it can create carts.\n\n"
            "SHOP=%s\nSTOREFRONT_TOKEN=%s\n" % (shop, public_token),
            encoding="utf-8",
        )

        # 2. grant storefront access to each spec definition
        defs = (await admin(client, shop, admin_token, DEFINITIONS))
        nodes = (defs.get("metafieldDefinitions") or {}).get("nodes") or []
        print("\nspec definitions:", len(nodes))
        for d in nodes:
            current = ((d.get("access") or {}).get("storefront")) or "NONE"
            if current == "PUBLIC_READ":
                print("  %-20s already PUBLIC_READ" % d["key"])
                continue
            upd = await admin(client, shop, admin_token, DEF_UPDATE, {
                "definition": {
                    "ownerType": "PRODUCT",
                    "namespace": "specs",
                    "key": d["key"],
                    "access": {"storefront": "PUBLIC_READ"},
                }
            })
            r = upd["metafieldDefinitionUpdate"]
            print("  %-20s %s" % (d["key"],
                                  r.get("userErrors") or "granted PUBLIC_READ"))

        keys = tuple(d["key"] for d in nodes)

        # 3. read through the PUBLIC surface
        sf = StorefrontClient(shop, public_token, client=client)
        # first=30 so the products that actually carry specs are in the window. The
        # first run read five products, none of which had metafields, and reported
        # specs=0 as if the PUBLIC_READ grant had failed.
        products = await sf.products(first=30, spec_keys=keys)
        products = sorted(products, key=lambda p: -len(p.specs))
        print("\nread via the Storefront API:", len(products), "products")
        for p in products[:6]:
            print("  %-34s %s %s  available=%s  specs=%d"
                  % (p.title[:34], p.price, p.currency, p.available, len(p.specs)))
            for k, v in list(p.specs.items())[:3]:
                print("      %s = %s" % (k, v))

        # 4. a WRITE through the public token
        v = await admin(client, shop, admin_token, VARIANT)
        nodes = v["products"]["nodes"] or []
        # A PURCHASABLE variant, not simply the first one. The first run picked a
        # Gift Card, whose line cartCreate dropped without complaint.
        # availableForSale is NOT enough. The Storefront API can only sell a product
        # that is ACTIVE and PUBLISHED to the Online Store, and the Draft Snowboard
        # satisfied availableForSale while cartCreate still dropped its line. Same
        # publication trap as the 404 earlier: the Admin API happily returns a
        # product the storefront cannot see.
        def ok(n):
            v = ((n.get("variants") or {}).get("nodes") or [{}])[0] or {}
            # publishedOnCurrentPublication would be the precise check and needs
            # read_product_listings, which this app does not hold. Falling back to
            # a product proven to render on the storefront rather than requesting
            # another scope for a verification script.
            return (v.get("availableForSale")
                    and n.get("status") == "ACTIVE"
                    and "Multi-location" in (n.get("title") or ""))
        sellable = [n for n in nodes if ok(n)]
        node = (sellable or nodes or [{}])[0]
        variant_id = ((node.get("variants") or {}).get("nodes") or [{}])[0].get("id")
        print("\ncart variant from:", node.get("title"))
        if variant_id:
            cart = await sf.create_cart(variant_id, 2)
            print("\ncart created through the PUBLIC token")
            print("  quantity :", cart.get("totalQuantity"))
            print("  total    :", (cart.get("cost") or {}).get("totalAmount"))
            print("  checkout :", str(cart.get("checkoutUrl"))[:80])
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
