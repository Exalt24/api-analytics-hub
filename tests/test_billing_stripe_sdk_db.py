"""The webhook receiver and the payment commands with the official stripe SDK in the two places it now sits: `stripe.Webhook.construct_event` as the verifier, and the SDK as the
provider, against real Postgres. Both directions, as the billing rules need them: a good signature credits once; a bad or stale signature, a replayed event id, an event
for another amount and an event type we do not act on credit nothing more.

Most tests put the stand-in (fake_provider.py) under the SDK; the last two run the SDK against stripe-mock, with an event signed by this repository's own HMAC code and
delivered to the receiver, because stripe-mock sends no webhooks. Nothing here reaches api.stripe.com.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import (CURRENCY, MOCK_API_KEY, SECRETS, STRIPE_MOCK_URL, counts, needs_db, needs_stripe_mock, new_tenants, reset_billing_schema,  # noqa: E402
                              topup_via_provider, with_rig)

pytestmark = needs_db

from app.billing import payments, signature, stripe_sdk, webhooks  # noqa: E402
from app.billing.fake_provider import FAKE_WEBHOOK_SECRET_NEW, Events  # noqa: E402

NOW = int(time.time())


class World:
    pass


@pytest.fixture()
def world():
    reset_billing_schema()
    w = World()
    w.a, w.b = new_tenants("tenant-a", "tenant-b")
    return w


async def deliver(rig, event, **kw):
    body, header = rig.events.signed(event)
    return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify, **kw)


async def asked(rig, tenant, amount=2000, key="pay-1"):
    """A top-up the SDK provider asked for, not yet credited (its event is not delivered). Returns the payment intent the stand-in holds."""
    res, _ = await topup_via_provider(rig, tenant, amount, key, deliver=False)
    return res, rig.fake.payment_intents[res.provider_payment_id]


# ---- a good signature credits once --------------------------------------------------------------------------------------------------

def test_a_good_signature_credits_the_wallet_once_and_a_replay_of_the_same_event_id_writes_nothing(world):
    async def go(rig):
        res, pi = await asked(rig, world.a)
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", pi))
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        mid = await counts(rig.db, world.a)
        again = [await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify) for _ in range(3)]
        return first, again, mid, await counts(rig.db, world.a)
    first, again, mid, after = with_rig(go, sdk=True)
    assert first.status == "processed" and [a.status for a in again] == ["duplicate"] * 3
    assert after == mid and after["spendable"] == 2000 and after["billing_webhook_event"] == 1 and after["billing_transaction"] == 1


def test_a_second_event_object_for_the_same_payment_credits_once_because_the_money_is_keyed_on_the_payment(world):
    async def go(rig):
        res, pi = await asked(rig, world.a)
        one = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        two = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return one, two, await counts(rig.db, world.a)
    one, two, c = with_rig(go, sdk=True)
    assert one.event_id != two.event_id and c["spendable"] == 2000 and c["billing_transaction"] == 1 and c["billing_webhook_event"] == 2


def test_ten_concurrent_deliveries_of_one_signed_event_credit_exactly_once(world):
    import asyncio

    async def go(rig):
        res, pi = await asked(rig, world.a)
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", pi))
        got = await asyncio.gather(*[webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify) for _ in range(10)])
        return got, await counts(rig.db, world.a)
    got, c = with_rig(go, size=12, sdk=True)
    assert sorted(g.status for g in got) == ["duplicate"] * 9 + ["processed"] and c["spendable"] == 2000 and c["billing_transaction"] == 1


def test_during_a_secret_rotation_an_event_signed_with_both_secrets_credits_once_under_the_new_one(world):
    async def go(rig):
        res, pi = await asked(rig, world.a, 1000, "pay-rot")
        rotating = Events((SECRETS[0], FAKE_WEBHOOK_SECRET_NEW))
        body, header = rotating.signed(rotating.payment_intent("payment_intent.succeeded", pi))
        first = await webhooks.receive(rig.db, rig.router, body, header, (FAKE_WEBHOOK_SECRET_NEW,), verify=rig.verify)
        return first, await counts(rig.db, world.a)
    first, c = with_rig(go, sdk=True)
    assert first.status == "processed" and c["spendable"] == 1000


# ---- a bad signature, a wrong amount and an unknown event are refused or do nothing ------------------------------------------------------

def test_a_tampered_stale_wrongly_signed_or_unsigned_event_is_a_400_and_writes_nothing(world):
    async def go(rig):
        res, pi = await asked(rig, world.a)
        event = rig.events.payment_intent("payment_intent.succeeded", pi)
        body, header = rig.events.signed(event)
        before = await counts(rig.db, world.a)
        tampered = body.replace(b'"amount":2000', b'"amount":9999')
        stale_body, stale_header = rig.events.signed(event, timestamp=NOW - 3600)
        other_body, other_header = Events(("some-other-secret",)).signed(event)
        answers = []
        for payload, hdr in ((tampered, header), (stale_body, stale_header), (other_body, other_header), (body, None), (body, ""), (body, "t=1,v1=00"), (body, "t=%d,v1=é" % NOW)):
            answers.append(await webhooks.handle_http(rig.db, rig.router, payload, hdr, SECRETS, verify=rig.verify))
        credited = await webhooks.handle_http(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        return answers, before, await counts(rig.db, world.a), credited
    answers, before, after, credited = with_rig(go, sdk=True)
    assert all(code == 400 and body == {"error": "invalid webhook"} for code, body in answers) and after["spendable"] == 2000 and before["spendable"] == 0
    assert credited == (200, {"received": True, "status": "processed", "event": credited[1]["event"]})


def test_a_refused_event_leaves_no_event_row_so_a_forged_id_cannot_block_the_real_one(world):
    async def go(rig):
        res, pi = await asked(rig, world.a)
        event = rig.events.payment_intent("payment_intent.succeeded", pi, event_id="evt_the_real_one")
        forged, bad_header = Events(("not-the-secret",)).signed(event)
        refused = await webhooks.handle_http(rig.db, rig.router, forged, bad_header, SECRETS, verify=rig.verify)
        stored_after_forgery = (await counts(rig.db, world.a))["billing_webhook_event"]
        real = await deliver(rig, event)
        return refused[0], stored_after_forgery, real
    code, stored, real = with_rig(go, sdk=True)
    assert code == 400 and stored == 0 and real.status == "processed"


def test_an_event_for_another_amount_than_we_asked_is_held_and_never_credited(world):
    async def go(rig):
        res, pi = await asked(rig, world.a, 2000)
        wrong = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", {**pi, "amount": 9999}))
        return wrong, await counts(rig.db, world.a)
    wrong, c = with_rig(go, sdk=True)
    assert wrong.status == "held" and "asked for 2000" in wrong.detail and c["spendable"] == 0 and c["billing_transaction"] == 0


def test_a_verified_event_of_a_type_we_do_not_act_on_is_ignored_once_and_moves_no_money(world):
    async def go(rig):
        res, pi = await asked(rig, world.a)
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.created", pi))
        return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify), await counts(rig.db, world.a)
    got, c = with_rig(go, sdk=True)
    assert got.status == "ignored" and c["spendable"] == 0 and c["billing_transaction"] == 0 and c["billing_webhook_event"] == 1


# ---- the SDK as the provider: a refund asked of it moves the wallet only when its event arrives -------------------------------------------

def test_a_refund_asked_through_the_sdk_reverses_the_wallet_once_when_the_event_arrives_and_a_replay_or_an_older_total_reverses_nothing(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-rf")
        refund = await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=500, key="rf-1")
        again = await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=500, key="rf-1")
        event = rig.events.charge_refunded(res.provider_payment_id, 2000, 500, "aud")
        body, header = rig.events.signed(event)
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        replay = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        older = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 2000, 300, "aud"))
        return refund, again, first, replay, older, await counts(rig.db, world.a), len(rig.fake.refunds)
    refund, again, first, replay, older, c, provider_refunds = with_rig(go, sdk=True)
    assert (refund.amount, again.id, again.replayed) == (500, refund.id, True) and provider_refunds == 1
    assert (first.status, replay.status, older.status) == ("processed", "duplicate", "ignored") and c["spendable"] == 1500


# ---- the SDK against stripe-mock, delivered to the real receiver ---------------------------------------------------------------------

@needs_stripe_mock
def test_a_top_up_asked_of_stripe_mock_is_credited_once_by_its_signed_succeeded_event_and_not_again_by_a_replay(world):
    async def go(rig):
        mock = stripe_sdk.StripeSdkProvider(MOCK_API_KEY, api_base=STRIPE_MOCK_URL)
        res = await payments.start_topup(rig.db, mock, world.a, amount_minor=2000, currency=CURRENCY, payment_method="pm_card_visa", key="mock-1")
        before = await counts(rig.db, world.a)
        again = await payments.start_topup(rig.db, mock, world.a, amount_minor=2000, currency=CURRENCY, payment_method="pm_card_visa", key="mock-1")
        pi = {"id": res.provider_payment_id, "object": "payment_intent", "amount": 2000, "currency": "aud", "status": "succeeded", "metadata": {"payment_id": res.payment_id}}
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", pi))
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        replay = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        return res, again, before, first, replay, await counts(rig.db, world.a)
    res, again, before, first, replay, after = with_rig(go, sdk=True)
    assert res.provider_payment_id.startswith("pi_") and again.replayed and again.payment_id == res.payment_id and before["spendable"] == 0
    assert (first.status, replay.status) == ("processed", "duplicate") and after["spendable"] == 2000 and after["billing_transaction"] == 1


@needs_stripe_mock
def test_a_refund_asked_of_stripe_mock_moves_the_wallet_only_when_its_signed_event_arrives(world):
    async def go(rig):
        mock = stripe_sdk.StripeSdkProvider(MOCK_API_KEY, api_base=STRIPE_MOCK_URL)
        res = await payments.start_topup(rig.db, mock, world.a, amount_minor=2000, currency=CURRENCY, payment_method="pm_card_visa", key="mock-rf")
        pi = {"id": res.provider_payment_id, "object": "payment_intent", "amount": 2000, "currency": "aud", "status": "succeeded", "metadata": {"payment_id": res.payment_id}}
        await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        refund = await payments.request_refund(rig.db, mock, world.a, res.payment_id, amount_minor=700, key="rf-mock")
        asked_only = await counts(rig.db, world.a)
        got = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 2000, 700, "aud"))
        return refund, asked_only, got, await counts(rig.db, world.a)
    refund, asked_only, got, after = with_rig(go, sdk=True)
    assert (refund.amount, refund.payment_intent.startswith("pi_")) == (700, True) and asked_only["spendable"] == 2000
    assert got.status == "processed" and after["spendable"] == 1300
