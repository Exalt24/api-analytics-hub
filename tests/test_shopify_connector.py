"""Shopify connector tests against a mocked transport.

WHAT THESE PROVE, and what they deliberately do not. They prove the pacing
arithmetic, the money conversion, the error taxonomy and the day-bucketing,
because those are the parts that are wrong in most integrations and are wrong
SILENTLY. They do not prove the query is accepted by a real shop, which needs a
development store, so nothing here should be described as a live integration.

The distinction matters: a suite this size passing is exactly the situation where
someone claims a working integration. On a previous project 48 green tests proved
nothing about whether the service could even boot, because the tests and the code
shared an assumption the real platform did not hold.
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

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken
from app.connectors.shopify import ShopifyConnector, _money_to_minor


def envelope(nodes, *, has_next=False, cursor="cur1", available=1000.0, actual=None):
    """A response shaped exactly like Shopify's, including the extensions block."""
    return {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        },
        "extensions": {
            "cost": {
                "requestedQueryCost": 100,
                "actualQueryCost": actual if actual is not None else 12,
                "throttleStatus": {
                    "maximumAvailable": 1000.0,
                    "currentlyAvailable": available,
                    "restoreRate": 50.0,
                },
            }
        },
    }


def order(created_at, total, currency="USD", refunded=None, customer="gid://c/1"):
    return {
        "id": "gid://shopify/Order/1",
        "createdAt": created_at,
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": currency}},
        "totalRefundedSet": (
            {"shopMoney": {"amount": refunded, "currencyCode": currency}} if refunded else {}
        ),
        "customer": {"id": customer},
    }


