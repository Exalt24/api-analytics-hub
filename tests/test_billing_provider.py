"""The provider client against the fake Stripe: the request shape the saved pages describe, retry with the SAME idempotency key on 429 and 5xx, a card error
recorded with the provider's code and never retried, and the double charge trap (a response lost AFTER the request ran) ending in exactly one charge.
Pure: an httpx MockTransport and a recording sleep, so nothing waits and nothing touches a network or a database."""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.billing.fake_provider import FAKE_API_KEY, FakeStripe  # noqa: E402
from app.billing.provider import (CardDeclined, OutcomeUnknown, ProviderUnavailable, RequestRejected, StripeStyleClient, form_fields)  # noqa: E402

META = {"tenant_id": "tenant-1", "payment_id": "pay-1"}


def rig(max_attempts=4):
    fake = FakeStripe()
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    http = fake.client()
    return fake, StripeStyleClient(http, FAKE_API_KEY, sleep=sleep, max_attempts=max_attempts), sleeps, http


def run(coro_fn, **kw):
    async def go():
        fake, client, sleeps, http = rig(**kw)
        try:
            return await coro_fn(fake, client, sleeps)
        finally:
            await http.aclose()
    return asyncio.run(go())


async def create(client, key="key-1", amount=2000, method="pm_fake_ok", meta=META):
    return await client.create_payment_intent(amount_minor=amount, currency="AUD", payment_method=method, metadata=dict(meta), idempotency_key=key)


# ---- the request the saved pages describe ---------------------------------------------------------------------------------------

def test_a_create_is_a_form_post_with_basic_auth_a_lowercase_currency_nested_metadata_and_the_idempotency_key_header():
    async def go(fake, client, sleeps):
        await create(client)
        return fake.requests[0]
    r = run(go)
    assert (r["method"], r["path"], r["idempotency_key"]) == ("POST", "/v1/payment_intents", "key-1")
    assert r["headers"]["content-type"] == "application/x-www-form-urlencoded"
    user = base64.b64decode(r["headers"]["authorization"].split()[1]).decode().split(":")[0]
    assert user == FAKE_API_KEY


def test_the_fake_intent_holds_what_was_sent_the_amount_the_lowercase_currency_and_the_metadata():
    async def go(fake, client, sleeps):
        pi = await create(client, amount=2500)
        return pi, fake.payment_intents[pi.id]
    pi, stored = run(go)
    assert (stored["amount"], stored["currency"], stored["metadata"]) == (2500, "aud", META)
    assert (pi.amount, pi.currency, pi.status, pi.succeeded) == (2500, "AUD", "succeeded", True)


def test_form_fields_write_nested_values_the_way_the_stripe_examples_do_and_booleans_as_words():
    assert form_fields({"amount": 5, "confirm": True, "x": False, "metadata": {"a": "b"}, "skip": None}) == [("amount", "5"), ("confirm", "true"), ("x", "false"), ("metadata[a]", "b")]


def test_a_get_never_carries_an_idempotency_key_and_a_post_only_ever_carries_one_it_was_given():
    async def go(fake, client, sleeps):
        pi = await create(client)
        await client.retrieve_payment_intent(pi.id)
        await client.find_payment_intent("payment_id", "pay-1")
        return fake.requests
    reqs = run(go)
    assert [r["idempotency_key"] for r in reqs] == ["key-1", None, None]
    assert [r["method"] for r in reqs] == ["POST", "GET", "GET"]


def test_an_idempotency_key_on_a_get_or_longer_than_255_characters_is_refused_before_anything_is_sent():
    async def go(fake, client, sleeps):
        with pytest.raises(ValueError):
            await client._send("GET", "/v1/payment_intents/x", idempotency_key="k")
        with pytest.raises(ValueError):
            await create(client, key="k" * 256)
        with pytest.raises(ValueError):
            await create(client, key="")
        return fake.requests
    assert run(go) == []


def test_retrieve_and_search_find_the_intent_by_id_and_by_metadata():
    async def go(fake, client, sleeps):
        pi = await create(client)
        return pi, await client.retrieve_payment_intent(pi.id), await client.find_payment_intent("payment_id", "pay-1"), await client.find_payment_intent("payment_id", "nope")
    pi, got, found, missing = run(go)
    assert got == pi and found.id == pi.id and missing is None


