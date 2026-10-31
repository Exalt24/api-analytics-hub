"""The payment provider on the official `stripe` SDK (app/billing/stripe_sdk.py), in three layers, none of which touches a database or api.stripe.com.

  1. stripe-mock (Stripe's own local mock server): the SDK's real requests are checked against Stripe's OpenAPI spec, and a wrong key or a wrongly typed amount is
     refused by it. It keeps no state and echoes only a few fields, so these tests check the request shape and the mapping of the answers, not a payment's life.
  2. The stand-in on the SDK's transport (fake_provider.py): scripts a card decline, a 429, a 5xx and a lost response, and shows the real SDK sending the SAME
     Idempotency-Key on its own retries, which is what makes a retry safe.
  3. `verify_webhook` against `stripe.Webhook.construct_event`, signed by this repository's own HMAC code (signature.build_header), so two independent implementations of
     Stripe's documented scheme have to agree.

stripe-mock tests skip when it is not running; CI starts it and sets STRIPE_MOCK_REQUIRE so a missing one FAILS instead.
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import MOCK_API_KEY, STRIPE_MOCK_URL, needs_stripe_mock, sdk_provider_on  # noqa: E402

from app.billing import signature, stripe_sdk  # noqa: E402
from app.billing.errors import WebhookRejected  # noqa: E402
from app.billing.fake_provider import FakeStripe  # noqa: E402
from app.billing.provider import CardDeclined, OutcomeUnknown, ProviderUnavailable, RequestRejected  # noqa: E402

META = {"tenant_id": "tenant-1", "payment_id": "pay-1"}


def on_stand_in(coro_fn, retries=2):
    async def go():
        fake = FakeStripe()
        http = fake.client()
        try:
            return await coro_fn(fake, sdk_provider_on(http, retries=retries))
        finally:
            await http.aclose()
    return asyncio.run(go())


def on_mock(coro_fn, key=MOCK_API_KEY):
    return asyncio.run(coro_fn(stripe_sdk.StripeSdkProvider(key, api_base=STRIPE_MOCK_URL)))


async def create(provider, amount=2500, method="pm_fake_ok", key="key-1"):
    return await provider.create_payment_intent(amount_minor=amount, currency="AUD", payment_method=method, metadata=META, idempotency_key=key)


# ---- stripe-mock: the SDK's real requests against Stripe's own spec ------------------------------------------------------------------

@needs_stripe_mock
def test_stripe_mock_accepts_the_create_request_and_the_answer_maps_to_our_intent():
    pi = on_mock(lambda p: create(p, amount=2000, method="pm_card_visa"))
    assert pi.id.startswith("pi_") and (pi.amount, pi.currency) == (2000, "AUD") and isinstance(pi.status, str) and pi.replayed is False


@needs_stripe_mock
def test_stripe_mock_answers_a_retrieve_and_a_search_and_both_map():
    async def go(p):
        return await p.retrieve_payment_intent("pi_123"), await p.find_payment_intent("payment_id", "pay-1")
    got, found = on_mock(go)
    assert got.id == "pi_123" and got.amount > 0 and found is not None and found.id.startswith("pi_")


@needs_stripe_mock
def test_stripe_mock_accepts_a_refund_request_and_echoes_the_amount_and_the_intent():
    refund = on_mock(lambda p: p.create_refund(payment_intent_id="pi_123", amount_minor=500, idempotency_key="rf-1", metadata={"payment_id": "pay-1"}))
    assert refund.id.startswith("re_") and (refund.amount, refund.payment_intent) == (500, "pi_123") and refund.status == "succeeded"


@needs_stripe_mock
def test_stripe_mock_refuses_a_key_it_does_not_recognise_and_the_sdk_error_becomes_a_rejection():
    with pytest.raises(RequestRejected) as exc:
        on_mock(lambda p: create(p), key="not-a-key")
    assert exc.value.status == 401


@needs_stripe_mock
def test_stripe_mock_refuses_an_amount_of_the_wrong_type_and_the_sdk_error_becomes_a_rejection_that_is_not_retried():
    with pytest.raises(RequestRejected) as exc:
        on_mock(lambda p: create(p, amount="not a number"))
    assert exc.value.status == 400 and exc.value.error_type == "invalid_request_error" and "amount" in exc.value.message


# ---- the stand-in on the SDK's transport: the real SDK's retries and errors ---------------------------------------------------------

def test_the_create_request_carries_the_key_as_a_bearer_token_a_form_body_and_our_idempotency_key():
    async def go(fake, p):
        pi = await create(p)
        return pi, fake
    pi, fake = on_stand_in(go)
    post = fake.requests[0]
    assert post["method"] == "POST" and post["path"] == "/v1/payment_intents" and post["idempotency_key"] == "key-1"
    assert post["headers"]["authorization"].startswith("Bearer ") and fake.payment_intents[pi.id]["metadata"] == META
    assert (pi.amount, pi.currency, pi.succeeded, pi.replayed) == (2500, "AUD", True, False)


def test_a_card_decline_is_a_clear_answer_with_the_providers_codes_and_is_never_retried():
    async def go(fake, p):
        with pytest.raises(CardDeclined) as exc:
            await create(p, method="pm_fake_declined")
        return exc.value, fake
    err, fake = on_stand_in(go)
    assert (err.code, err.decline_code, err.status) == ("card_declined", "insufficient_funds", 402) and err.payment_intent_id.startswith("pi_")
    assert len([r for r in fake.requests if r["method"] == "POST"]) == 1 and fake.charges == []


def test_the_sdk_retries_a_5xx_with_the_same_key_and_the_provider_charges_once():
    async def go(fake, p):
        fake.fail_before_execution(500, 503)
        pi = await create(p)
        return pi, fake
    pi, fake = on_stand_in(go)
    posts = [r for r in fake.requests if r["method"] == "POST"]
    assert len(posts) == 3 and {r["idempotency_key"] for r in posts} == {"key-1"} and (fake.executions, len(fake.charges)) == (1, 1) and pi.succeeded


def test_a_429_is_not_retried_by_the_sdk_so_it_is_provider_unavailable_with_nothing_created_and_asking_again_with_the_same_key_charges_once():
    async def go(fake, p):
        fake.fail_before_execution(429)
        with pytest.raises(ProviderUnavailable):
            await create(p)
        before = (fake.executions, len(fake.requests))
        pi = await create(p)
        return before, pi, fake
    before, pi, fake = on_stand_in(go)
    assert before == (0, 1) and pi.succeeded and (fake.executions, len(fake.charges)) == (1, 1) and {r["idempotency_key"] for r in fake.requests} == {"key-1"}


def test_server_errors_that_outlast_the_retries_are_an_unknown_outcome_never_a_new_attempt_with_a_new_key():
    async def go(fake, p):
        fake.fail_before_execution(500, 500, 500)
        with pytest.raises(OutcomeUnknown):
            await create(p)
        return fake
    fake = on_stand_in(go)
    assert {r["idempotency_key"] for r in fake.requests if r["method"] == "POST"} == {"key-1"}


def test_a_response_lost_after_the_request_ran_is_retried_with_the_same_key_and_the_provider_still_charges_once():
    async def go(fake, p):
        fake.lose_response_after_execution(1)
        pi = await create(p)
        return pi, fake
    pi, fake = on_stand_in(go)
    assert (fake.executions, len(fake.charges)) == (1, 1) and pi.replayed is True and pi.succeeded


def test_when_every_answer_is_lost_the_intent_is_found_by_its_metadata_and_a_lagging_search_leaves_it_unknown():
    async def found(fake, p):
        fake.lose_response_after_execution(3)
        return await create(p), fake
    pi, fake = on_stand_in(found)
    assert pi.id in fake.payment_intents and len(fake.charges) == 1

    async def lagging(fake, p):
        fake.lose_response_after_execution(3)
        fake.search_lag = True
        with pytest.raises(OutcomeUnknown):
            await create(p)
        return fake
    fake = on_stand_in(lagging)
    assert len(fake.charges) == 1 and {r["idempotency_key"] for r in fake.requests if r["method"] == "POST"} == {"key-1"}


def test_the_same_key_with_different_parameters_is_refused_by_the_provider_and_mapped_to_a_rejection():
    async def go(fake, p):
        await create(p, amount=2500)
        with pytest.raises(RequestRejected) as exc:
            await create(p, amount=2600)
        return exc.value, fake
    err, fake = on_stand_in(go)
    assert err.error_type == "idempotency_error" and err.status == 400 and len(fake.charges) == 1


def test_a_refund_with_the_same_key_twice_refunds_once_and_a_refund_over_the_unrefunded_amount_is_refused():
    async def go(fake, p):
        pi = await create(p, amount=2000)
        first = await p.create_refund(payment_intent_id=pi.id, amount_minor=500, idempotency_key="rf-1")
        again = await p.create_refund(payment_intent_id=pi.id, amount_minor=500, idempotency_key="rf-1")
        with pytest.raises(RequestRejected) as exc:
            await p.create_refund(payment_intent_id=pi.id, amount_minor=5000, idempotency_key="rf-2")
        return first, again, exc.value, fake
    first, again, err, fake = on_stand_in(go)
    assert (first.amount, first.replayed, again.id, again.replayed) == (500, False, first.id, True) and len(fake.refunds) == 1 and err.code == "amount_too_large"


def test_an_unknown_intent_is_a_rejection_and_a_search_value_with_a_quote_or_a_key_of_the_wrong_length_is_refused_before_anything_is_sent():
    async def go(fake, p):
        with pytest.raises(RequestRejected) as exc:
            await p.retrieve_payment_intent("pi_nope")
        sent = len(fake.requests)
        for call in (lambda: p.find_payment_intent("payment_id", 'a"b'), lambda: create(p, key=""), lambda: create(p, key="k" * 256),
                     lambda: p.create_refund(payment_intent_id="pi_1", amount_minor=1, idempotency_key="")):
            with pytest.raises(ValueError):
                await call()
        return exc.value, sent, len(fake.requests)
    err, sent, after = on_stand_in(go)
    assert err.status == 404 and err.code == "resource_missing" and after == sent


# ---- the webhook check: stripe.Webhook.construct_event, signed by our own HMAC code ----------------------------------------------------

SECRET, OTHER = "whsec_invented_for_tests_one", "whsec_invented_for_tests_two"
BODY = b'{"id":"evt_1","object":"event","type":"payment_intent.succeeded","data":{"object":{"id":"pi_1"}}}'


def header(payload=BODY, secrets=(SECRET,), ts=None):
    return signature.build_header(payload, secrets, int(time.time()) if ts is None else ts)


def rejected(payload=BODY, hdr="", secrets=(SECRET,), **kw):
    with pytest.raises(WebhookRejected) as exc:
        stripe_sdk.verify_webhook(payload, hdr, secrets, **kw)
    return exc.value


def test_a_header_built_by_our_own_code_the_documented_way_passes_the_sdks_check():
    assert stripe_sdk.verify_webhook(BODY, header(), (SECRET,)) is None


def test_the_rejection_carries_a_reason_for_our_log_and_the_message_never_names_the_check():
    err = rejected(BODY, header(secrets=(OTHER,)))
    assert str(err) == "webhook rejected" and "signature" in err.reason.lower()


@pytest.mark.parametrize("seed", range(8))
def test_a_seeded_tamper_of_any_byte_of_the_body_or_the_header_is_rejected(seed):
    rng = random.Random(seed)
    body = json.dumps({"id": "evt_%d" % seed, "pad": "".join(rng.choice("abcdef0123456789 ") for _ in range(rng.randrange(20, 120)))}).encode()
    good = header(body)
    assert stripe_sdk.verify_webhook(body, good, (SECRET,)) is None
    i = rng.randrange(len(body))
    rejected(body[:i] + bytes([body[i] ^ 1]) + body[i + 1:], good)
    ts, sig = good.split(",")
    k = rng.randrange(len(sig) - 3) + 3
    flipped = sig[:k] + ("0" if sig[k] != "0" else "1") + sig[k + 1:]
    rejected(body, "%s,%s" % (ts, flipped))


def test_a_wrong_secret_a_reserialised_body_and_a_missing_or_malformed_header_are_rejected():
    rejected(BODY, header(secrets=(OTHER,)))
    rejected(BODY.replace(b",", b", "), header())
    for bad in (None, "", "t=1", "v1=00", "garbage", "t=abc,v1=00", "t=%d,v0=%s" % (int(time.time()), signature.expected_signature(BODY, SECRET, int(time.time())))):
        rejected(BODY, bad)
    rejected(BODY, header(), ())


def test_an_old_timestamp_is_rejected_even_when_the_signature_over_it_is_right_and_the_window_is_the_one_asked_for():
    now = int(time.time())
    rejected(BODY, header(ts=now - 3600))
    assert stripe_sdk.verify_webhook(BODY, header(ts=now - 3600), (SECRET,), tolerance=7200) is None
    rejected(BODY, header(ts=now - 100), tolerance=10)


def test_a_tolerance_of_zero_or_less_is_refused_because_the_sdk_would_read_it_as_no_recency_check():
    for tol in (0, -5):
        with pytest.raises(ValueError):
            stripe_sdk.verify_webhook(BODY, header(), (SECRET,), tolerance=tol)
    # the reason for the guard, shown: the SDK itself accepts a day-old event at tolerance 0
    import stripe
    stripe.Webhook.construct_event(BODY, header(ts=int(time.time()) - 86400), SECRET, tolerance=0)


def test_during_a_secret_rotation_either_secret_verifies_and_a_header_signed_by_both_passes_either():
    both = header(secrets=(SECRET, OTHER))
    assert stripe_sdk.verify_webhook(BODY, both, (OTHER,)) is None and stripe_sdk.verify_webhook(BODY, both, (SECRET,)) is None
    assert stripe_sdk.verify_webhook(BODY, header(secrets=(OTHER,)), (SECRET, OTHER)) is None


def test_a_correctly_signed_body_that_is_not_json_is_a_rejection_and_a_non_ascii_header_is_too():
    raw = b"not json at all"
    assert rejected(raw, header(raw)).reason == "body is not JSON"
    rejected(BODY, "t=%d,v1=éé" % int(time.time()))
