"""The partner client against the stand-in partner server (tests/fulfillment_stand_in.py), no database.

What each test pins: every attempt of one send carries the SAME Idempotency-Key; what is retried (429, 5xx, 409, a dropped connection, a lost answer) and what is not (any other 4xx); the waits
(exponential, the partner's Retry-After honoured, never above the cap); that the attempts are bounded; that a lost answer after the partner made the order still ends in ONE order at the partner; and
that nothing from a body reaches an exception message.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fulfillment_stand_in import StandIn  # noqa: E402

from app.fulfillment.client import PartnerClient  # noqa: E402
from app.fulfillment.errors import PartnerRejected, PartnerUnavailable  # noqa: E402

KEY = "partner-api-key-invented-for-tests"
SECRET = "partner-signing-secret-invented-for-tests"
REF = "ord_" + "ab12" * 6
KEY_1 = "sub-00000000-0000-0000-0000-000000000001"


def with_partner(fn, **client_kw):
    """Run `fn(stand_in, client, sleeps)` against a started stand-in; the client waits for nothing and records each wait."""
    async def go():
        stand_in = StandIn(KEY, SECRET).start()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
        http = httpx.AsyncClient(timeout=5)
        try:
            client = PartnerClient(http, stand_in.base_url, KEY, sleep=sleep, rand=lambda: 0.0, **client_kw)
            return await fn(stand_in, client, sleeps)
        finally:
            await http.aclose()
            stand_in.stop()
    return asyncio.run(go())


async def send(client, ref=REF, key=KEY_1, clinic="clinic-one"):
    return await client.submit_order(order_ref=ref, clinic_ref=clinic, idempotency_key=key)


def test_a_clean_send_is_one_request_with_the_key_the_bearer_token_and_only_the_two_fields():
    async def go(partner, client, sleeps):
        receipt = await send(client)
        return receipt, partner.requests, sleeps
    receipt, requests, sleeps = with_partner(go)
    assert (receipt.replayed, receipt.attempts) == (False, 1) and receipt.partner_order_id.startswith("po_") and sleeps == []
    only = requests[0]
    assert only["headers"]["Idempotency-Key"] == KEY_1 and only["headers"]["Authorization"] == "Bearer " + KEY and only["headers"]["Content-Type"].startswith("application/json")
    assert sorted(__import__("json").loads(only["body"])) == ["clinic_ref", "order_ref"]


@pytest.mark.parametrize("faults,delays", [(["429"], [2.0]), (["503", "500"], [0.5, 1.0]), (["409"], [2.0]), (["dropped", "dropped", "503"], [0.5, 1.0, 2.0])])
def test_a_transient_failure_is_retried_with_the_same_key_after_the_documented_wait_and_one_order_is_made(faults, delays):
    async def go(partner, client, sleeps):
        partner.fail_posts = list(faults)
        receipt = await send(client)
        return receipt, partner, sleeps
    receipt, partner, sleeps = with_partner(go)
    assert receipt.attempts == len(faults) + 1 and len(partner.orders) == 1
    assert {r["headers"]["Idempotency-Key"] for r in partner.requests} == {KEY_1} and len(partner.requests) == len(faults) + 1
    assert sleeps == delays, "the partner's own Retry-After when it sent one, else base * 2^n"


def test_the_wait_is_exponential_capped_and_follows_the_partners_retry_after():
    async def go(partner, client, sleeps):
        partner.retry_after = None
        partner.fail_posts = ["503"] * 6
        with pytest.raises(PartnerUnavailable):
            await send(client)
        return sleeps
    assert with_partner(go, max_attempts=6, base_delay=1.0, max_delay=5.0) == [1.0, 2.0, 4.0, 5.0, 5.0]

    async def hinted(partner, client, sleeps):
        partner.retry_after = "9999"
        partner.fail_posts = ["429"]
        await send(client)
        return sleeps
    assert with_partner(hinted, max_delay=30.0) == [30.0]


def test_the_attempts_are_bounded_and_running_out_is_unavailable_with_the_last_status_and_the_count():
    async def go(partner, client, sleeps):
        partner.fail_posts = ["503"] * 10
        with pytest.raises(PartnerUnavailable) as err:
            await send(client)
        return err.value, len(partner.requests), len(partner.orders), len(sleeps)
    err, requests, orders, waits = with_partner(go, max_attempts=3)
    assert (err.attempts, err.status, requests, orders, waits) == (3, 503, 3, 0, 2)


def test_nothing_answering_at_all_is_unavailable_with_no_status():
    async def go(partner, client, sleeps):
        partner.fail_posts = ["dropped"] * 4
        with pytest.raises(PartnerUnavailable) as err:
            await send(client)
        return err.value
    err = with_partner(go)
    assert (err.attempts, err.status) == (4, None)


@pytest.mark.parametrize("fault,status,code", [("400", 400, "invalid_order"), ("401", 401, "unauthorized"), ("422", 422, "duplicate_order")])
def test_a_permanent_4xx_is_never_retried_and_carries_the_partners_code(fault, status, code):
    async def go(partner, client, sleeps):
        partner.fail_posts = [fault]
        with pytest.raises(PartnerRejected) as err:
            await send(client)
        return err.value, len(partner.requests), sleeps
    err, requests, sleeps = with_partner(go)
    assert (err.status, err.code, err.attempts, requests, sleeps) == (status, code, 1, 1, [])


def test_an_answer_lost_after_the_partner_made_the_order_is_retried_and_ends_in_one_order():
    async def go(partner, client, sleeps):
        partner.fail_posts = ["lost"]
        receipt = await send(client)
        return receipt, partner
    receipt, partner = with_partner(go)
    assert receipt.replayed is True and receipt.attempts == 2 and len(partner.orders) == 1
    assert receipt.partner_order_id == partner.orders[REF]["partner_order_id"]


def test_the_same_key_with_another_order_is_the_partners_422_and_a_new_key_for_an_existing_order_is_its_duplicate_refusal():
    async def go(partner, client, sleeps):
        await send(client)
        with pytest.raises(PartnerRejected) as reused:
            await send(client, ref="ord_" + "cd34" * 6)
        with pytest.raises(PartnerRejected) as duplicate:
            await send(client, key="sub-00000000-0000-0000-0000-000000000002")
        again = await send(client)             # the control: the same key and body is simply answered again
        return reused.value, duplicate.value, again, len(partner.orders)
    reused, duplicate, again, orders = with_partner(go)
    assert (reused.code, duplicate.code, again.replayed, orders) == ("idempotency_key_reused", "duplicate_order", True, 1)


def test_a_yes_without_a_usable_partner_order_id_is_permanent_and_a_key_that_is_too_short_is_refused_before_sending():
    async def go(partner, client, sleeps):
        partner.fail_posts = ["bad_body"]
        with pytest.raises(PartnerRejected) as err:
            await send(client)
        with pytest.raises(ValueError):
            await send(client, key="short")
        return err.value, len(partner.requests)
    err, requests = with_partner(go)
    assert (err.code, err.attempts, requests) == ("bad_response", 1, 1)


def test_no_body_text_reaches_an_exception_message_even_when_the_partners_error_carries_some():
    """The contract's error body is {"code": ...}; a partner that sends a message with a name in it must not see that name in our exceptions, so only a code of a safe alphabet is kept."""
    async def go(partner, client, sleeps):
        original = partner._send

        def hostile(h, status, body, **kw):
            if status == 422:
                body = {"code": "Jane Roe, 12 Elm Street", "message": "Jane Roe 1980-01-01 diabetes"}
            return original(h, status, body, **kw)
        partner._send = hostile
        partner.fail_posts = ["422"]
        with pytest.raises(PartnerRejected) as err:
            await send(client)
        return err.value
    err = with_partner(go)
    assert err.code == "error" and "Jane" not in str(err) and "Elm" not in str(err) and "1980" not in str(err) and "diabetes" not in str(err)