def test_a_search_value_with_a_quote_or_a_backslash_is_refused_rather_than_sent_as_a_broken_query():
    async def go(fake, client, sleeps):
        for bad in ('a"b', "a\\b"):
            with pytest.raises(ValueError):
                await client.find_payment_intent("payment_id", bad)
    run(go)


# ---- retries: same key, backoff, and what is not retried ----------------------------------------------------------------------------

def test_a_429_is_retried_with_the_same_key_and_the_provider_charges_once():
    async def go(fake, client, sleeps):
        fake.fail_before_execution(429, 429)
        pi = await create(client)
        return pi, fake, sleeps
    pi, fake, sleeps = run(go)
    posts = [r for r in fake.requests if r["method"] == "POST"]
    assert len(posts) == 3 and {r["idempotency_key"] for r in posts} == {"key-1"}
    assert (fake.executions, len(fake.charges)) == (1, 1) and pi.succeeded
    assert len(sleeps) == 2


def test_a_5xx_is_retried_with_the_same_key_and_a_retry_after_header_is_honoured():
    async def go(fake, client, sleeps):
        fake.fail_before_execution(500, 503, 429)
        await create(client)
        return fake, sleeps
    fake, sleeps = run(go)
    assert {r["idempotency_key"] for r in fake.requests} == {"key-1"} and len(fake.requests) == 4
    assert sleeps[2] == 0.0           # the scripted 429 says Retry-After: 0, and that is what is waited


def test_backoff_doubles_from_the_base_delay_and_is_capped():
    async def go(fake, client, sleeps):
        fake.fail_before_execution(500, 500, 500)
        await create(client)
        return sleeps
    assert run(go) == [0.5, 1.0, 2.0]


def test_when_only_429s_come_back_it_gives_up_as_unavailable_because_nothing_was_created():
    async def go(fake, client, sleeps):
        fake.fail_before_execution(429, 429, 429, 429)
        with pytest.raises(ProviderUnavailable):
            await create(client)
        return fake
    fake = run(go)
    assert fake.executions == 0 and fake.charges == []


def test_a_card_error_is_a_clear_no_with_the_providers_code_and_is_never_retried():
    async def go(fake, client, sleeps):
        with pytest.raises(CardDeclined) as exc:
            await create(client, method="pm_fake_declined")
        return exc.value, fake, sleeps
    err, fake, sleeps = run(go)
    assert (err.code, err.decline_code, err.status) == ("card_declined", "insufficient_funds", 402)
    assert err.payment_intent_id and err.payment_intent_id in fake.payment_intents
    assert len(fake.requests) == 1 and sleeps == [] and fake.charges == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_another_4xx_is_a_rejection_that_is_not_retried(status):
    async def go(fake, client, sleeps):
        def handler(request):
            return httpx.Response(status, json={"error": {"type": "invalid_request_error", "code": "x_code", "message": "no"}})
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(RequestRejected) as exc:
            await create(client)
        return exc.value, sleeps
    err, sleeps = run(go)
    assert (err.status, err.code) == (status, "x_code") and sleeps == []


def test_the_same_key_with_different_parameters_is_the_providers_idempotency_error_and_is_not_retried():
    async def go(fake, client, sleeps):
        await create(client, amount=2000)
        with pytest.raises(RequestRejected) as exc:
            await create(client, amount=3000)
        return exc.value, fake
    err, fake = run(go)
    assert err.error_type == "idempotency_error" and fake.executions == 1 and len(fake.charges) == 1


def test_stripe_should_retry_false_stops_a_retry_even_on_a_5xx():
    async def go(fake, client, sleeps):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(500, json={"error": {"type": "api_error", "message": "stop"}}, headers={"Stripe-Should-Retry": "false"})
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(RequestRejected):
            await create(client)
        return calls
    assert len(run(go)) == 1


# ---- the double charge trap -----------------------------------------------------------------------------------------------------------

