"""Bank debit (ACH) top-ups against real Postgres and the fake Stripe.

THE RULE UNDER TEST: a bank debit does not credit the wallet when it is created. The provider answers `processing` (the debit takes days), the payment row is `processing`, and the wallet
is credited only by the signed `payment_intent.succeeded` event (or by refresh_payment asking the provider). A return after processing (`payment_intent.payment_failed`) leaves the wallet
exactly as it was and records a failed payment. Replaying either event, or delivering a second event object for the same payment, changes nothing.

Each refusal is paired with the case that must still work, so a build that refused everything would not pass. The books are reconciled after each flow.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from portal_fixtures import ACH_PM, CUSTOMER, MANDATE, USD, needs_db, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN, SECRETS, counts  # noqa: E402

pytestmark = needs_db

from app.billing import payments, reports, webhooks  # noqa: E402
from app.billing.errors import IdempotencyConflict  # noqa: E402


@pytest.fixture()
def world():
    class W:
        pass
    w = W()
    w.a, w.b = reset_world("tenant-a", "tenant-b")
    return w


async def start_ach(rig, tenant, amount=5000, key="ach-1", **kw):
    return await payments.start_topup(rig.db, rig.provider, tenant, amount_minor=amount, currency=USD, payment_method=ACH_PM, key=key, method_type="us_bank_account",
                                      customer=kw.get("customer", CUSTOMER), mandate=kw.get("mandate", MANDATE))


async def deliver(rig, event):
    body, header = rig.events.signed(event)
    return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)


async def payment_row(rig, tenant, payment_id):
    async with rig.db.tenant(tenant) as conn:
        return await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)


async def reconciled(rig, tenant):
    async with rig.db.tenant(tenant) as conn:
        return await reports.reconcile(conn, tenant)


# ---- created: processing, nothing credited -----------------------------------------------------------------------------------------------

def test_a_bank_debit_is_processing_when_created_and_the_wallet_is_not_credited(world):
    async def go(rig):
        res = await start_ach(rig, world.a.tenant)
        pi = rig.fake.payment_intents[res.provider_payment_id]
        return res, pi, await counts(rig.db, world.a.tenant), await payment_row(rig, world.a.tenant, res.payment_id), rig.fake.requests[-1]["params"]
    res, pi, c, row, sent = with_portal(go)
    assert res.status == "processing" and pi["status"] == "processing" and not pi["latest_charge"]
    assert (c["spendable"], c["billing_transaction"], c["billing_entry"]) == (0, 0, 0)
    assert (row["payment_method_type"], row["customer_ref"], row["mandate_ref"], row["credited_minor"], row["currency"].strip()) == ("us_bank_account", CUSTOMER, MANDATE, 0, USD)
    # the shape of the request: the bank account token, the customer and the mandate, confirmed now
    assert sent["payment_method"] == ACH_PM and sent["customer"] == CUSTOMER and sent["mandate"] == MANDATE and sent["confirm"] == "true"


def test_a_card_top_up_is_unchanged_by_the_bank_debit_support(world):
    """The control case: a card still succeeds at once and its request carries none of the bank debit fields."""
    async def go(rig):
        res = await payments.start_topup(rig.db, rig.provider, world.a.tenant, amount_minor=2000, currency=USD, payment_method="pm_fake_ok", key="card-1")
        return res, rig.fake.requests[-1]["params"]
    res, sent = with_portal(go)
    assert res.status == "succeeded" and "customer" not in sent and "mandate" not in sent


# ---- succeeded: credited once, by the event -----------------------------------------------------------------------------------------------

def test_the_wallet_is_credited_by_the_succeeded_event_once_and_replays_change_nothing(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t)
        before = await counts(rig.db, t)
        pi = rig.fake.settle_ach(res.provider_payment_id)
        event = rig.events.payment_intent("payment_intent.succeeded", pi)
        body, header = rig.events.signed(event)
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        mid = await counts(rig.db, t)
        again = [await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify) for _ in range(3)]
        second_object = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return before, first, mid, again, second_object, await counts(rig.db, t), await payment_row(rig, t, res.payment_id), await reconciled(rig, t)
    before, first, mid, again, second_object, after, row, report = with_portal(go)
    assert before["spendable"] == 0 and first.status == "processed" and mid["spendable"] == 5000
    assert [a.status for a in again] == ["duplicate"] * 3 and second_object.status == "processed"
    assert after["spendable"] == 5000 and after["billing_transaction"] == 1                  # one credit, however many events
    assert (row["status"], row["credited_minor"]) == ("succeeded", 5000) and report.ok


def test_a_lost_succeeded_event_is_settled_by_asking_the_provider_and_the_late_event_credits_nothing_more(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t)
        still = await payments.refresh_payment(rig.db, rig.provider, t, res.payment_id)          # not cleared yet: still processing, nothing credited
        mid = await counts(rig.db, t)
        pi = rig.fake.settle_ach(res.provider_payment_id)
        settled = await payments.refresh_payment(rig.db, rig.provider, t, res.payment_id)
        late = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return still, mid, settled, late, await counts(rig.db, t)
    still, mid, settled, late, after = with_portal(go)
    assert still.status == "processing" and mid["spendable"] == 0
    assert settled.status == "succeeded" and after["spendable"] == 5000 and after["billing_transaction"] == 1 and late.status == "processed"


# ---- returned after processing: nothing credited, a failed payment recorded ---------------------------------------------------------------

def test_a_return_after_processing_leaves_the_wallet_unchanged_and_records_a_failed_payment(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t)
        before = await counts(rig.db, t)
        pi = rig.fake.fail_ach(res.provider_payment_id, code="account_closed", message="The bank account is closed.")
        event = rig.events.payment_intent("payment_intent.payment_failed", pi)
        body, header = rig.events.signed(event)
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        mid = await counts(rig.db, t)
        again = [await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify) for _ in range(3)]
        second_object = await deliver(rig, rig.events.payment_intent("payment_intent.payment_failed", pi))
        async with rig.db.tenant(t) as conn:
            notes = await conn.fetch("select kind, subject_ref from billing_notification")
        return before, first, mid, again, second_object, await counts(rig.db, t), await payment_row(rig, t, res.payment_id), notes, await reconciled(rig, t)
    before, first, mid, again, second_object, after, row, notes, report = with_portal(go)
    assert first.status == "processed" and "credited nothing" in first.detail
    assert [a.status for a in again] == ["duplicate"] * 3 and second_object.status == "ignored"
    assert (row["status"], row["failure_code"], row["credited_minor"]) == ("failed", "account_closed", 0)
    assert (before["spendable"], mid["spendable"], after["spendable"]) == (0, 0, 0) and after["billing_transaction"] == 0 and after["billing_entry"] == 0
    assert [n["kind"] for n in notes] == ["payment_failed"] and report.ok            # told once, however many events came


def test_refreshing_a_returned_debit_records_the_failure_and_credits_nothing(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t)
        rig.fake.fail_ach(res.provider_payment_id)
        settled = await payments.refresh_payment(rig.db, rig.provider, t, res.payment_id)
        return settled, await counts(rig.db, t)
    settled, c = with_portal(go)
    assert settled.status == "failed" and settled.failure_code == "insufficient_funds" and c["spendable"] == 0 and c["billing_transaction"] == 0


def test_a_late_failure_never_undoes_a_success_and_a_late_success_never_follows_a_failure_into_the_wallet_twice(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t)
        pi = dict(rig.fake.settle_ach(res.provider_payment_id))
        await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        late_fail = await deliver(rig, rig.events.payment_intent("payment_intent.payment_failed", {**pi, "status": "requires_payment_method", "last_payment_error": {"code": "x"}}))
        return late_fail, await counts(rig.db, t), await payment_row(rig, t, res.payment_id)
    late_fail, c, row = with_portal(go)
    assert late_fail.status == "ignored" and c["spendable"] == 5000 and row["status"] == "succeeded"


# ---- the request itself ------------------------------------------------------------------------------------------------------------------

def test_the_same_key_again_is_a_replay_with_one_intent_at_the_provider_and_other_content_is_refused(world):
    async def go(rig):
        t = world.a.tenant
        a = await start_ach(rig, t)
        b = await start_ach(rig, t)
        with pytest.raises(IdempotencyConflict):
            await start_ach(rig, t, amount=6000)
        with pytest.raises(IdempotencyConflict):
            await start_ach(rig, t, customer="cus_someone_else")
        return a, b, rig.fake.executions, await counts(rig.db, t)
    a, b, executions, c = with_portal(go)
    assert (a.replayed, b.replayed, a.payment_id == b.payment_id, executions, c["billing_payment"]) == (False, True, True, 1, 1)


def test_a_bank_debit_without_its_customer_or_mandate_or_in_another_currency_is_refused_before_the_provider_is_called(world):
    async def go(rig):
        t = world.a.tenant
        refused = []
        for kw in ({"customer": None}, {"mandate": None}, {"customer": ""}):
            with pytest.raises(ValueError):
                await start_ach(rig, t, key="k-%d" % len(refused), **kw)
            refused.append(kw)
        with pytest.raises(ValueError):
            await payments.start_topup(rig.db, rig.provider, t, amount_minor=5000, currency="AUD", payment_method=ACH_PM, key="aud", method_type="us_bank_account", customer=CUSTOMER, mandate=MANDATE)
        with pytest.raises(ValueError):
            await payments.start_topup(rig.db, rig.provider, t, amount_minor=5000, currency=USD, payment_method=ACH_PM, key="wire", method_type="wire")
        return len(rig.fake.requests), await counts(rig.db, t)
    requests, c = with_portal(go)
    assert requests == 0 and c["billing_payment"] == 0


def test_the_provider_itself_refuses_a_bank_debit_with_no_customer_or_an_unknown_account_and_that_is_a_failed_payment(world):
    async def go(rig):
        t = world.a.tenant
        res = await payments.start_topup(rig.db, rig.provider, t, amount_minor=5000, currency=USD, payment_method="pm_fake_ach_unknown", key="bad-pm", method_type="us_bank_account",
                                         customer=CUSTOMER, mandate=MANDATE)
        return res, await counts(rig.db, t)
    res, c = with_portal(go)
    assert res.status == "failed" and res.failure_code == "resource_missing" and c["spendable"] == 0


def test_the_table_refuses_a_bank_debit_that_is_not_usd_or_has_no_mandate_and_an_invoice_payment_that_credits_the_wallet(world):
    import uuid
    t = world.a.tenant
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        base = "insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, payment_method_type, customer_ref, mandate_ref) values (%s, %s, 'h', 'p', 100, %s, 'us_bank_account', %s, %s)"
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, "a" + uuid.uuid4().hex, "AUD", "cus", "mandate"))
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, "b" + uuid.uuid4().hex, "USD", None, "mandate"))
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, "c" + uuid.uuid4().hex, "USD", "cus", None))
        cur.execute(base, (t, "d" + uuid.uuid4().hex, "USD", "cus", "mandate"))             # the control: a complete USD bank debit is accepted
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, purpose, credited_minor) "
                        "values (%s, %s, 'h', 'p', 100, 'USD', 'invoice', 100)", (t, "e" + uuid.uuid4().hex))


# ---- a debit that cleared can be refunded like a card payment -----------------------------------------------------------------------------

def test_a_cleared_debit_is_refunded_through_the_existing_refund_path_and_reversed_once(world):
    async def go(rig):
        t = world.a.tenant
        res = await start_ach(rig, t, amount=5000)
        pi = rig.fake.settle_ach(res.provider_payment_id)
        await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        refund = await payments.request_refund(rig.db, rig.provider, t, res.payment_id, amount_minor=1200, key="rf-1")
        mid = await counts(rig.db, t)                                                           # the wallet moves on the event, not on the request
        ev = rig.events.charge_refunded(res.provider_payment_id, 5000, 1200, currency="usd")
        first = await deliver(rig, ev)
        again = await deliver(rig, ev)
        return refund, mid, first, again, await counts(rig.db, t), await reconciled(rig, t)
    refund, mid, first, again, after, report = with_portal(go)
    assert refund.amount == 1200 and mid["spendable"] == 5000 and first.status == "processed" and again.status == "duplicate"
    assert after["spendable"] == 3800 and report.ok


# ---- the provider's answer can be lost, and the debit is still made once ------------------------------------------------------------------

def test_a_response_lost_after_the_debit_was_created_makes_one_debit_because_the_retry_carries_the_same_key(world):
    async def go(rig):
        t = world.a.tenant
        rig.fake.lose_response_after_execution(1)
        res = await start_ach(rig, t)
        return res, rig.fake.executions, len(rig.fake.payment_intents), await counts(rig.db, t)
    res, executions, intents, c = with_portal(go)
    assert res.status == "processing" and executions == 1 and intents == 1 and c["spendable"] == 0


def test_every_tenants_debit_is_its_own_and_an_event_for_another_tenants_payment_is_not_applied_to_this_one(world):
    async def go(rig):
        res_a = await start_ach(rig, world.a.tenant, key="a-1")
        res_b = await start_ach(rig, world.b.tenant, key="b-1", amount=700)
        pi = rig.fake.settle_ach(res_a.provider_payment_id)
        await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return await counts(rig.db, world.a.tenant), await counts(rig.db, world.b.tenant), res_b
    a, b, res_b = with_portal(go)
    assert a["spendable"] == 5000 and b["spendable"] == 0 and res_b.status == "processing"