def connector_with(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ShopifyConnector(
        shop_domain="test.myshopify.com", access_token="shpat_x", client=client, **kw
    )


async def collect(conn, start=date(2026, 8, 1), end=date(2026, 8, 3)):
    out = []
    async for page in conn.fetch(window_start=start, window_end=end):
        out.append(page)
    return out


# ---------------------------------------------------------------- money

def test_decimal_string_becomes_exact_minor_units():
    # float("19.99") is 19.989999999999998 and a day of orders drifts by cents.
    assert _money_to_minor("19.99", "USD") == 1999
    assert _money_to_minor("0.01", "USD") == 1
    assert _money_to_minor("1234567.89", "USD") == 123456789


def test_zero_decimal_currency_is_not_scaled():
    # Scaling JPY by 100 invents money that does not exist.
    assert _money_to_minor("1500", "JPY") == 1500
    assert _money_to_minor("1500", "usd") == 150000


# ---------------------------------------------------------------- error taxonomy

@pytest.mark.parametrize(
    "status,expected",
    [(401, AuthExpired), (403, AuthRevoked), (402, AuthRevoked), (429, RateLimited),
     (503, RateLimited), (418, UpstreamBroken)],
)
def test_http_status_maps_to_the_right_error(status, expected):
    conn = connector_with(lambda req: httpx.Response(status, text="nope"))
    with pytest.raises(expected):
        asyncio.run(collect(conn))


def test_graphql_throttled_arrives_as_http_200_and_is_still_rate_limited():
    # The single most common way a Shopify sync silently returns nothing.
    body = {
        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        "extensions": {"cost": {"throttleStatus": {
            "maximumAvailable": 1000.0, "currentlyAvailable": 0.0, "restoreRate": 50.0}}},
    }
    conn = connector_with(lambda req: httpx.Response(200, json=body))
    with pytest.raises(RateLimited):
        asyncio.run(collect(conn))


def test_429_carries_retry_after_through():
    conn = connector_with(
        lambda req: httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
    )
    with pytest.raises(RateLimited) as exc:
        asyncio.run(collect(conn))
    assert exc.value.retry_after_seconds == 7.0


def test_missing_data_block_is_upstream_broken_not_an_empty_result():
    # Returning zero rows here would chart as "no sales", which is a lie.
    conn = connector_with(lambda req: httpx.Response(200, json={"extensions": {}}))
    with pytest.raises(UpstreamBroken):
        asyncio.run(collect(conn))


# ---------------------------------------------------------------- pacing

def test_low_bucket_causes_a_measured_wait_and_is_counted():
    """The bucket is nearly empty, so the connector must wait before spending.

    Asserted through the recorded throttle_waits rather than by timing the test,
    because a timing assertion is flaky and proves less.
    """
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        # First response reports an almost-empty bucket, so the SECOND call has
        # to wait for a refill.
        available = 20.0 if calls["n"] == 1 else 900.0
        has_next = calls["n"] == 1
        return httpx.Response(200, json=envelope(
            [order("2026-08-01T10:00:00Z", "10.00")], has_next=has_next, available=available))

    conn = connector_with(handler, page_size=100)
    pages = asyncio.run(collect(conn))
    assert sum(p.throttle_waits for p in pages) >= 1, "a near-empty bucket must throttle"


def test_pacing_holds_headroom_back_even_when_the_request_would_fit():
    """The reserve is the point, not just "wait when you cannot afford it".

    Found by mutation 2026-08-20: setting BUCKET_RESERVE_RATIO to 0 left the
    suite green, because the only pacing test used a bucket too small to cover
    the request at all, which waits either way. This picks a bucket that CAN
    cover the request but would breach the reserve, which is the case the reserve
    exists for: the bucket is shared per app per shop, so spending to the floor
    means the next legitimate query is refused rather than merely slowed.
    """
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        # 400 available, request costs ~100, floor is 1000 * 0.33 = 330.
        # 400 - 100 = 300, which is under the floor, so a reserve-aware pacer
        # waits and a reserve-free one does not.
        available = 400.0 if calls["n"] == 1 else 900.0
        return httpx.Response(200, json=envelope(
            [order("2026-08-01T10:00:00Z", "10.00")],
            has_next=calls["n"] == 1,
            available=available,
        ))

    conn = connector_with(handler, page_size=100)
    pages = asyncio.run(collect(conn))
    assert sum(p.throttle_waits for p in pages) >= 1, (
        "a bucket above the request but below the reserve must still wait"
    )


def test_canonical_point_rejects_money_without_a_currency():
    """Guard tested directly, because the connector always supplies one.

    Found by mutation 2026-08-20: deleting this check left the suite green, since
    every existing test went through the connector, which never omits a currency.
    A future connector will, and this is the boundary that catches it.
    """
    from app.connectors.base import CanonicalPoint

    with pytest.raises(UpstreamBroken):
        CanonicalPoint("gross_sales", date(2026, 8, 1), 1000, None)

    with pytest.raises(UpstreamBroken):
        CanonicalPoint("orders", date(2026, 8, 1), 5, "USD")

    # And the valid shapes still construct, so the guard is not simply refusing
    # everything, which would also make the mutation test pass for a bad reason.
    assert CanonicalPoint("gross_sales", date(2026, 8, 1), 1000, "USD").currency == "USD"
    assert CanonicalPoint("orders", date(2026, 8, 1), 5).currency is None


def test_canonical_point_rejects_a_float_value():
    from app.connectors.base import CanonicalPoint

    with pytest.raises(UpstreamBroken):
        CanonicalPoint("orders", date(2026, 8, 1), 5.0)


def test_full_bucket_never_waits():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope(
            [order("2026-08-01T10:00:00Z", "10.00")], available=1000.0))
    )
    pages = asyncio.run(collect(conn))
    assert sum(p.throttle_waits for p in pages) == 0


def test_first_call_is_not_paced_because_the_budget_is_unknown():
    # There is nothing to pace against before the first response, and inventing a
    # default would either stall a healthy sync or overrun a throttled one.
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([], available=1000.0))
    )
    assert conn._throttle is None
    asyncio.run(collect(conn))
    assert conn._throttle is not None, "the response must teach us the budget"


# ---------------------------------------------------------------- day bucketing

