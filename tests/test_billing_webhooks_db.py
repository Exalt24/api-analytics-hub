"""The webhook receiver against real Postgres: a bad signature or a stale timestamp writes nothing; an event id is applied once (a replay, ten concurrent copies, and
a second event object for the same payment); an event that arrives early is held and applied when what it waited for arrives; a failed payment never credits;
a refund reverses exactly the provider's total; a fault rolls the whole delivery back so the provider's retry applies it cleanly; subscription events keep the
plan state without letting a late event revive a canceled plan."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import CURRENCY, SECRETS, counts, needs_db, new_tenants, reset_billing_schema, topup_via_provider, with_rig  # noqa: E402

pytestmark = needs_db

from app.billing import payments, reports, signature, subscriptions, webhooks  # noqa: E402
from app.billing.errors import WebhookRejected, WebhookUnroutable  # noqa: E402
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
    return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, **kw)


async def events_stored(rig, tenant):
    async with rig.db.tenant(tenant) as conn:
        return [(r["provider_event_id"], r["status"]) for r in await conn.fetch("select * from billing_webhook_event order by id")]


async def subscribe(rig, tenant, plan="starter", customer="cus_a", sub="sub_a"):
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc) - timedelta(days=10)
    async with rig.db.tenant(tenant) as conn:
        await subscriptions.start_subscription(conn, tenant, plan_code=plan, provider_customer_id=customer, provider_subscription_id=sub,
                                               period_start=start, period_end=start + timedelta(days=30))


async def sub_row(rig, tenant):
    async with rig.db.tenant(tenant) as conn:
        return await conn.fetchrow("select s.*, p.code as plan_code from billing_subscription s join billing_plan p on p.id = s.plan_id")


# ---- authenticity ---------------------------------------------------------------------------------------------------------------------

def test_a_tampered_stale_or_wrongly_signed_event_is_rejected_and_nothing_is_written(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-1", deliver=False)
        event = rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id])
        body, header = rig.events.signed(event)
        before = await counts(rig.db, world.a)
        tampered = body.replace(b'"amount":2000', b'"amount":9999')
        stale_body, stale_header = rig.events.signed(event, timestamp=NOW - 3600)
        other_body, other_header = Events(("some-other-secret",)).signed(event)
        answers = []
        for payload, hdr in ((tampered, header), (stale_body, stale_header), (other_body, other_header), (body, None), (body, "t=1,v1=00")):
            answers.append(await webhooks.handle_http(rig.db, rig.router, payload, hdr, SECRETS))
        return answers, before, await counts(rig.db, world.a)
    answers, before, after = with_rig(go)
    assert all(code == 400 and body == {"error": "invalid webhook"} for code, body in answers) and after == before and after["spendable"] == 0


def test_the_rejection_never_says_which_check_failed_to_the_sender_but_logs_it(world, caplog):
    async def go(rig):
        event = rig.events.event("payment_intent.succeeded", {"id": "pi_x"})
        body, header = rig.events.signed(event, timestamp=NOW - 3600)
        return await webhooks.handle_http(rig.db, rig.router, body, header, SECRETS)
    with caplog.at_level("WARNING", logger="billing.webhooks"):
        code, answer = with_rig(go)
    assert code == 400 and "tolerance" not in json.dumps(answer) and "timestamp outside the tolerance" in caplog.text


def test_during_a_secret_rotation_an_event_signed_with_both_secrets_is_accepted_by_the_old_or_the_new_one(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 1000, "pay-rot", deliver=False)
        pi = rig.fake.payment_intents[res.provider_payment_id]
        rotating = Events((SECRETS[0], FAKE_WEBHOOK_SECRET_NEW))
        body, header = rotating.signed(rotating.payment_intent("payment_intent.succeeded", pi))
        first = await webhooks.receive(rig.db, rig.router, body, header, (FAKE_WEBHOOK_SECRET_NEW,))
        return first, await counts(rig.db, world.a)
    first, c = with_rig(go)
    assert first.status == "processed" and c["spendable"] == 1000


def test_a_signed_body_that_is_not_an_event_or_not_json_is_a_rejection(world):
    async def go(rig):
        out = []
        for raw in (b"not json", b"[]", b'{"id": 5}', b'{"id":"e","type":"x","created":"now","data":{"object":{}}}', b'{"id":"e","type":"x","created":1,"data":{}}'):
            header = signature.build_header(raw, SECRETS, NOW)
            out.append((await webhooks.handle_http(rig.db, rig.router, raw, header, SECRETS))[0])
        return out
    assert with_rig(go) == [400] * 5


# ---- duplicate events -----------------------------------------------------------------------------------------------------------------

def test_a_replayed_event_id_writes_nothing_the_second_time(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-dup", deliver=False)
        event = rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id])
        body, header = rig.events.signed(event)
        first = await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
        mid = await counts(rig.db, world.a)
        again = [await webhooks.receive(rig.db, rig.router, body, header, SECRETS) for _ in range(3)]
        return first, again, mid, await counts(rig.db, world.a)
    first, again, mid, after = with_rig(go)
    assert first.status == "processed" and [a.status for a in again] == ["duplicate"] * 3
    assert after == mid and after["spendable"] == 2000 and after["billing_webhook_event"] == 1 and after["billing_transaction"] == 1


def test_ten_concurrent_deliveries_of_one_event_apply_it_exactly_once(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-conc", deliver=False)
        event = rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id])
        body, header = rig.events.signed(event)
        got = await asyncio.gather(*[webhooks.receive(rig.db, rig.router, body, header, SECRETS) for _ in range(10)], return_exceptions=True)
        return got, await counts(rig.db, world.a)
    got, c = with_rig(go, size=12)
    assert [g for g in got if isinstance(g, Exception)] == []
    assert sorted(g.status for g in got).count("processed") == 1 and sorted(g.status for g in got).count("duplicate") == 9
    assert (c["spendable"], c["billing_transaction"], c["billing_webhook_event"]) == (2000, 1, 1)


def test_two_different_event_objects_for_the_same_payment_credit_once_because_the_money_is_keyed_on_the_payment(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-two", deliver=False)
        pi = rig.fake.payment_intents[res.provider_payment_id]
        first = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        second = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.assert_reconciled(conn, world.a)
        return first, second, await counts(rig.db, world.a), rec
    first, second, c, rec = with_rig(go)
    assert first.event_id != second.event_id and "applied" in first.detail and "replayed" in second.detail
    assert (c["spendable"], c["billing_transaction"], c["billing_webhook_event"]) == (2000, 1, 2) and rec.ok


def test_the_same_event_id_in_two_tenants_is_two_events_and_never_leaks_between_them(world):
    async def go(rig):
        ra, _ = await topup_via_provider(rig, world.a, 1000, "pay-a", deliver=False)
        rb, _ = await topup_via_provider(rig, world.b, 1500, "pay-b", deliver=False)
        a = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[ra.provider_payment_id], event_id="evt_same"))
        b = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[rb.provider_payment_id], event_id="evt_same"))
        return a, b, (await counts(rig.db, world.a))["spendable"], (await counts(rig.db, world.b))["spendable"]
    a, b, sa, sb = with_rig(go)
    assert (a.tenant_id, b.tenant_id) == (world.a, world.b) and (sa, sb) == (1000, 1500)


# ---- failed payments never credit ------------------------------------------------------------------------------------------------------------

def test_a_payment_failed_event_is_recorded_and_noticed_and_credits_nothing(world):
    async def go(rig):
        res = await payments.start_topup(rig.db, rig.provider, world.a, amount_minor=2000, currency=CURRENCY, payment_method="pm_fake_ok", key="pay-f")
        pi = dict(rig.fake.payment_intents[res.provider_payment_id], status="requires_payment_method",
                  last_payment_error={"code": "card_declined", "decline_code": "generic_decline", "message": "Your card was declined."})
        # the provider reports a failure for a payment we had not yet seen succeed: simulate a row still processing
        async with rig.db.tenant(world.a) as conn:
            await conn.execute("update billing_payment set status = 'processing'")
        got = await deliver(rig, rig.events.payment_intent("payment_intent.payment_failed", pi))
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
            note = await conn.fetchrow("select * from billing_notification")
        return got, row, note, await counts(rig.db, world.a)
    got, row, note, c = with_rig(go)
    assert got.status == "processed" and (row["status"], row["failure_code"], row["credited_minor"]) == ("failed", "generic_decline", 0)
    assert note["kind"] == "payment_failed" and c["spendable"] == 0 and c["billing_transaction"] == 0


def test_a_failure_event_that_arrives_after_the_payment_succeeded_changes_nothing(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-late")
        pi = dict(rig.fake.payment_intents[res.provider_payment_id], last_payment_error={"code": "card_declined", "message": "x"})
        got = await deliver(rig, rig.events.payment_intent("payment_intent.payment_failed", pi))
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_payment")
        return got, row, await counts(rig.db, world.a)
    got, row, c = with_rig(go)
    assert got.status == "ignored" and row["status"] == "succeeded" and c["spendable"] == 2000 and c["billing_notification"] == 0


def test_a_success_that_arrives_after_a_recorded_failure_is_credited_once_because_the_provider_has_the_last_word(world):
    async def go(rig):
        res = await payments.start_topup(rig.db, rig.provider, world.a, amount_minor=2000, currency=CURRENCY, payment_method="pm_fake_declined", key="pay-retry")
        assert res.status == "failed"
        pi = dict(rig.fake.payment_intents[res.provider_payment_id], status="succeeded", last_payment_error=None)
        got = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        again = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return got, again, await counts(rig.db, world.a)
    got, again, c = with_rig(go)
    assert got.status == "processed" and c["spendable"] == 2000 and c["billing_transaction"] == 1 and "replayed" in again.detail


# ---- out of order ---------------------------------------------------------------------------------------------------------------------------

def test_a_refund_that_beats_its_payment_is_held_and_applied_when_the_payment_succeeds(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-ooo", deliver=False)
        refund_event = rig.events.charge_refunded(res.provider_payment_id, 2000, 800, "aud")
        early = await deliver(rig, refund_event)
        spend_early = (await counts(rig.db, world.a))["spendable"]
        ok = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id]))
        stored = await events_stored(rig, world.a)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.assert_reconciled(conn, world.a)
        return early, spend_early, ok, stored, await counts(rig.db, world.a), rec
    early, spend_early, ok, stored, c, rec = with_rig(go)
    assert early.status == "held" and spend_early == 0 and ok.status == "processed"
    assert [s for _, s in stored] == ["processed", "processed"] and c["spendable"] == 1200 and c["billing_transaction"] == 2 and rec.ok


def test_an_older_refund_total_arriving_after_a_newer_one_reverses_nothing_more(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 3000, "pay-old")
        newer = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 3000, 2000, "aud"))
        older = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 3000, 1000, "aud"))
        return newer, older, await counts(rig.db, world.a)
    newer, older, c = with_rig(go)
    assert newer.status == "processed" and older.status == "ignored" and c["spendable"] == 1000


def test_a_refund_the_wallet_cannot_cover_is_held_with_a_notice_and_applies_once_the_balance_is_back(world):
    async def go(rig):
        from app.billing import wallet
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-spent")
        async with rig.db.tenant(world.a) as conn:
            await conn.execute("insert into billing_usage_price (tenant_id, event_type, currency, unit_price_minor) values ($1::uuid, 'sync_run', 'AUD', 100)", world.a)
            await wallet.debit_usage(conn, world.a, event_type="sync_run", quantity=19, currency=CURRENCY, key="spent")      # 1900 of the 2000 is gone
        held = await deliver(rig, rig.events.charge_refunded(res.provider_payment_id, 2000, 1000, "aud"))
        mid = await counts(rig.db, world.a)
        async with rig.db.tenant(world.a) as conn:
            await wallet.top_up(conn, world.a, amount_minor=1000, currency=CURRENCY, provider_ref="pi_other_topup")
        moved = await webhooks.reprocess_held(rig.db, world.a)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.assert_reconciled(conn, world.a)
        return held, mid, moved, await counts(rig.db, world.a), rec
    held, mid, moved, c, rec = with_rig(go)
    assert held.status == "held" and mid["spendable"] == 100 and mid["billing_notification"] == 1
    assert [m.status for m in moved] == ["processed"] and c["spendable"] == 100 and rec.ok


def test_an_event_for_nothing_of_ours_is_unroutable_answers_503_and_writes_nothing(world):
    async def go(rig):
        event = rig.events.payment_intent("payment_intent.succeeded", {"id": "pi_unknown", "amount": 5, "currency": "aud", "metadata": {}})
        body, header = rig.events.signed(event)
        with pytest.raises(WebhookUnroutable):
            await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
        answer = await webhooks.handle_http(rig.db, rig.router, body, header, SECRETS)
        return answer, await counts(rig.db, world.a), await counts(rig.db, world.b)
    (code, body), a, b = with_rig(go)
    assert code == 503 and a["billing_webhook_event"] == b["billing_webhook_event"] == 0


def test_an_event_that_names_another_tenant_than_its_payment_belongs_to_is_refused_not_credited(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 1000, "pay-mix", deliver=False)
        pi = dict(rig.fake.payment_intents[res.provider_payment_id])
        pi["metadata"] = dict(pi["metadata"], tenant_id=world.b)
        event = rig.events.payment_intent("payment_intent.succeeded", pi)
        body, header = rig.events.signed(event)
        with pytest.raises(WebhookUnroutable):
            await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
        return await counts(rig.db, world.a), await counts(rig.db, world.b)
    a, b = with_rig(go)
    assert a["spendable"] == b["spendable"] == 0


def test_a_payment_event_is_routed_by_our_payment_id_when_the_response_carrying_the_provider_id_was_lost(world):
    async def go(rig):
        rig.fake.lose_response_after_execution(4)
        rig.fake.search_lag = True
        res = await payments.start_topup(rig.db, rig.provider, world.a, amount_minor=1200, currency=CURRENCY, payment_method="pm_fake_ok", key="pay-lost")
        assert res.provider_payment_id is None
        (pi,) = rig.fake.payment_intents.values()
        got = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return got, await counts(rig.db, world.a), rig.fake
    got, c, fake = with_rig(go)
    assert got.tenant_id == world.a and c["spendable"] == 1200 and c["billing_payment"] == 1 and len(fake.charges) == 1


# ---- one transaction with the ledger ------------------------------------------------------------------------------------------------------------

def test_a_fault_while_applying_rolls_back_the_event_row_with_the_ledger_and_the_providers_retry_applies_it(world, monkeypatch):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 2000, "pay-boom", deliver=False)
        event = rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id])
        body, header = rig.events.signed(event)
        real = payments.settle_succeeded

        async def boom(conn, tenant_id, intent, at=None):
            await real(conn, tenant_id, intent, at=at)          # the ledger lines are written ...
            raise RuntimeError("the database went away")        # ... and then the handler dies

        monkeypatch.setattr(payments, "settle_succeeded", boom)
        with pytest.raises(RuntimeError):
            await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
        after_fault = await counts(rig.db, world.a)
        monkeypatch.setattr(payments, "settle_succeeded", real)
        retry = await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
        return after_fault, retry, await counts(rig.db, world.a)
    after_fault, retry, c = with_rig(go)
    assert (after_fault["billing_webhook_event"], after_fault["billing_transaction"], after_fault["spendable"]) == (0, 0, 0)
    assert retry.status == "processed" and (c["billing_webhook_event"], c["billing_transaction"], c["spendable"]) == (1, 1, 2000)


def test_an_event_type_this_module_does_not_act_on_is_stored_as_ignored_once(world):
    async def go(rig):
        res, _ = await topup_via_provider(rig, world.a, 1000, "pay-ign", deliver=False)
        pi = rig.fake.payment_intents[res.provider_payment_id]
        # routed by the payment intent, but a type we do not handle
        event = rig.events.event("payment_intent.created", pi)
        got = await webhooks.process(rig.db, world.a, event)
        again = await webhooks.process(rig.db, world.a, event)
        return got, again, await events_stored(rig, world.a)
    got, again, stored = with_rig(go)
    assert (got.status, again.status) == ("ignored", "duplicate") and stored == [(got.event_id, "ignored")]


# ---- subscriptions -----------------------------------------------------------------------------------------------------------------------------

def test_a_paid_invoice_books_the_money_the_credits_and_the_revenue_once(world):
    async def go(rig):
        await subscribe(rig, world.a)
        ev = rig.events.invoice("invoice.paid", "cus_a", 2900, "aud", "in_1")
        first = await deliver(rig, ev)
        again = await deliver(rig, ev)
        other = await deliver(rig, rig.events.invoice("invoice.paid", "cus_a", 2900, "aud", "in_1"))      # a second event object, same invoice
        async with rig.db.tenant(world.a) as conn:
            rev = await conn.fetchval("select -balance_minor from billing_account where kind = 'platform_revenue'")
            rec = await reports.assert_reconciled(conn, world.a)
        return first, again, other, await counts(rig.db, world.a), rev, rec
    first, again, other, c, rev, rec = with_rig(go)
    assert first.status == "processed" and again.status == "duplicate" and "replayed" in other.detail
    assert (c["spendable"], rev, c["billing_transaction"]) == (2500, 400, 1) and rec.ok


def test_the_credits_a_payment_earns_are_the_plans_credits_in_proportion_rounded_down_and_never_more_than_the_plan():
    cases = [(2500, 2900, 2900, 2500), (2500, 2900, 1450, 1250), (2500, 2900, 1, 0), (2500, 2900, 99999, 2500), (2500, 0, 100, 0), (2500, 2900, 0, 0), (9000, 9900, 5960, 5418)]
    for credits, price, paid, want in cases:
        assert subscriptions.credits_for_payment(credits, price, paid) == want


def test_a_failed_invoice_makes_the_plan_past_due_with_a_notice_and_a_later_paid_invoice_makes_it_active_again(world):
    async def go(rig):
        await subscribe(rig, world.a)
        t = NOW
        failed = await deliver(rig, rig.events.invoice("invoice.payment_failed", "cus_a", 0, "aud", "in_f", created=t))
        past = await sub_row(rig, world.a)
        paid = await deliver(rig, rig.events.invoice("invoice.paid", "cus_a", 2900, "aud", "in_g", created=t + 60))
        return failed, past, paid, await sub_row(rig, world.a), await counts(rig.db, world.a)
    failed, past, paid, now_row, c = with_rig(go)
    assert failed.status == "processed" and (past["status"], past["past_due_since"] is not None) == ("past_due", True)
    assert now_row["status"] == "active" and now_row["past_due_since"] is None and c["billing_notification"] == 1 and c["spendable"] == 2500


def test_a_failed_invoice_never_credits_the_wallet(world):
    async def go(rig):
        await subscribe(rig, world.a)
        await deliver(rig, rig.events.invoice("invoice.payment_failed", "cus_a", 2900, "aud", "in_nope"))
        return await counts(rig.db, world.a)
    c = with_rig(go)
    assert c["spendable"] == 0 and c["billing_transaction"] == 0


def test_an_older_subscription_event_arriving_late_does_not_undo_a_newer_state(world):
    async def go(rig):
        await subscribe(rig, world.a)
        t = NOW
        await deliver(rig, rig.events.invoice("invoice.payment_failed", "cus_a", 0, "aud", "in_f", created=t))
        late = await deliver(rig, rig.events.subscription("customer.subscription.updated", "sub_a", "cus_a", "active", created=t - 500))
        return late, await sub_row(rig, world.a)
    late, row = with_rig(go)
    assert late.status == "ignored" and row["status"] == "past_due"


def test_a_subscription_update_follows_the_providers_status_and_switches_the_plan_by_price_id(world):
    async def go(rig):
        await subscribe(rig, world.a)
        got = await deliver(rig, rig.events.subscription("customer.subscription.updated", "sub_a", "cus_a", "past_due", price_id="price_fake_growth", created=NOW))
        return got, await sub_row(rig, world.a)
    got, row = with_rig(go)
    assert got.status == "processed" and (row["status"], row["plan_code"], row["provider_status"]) == ("past_due", "growth", "past_due")


def test_a_provider_status_we_have_no_mapping_for_changes_nothing_but_the_status_word(world):
    async def go(rig):
        await subscribe(rig, world.a)
        await deliver(rig, rig.events.subscription("customer.subscription.updated", "sub_a", "cus_a", "paused", created=NOW))
        return await sub_row(rig, world.a)
    row = with_rig(go)
    assert (row["status"], row["provider_status"]) == ("active", "paused")


def test_a_canceled_subscription_is_final_so_no_late_event_revives_it_but_a_late_payment_is_still_booked(world):
    async def go(rig):
        await subscribe(rig, world.a)
        t = NOW
        gone = await deliver(rig, rig.events.subscription("customer.subscription.deleted", "sub_a", "cus_a", "canceled", created=t))
        revive = await deliver(rig, rig.events.subscription("customer.subscription.updated", "sub_a", "cus_a", "active", created=t + 100))
        failed = await deliver(rig, rig.events.invoice("invoice.payment_failed", "cus_a", 0, "aud", "in_z", created=t + 200))
        paid = await deliver(rig, rig.events.invoice("invoice.paid", "cus_a", 2900, "aud", "in_y", created=t + 300))
        return gone, revive, failed, paid, await sub_row(rig, world.a), await counts(rig.db, world.a)
    gone, revive, failed, paid, row, c = with_rig(go)
    assert gone.status == "processed" and (revive.status, failed.status) == ("ignored", "ignored") and row["status"] == "canceled" and row["canceled_at"] is not None
    assert paid.status == "processed" and c["spendable"] == 2500        # the money was received, so it is booked; the plan stays canceled


def test_an_invoice_in_another_currency_than_the_plan_is_held_and_books_nothing(world):
    async def go(rig):
        await subscribe(rig, world.a)
        got = await deliver(rig, rig.events.invoice("invoice.paid", "cus_a", 2900, "usd", "in_usd"))
        return got, await counts(rig.db, world.a)
    got, c = with_rig(go)
    assert got.status == "held" and "USD" in got.detail and c["billing_transaction"] == 0


def test_an_invoice_for_a_tenant_with_no_subscription_row_yet_is_unroutable_and_retried_later(world):
    async def go(rig):
        event = rig.events.invoice("invoice.paid", "cus_nobody", 2900, "aud", "in_x")
        body, header = rig.events.signed(event)
        return await webhooks.handle_http(rig.db, rig.router, body, header, SECRETS)
    assert with_rig(go)[0] == 503


def test_two_customers_never_get_each_others_invoices(world):
    async def go(rig):
        await subscribe(rig, world.a, customer="cus_a", sub="sub_a")
        await subscribe(rig, world.b, plan="growth", customer="cus_b", sub="sub_b")
        await deliver(rig, rig.events.invoice("invoice.paid", "cus_b", 9900, "aud", "in_b"))
        return await counts(rig.db, world.a), await counts(rig.db, world.b)
    a, b = with_rig(go)
    assert a["spendable"] == 0 and b["spendable"] == 9000


def test_the_newer_api_shape_where_the_invoice_names_its_subscription_under_parent_is_routed_too(world):
    async def go(rig):
        await subscribe(rig, world.a)
        ev = rig.events.invoice("invoice.paid", "cus_unlisted", 2900, "aud", "in_np")
        ev["data"]["object"]["parent"] = {"subscription_details": {"subscription": "sub_a"}}
        return await deliver(rig, ev)
    assert with_rig(go).tenant_id == world.a
