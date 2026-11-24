#!/usr/bin/env python
# LIFESPAN: permanent
"""Put a known sales history on the DEVELOPMENT store for an inventory planning run, and take it off again.

    python scripts/inventory_dev_store.py prepare [--state FILE]    create the test products and the dated orders, set the stock
    python scripts/inventory_dev_store.py revert  [--state FILE]    delete every product and order `prepare` made, and nothing else
    python scripts/inventory_dev_store.py show                       what the plan sets out to create, without writing anything

It writes to the store whose token file SHOPIFY_DEV_TOKEN_FILE names, and refuses a store that does not report `partnerDevelopment` (the guard the seeder uses). Everything it makes
is titled "IPT ..." (inventory planning test), the orders are created as test orders, and the ids go into the state file as each one is made, so a run that stops half way is
finished by running `prepare` again and a `revert` removes exactly what is listed. Existing products, orders and stock are never touched.

The sales are dated by `processedAt` (the day the platform attributes an order to), counted back from today, so the same scene is built on any day: a best seller, a rising product,
a slow one, a size that sells steadily and a variant that has sold out. Stock is set with `inventorySetQuantities`. Order creation is throttled by Shopify (about one in twelve seconds),
so a full `prepare` takes several minutes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.shopify_seed import ORDER_CREATE_PACING_SECONDS, SeedRefused, ShopifySeeder  # noqa: E402
from app.secret_files import DEV_TOKEN  # noqa: E402

DEFAULT_STATE = Path(__file__).resolve().parents[1] / ".secrets" / "inventory_dev_store_state.json"

# product title, option name, [(option value, sku, price, unit cost, stock, [(days before today, units)])]
SCENE = [
    ("IPT Studio Tee", "Size", [
        ("M", "IPT-TEE-M", "25.00", "8.00", 10, [(3, 2), (6, 2), (9, 2), (13, 2), (16, 2), (20, 2), (24, 2), (27, 2), (33, 2), (40, 2), (47, 2), (54, 2)]),
        ("S", "IPT-TEE-S", "25.00", "8.00", 40, [(5, 1), (15, 1), (25, 1), (35, 1), (45, 1)]),
        ("L", "IPT-TEE-L", "25.00", "8.00", 60, [(40, 1)]),
    ]),
    ("IPT Canvas Cap", "Color", [
        ("Red", "IPT-CAP-R", "18.00", "6.00", 120, [(45, 1), (21, 2), (12, 2), (7, 2), (2, 1)]),
    ]),
    ("IPT Wool Sock", "Size", [
        ("L", "IPT-SOCK-L", "12.00", "3.50", 0, [(4, 1), (8, 1), (11, 1), (19, 1)]),
    ]),
]

CREATE = """mutation($p: ProductCreateInput!) { productCreate(product: $p) {
  product { id variants(first: 5) { nodes { id inventoryItem { id } } } } userErrors { field message } } }"""
BULK_UPDATE = """mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $pid, variants: $v) {
  productVariants { id sku } userErrors { field message } } }"""
BULK_CREATE = """mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkCreate(productId: $pid, variants: $v) {
  productVariants { id sku inventoryItem { id } } userErrors { field message } } }"""
SET_QTY = """mutation($i: InventorySetQuantitiesInput!, $key: String!) { inventorySetQuantities(input: $i) @idempotent(key: $key) {
  inventoryAdjustmentGroup { id } userErrors { field message } } }"""
LOCATIONS = "query { locations(first: 10) { nodes { id name isActive } } }"
ORDER_DELETE = "mutation($id: ID!) { orderDelete(orderId: $id) { deletedId userErrors { field message } } }"
PRODUCT_DELETE = "mutation($i: ProductDeleteInput!) { productDelete(input: $i) { deletedProductId userErrors { field message } } }"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"products": {}, "variants": {}, "orders": [], "stock_set": [], "dated": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def seeder() -> ShopifySeeder:
    text = DEV_TOKEN.read()
    shop, token = re.search(r"^SHOP=(.+)$", text, re.M), re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M)
    if not shop or not token:
        raise SystemExit("the Shopify token file needs a SHOP= line and an ACCESS_TOKEN= line")
    return ShopifySeeder(shop.group(1).strip(), token.group(1).strip())


def orders_by_day() -> dict[int, list[tuple[str, int]]]:
    """days before today -> [(sku, units)]: two products sold on the same day share one order."""
    out: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for _title, _opt, variants in SCENE:
        for _value, sku, _price, _cost, _stock, sales in variants:
            for days_ago, units in sales:
                out[days_ago].append((sku, units))
    return dict(sorted(out.items(), reverse=True))


async def prepare(path: Path) -> int:
    s = seeder()
    info = await s.preflight()
    print("target:", info["domain"], "plan", info["plan"], "development:", info["partner_development"], "existing orders:", info["existing_orders"])
    state = load_state(path)
    loc = next(n for n in (await s._gql(LOCATIONS))["locations"]["nodes"] if n["isActive"] and n["name"] == "Shop location")["id"]

    for title, option, variants in SCENE:
        if title in state["products"]:
            continue
        first = variants[0]
        made = (await s._gql(CREATE, {"p": {"title": title, "status": "ACTIVE", "productOptions": [{"name": option, "values": [{"name": first[0]}]}]}}))["productCreate"]["product"]
        pid = made["id"]
        state["products"][title] = pid
        save_state(path, state)
        v0 = made["variants"]["nodes"][0]
        await s._gql(BULK_UPDATE, {"pid": pid, "v": [{"id": v0["id"], "price": first[2], "inventoryItem": {"sku": first[1], "tracked": True, "cost": first[3]}}]})
        state["variants"][first[1]] = {"variant": v0["id"], "item": v0["inventoryItem"]["id"]}
        rest = variants[1:]
        if rest:
            created = (await s._gql(BULK_CREATE, {"pid": pid, "v": [
                {"optionValues": [{"optionName": option, "name": v[0]}], "price": v[2], "inventoryItem": {"sku": v[1], "tracked": True, "cost": v[3]}} for v in rest]}))["productVariantsBulkCreate"]["productVariants"]
            for made_v in created:
                state["variants"][made_v["sku"]] = {"variant": made_v["id"], "item": made_v["inventoryItem"]["id"]}
        save_state(path, state)
        print("created", title, [v[1] for v in variants])

    wanted = [(sku, stock) for _t, _o, vs in SCENE for (_v, sku, _p, _c, stock, _s) in vs]
    todo = [(sku, q) for sku, q in wanted if sku not in state["stock_set"]]
    if todo:
        await s._gql(SET_QTY, {"i": {"name": "available", "reason": "correction",
                                     "quantities": [{"inventoryItemId": state["variants"][sku]["item"], "locationId": loc, "quantity": q, "changeFromQuantity": None} for sku, q in todo]},
                                  "key": "ipt-stock-%s" % uuid.uuid4().hex})
        state["stock_set"] = sorted({*state["stock_set"], *[sku for sku, _ in todo]})
        save_state(path, state)
        print("stock set:", dict(todo))

    today = datetime.now(timezone.utc).date()
    for days_ago, lines in orders_by_day().items():
        key = "%s:%s" % ((today - timedelta(days=days_ago)).isoformat(), "+".join("%s*%d" % l for l in lines))
        if key in state["dated"]:
            continue
        day = today - timedelta(days=days_ago)
        order = await s._create_order_with_backoff({
            "lineItems": [{"variantId": state["variants"][sku]["variant"], "quantity": units} for sku, units in lines],
            "financialStatus": "PAID", "test": True, "tags": ["inventory-planning-test"],
            "processedAt": datetime(day.year, day.month, day.day, 11, 0, tzinfo=timezone.utc).isoformat()})
        if not order.get("id"):
            raise SystemExit("an order was not created: %s" % key)
        state["orders"].append(order["id"])
        state["dated"].append(key)
        save_state(path, state)
        print("order", order.get("name"), key)
        await asyncio.sleep(ORDER_CREATE_PACING_SECONDS)
    print("prepared: %d products, %d variants, %d orders" % (len(state["products"]), len(state["variants"]), len(state["orders"])))
    return 0


async def revert(path: Path) -> int:
    s = seeder()
    await s.preflight()
    state = load_state(path)
    gone_orders = gone_products = 0
    for oid in list(state["orders"]):
        await s._gql(ORDER_DELETE, {"id": oid})
        state["orders"].remove(oid)
        gone_orders += 1
        save_state(path, state)
    for title, pid in list(state["products"].items()):
        await s._gql(PRODUCT_DELETE, {"i": {"id": pid}})
        del state["products"][title]
        gone_products += 1
        save_state(path, state)
    print("reverted: %d orders and %d products deleted" % (gone_orders, gone_products))
    return 0


def show() -> int:
    for title, _opt, variants in SCENE:
        for value, sku, price, cost, stock, sales in variants:
            print("%-16s %-4s %-11s price %s cost %s stock %3d  sold %d units in %d orders" % (title, value, sku, price, cost, stock, sum(u for _d, u in sales), len(sales)))
    print("%d orders in all (two products that sold on one day share an order)" % len(orders_by_day()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("prepare", "revert", "show"))
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = ap.parse_args()
    if args.command == "show":
        return show()
    try:
        return asyncio.run(prepare(args.state) if args.command == "prepare" else revert(args.state))
    except SeedRefused as exc:
        print("REFUSED:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