def test_orders_are_bucketed_by_utc_date_not_local_offset():
    """A +13 shop books a third of its trading day on the wrong date otherwise."""
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            # 2026-08-02T01:00+13:00 is 2026-08-01T12:00Z, so it belongs to Aug 1.
            order("2026-08-02T01:00:00+13:00", "50.00"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    points = [p for page in pages for p in page.points if p.metric_key == "orders"]
    assert [p.observed_on for p in points] == [date(2026, 8, 1)]


def test_net_sales_is_gross_minus_refunds_in_minor_units():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            order("2026-08-01T10:00:00Z", "100.00", refunded="25.50"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    by_key = {p.metric_key: p for page in pages for p in page.points}
    assert by_key["gross_sales"].value_numeric == 10000
    assert by_key["refunds"].value_numeric == 2550
    assert by_key["net_sales"].value_numeric == 7450


def test_two_currencies_on_one_day_refuses_rather_than_averaging():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            order("2026-08-01T10:00:00Z", "100.00", currency="USD"),
            order("2026-08-01T11:00:00Z", "100.00", currency="EUR"),
        ]))
    )
    with pytest.raises(UpstreamBroken):
        asyncio.run(collect(conn))


def test_money_points_carry_a_currency_and_counts_do_not():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            order("2026-08-01T10:00:00Z", "40.00"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    for p in (pt for page in pages for pt in page.points):
        if p.metric_key in {"gross_sales", "refunds", "net_sales", "avg_order_value"}:
            assert p.currency, f"{p.metric_key} is money and must carry a currency"
        else:
            assert p.currency is None, f"{p.metric_key} is not money"


def test_average_order_value_stays_an_integer_in_minor_units():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            order("2026-08-01T10:00:00Z", "10.00", customer="gid://c/1"),
            order("2026-08-01T11:00:00Z", "10.01", customer="gid://c/2"),
            order("2026-08-01T12:00:00Z", "10.01", customer="gid://c/3"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    aov = next(p for page in pages for p in page.points if p.metric_key == "avg_order_value")
    assert isinstance(aov.value_numeric, int)
    assert aov.value_numeric == 3002 // 3


def test_distinct_customers_are_counted_once_per_day():
    conn = connector_with(
        lambda req: httpx.Response(200, json=envelope([
            order("2026-08-01T10:00:00Z", "10.00", customer="gid://c/1"),
            order("2026-08-01T11:00:00Z", "10.00", customer="gid://c/1"),
            order("2026-08-01T12:00:00Z", "10.00", customer="gid://c/2"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    cust = next(p for page in pages for p in page.points if p.metric_key == "customers")
    assert cust.value_numeric == 2


# ---------------------------------------------------------------- pagination

def test_pagination_yields_a_resume_cursor_before_finishing():
    """A rate limit halfway through must not discard the first half."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        first_page = calls["n"] == 1
        return httpx.Response(200, json=envelope(
            [order(f"2026-08-0{calls['n']}T10:00:00Z", "10.00")],
            has_next=first_page,
            cursor=f"cursor-{calls['n']}",
        ))

    conn = connector_with(handler)
    pages = asyncio.run(collect(conn))
    assert calls["n"] == 2, "should have followed hasNextPage exactly once"
    assert pages[0].cursor == "cursor-1", "an intermediate page must expose a resume cursor"
    assert pages[-1].cursor is None, "the final page has nothing to resume from"


def test_cursor_is_sent_on_a_resumed_fetch():
    seen = {}

    def handler(req):
        seen["after"] = json.loads(req.content)["variables"]["after"]
        return httpx.Response(200, json=envelope([]))

    conn = connector_with(handler)

    async def run():
        async for _ in conn.fetch(
            window_start=date(2026, 8, 1), window_end=date(2026, 8, 3), cursor="resume-me"
        ):
            pass

    asyncio.run(run())
    assert seen["after"] == "resume-me"


# ---------------------------------------------------------------- verify

def test_verify_returns_a_human_label():
    conn = connector_with(lambda req: httpx.Response(200, json={
        "data": {"shop": {"name": "Test Shop", "myshopifyDomain": "test.myshopify.com",
                          "currencyCode": "USD"}},
        "extensions": {"cost": {"throttleStatus": {
            "maximumAvailable": 1000.0, "currentlyAvailable": 999.0, "restoreRate": 50.0}}},
    }))
    assert "Test Shop" in asyncio.run(conn.verify())


def test_verify_refuses_a_shop_with_no_name_rather_than_reporting_success():
    conn = connector_with(lambda req: httpx.Response(200, json={"data": {"shop": {}}}))
    with pytest.raises(UpstreamBroken):
        asyncio.run(conn.verify())
