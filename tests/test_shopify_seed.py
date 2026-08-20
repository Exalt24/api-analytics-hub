"""Seeder tests, focused on the refusal guard.

The guard is the only part of a seeder that matters before it runs. Everything
else fails visibly; writing fake orders into the wrong store fails expensively
and quietly, and someone has to clean a merchant's books.

Precedent in this workspace: a script named like a test read `.env.local`, which
was production, and seeded fake data into a live app used by real families.
Nothing was lost by luck rather than design.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.shopify_seed import MAX_EXISTING_ORDERS, SeedRefused, ShopifySeeder


def responder(*, partner_dev=True, order_count=3, plan="Developer Preview"):
    """Answers the two preflight queries the way Shopify would."""

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        q = body.get("query", "")
        if "shop {" in q:
            return httpx.Response(200, json={"data": {"shop": {
                "name": "Test Dev Store",
                "myshopifyDomain": "dac-analytics-hub.myshopify.com",
                "currencyCode": "USD",
                "plan": {"displayName": plan, "partnerDevelopment": partner_dev,
                         "shopifyPlus": False},
            }}})
        if "ordersCount" in q:
            return httpx.Response(200, json={"data": {"ordersCount": {"count": order_count}}})
        return httpx.Response(200, json={"data": {}})

    return handler


def seeder_with(handler):
    return ShopifySeeder(
        "dac-analytics-hub.myshopify.com", "shpat_x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ---------------------------------------------------------------- the guard

def test_a_dev_store_with_few_orders_is_accepted():
    info = asyncio.run(seeder_with(responder()).preflight())
    assert info["partner_development"] is True
    assert info["existing_orders"] == 3
    assert info["domain"].endswith(".myshopify.com")


def test_a_non_dev_store_is_REFUSED():
    """The case that matters: this could be a real merchant's shop."""
    with pytest.raises(SeedRefused) as exc:
        asyncio.run(seeder_with(responder(partner_dev=False)).preflight())
    assert "partnerDevelopment" in str(exc.value)


def test_a_store_holding_lots_of_orders_is_REFUSED_even_if_it_claims_to_be_dev():
    """Two independent signals, because a single one can be wrong. A store with
    thousands of orders is not a fresh dev store whatever its plan says."""
    with pytest.raises(SeedRefused):
        asyncio.run(
            seeder_with(responder(order_count=MAX_EXISTING_ORDERS + 1)).preflight()
        )


def test_force_overrides_the_non_dev_refusal_deliberately():
    info = asyncio.run(seeder_with(responder(partner_dev=False)).preflight(force=True))
    assert info["partner_development"] is False


def test_force_also_overrides_the_order_count_refusal():
    info = asyncio.run(
        seeder_with(responder(order_count=99999)).preflight(force=True)
    )
    assert info["existing_orders"] == 99999


# ---------------------------------------------------------------- user errors

def test_a_userErrors_response_raises_instead_of_silently_creating_nothing():
    """Shopify reports business-rule failures with HTTP 200 and a userErrors
    array. A seeder that only checks the status code reports success having
    created nothing, which is the worst possible outcome: a green run and an
    empty store."""
    def handler(req):
        return httpx.Response(200, json={"data": {"productCreate": {
            "product": None,
            "userErrors": [{"field": "title", "message": "Title can't be blank"}],
        }}})

    s = seeder_with(handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(s.ensure_products())
    assert "userErrors" in str(exc.value)


def test_a_product_created_without_a_variant_is_an_error_not_a_skip():
    def handler(req):
        return httpx.Response(200, json={"data": {"productCreate": {
            "product": {"id": "gid://p/1", "title": "x", "variants": {"nodes": []}},
            "userErrors": [],
        }}})

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(seeder_with(handler).ensure_products())
    assert "no default variant" in str(exc.value)


def test_seeded_customer_emails_are_unroutable():
    """example.invalid can never deliver, so a seeder cannot email a stranger."""
    sent = []

    def handler(req):
        body = json.loads(req.content)
        if "CustomerCreate" in body.get("query", ""):
            sent.append(body["variables"]["input"]["email"])
            return httpx.Response(200, json={"data": {"customerCreate": {
                "customer": {"id": f"gid://c/{len(sent)}", "displayName": "x"},
                "userErrors": [],
            }}})
        return httpx.Response(200, json={"data": {}})

    asyncio.run(seeder_with(handler).ensure_customers())
    assert sent, "no customers were created"
    # RFC 2606 reserves these four names precisely so they can never resolve to a
    # real host, so any of them satisfies the guarantee. Pinning one literal made
    # this fail on a change that preserved the property, which is the test
    # measuring the implementation instead of the promise.
    RESERVED_DOMAINS = ("@example.com", "@example.net", "@example.org",
                        "@example.invalid", "@example.test")
    for email in sent:
        assert email.endswith(RESERVED_DOMAINS), f"{email} could reach a real inbox"
