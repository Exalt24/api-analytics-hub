"""Payments against real Postgres and the fake Stripe: no duplicate charge on a retry, a repeated request or a response lost after the request ran; a card error
recorded with the provider's code and never credited; a refund asked once and reversed exactly once. The wallet moves only through the signed events, or through
refresh_payment, and never twice."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import CURRENCY, SECRETS, counts, needs_db, new_tenants, reset_billing_schema, topup_via_provider, with_rig  # noqa: E402

pytestmark = needs_db

from app.billing import payments, reports, webhooks  # noqa: E402
from app.billing.errors import BillingError, IdempotencyConflict  # noqa: E402
from app.billing.provider import ProviderUnavailable  # noqa: E402


class World:
    pass


@pytest.fixture()
def world():
    reset_billing_schema()
    w = World()
    w.a, w.b = new_tenants("tenant-a", "tenant-b")
    return w


async def start(rig, tenant, amount=2000, key="pay-1", method="pm_fake_ok"):
    return await payments.start_topup(rig.db, rig.provider, tenant, amount_minor=amount, currency=CURRENCY, payment_method=method, key=key)


async def deliver(rig, event, **kw):
    body, header = rig.events.signed(event)
    return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, **kw)


# ---- no duplicate charge ----------------------------------------------------------------------------------------------------------------

def test_the_same_request_twice_makes_one_payment_row_and_one_charge_at_the_provider(world):
    async def go(rig):
        a = await start(rig, world.a)
        b = await start(rig, world.a)
        return a, b, rig.fake, await counts(rig.db, world.a)
    a, b, fake, c = with_rig(go)
    assert (a.replayed, b.replayed, a.payment_id == b.payment_id, a.status, b.status) == (False, True, True, "succeeded", "succeeded")
    assert fake.executions == 1 and len(fake.charges) == 1 and c["billing_payment"] == 1 and c["spendable"] == 0     # nothing credited until the event


def test_the_same_key_with_a_different_amount_or_method_is_refused_and_nothing_is_sent(world):
    async def go(rig):
        await start(rig, world.a)
        sent = len(rig.fake.requests)
        for kw in ({"amount": 2001}, {"method": "pm_fake_declined"}):
            with pytest.raises(IdempotencyConflict):
                await start(rig, world.a, **kw)
        return sent, len(rig.fake.requests), rig.fake.executions
    sent, after, executions = with_rig(go)
    assert sent == after and executions == 1


def test_ten_concurrent_copies_of_one_request_charge_once(world):
    async def go(rig):
        results = await asyncio.gather(*[start(rig, world.a, key="burst") for _ in range(10)])
        return results, rig.fake, await counts(rig.db, world.a)
    results, fake, c = with_rig(go, size=12)
    assert len({r.payment_id for r in results}) == 1 and c["billing_payment"] == 1
    assert fake.executions == 1 and len(fake.charges) == 1


def test_the_provider_key_is_stored_before_the_call_and_every_retry_sends_that_same_key(world):
    async def go(rig):
        rig.fake.fail_before_execution(500, 429)
        res = await start(rig, world.a)
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
        return res, row, rig.fake
    res, row, fake = with_rig(go)
    keys = {r["idempotency_key"] for r in fake.requests if r["method"] == "POST"}
    assert keys == {row["provider_idempotency_key"]} and len(fake.requests) == 3 and fake.executions == 1 and res.status == "succeeded"


def test_a_provider_that_stays_down_leaves_the_payment_created_and_asking_again_reuses_the_same_provider_key(world):
    async def go(rig):
        rig.fake.fail_before_execution(429, 429, 429, 429)
        first = await start(rig, world.a)
        second = await start(rig, world.a)
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
        return first, second, row, rig.fake
    first, second, row, fake = with_rig(go)
    assert first.status == "created" and second.status == "succeeded" and first.payment_id == second.payment_id
    assert {r["idempotency_key"] for r in fake.requests} == {row["provider_idempotency_key"]} and fake.executions == 1


def test_a_response_lost_after_the_provider_ran_is_settled_with_one_charge_and_one_credit(world):
    async def go(rig):
        rig.fake.lose_response_after_execution(4)
        rig.fake.search_lag = True                        # the search cannot find it either, so the outcome is unknown
        res = await start(rig, world.a)
        assert res.status == "unknown" and res.provider_payment_id is None
        before = (rig.fake.executions, len(rig.fake.charges))
        settled = await payments.refresh_payment(rig.db, rig.provider, world.a, res.payment_id)       # the same key: the saved answer, nothing new
        again = await payments.refresh_payment(rig.db, rig.provider, world.a, res.payment_id)
        return res, before, settled, again, rig.fake, await counts(rig.db, world.a)
    res, before, settled, again, fake, c = with_rig(go)
    assert before == (1, 1) and (fake.executions, len(fake.charges)) == (1, 1)
    assert settled.status == again.status == "succeeded" and c["spendable"] == 2000 and c["billing_transaction"] == 1


def test_a_lost_response_that_the_search_can_find_is_resolved_by_finding_it_never_by_creating_again(world):
    async def go(rig):
        rig.fake.lose_response_after_execution(4)
        res = await start(rig, world.a)
        return res, rig.fake
    res, fake = with_rig(go)
    assert res.status == "succeeded" and fake.executions == 1 and len(fake.charges) == 1
    assert any(r["path"] == "/v1/payment_intents/search" for r in fake.requests)


def test_the_event_and_the_refresh_both_reach_the_same_payment_and_the_wallet_is_credited_once(world):
    async def go(rig):
        res, got = await topup_via_provider(rig, world.a, 2000, "pay-both")
        refreshed = await payments.refresh_payment(rig.db, rig.provider, world.a, res.payment_id)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.assert_reconciled(conn, world.a)
        return got, refreshed, await counts(rig.db, world.a), rec
    got, refreshed, c, rec = with_rig(go)
    assert got.status == "processed" and refreshed.status == "succeeded"
    assert (c["billing_transaction"], c["spendable"]) == (1, 2000) and rec.ok


# ---- a failed payment never credits ------------------------------------------------------------------------------------------------------

def test_a_card_error_is_recorded_with_the_providers_code_queued_as_a_notice_and_never_credited(world):
    async def go(rig):
        res = await start(rig, world.a, method="pm_fake_declined")
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
            note = await conn.fetchrow("select * from billing_notification")
        return res, row, note, rig.fake, await counts(rig.db, world.a)
    res, row, note, fake, c = with_rig(go)
    assert (res.status, res.failure_code) == ("failed", "insufficient_funds") and (row["status"], row["credited_minor"]) == ("failed", 0)
    assert row["provider_payment_id"] and note["kind"] == "payment_failed" and "declined" in note["message"]
    assert len(fake.requests) == 1 and c["billing_transaction"] == 0 and c["spendable"] == 0       # not retried, nothing booked


def test_asking_again_after_a_decline_returns_the_failure_and_does_not_call_the_provider_again(world):
    async def go(rig):
        await start(rig, world.a, method="pm_fake_declined")
        before = len(rig.fake.requests)
        again = await start(rig, world.a, method="pm_fake_declined")
        return before, len(rig.fake.requests), again, await counts(rig.db, world.a)
    before, after, again, c = with_rig(go)
    assert before == after and (again.status, again.replayed) == ("failed", True) and c["billing_notification"] == 1


def test_a_failed_payment_row_cannot_be_given_credit_even_by_a_direct_write(world):
    import asyncpg
    async def go(rig):
        await start(rig, world.a, method="pm_fake_declined")
        with pytest.raises(asyncpg.CheckViolationError, match="failed_never_credits"):
            async with rig.db.tenant(world.a) as conn:
                await conn.execute("update billing_payment set credited_minor = 100")
    with_rig(go)


def test_another_provider_refusal_is_recorded_as_failed_with_its_code_and_not_retried(world):
    import httpx
    async def go(rig):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "code": "parameter_invalid_empty", "message": "payment_method is missing"}})
        rig.provider._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        res = await start(rig, world.a)
        async with rig.db.tenant(world.a) as conn:
            note = await conn.fetchval("select message from billing_notification")
        return res, len(calls), note, await counts(rig.db, world.a)
    res, calls, note, c = with_rig(go)
    assert (res.status, res.failure_code, calls) == ("failed", "parameter_invalid_empty", 1) and "refused by the provider" in note and c["spendable"] == 0


# ---- amounts must match what we asked for -------------------------------------------------------------------------------------------------

def test_an_event_for_another_amount_than_we_asked_is_held_and_never_credited(world):
    async def go(rig):
        res = await start(rig, world.a, amount=2000)
        pi = dict(rig.fake.payment_intents[res.provider_payment_id], amount=20000)
        got = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return got, await counts(rig.db, world.a)
    got, c = with_rig(go)
    assert got.status == "held" and "asked for 2000" in got.detail and c["spendable"] == 0 and c["billing_transaction"] == 0


# ---- refunds ---------------------------------------------------------------------------------------------------------------------------------

def test_a_refund_is_asked_once_with_a_key_derived_from_ours_and_reversed_once_when_its_event_arrives(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-r")
        r1 = await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=500, key="refund-1")
        r2 = await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=500, key="refund-1")
        mid = await counts(rig.db, world.a)
        got = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 2000, 500, "aud"))
        again = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 2000, 500, "aud"))
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
            rec = await reports.assert_reconciled(conn, world.a)
        return r1, r2, mid, got, again, row, await counts(rig.db, world.a), rig.fake, rec
    r1, r2, mid, got, again, row, c, fake, rec = with_rig(go)
    assert r1.id == r2.id and r2.replayed and len(fake.refunds) == 1
    assert mid["spendable"] == 2000                                         # asking does not move the wallet
    assert (got.status, got.detail) == ("processed", "reversed 500 (total refunded 500)") and again.status == "ignored" and "already reversed" in again.detail
    assert (c["spendable"], row["refunded_minor"]) == (1500, 500) and rec.ok


def test_refunds_in_pieces_reverse_exactly_the_providers_total_and_a_total_refund_leaves_nothing_behind(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 3000, "pay-p")
        for total in (1000, 1800, 3000):
            await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 3000, total, "aud"))
        async with rig.db.tenant(world.a) as conn:
            return await reports.assert_reconciled(conn, world.a), await counts(rig.db, world.a)
    rec, c = with_rig(go)
    assert c["spendable"] == 0 and c["billing_transaction"] == 4 and rec.ok


def test_more_than_is_refundable_is_refused_before_the_provider_is_asked(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 1000, "pay-o")
        sent = len(rig.fake.requests)
        with pytest.raises(BillingError):
            await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=1001, key="over")
        return sent, len(rig.fake.requests)
    sent, after = with_rig(go)
    assert sent == after


def test_a_payment_that_did_not_succeed_cannot_be_refunded(world):
    async def go(rig):
        res = await start(rig, world.a, method="pm_fake_declined")
        with pytest.raises(BillingError):
            await payments.request_refund(rig.db, rig.provider, world.a, res.payment_id, amount_minor=100, key="nope")
    with_rig(go)


def test_a_payment_of_one_tenant_cannot_be_refunded_by_another(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 1000, "pay-x")
        with pytest.raises(BillingError):
            await payments.request_refund(rig.db, rig.provider, world.b, res.payment_id, amount_minor=100, key="steal")
        return len(rig.fake.refunds)
    assert with_rig(go) == 0
