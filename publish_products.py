#!/usr/bin/env python
"""Publish API-created products to the Online Store sales channel.

`productCreate` does NOT publish. The product exists, the Admin API returns it,
every metafield is attached, and the storefront serves a 404, which reads exactly
like a broken theme. That is a genuinely confusing hour the first time, because
nothing in the create response hints at it.

`publishablePublish` attaches the product to a publication, and the Online Store
publication id has to be looked up rather than guessed.
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

SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")
API = "2026-07"

PUBLICATIONS = """
query { publications(first: 10) { nodes { id name } } }
"""

PRODUCTS = """
query { products(first: 50) { nodes { id handle title
  publishedOnCurrentPublication } } }
"""

PUBLISH = """
mutation Publish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { availablePublicationsCount { count } }
    userErrors { field message }
  }
}
"""


async def gql(client, shop, token, query, variables=None):
    r = await client.post(
        "https://%s/admin/api/%s/graphql.json" % (shop, API),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        content=json.dumps({"query": query, "variables": variables or {}}),
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("GraphQL errors: %s" % body["errors"][:2])
    return body["data"]


async def main() -> int:
    text = io.open(SECRETS, encoding="utf-8").read()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()

    async with httpx.AsyncClient(timeout=60.0) as client:
        pubs = (await gql(client, shop, token, PUBLICATIONS))["publications"]["nodes"]
        for p in pubs:
            print("publication:", p["name"], p["id"])
        online = next((p for p in pubs if "online store" in p["name"].lower()), None)
        if not online:
            print("no Online Store publication found")
            return 1

        products = (await gql(client, shop, token, PRODUCTS))["products"]["nodes"]
        todo = [p for p in products if not p.get("publishedOnCurrentPublication")]
        print("\nproducts needing publication:", len(todo))

        for p in todo:
            data = await gql(client, shop, token, PUBLISH, {
                "id": p["id"],
                "input": [{"publicationId": online["id"]}],
            })
            errs = (data["publishablePublish"] or {}).get("userErrors") or []
            print("  %-40s %s" % (p["handle"], errs or "published"))

        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