def test_a_response_lost_after_the_request_ran_is_retried_with_the_same_key_and_there_is_exactly_one_charge():
    async def go(fake, client, sleeps):
        fake.lose_response_after_execution(1)
        pi = await create(client)
        return pi, fake
    pi, fake = run(go)
    posts = [r for r in fake.requests if r["method"] == "POST"]
    assert len(posts) == 2 and {r["idempotency_key"] for r in posts} == {"key-1"}
    assert fake.executions == 1 and len(fake.charges) == 1
    assert pi.succeeded and pi.replayed                  # the second answer was the saved first one (Idempotent-Replayed: true)


def test_timeouts_on_every_attempt_search_by_metadata_before_ever_creating_again_and_find_the_one_intent():
    async def go(fake, client, sleeps):
        fake.lose_response_after_execution(4)
        pi = await create(client)
        return pi, fake
    pi, fake = run(go)
    assert fake.executions == 1 and len(fake.charges) == 1
    assert pi.metadata == META and pi.id in fake.payment_intents


def test_when_search_lags_and_every_attempt_timed_out_the_outcome_is_unknown_and_nothing_new_is_created():
    async def go(fake, client, sleeps):
        fake.search_lag = True
        fake.lose_response_after_execution(4)
        with pytest.raises(OutcomeUnknown):
            await create(client)
        before = (fake.executions, len(fake.charges))
        # settling it later with the SAME key returns the saved answer and creates nothing
        pi = await create(client)
        return before, (fake.executions, len(fake.charges)), pi
    before, after, pi = run(go)
    assert before == after == (1, 1) and pi.replayed


def test_timeout_then_retry_over_many_seeded_failure_patterns_never_makes_a_second_charge():
    import random
    rng = random.Random(7)
    for _ in range(40):
        async def go(fake, client, sleeps):
            for key in ("k-a", "k-b"):
                for _ in range(rng.randrange(0, 4)):
                    rng.choice([lambda: fake.lose_response_after_execution(1), lambda: fake.fail_before_execution(rng.choice([429, 500, 502]))])()
                try:
                    await create(client, key=key, meta={"payment_id": key})
                except (OutcomeUnknown, ProviderUnavailable):
                    fake._before.clear()
                    fake._lose = 0
                    await create(client, key=key, meta={"payment_id": key})
            return fake
        fake = run(go)
        assert len(fake.charges) == 2 and fake.executions == 2


def test_a_connect_error_before_anything_was_sent_is_unavailable_and_not_an_unknown_outcome():
    async def go(fake, client, sleeps):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderUnavailable):
            await create(client)
    run(go)


def test_a_read_timeout_on_every_attempt_with_no_search_hit_is_an_unknown_outcome():
    async def go(fake, client, sleeps):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(OutcomeUnknown):
            await create(client)
    run(go)


# ---- refunds ------------------------------------------------------------------------------------------------------------------------------

def test_a_refund_is_a_post_with_its_own_key_and_the_same_key_twice_refunds_once():
    async def go(fake, client, sleeps):
        pi = await create(client)
        a = await client.create_refund(payment_intent_id=pi.id, amount_minor=500, idempotency_key="rf-1")
        b = await client.create_refund(payment_intent_id=pi.id, amount_minor=500, idempotency_key="rf-1")
        return a, b, fake
    a, b, fake = run(go)
    assert a.id == b.id and b.replayed and not a.replayed
    assert len(fake.refunds) == 1 and fake.refunds[a.id]["amount"] == 500


def test_a_refund_over_the_remaining_amount_is_refused_by_the_provider_and_not_retried():
    async def go(fake, client, sleeps):
        pi = await create(client)
        await client.create_refund(payment_intent_id=pi.id, amount_minor=1500, idempotency_key="rf-1")
        with pytest.raises(RequestRejected) as exc:
            await client.create_refund(payment_intent_id=pi.id, amount_minor=600, idempotency_key="rf-2")
        return exc.value
    assert run(go).code == "amount_too_large"


def test_a_refund_response_lost_after_it_ran_is_retried_with_its_key_and_refunds_once():
    async def go(fake, client, sleeps):
        pi = await create(client)
        fake.lose_response_after_execution(1)
        r = await client.create_refund(payment_intent_id=pi.id, amount_minor=700, idempotency_key="rf-9")
        return r, fake
    r, fake = run(go)
    assert r.replayed and len(fake.refunds) == 1 and sum(x["amount"] for x in fake.refunds.values()) == 700
