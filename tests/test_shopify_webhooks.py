"""Webhook tests. The ordering tests are the ones that matter.

"Verified before deduplicated" and "handler errors propagate" are both invisible
in normal operation and both lose data or admit forgery when wrong.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.shopify_webhooks import (
    ANALYTICS_TOPICS,
    DeliveryLog,
    DuplicateDelivery,
    WebhookRejected,
    WebhookReceiver,
    verify_signature,
)

SECRET = "shpss_topsecret"


def sign(raw: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode()


def headers_for(raw: bytes, *, topic="orders/create", delivery_id="d-1", secret=SECRET):
    return {
        "X-Shopify-Hmac-Sha256": sign(raw, secret),
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": "dac-analytics-hub.myshopify.com",
        "X-Shopify-Webhook-Id": delivery_id,
        "X-Shopify-Api-Version": "2026-07",
    }


BODY = json.dumps({"id": 12345, "total_price": "19.99"}, separators=(",", ":")).encode()


# ---------------------------------------------------------------- signature

def test_a_correct_signature_verifies():
    verify_signature(BODY, headers_for(BODY), SECRET)


def test_a_wrong_secret_is_rejected():
    with pytest.raises(WebhookRejected):
        verify_signature(BODY, headers_for(BODY, secret="wrong"), SECRET)


def test_a_missing_signature_header_is_rejected():
    with pytest.raises(WebhookRejected):
        verify_signature(BODY, {"X-Shopify-Topic": "orders/create"}, SECRET)


def test_a_truncated_signature_is_rejected():
    h = headers_for(BODY)
    h["X-Shopify-Hmac-Sha256"] = h["X-Shopify-Hmac-Sha256"][:10]
    with pytest.raises(WebhookRejected):
        verify_signature(BODY, h, SECRET)


def test_a_non_ascii_signature_header_is_REJECTED_not_a_500():
    """The real hazard, found by a mutation that would not go red 2026-08-20.

    compare_digest returns False on a length mismatch, so the length check was
    never the safety property I claimed. What it DOES raise on is non-ASCII, so a
    single UTF-8 byte in this header turns a rejection into an unhandled
    TypeError: a free denial of service, and one that hides the attempt from
    anything counting rejections.
    """
    h = headers_for(BODY)
    h["X-Shopify-Hmac-Sha256"] = "abcé" + "A" * 40
    with pytest.raises(WebhookRejected):
        verify_signature(BODY, h, SECRET)


def test_an_empty_signature_header_is_rejected():
    h = headers_for(BODY)
    h["X-Shopify-Hmac-Sha256"] = ""
    with pytest.raises(WebhookRejected):
        verify_signature(BODY, h, SECRET)


def test_reserialised_json_does_NOT_verify():
    """THE canonical Shopify webhook bug, pinned.

    Parsing and re-dumping changes separators and key order, the digest changes,
    and it presents as a wrong secret. Someone then rotates the secret, which
    does not help, and loses an afternoon.
    """
    reserialised = json.dumps(json.loads(BODY)).encode()  # note: default separators
    assert reserialised != BODY, "fixture must actually differ to prove anything"
    with pytest.raises(WebhookRejected):
        verify_signature(reserialised, headers_for(BODY), SECRET)


def test_a_string_body_is_refused_at_the_type_level():
    """Accepting str would invite exactly the re-serialisation bug above."""
    with pytest.raises(TypeError):
        verify_signature(BODY.decode(), headers_for(BODY), SECRET)


def test_headers_are_read_case_insensitively():
    lowered = {k.lower(): v for k, v in headers_for(BODY).items()}
    verify_signature(BODY, lowered, SECRET)


# ---------------------------------------------------------------- ordering

def test_verification_happens_BEFORE_the_delivery_is_recorded():
    """Otherwise an attacker records the id of an event they know is coming, and
    the genuine delivery is then discarded as a duplicate. Cache poisoning that
    causes silent data loss.
    """
    async def handler(topic, parsed, headers):
        pass

    rx = WebhookReceiver(secret=SECRET, handler=handler)
    forged = headers_for(BODY, delivery_id="d-poison", secret="wrong")

    with pytest.raises(WebhookRejected):
        asyncio.run(rx.handle(BODY, forged, json.loads(BODY)))

    assert not rx.log.seen("d-poison"), "an unverified delivery id was recorded"

    # And the genuine delivery with that id still gets through.
    genuine = headers_for(BODY, delivery_id="d-poison")
    assert asyncio.run(rx.handle(BODY, genuine, json.loads(BODY))) == "orders/create"


def test_a_repeated_delivery_id_is_a_duplicate():
    calls = []

    async def handler(topic, parsed, headers):
        calls.append(topic)

    rx = WebhookReceiver(secret=SECRET, handler=handler)
    h = headers_for(BODY, delivery_id="d-42")
    asyncio.run(rx.handle(BODY, h, json.loads(BODY)))
    with pytest.raises(DuplicateDelivery):
        asyncio.run(rx.handle(BODY, h, json.loads(BODY)))
    assert calls == ["orders/create"], "the handler must run exactly once"


def test_the_same_order_updated_twice_is_NOT_treated_as_a_duplicate():
    """Keying on the resource id would silently drop the second real update.
    Shopify sends a distinct webhook id per delivery, which is the correct key."""
    calls = []

    async def handler(topic, parsed, headers):
        calls.append(parsed["id"])

    rx = WebhookReceiver(secret=SECRET, handler=handler)
    asyncio.run(rx.handle(BODY, headers_for(BODY, delivery_id="d-1"), json.loads(BODY)))
    asyncio.run(rx.handle(BODY, headers_for(BODY, delivery_id="d-2"), json.loads(BODY)))
    assert calls == [12345, 12345], "two deliveries of one order must both be handled"


def test_a_handler_error_PROPAGATES_so_shopify_retries():
    """Returning 200 cancels the retry and loses the event permanently. Shopify
    retries over roughly 48 hours and that is the only safety net."""
    async def boom(topic, parsed, headers):
        raise RuntimeError("database down")

    rx = WebhookReceiver(secret=SECRET, handler=boom)
    with pytest.raises(RuntimeError, match="database down"):
        asyncio.run(rx.handle(BODY, headers_for(BODY), json.loads(BODY)))


def test_a_failed_handler_still_marks_the_delivery_seen_which_is_a_known_tradeoff():
    """Documenting the behaviour rather than pretending it is not a choice.

    The id is recorded before dispatch, so a retry after a handler failure is
    treated as a duplicate. That is the safe direction for an ANALYTICS sync,
    where a missed increment is corrected by the next scheduled run, and it would
    be the wrong direction for something like payment capture. Stated here so the
    tradeoff is visible instead of surprising.
    """
    async def boom(topic, parsed, headers):
        raise RuntimeError("nope")

    rx = WebhookReceiver(secret=SECRET, handler=boom)
    h = headers_for(BODY, delivery_id="d-fail")
    with pytest.raises(RuntimeError):
        asyncio.run(rx.handle(BODY, h, json.loads(BODY)))
    assert rx.log.seen("d-fail")


# ---------------------------------------------------------------- memory bound

def test_the_delivery_log_is_bounded_and_evicts_oldest_first():
    log = DeliveryLog(capacity=3)
    for i in range(5):
        log.record(f"d-{i}")
    assert len(log) == 3
    assert not log.seen("d-0"), "oldest should have been evicted"
    assert log.seen("d-4")


def test_reseeing_an_id_refreshes_it_rather_than_duplicating():
    log = DeliveryLog(capacity=2)
    log.record("a")
    log.record("b")
    log.record("a")          # refresh a
    log.record("c")          # should evict b, not a
    assert log.seen("a")
    assert log.seen("c")
    assert not log.seen("b")


def test_the_analytics_topic_list_covers_the_ones_that_cause_silent_drift():
    for required in ("orders/create", "orders/updated", "refunds/create", "app/uninstalled"):
        assert required in ANALYTICS_TOPICS, f"{required} missing from the reasoned topic list"
