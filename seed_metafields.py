#!/usr/bin/env python
"""Put real specifications on the dev-store products as metafields.

WHY THIS EXISTS. Two things downstream need structured specs rather than a prose
description: the theme's specification-table section, which renders whatever lives
in the `specs` namespace, and the catalogue copywriter, which can only write a
grounded meta description if there are facts to ground on. With no metafields both
are correct and useless, which is exactly what the first live run showed: a Gift
Card with a title and a price produced a 27-character description that was
rightly rejected.

The definitions are created FIRST and pinned. An unpinned metafield does not
appear in the admin product page, so a merchant cannot see or edit the value and
concludes the app is broken. That is a real support cost for a two-line difference
in the create call.
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

DEFINITION_CREATE = """
mutation DefCreate($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition { id key }
    userErrors { field message code }
  }
}
"""

METAFIELDS_SET = """
mutation Set($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { key namespace value type }
    userErrors { field message }
  }
}
"""

PRODUCTS = """
query { products(first: 20) { nodes { id title } } }
"""

#: key -> (label, shopify type). single_line_text_field for anything with a unit
#: baked into the string, because Shopify's `dimension` type stores a value and a
#: unit separately and renders as an object if a template prints it raw.
DEFINITIONS = [
    ("material", "Material", "single_line_text_field"),
    ("frequency_response", "Frequency response", "single_line_text_field"),
    ("impedance", "Impedance", "single_line_text_field"),
    ("connector", "Connector", "single_line_text_field"),
    ("weight_grams", "Weight (g)", "number_integer"),
    ("warranty_months", "Warranty (months)", "number_integer"),
]

#: Deliberately partial: some products get fewer specs, so the spec table has to
#: cope with a sparse namespace rather than a tidy uniform one.
SPECS = {
    "Studio Monitor Headphones": {
        "material": "Aluminium and protein leather",
        "frequency_response": "10 Hz to 40 kHz",
        "impedance": "38 ohms",
        "connector": "3.5 mm TRS with 6.35 mm adapter",
        "weight_grams": 320,
        "warranty_months": 24,
    },
    "USB Audio Interface": {
        "material": "Anodised aluminium chassis",
        "frequency_response": "20 Hz to 20 kHz",
        "connector": "USB-C",
        "weight_grams": 640,
        "warranty_months": 12,
    },
    "Monitor Arm": {
        "material": "Cold-rolled steel",
        "weight_grams": 2900,
        "warranty_months": 60,
    },
    "65% Mechanical Keyboard": {
        "material": "Aluminium top case, PBT keycaps",
        "connector": "USB-C detachable",
        "weight_grams": 780,
        "warranty_months": 12,
    },
}


def creds() -> tuple[str, str]:
    text = io.open(SECRETS, encoding="utf-8").read()
    return (re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip(),
            re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip())


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
    shop, token = creds()
    async with httpx.AsyncClient(timeout=60.0) as client:
        for key, name, mtype in DEFINITIONS:
            data = await gql(client, shop, token, DEFINITION_CREATE, {
                "definition": {
                    "name": name,
                    "namespace": "specs",
                    "key": key,
                    "type": mtype,
                    "ownerType": "PRODUCT",
                    # Pinned, so the merchant can actually see and edit it on the
                    # product page. Unpinned definitions are invisible in admin.
                    "pin": True,
                }
            })
            res = data["metafieldDefinitionCreate"]
            errs = res.get("userErrors") or []
            taken = any(e.get("code") == "TAKEN" for e in errs)
            print("definition %-20s %s" % (
                key, "exists" if taken else ("created" if res.get("createdDefinition")
                                             else errs)))

        products = (await gql(client, shop, token, PRODUCTS))["products"]["nodes"]
        by_title = {p["title"]: p["id"] for p in products}

        payload = []
        for title, specs in SPECS.items():
            pid = by_title.get(title)
            if not pid:
                print("skip, no such product:", title)
                continue
            for key, value in specs.items():
                payload.append({
                    "ownerId": pid,
                    "namespace": "specs",
                    "key": key,
                    "value": str(value),
                    "type": ("number_integer"
                             if isinstance(value, int) else "single_line_text_field"),
                })

        if not payload:
            print("nothing to write")
            return 1

        # metafieldsSet takes at most 25 per call.
        written = 0
        for i in range(0, len(payload), 25):
            chunk = payload[i:i + 25]
            data = await gql(client, shop, token, METAFIELDS_SET,
                             {"metafields": chunk})
            res = data["metafieldsSet"]
            if res.get("userErrors"):
                print("userErrors:", res["userErrors"][:3])
            written += len(res.get("metafields") or [])
        print("\nmetafields written:", written)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
