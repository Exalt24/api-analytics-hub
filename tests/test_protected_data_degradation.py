"""Protected customer data is a separate gate from scopes, and the connector has
to survive it rather than treat it as a dead token.

Grounded in a real refusal from dac-dev-store on 2026-08-21, with read_customers
and read_orders both granted at install:

    ACCESS_DENIED  This app is not approved to access the Order object.
    See https://shopify.dev/docs/apps/launch/protected-customer-data

The tests below pin four separate claims, because each one failed a different way
while this was being written:

  1. the two ACCESS_DENIED kinds are told apart, and by the DOCUMENTATION link
     rather than by prose that Shopify can reword;
  2. the retry happens once, not once per page, which is what a latched flag
     buys and what a naive try/except in the loop would get wrong;
  3. the `customers` metric DISAPPEARS rather than being reported as zero, since
     a zero written into a dated snapshot is a permissions bug wearing the
     costume of a business result;
  4. a refusal that survives stripping the field propagates, because there is
     nothing left to degrade to and pretending otherwise would return an empty
     window as if the shop had no sales.
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

from app.connectors.base import AuthRevoked, ProtectedDataDenied
from app.connectors.shopify import ShopifyConnector

THROTTLE = {
    "cost": {
        "requestedQueryCost": 100,
        "actualQueryCost": 12,
        "throttleStatus": {
            "maximumAvailable": 1000.0,
            "currentlyAvailable": 900.0,
            "restoreRate": 50.0,
        },
    }
}

PROTECTED_ERROR = {
    "errors": [
        {
            "message": "This app is not approved to access the Order object.",
            "extensions": {
                "code": "ACCESS_DENIED",
                "documentation": "https://shopify.dev/docs/apps/launch/protected-customer-data",
            },
            "path": ["orders"],
        }
    ],
    "data": None,
    "extensions": THROTTLE,
}

UNINSTALLED_ERROR = {
    "errors": [
        {
            "message": "Access denied for orders field. Required access: read_orders",
            "extensions": {"code": "ACCESS_DENIED"},
        }
    ],
    "data": None,
    "extensions": THROTTLE,
}


def order_node(created_at, total, customer="gid://shopify/Customer/1"):
    node = {
        "id": "gid://shopify/Order/1",
        "createdAt": created_at,
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "USD"}},
        "totalRefundedSet": {},
    }
    if customer is not None:
        node["customer"] = {"id": customer}
    return node


def ok(nodes):
    return {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }
        },
        "extensions": THROTTLE,
    }


def connector_with(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ShopifyConnector(
        shop_domain="test.myshopify.com", access_token="shpat_x", client=client
    )


async def collect(conn):
    out = []
    async for page in conn.fetch(window_start=date(2026, 8, 1), window_end=date(2026, 8, 3)):
        out.append(page)
    return out


def points_of(pages):
    return [p for page in pages for p in page.points]


# --------------------------------------------------------- 1. told apart

def test_protected_data_denial_is_not_a_revoked_token():
    """Same ACCESS_DENIED code, opposite remedy: one needs the merchant to
    reconnect, the other needs the developer to file a data declaration."""
    conn = connector_with(lambda req: httpx.Response(200, json=PROTECTED_ERROR))
    with pytest.raises(ProtectedDataDenied):
        asyncio.run(collect(conn))


def test_plain_access_denied_is_still_a_revoked_token():
    """The negative half. Without this, classifying everything as the new error
    would pass the test above while making the taxonomy useless."""
    conn = connector_with(lambda req: httpx.Response(200, json=UNINSTALLED_ERROR))
    with pytest.raises(AuthRevoked):
        asyncio.run(collect(conn))


def test_classification_keys_off_the_documentation_link_not_the_prose():
    """Shopify can reword a message; the doc URL is the stable signal. This body
    carries the link but a message that says nothing about customer data."""
    body = {
        "errors": [
            {
                "message": "Not approved for this object.",
                "extensions": {
                    "code": "ACCESS_DENIED",
                    "documentation": "https://shopify.dev/docs/apps/launch/protected-customer-data",
                },
            }
        ],
        "data": None,
        "extensions": THROTTLE,
    }
    conn = connector_with(lambda req: httpx.Response(200, json=body))
    with pytest.raises(ProtectedDataDenied):
        asyncio.run(collect(conn))


# --------------------------------------------------- 2 and 3. degradation

def test_denial_of_the_customer_field_degrades_instead_of_failing():
    calls = {"n": 0, "asked_for_customer": []}

    def handler(req):
        calls["n"] += 1
        body = json.loads(req.content)
        calls["asked_for_customer"].append("customer {" in body["query"])
        if calls["n"] == 1:
            return httpx.Response(200, json=PROTECTED_ERROR)
        return httpx.Response(200, json=ok([order_node("2026-08-01T10:00:00Z", "50.00", customer=None)]))

    conn = connector_with(handler)
    pages = asyncio.run(collect(conn))

    # The first attempt asked for the protected field, the retry did not.
    assert calls["asked_for_customer"] == [True, False]
    kinds = {p.metric_key for p in points_of(pages)}
    assert "orders" in kinds and "gross_sales" in kinds
    assert "customers" not in kinds, "a withheld metric must vanish, not read zero"
    assert conn.degraded_metrics == {"customers"}
    assert "customers" not in conn.available_metrics
    assert "customers" in conn.supported_metrics, (
        "the platform still supports it; only this app right now does not"
    )


def test_the_retry_is_latched_so_it_costs_one_call_not_one_per_page():
    """Two pages. A per-page try/except would spend a refused call on each."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        body = json.loads(req.content)
        if "customer {" in body["query"]:
            return httpx.Response(200, json=PROTECTED_ERROR)
        if state["n"] == 2:
            return httpx.Response(200, json={
                "data": {"orders": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "cur1"},
                    "nodes": [order_node("2026-08-01T10:00:00Z", "10.00", customer=None)],
                }},
                "extensions": THROTTLE,
            })
        return httpx.Response(200, json=ok([order_node("2026-08-02T10:00:00Z", "20.00", customer=None)]))

    conn = connector_with(handler)
    asyncio.run(collect(conn))
    # one refused + two successful pages. Three, never four.
    assert state["n"] == 3


def test_a_refusal_that_survives_stripping_propagates():
    """If the Order object itself is withheld there is nothing to fall back to,
    and returning an empty window would read as a shop with no sales."""
    conn = connector_with(lambda req: httpx.Response(200, json=PROTECTED_ERROR))
    with pytest.raises(ProtectedDataDenied):
        asyncio.run(collect(conn))


def test_customers_metric_survives_when_nothing_is_denied():
    """The control. Without it, a connector that always dropped the metric would
    pass every test above."""
    conn = connector_with(
        lambda req: httpx.Response(200, json=ok([
            order_node("2026-08-01T10:00:00Z", "50.00", customer="gid://shopify/Customer/7"),
            order_node("2026-08-01T11:00:00Z", "25.00", customer="gid://shopify/Customer/7"),
        ]))
    )
    pages = asyncio.run(collect(conn))
    customers = [p for p in points_of(pages) if p.metric_key == "customers"]
    assert len(customers) == 1
    assert customers[0].value_numeric == 1, "same buyer twice on one day is one customer"
    assert conn.degraded_metrics == set()
    assert "customers" in conn.available_metrics
