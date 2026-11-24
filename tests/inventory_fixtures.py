# LIFESPAN: permanent (the shared setup of the inventory planning tests)
"""Shared setup for the inventory planning database tests, and a stand-in Shopify for the reader tests.

Like the other database suites this DROPS and recreates the `public` schema of the database it points at (ANALYTICS_TEST_DSN, default the compose Postgres on 5433), so aim it at a
scratch database. It applies 001, 002, 003, 020 and 021: inventory planning needs the tenant table, the request role, the API keys the web tests read and its own tables.

`FakeShop` is an httpx transport that answers the two queries app/inventory/reader.py sends, written from the shapes Shopify's Admin GraphQL documentation gives for
`productVariants`, `inventoryLevels` and `orders`. It pages, filters orders by the processed_at window of the query, reports a cost bucket in `extensions`, and can be told to
withhold a quantity or serve an order with more than 100 lines. It is not Shopify.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
TODAY = date(2026, 11, 24)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "020_inventory_planning.sql", "021_purchase_orders.sql")

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")


def reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


if os.environ.get("INVENTORY_REQUIRE_DB") == "1" and not reachable():
    # CI sets this, so a Postgres that did not start fails the run instead of skipping every database test
    raise RuntimeError("INVENTORY_REQUIRE_DB=1 but Postgres is not reachable at ANALYTICS_TEST_DSN")

needs_db = pytest.mark.skipif(not reachable(), reason="Postgres not reachable, run docker compose up -d")


def run(coro):
    return asyncio.run(coro)


def reset_schema(extra: tuple[str, ...] = ()) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in (*MIGRATIONS, *extra):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


def add_tenant(slug: str) -> uuid.UUID:
    tid = uuid.uuid4()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, slug, slug))
    return tid


def admin_rows(sql: str, params: tuple = ()) -> list[tuple]:
    """A query as the bootstrap superuser, which sees every tenant: for checking what was really stored."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def gid(kind: str, n: int) -> str:
    return "gid://shopify/%s/%d" % (kind, n)


# ------------------------------------------------------------------------------------------------ a stand-in Shopify

def variant_node(n: int, *, product: str, title: str = "Default Title", sku: str | None = None, available: int = 10, incoming: int = 0, tracked: bool = True,
                 cost: str | None = None, locations: int = 1, withhold_available: bool = False) -> dict[str, Any]:
    levels = []
    for loc in range(locations):
        qs = [{"name": "available", "quantity": available if loc == 0 else 0}, {"name": "on_hand", "quantity": available if loc == 0 else 0},
              {"name": "committed", "quantity": 0}, {"name": "incoming", "quantity": incoming if loc == 0 else 0}]
        if withhold_available:
            qs = [q for q in qs if q["name"] != "available"]
        levels.append({"location": {"id": gid("Location", 900 + loc), "name": "Location %d" % loc}, "quantities": qs})
    return {"id": gid("ProductVariant", n), "sku": sku, "title": title, "product": {"id": gid("Product", n), "title": product},
            "inventoryItem": {"id": gid("InventoryItem", n), "tracked": tracked, "unitCost": {"amount": cost, "currencyCode": "USD"} if cost else None,
                              "inventoryLevels": {"nodes": levels}}}


def line_node(n: int, variant: int | None, qty: int, price: str = "10.00", sku: str | None = None, current: int | None = None) -> dict[str, Any]:
    return {"id": gid("LineItem", n), "sku": sku, "quantity": qty, "currentQuantity": qty if current is None else current,
            "variant": {"id": gid("ProductVariant", variant)} if variant else None,
            "discountedUnitPriceSet": {"shopMoney": {"amount": price, "currencyCode": "USD"}}}


def order_node(n: int, processed: datetime, lines: list[dict[str, Any]], *, cancelled: bool = False) -> dict[str, Any]:
    return {"id": gid("Order", n), "processedAt": processed.isoformat().replace("+00:00", "Z"), "createdAt": processed.isoformat().replace("+00:00", "Z"),
            "cancelledAt": processed.isoformat().replace("+00:00", "Z") if cancelled else None, "lineItems": {"nodes": lines, "pageInfo": {"hasNextPage": False, "endCursor": None}}}


class FakeShop:
    def __init__(self, variants: list[dict[str, Any]], orders: list[dict[str, Any]], page_size: int = 100):
        self.variants, self.orders, self.page_size = variants, orders, page_size
        self.requests: list[str] = []
        self.extra_lines: dict[str, list[dict[str, Any]]] = {}          # order id -> lines served by the MoreLines query

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    @staticmethod
    def _page(items: list, first: int, after: str | None) -> tuple[list, dict[str, Any]]:
        start = int(after) if after else 0
        chunk = items[start:start + first]
        more = start + first < len(items)
        return chunk, {"hasNextPage": more, "endCursor": str(start + first) if more else None}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, v = body["query"], body.get("variables") or {}
        self.requests.append(query.split("(")[0].split("{")[0].strip() or query[:30])
        ext = {"cost": {"requestedQueryCost": 50, "actualQueryCost": 20, "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": 980.0, "restoreRate": 50.0}}}
        if "productVariants" in query:
            chunk, info = self._page(self.variants, min(v["first"], self.page_size), v.get("after"))
            data: dict[str, Any] = {"productVariants": {"pageInfo": info, "nodes": chunk}}
        elif "MoreLines" in query:
            lines = self.extra_lines.get(v["id"], [])
            chunk, info = self._page(lines, 100, v.get("after"))
            data = {"order": {"lineItems": {"pageInfo": info, "nodes": chunk}}}
        elif "orders(" in query:
            m = re.search(r"processed_at:>=(\S+) processed_at:<=(\S+)", v["q"])
            lo, hi = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
            inside = [o for o in self.orders if lo <= datetime.fromisoformat(o["processedAt"].replace("Z", "+00:00")).date() <= hi]
            chunk, info = self._page(inside, min(v["first"], self.page_size), v.get("after"))
            data = {"orders": {"pageInfo": info, "nodes": chunk}}
        else:
            return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})
        return httpx.Response(200, json={"data": data, "extensions": ext})


def days_ago(n: int, hour: int = 12) -> datetime:
    d = TODAY - timedelta(days=n)
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=timezone.utc)
