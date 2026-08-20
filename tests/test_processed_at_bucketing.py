"""Revenue is attributed to processedAt, not createdAt, and that is a bug class
rather than a preference.

The failure it prevents, concretely: a shop migrating onto Shopify gets one
`createdAt` for every historical order, the day of the import. Bucketing on
createdAt then renders an entire trading history as a single enormous day, with
every individual figure correct, which is exactly why nobody notices. Every order
this project's seeder writes has the same shape, because the Admin API lets you
set `processedAt` and does not let you set `createdAt` at all.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import UpstreamBroken
from app.connectors.shopify import ShopifyConnector

THROTTLE = {
    "cost": {
        "requestedQueryCost": 100,
        "actualQueryCost": 12,
        "throttleStatus": {
            "maximumAvailable": 20000.0,
            "currentlyAvailable": 19900.0,
            "restoreRate": 1000.0,
        },
    }
}


def node(*, created, processed, total="100.00", oid="1"):
    n = {
        "id": "gid://shopify/Order/%s" % oid,
        "createdAt": created,
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "USD"}},
        "totalRefundedSet": {},
        "customer": {"id": "gid://shopify/Customer/1"},
    }
    if processed is not None:
        n["processedAt"] = processed
    return n


def respond(nodes):
    return {
        "data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": nodes}},
        "extensions": THROTTLE,
    }


def run(nodes, capture=None):
    def handler(req):
        if capture is not None:
            capture.append(json.loads(req.content))
        return httpx.Response(200, json=respond(nodes))

    conn = ShopifyConnector(
        shop_domain="test.myshopify.com",
        access_token="shpat_x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def go():
        out = []
        async for page in conn.fetch(window_start=date(2026, 8, 14), window_end=date(2026, 8, 21)):
            out.extend(page.points)
        return out

    return asyncio.run(go())


def days_of(points, metric="orders"):
    return sorted(p.observed_on for p in points if p.metric_key == metric)


def test_a_backdated_order_lands_on_the_day_it_was_processed():
    """The migration case. Three orders imported today, processed on three
    different days: bucketing on createdAt would pile all three on one day."""
    points = run([
        node(created="2026-08-20T16:00:00Z", processed="2026-08-16T09:00:00Z", oid="1"),
        node(created="2026-08-20T16:00:00Z", processed="2026-08-17T09:00:00Z", oid="2"),
        node(created="2026-08-20T16:00:00Z", processed="2026-08-18T09:00:00Z", oid="3"),
    ])
    assert days_of(points) == [date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18)]


def test_createdat_is_the_fallback_when_an_order_is_unprocessed():
    """processedAt is nullable, and a null must not become today. The order still
    counts, on its creation day."""
    points = run([node(created="2026-08-19T09:00:00Z", processed=None, oid="1")])
    assert days_of(points) == [date(2026, 8, 19)]


def test_neither_date_is_a_hard_error_not_an_implicit_today():
    with pytest.raises(UpstreamBroken):
        run([{
            "id": "gid://shopify/Order/1",
            "currentTotalPriceSet": {"shopMoney": {"amount": "10.00", "currencyCode": "USD"}},
            "totalRefundedSet": {},
        }])


def test_the_query_filters_on_the_same_field_it_buckets_on():
    """Filtering on created_at while bucketing on processed_at drops backdated
    orders: the day they belong to reads zero instead of missing, which is the
    worse of the two failures because it looks like data."""
    seen: list[dict] = []
    run([node(created="2026-08-20T16:00:00Z", processed="2026-08-16T09:00:00Z")], capture=seen)
    q = seen[0]["variables"]["q"]
    assert "processed_at:>=" in q and "processed_at:<=" in q
    assert "created_at" not in q


def test_utc_normalisation_still_applies_to_processed_at():
    """A +13 offset late in the day belongs to the previous UTC day. This is the
    same guard as before, re-pinned on the new field, because moving which field
    is read is exactly where such a guard gets dropped."""
    points = run([node(created="2026-08-20T16:00:00Z", processed="2026-08-18T01:30:00+13:00")])
    assert days_of(points) == [date(2026, 8, 17)]
