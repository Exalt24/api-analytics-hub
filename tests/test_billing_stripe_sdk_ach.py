"""The bank debit (ACH) request, through the official `stripe` SDK, in two layers and neither one touches a database or api.stripe.com.

  1. stripe-mock (Stripe's own local mock server): the SDK's real request for a us_bank_account PaymentIntent is checked against Stripe's OpenAPI spec, and a wrongly named or wrongly
     typed parameter is refused by it. stripe-mock keeps no state and echoes only a few fields, so these tests check the SHAPE of the request and the mapping of the answer.
  2. The stand-in on the SDK's transport (fake_provider.py): the request the SDK actually put on the wire is read back (the form fields, the Idempotency-Key header, the key sent as the
     user of the auth header), a lost response is retried with the same key, and the answer `processing` maps to our intent.

stripe-mock tests skip when it is not running; CI starts it and sets STRIPE_MOCK_REQUIRE so a missing one FAILS instead.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import MOCK_API_KEY, STRIPE_MOCK_URL, needs_stripe_mock, sdk_provider_on  # noqa: E402

from app.billing import stripe_sdk  # noqa: E402
from app.billing.fake_provider import FakeStripe  # noqa: E402
from app.billing.provider import RequestRejected, bank_debit_params  # noqa: E402

META = {"tenant_id": "tenant-1", "payment_id": "pay-1", "invoice_id": "inv-1"}
ACH = dict(amount_minor=5000, currency="USD", payment_method="pm_fake_ach_ok", metadata=META, payment_method_type="us_bank_account", customer="cus_fake_1", mandate="mandate_fake_1")


def on_stand_in(coro_fn, retries=2):
    async def go():
        fake = FakeStripe()
        http = fake.client()
        try:
            return await coro_fn(fake, sdk_provider_on(http, retries=retries))
        finally:
            await http.aclose()
    return asyncio.run(go())


def on_mock(coro_fn):
    return asyncio.run(coro_fn(stripe_sdk.StripeSdkProvider(MOCK_API_KEY, api_base=STRIPE_MOCK_URL)))


# ---- stripe-mock ----------------------------------------------------------------------------------------------------------------------------

@needs_stripe_mock
def test_stripe_mock_accepts_the_bank_debit_request_and_the_answer_maps_to_our_intent():
    pi = on_mock(lambda p: p.create_payment_intent(idempotency_key="ach-1", **ACH))
    assert pi.id.startswith("pi_") and (pi.amount, pi.currency) == (5000, "USD") and isinstance(pi.status, str) and pi.replayed is False


@needs_stripe_mock
def test_stripe_mock_refuses_a_bank_debit_parameter_its_spec_does_not_have_so_the_spec_check_is_real():
    async def go(p):
        # the provider's own validation is what the test is about, so the request is sent by the SDK underneath with a parameter Stripe's spec does not have
        with pytest.raises(stripe_sdk.stripe.InvalidRequestError):
            await p._client.v1.payment_intents.create_async(params={"amount": 5000, "currency": "usd", "payment_method_types": ["us_bank_account"]}, options={"idempotency_key": "ach-typo"})
        return True
    assert on_mock(go)


@needs_stripe_mock
def test_stripe_mock_refuses_an_amount_of_the_wrong_type_on_a_bank_debit_and_it_is_a_rejection():
    async def go(p):
        with pytest.raises(stripe_sdk.stripe.InvalidRequestError):
            await p._client.v1.payment_intents.create_async(params={"amount": "five thousand", "currency": "usd", "payment_method_types": ["us_bank_account"]},
                                                           options={"idempotency_key": "ach-type"})
        return True
    assert on_mock(go)


@needs_stripe_mock
def test_stripe_mock_accepts_the_refund_of_a_debit_and_echoes_the_amount_and_the_intent():
    refund = on_mock(lambda p: p.create_refund(payment_intent_id="pi_123", amount_minor=1200, idempotency_key="ach-rf-1", metadata={"invoice_id": "inv-1"}))
    assert refund.id.startswith("re_") and (refund.amount, refund.payment_intent) == (1200, "pi_123")


# ---- the request as it goes on the wire ----------------------------------------------------------------------------------------------------

def test_the_sdk_sends_the_bank_debit_as_the_documented_form_with_the_customer_the_mandate_and_the_idempotency_key():
    async def go(fake, p):
        pi = await p.create_payment_intent(idempotency_key="ach-key-1", **ACH)
        return pi, fake.requests[-1], fake.payment_intents[pi.id]
    pi, req, stored = on_stand_in(go)
    sent = req["params"]
    assert req["method"] == "POST" and req["path"] == "/v1/payment_intents" and req["idempotency_key"] == "ach-key-1"
    assert sent["customer"] == "cus_fake_1" and sent["mandate"] == "mandate_fake_1" and sent["payment_method"] == "pm_fake_ach_ok"
    assert sent["amount"] == "5000" and sent["currency"] == "usd" and sent["confirm"] == "true" and sent["metadata"] == META
    assert "payment_method_types" not in sent                                       # stripe-mock's spec has no such parameter on a PaymentIntent create
    assert (pi.status, pi.currency, stored["status"]) == ("processing", "USD", "processing")      # a debit is processing when confirmed, never succeeded


def test_a_card_request_still_carries_automatic_payment_methods_and_none_of_the_bank_debit_fields():
    async def go(fake, p):
        await p.create_payment_intent(amount_minor=2500, currency="AUD", payment_method="pm_fake_ok", metadata=META, idempotency_key="card-1")
        return fake.requests[-1]["params"]
    sent = on_stand_in(go)
    assert "automatic_payment_methods" in sent and "customer" not in sent and "mandate" not in sent


def test_a_lost_response_to_the_bank_debit_is_retried_by_the_sdk_with_the_same_key_and_one_debit_exists():
    async def go(fake, p):
        fake.lose_response_after_execution(1)
        pi = await p.create_payment_intent(idempotency_key="ach-key-2", **ACH)
        return pi, fake.executions, len(fake.payment_intents), [r["idempotency_key"] for r in fake.requests if r["method"] == "POST"]
    pi, executions, intents, keys = on_stand_in(go)
    assert pi.status == "processing" and pi.replayed is True and executions == 1 and intents == 1 and keys == ["ach-key-2", "ach-key-2"]


def test_the_stand_in_refuses_what_stripe_refuses_for_a_bank_debit_and_the_sdk_error_is_a_rejection():
    async def go(fake, p):
        out = []
        for override in ({"payment_method": "pm_fake_ach_unknown"},):
            with pytest.raises(RequestRejected) as err:
                await p.create_payment_intent(idempotency_key="ach-bad-pm", **{**ACH, **override})
            out.append((err.value.status, err.value.code))
        # the SDK call underneath, without the customer and without the mandate, is refused by the stand-in as the provider refuses it
        for missing in ("customer", "mandate"):
            with pytest.raises(stripe_sdk.stripe.InvalidRequestError):
                params = {"amount": 5000, "currency": "usd", "payment_method": "pm_fake_ach_ok", "confirm": True, "payment_method_types": ["us_bank_account"],
                          "customer": "cus_1", "mandate": "m_1"}
                del params[missing]
                await p._client.v1.payment_intents.create_async(params=params, options={"idempotency_key": "ach-missing-" + missing})
        return out, len(fake.payment_intents)
    out, intents = on_stand_in(go)
    assert out == [(400, "resource_missing")] and intents == 0


def test_the_request_is_refused_here_before_anything_is_sent_when_a_debit_is_not_usd_or_lacks_a_customer_or_a_mandate():
    async def go(fake, p):
        for kw in ({"currency": "AUD"}, {"customer": None}, {"mandate": None}, {"payment_method_type": "wire"}):
            with pytest.raises(ValueError):
                await p.create_payment_intent(idempotency_key="k", **{**ACH, **kw})
        return len(fake.requests)
    assert on_stand_in(go) == 0
    assert bank_debit_params("card", "AUD", None, None) == {}
    assert bank_debit_params("us_bank_account", "usd", "cus_1", "m_1") == {"customer": "cus_1", "mandate": "m_1"}
