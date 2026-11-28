"""Invoices against real Postgres and the fake Stripe: a draft with lines, finalize, pay from the wallet, by card or by bank debit, void, refund in full or in part, and the
commission each of those moves.

The rules under test, each paired with the case that must still work:
  - an invoice is paid exactly once, by whichever route reaches it first (a second payment, a replayed event and a second event object all write nothing);
  - the wallet pays an invoice only with enough credit, and a refusal writes nothing at all;
  - a card or bank debit paid invoice never touches the wallet; a bank debit leaves the invoice open until the signed event, and a return leaves it open and pays nothing;
  - while a payment is in flight the invoice can be neither paid again nor voided; a draft cannot be paid and a paid invoice cannot be voided;
  - a refund never passes the total, is asked once however often it is repeated, and the books (billing_discrepancies and billing_invoice_discrepancies) agree after every flow;
  - the sales rep's commission is a percentage rounded down in integer minor units, and the refunds reverse it to the percentage of what is still paid.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from portal_fixtures import ACH_PM, CUSTOMER, MANDATE, USD, as_owner_count, needs_db, new_key, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN, SECRETS, counts  # noqa: E402

pytestmark = needs_db

from app.billing import commissions, invoices, payments, reports, wallet, webhooks  # noqa: E402
from app.billing.errors import IdempotencyConflict, InvalidInvoice, InvoiceNotFound, InvoicePaymentPending, InvoiceStateError  # noqa: E402

LINES = [{"description": "Consultation kit", "quantity": 3, "unit_amount_minor": 2500}, {"description": "Courier", "quantity": 1, "unit_amount_minor": 1500}]      # 9000


@pytest.fixture()
def world():
    class W:
        pass
    w = W()
    w.a, w.b = reset_world("tenant-a", "tenant-b")
    return w


async def admin_db(rig, world):
    return rig.acting(world.a, "admin")


async def open_invoice(rig, world, *, clinic="c1", lines=LINES, key=None, db=None):
    db = db or rig.acting(world.a, "admin")
    inv, _ = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics[clinic], currency=USD, lines=lines, key=key or new_key(), actor="admin")
    return await invoices.finalize(db, world.a.tenant, inv["id"], actor="admin")


async def fund(rig, world, amount, key="fund-1"):
    res = await payments.start_topup(rig.db, rig.provider, world.a.tenant, amount_minor=amount, currency=USD, payment_method="pm_fake_ok", key=key)
    pi = rig.fake.payment_intents[res.provider_payment_id]
    body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", pi))
    await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)


async def deliver(rig, event):
    body, header = rig.events.signed(event)
    return await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)


async def books(rig, tenant):
    async with rig.db.tenant(tenant) as conn:
        return await reports.reconcile(conn, tenant)


async def commission_rows(rig, world, invoice_id):
    async with rig.acting(world.a, "admin").tenant(world.a.tenant) as conn:
        return await commissions.list_commissions(conn, invoice_id=invoice_id)


async def tx_count(world, kind):
    return await as_owner_count("select count(*) from billing_transaction where tenant_id = $1::uuid and kind = $2", world.a.tenant, kind)


# ---- draft and finalize ------------------------------------------------------------------------------------------------------------------

def test_a_draft_has_its_lines_and_no_total_until_it_is_finalized_and_then_the_total_is_the_sum_of_the_lines(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        draft, replayed = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="inv-1", actor="admin")
        again, again_replayed = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="inv-1", actor="admin")
        done = await invoices.finalize(db, world.a.tenant, draft["id"], actor="admin")
        done_twice = await invoices.finalize(db, world.a.tenant, draft["id"], actor="admin")
        return draft, replayed, again, again_replayed, done, done_twice
    draft, replayed, again, again_replayed, done, done_twice = with_portal(go)
    assert (draft["status"], draft["total_minor"], replayed) == ("draft", 0, False) and [l["amount_minor"] for l in draft["lines"]] == [7500, 1500]
    assert again["id"] == draft["id"] and again_replayed is True
    assert (done["status"], done["total_minor"]) == ("open", 9000) and done_twice == done and done["number"].startswith("INV-")


def test_the_same_key_with_other_lines_is_refused_and_a_bad_invoice_writes_nothing(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="inv-1", actor="admin")
        with pytest.raises(IdempotencyConflict):
            await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES[:1], key="inv-1", actor="admin")
        with pytest.raises(IdempotencyConflict):
            await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c2"], currency=USD, lines=LINES, key="inv-1", actor="admin")
        bad = [[], LINES * 30, [{"description": "x", "quantity": 0, "unit_amount_minor": 5}], [{"description": "x", "quantity": 1, "unit_amount_minor": 1.5}],
               [{"description": "x", "quantity": True, "unit_amount_minor": 5}], [{"description": "", "quantity": 1, "unit_amount_minor": 5}], [{"description": "x", "quantity": 1, "unit_amount_minor": -5}]]
        for i, lines in enumerate(bad):
            with pytest.raises(InvalidInvoice):
                await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=lines, key="bad-%d" % i, actor="admin")
        with pytest.raises(InvalidInvoice):
            await invoices.create_draft(db, world.a.tenant, clinic_id=world.b.clinics["c1"], currency=USD, lines=LINES, key="other-tenants-clinic", actor="admin")
        return await as_owner_count("select count(*) from billing_invoice"), await as_owner_count("select count(*) from billing_invoice_line")
    assert with_portal(go) == (1, 2)


def test_a_draft_cannot_be_paid_a_finalized_invoice_has_frozen_lines_and_an_empty_one_cannot_be_finalized(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        draft, _ = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="d", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.pay(db, rig.provider, world.a.tenant, draft["id"], method="wallet", key="p", actor="admin")
        await invoices.finalize(db, world.a.tenant, draft["id"], actor="admin")
        async with db.tenant(world.a.tenant) as conn:
            with pytest.raises(Exception, match="cannot change"):
                await conn.execute("insert into billing_invoice_line (tenant_id, invoice_id, clinic_id, position, description, quantity, unit_amount_minor, amount_minor) "
                                   "values ($1::uuid, $2::uuid, $3::uuid, 9, 'late', 1, 100, 100)", world.a.tenant, draft["id"], world.a.clinics["c1"])
        return True
    assert with_portal(go)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select id, tenant_id, clinic_id from billing_invoice limit 1")
        inv, tenant, clinic = cur.fetchone()
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update billing_invoice_line set quantity = 9, amount_minor = unit_amount_minor * 9 where invoice_id = %s", (inv,))
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update billing_invoice set total_minor = 1 where id = %s", (inv,))
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("delete from billing_invoice where id = %s", (inv,))
        # the control: a draft invoice takes a new line
        cur.execute("insert into billing_invoice (tenant_id, clinic_id, currency, idempotency_key, request_hash, created_by) values (%s, %s, 'USD', 'ctl', 'h', 'x') returning id", (tenant, clinic))
        draft = cur.fetchone()[0]
        cur.execute("insert into billing_invoice_line (tenant_id, invoice_id, clinic_id, position, description, quantity, unit_amount_minor, amount_minor) values (%s, %s, %s, 1, 'ok', 2, 50, 100)",
                    (tenant, draft, clinic))
        with pytest.raises(psycopg.errors.CheckViolation):          # a line whose amount is not its product
            cur.execute("insert into billing_invoice_line (tenant_id, invoice_id, clinic_id, position, description, quantity, unit_amount_minor, amount_minor) values (%s, %s, %s, 2, 'bad', 2, 50, 99)",
                        (tenant, draft, clinic))


def test_a_total_that_is_not_the_sum_of_the_lines_cannot_be_finalized_by_hand(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        draft, _ = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="d", actor="admin")
        async with db.tenant(world.a.tenant) as conn:
            with pytest.raises(Exception, match="cannot be 1"):
                await conn.execute("update billing_invoice set status = 'open', total_minor = 1 where id = $1::uuid", draft["id"])
        async with db.tenant(world.a.tenant) as conn:
            await conn.execute("update billing_invoice set status = 'open', total_minor = 9000 where id = $1::uuid", draft["id"])       # the control: the right total goes through
        return True
    assert with_portal(go)


# ---- paying from the wallet --------------------------------------------------------------------------------------------------------------

def test_paying_from_the_wallet_debits_it_once_and_a_second_payment_is_a_replay(world):
    async def go(rig):
        await fund(rig, world, 20000)
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        first = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p1", actor="admin")
        second = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p2", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p3", actor="admin")
        return first, second, await counts(rig.db, world.a.tenant), await tx_count(world, "invoice_payment"), await books(rig, world.a.tenant)
    first, second, c, tx, report = with_portal(go)
    assert (first.status, first.replayed, first.invoice["status"], first.invoice["pay_method"]) == ("paid", False, "paid", "wallet") and second.replayed and second.status == "paid"
    assert c["spendable"] == 11000 and tx == 1 and report.ok


def test_not_enough_credit_writes_nothing_and_the_invoice_stays_open(world):
    async def go(rig):
        await fund(rig, world, 4000)
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        res = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p1", actor="admin")
        after_refusal = await counts(rig.db, world.a.tenant), await tx_count(world, "invoice_payment"), (await invoices.get_invoice(db, world.a.tenant, inv["id"]))["status"]
        await fund(rig, world, 5000, key="fund-2")       # the control: with the credit in, the same invoice is paid
        ok = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p2", actor="admin")
        return res, after_refusal, ok, await counts(rig.db, world.a.tenant)
    res, (c, tx, status), ok, c2 = with_portal(go)
    assert (res.status, res.shortfall_minor, status, tx, c["spendable"]) == ("insufficient_funds", 5000, "open", 0, 4000)
    assert ok.status == "paid" and c2["spendable"] == 0


# ---- paying by card ------------------------------------------------------------------------------------------------------------------------

def test_paying_by_card_charges_once_pays_the_invoice_and_leaves_the_wallet_alone(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        res = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p1", actor="admin")
        again = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p1", actor="admin")
        pi = rig.fake.payment_intents[res.invoice and (await payment_provider_id(rig, world, res.payment_id))]
        late = await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", pi))
        return res, again, late, rig.fake, await counts(rig.db, world.a.tenant), await books(rig, world.a.tenant), await payment_row(rig, world, res.payment_id), pi
    res, again, late, fake, c, report, row, pi = with_portal(go)
    assert (res.status, res.invoice["status"], res.invoice["pay_method"]) == ("paid", "paid", "card") and again.status == "paid" and again.replayed
    assert fake.executions == 1 and len(fake.charges) == 1 and late.status == "processed"
    assert c["spendable"] == 0 and c["billing_transaction"] == 1 and report.ok                      # one invoice payment in the ledger, no wallet credit
    assert (row["purpose"], row["credited_minor"], row["status"]) == ("invoice", 0, "succeeded") and pi["metadata"]["invoice_id"] == res.invoice["id"]


async def payment_provider_id(rig, world, payment_id):
    return (await payment_row(rig, world, payment_id))["provider_payment_id"]


async def payment_row(rig, world, payment_id):
    async with rig.db.tenant(world.a.tenant) as conn:
        return await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)


def test_a_declined_card_leaves_the_invoice_open_and_a_retry_with_a_new_key_pays_it_once(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        declined = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_declined", key="p1", actor="admin")
        mid = (await invoices.get_invoice(db, world.a.tenant, inv["id"]))["status"], await tx_count(world, "invoice_payment")
        ok = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p2", actor="admin")
        return declined, mid, ok, len(rig.fake.charges), await books(rig, world.a.tenant)
    declined, mid, ok, charges, report = with_portal(go)
    assert declined.status == "failed" and declined.failure_code == "insufficient_funds" and mid == ("open", 0)
    assert ok.status == "paid" and charges == 1 and report.ok


# ---- paying by bank debit ------------------------------------------------------------------------------------------------------------------

def test_a_bank_debit_leaves_the_invoice_open_until_the_succeeded_event_and_then_pays_it_once(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        res = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="us_bank_account", payment_method=ACH_PM, customer=CUSTOMER, mandate=MANDATE, key="p1", actor="admin")
        mid = (await invoices.get_invoice(db, world.a.tenant, inv["id"]))["status"], await tx_count(world, "invoice_payment")
        with pytest.raises(InvoicePaymentPending):
            await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="us_bank_account", payment_method=ACH_PM, customer=CUSTOMER, mandate=MANDATE, key="p2", actor="admin")
        with pytest.raises(InvoicePaymentPending):
            await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p3", actor="admin")
        with pytest.raises(InvoicePaymentPending):
            await invoices.void(db, world.a.tenant, inv["id"], actor="admin")
        same = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="us_bank_account", payment_method=ACH_PM, customer=CUSTOMER, mandate=MANDATE, key="p1", actor="admin")
        pi = rig.fake.settle_ach(await payment_provider_id(rig, world, res.payment_id))
        ev = rig.events.payment_intent("payment_intent.succeeded", pi)
        first = await deliver(rig, ev)
        again = await deliver(rig, ev)
        after = await invoices.get_invoice(db, world.a.tenant, inv["id"])
        return res, mid, same, first, again, after, await counts(rig.db, world.a.tenant), await tx_count(world, "invoice_payment"), await books(rig, world.a.tenant)
    res, mid, same, first, again, after, c, tx, report = with_portal(go)
    assert res.status == "processing" and mid == ("open", 0) and same.status == "processing" and same.replayed
    assert first.status == "processed" and again.status == "duplicate"
    assert (after["status"], after["pay_method"]) == ("paid", "us_bank_account") and tx == 1 and c["spendable"] == 0 and report.ok


def test_a_returned_bank_debit_leaves_the_invoice_open_pays_nothing_and_it_can_be_paid_again(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        res = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="us_bank_account", payment_method=ACH_PM, customer=CUSTOMER, mandate=MANDATE, key="p1", actor="admin")
        pi = rig.fake.fail_ach(await payment_provider_id(rig, world, res.payment_id), code="account_closed")
        ev = rig.events.payment_intent("payment_intent.payment_failed", pi)
        first = await deliver(rig, ev)
        again = await deliver(rig, ev)
        mid = (await invoices.get_invoice(db, world.a.tenant, inv["id"]))["status"], await tx_count(world, "invoice_payment"), (await payment_row(rig, world, res.payment_id))["status"]
        retry = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p2", actor="admin")
        return first, again, mid, retry, await books(rig, world.a.tenant)
    first, again, mid, retry, report = with_portal(go)
    assert (first.status, again.status) == ("processed", "duplicate") and mid == ("open", 0, "failed")
    assert retry.status == "paid" and report.ok


# ---- void ---------------------------------------------------------------------------------------------------------------------------------

def test_a_draft_and_an_open_invoice_can_be_voided_a_paid_one_cannot_and_a_void_one_cannot_be_paid(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        draft, _ = await invoices.create_draft(db, world.a.tenant, clinic_id=world.a.clinics["c1"], currency=USD, lines=LINES, key="d1", actor="admin")
        voided_draft = await invoices.void(db, world.a.tenant, draft["id"], actor="admin")
        voided_twice = await invoices.void(db, world.a.tenant, draft["id"], actor="admin")
        opened = await open_invoice(rig, world)
        voided_open = await invoices.void(db, world.a.tenant, opened["id"], actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.pay(db, rig.provider, world.a.tenant, opened["id"], method="wallet", key="p", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.finalize(db, world.a.tenant, opened["id"], actor="admin")
        await fund(rig, world, 20000)
        paid = await open_invoice(rig, world)
        await invoices.pay(db, rig.provider, world.a.tenant, paid["id"], method="wallet", key="pp", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.void(db, world.a.tenant, paid["id"], actor="admin")
        with pytest.raises(InvoiceNotFound):
            await invoices.void(db, world.a.tenant, "not-a-uuid", actor="admin")
        return voided_draft, voided_twice, voided_open, await books(rig, world.a.tenant)
    voided_draft, voided_twice, voided_open, report = with_portal(go)
    assert voided_draft["status"] == "void" and voided_twice == voided_draft and voided_open["status"] == "void" and report.ok


# ---- refunds ---------------------------------------------------------------------------------------------------------------------------------

def test_a_wallet_paid_invoice_is_refunded_in_part_then_in_full_and_never_beyond_the_total(world):
    async def go(rig):
        await fund(rig, world, 20000)
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p1", actor="admin")
        part = await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=2000, key="r1", actor="admin")
        replay = await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=2000, key="r1", actor="admin")
        with pytest.raises(IdempotencyConflict):
            await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=2500, key="r1", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=7001, key="r2", actor="admin")
        mid = (await counts(rig.db, world.a.tenant))["spendable"]
        rest = await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=7000, key="r3", actor="admin")
        with pytest.raises(InvoiceStateError):
            await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=1, key="r4", actor="admin")
        return part, replay, mid, rest, await invoices.get_invoice(db, world.a.tenant, inv["id"]), await counts(rig.db, world.a.tenant), await books(rig, world.a.tenant)
    part, replay, mid, rest, after, c, report = with_portal(go)
    assert (part.status, part.replayed, replay.replayed, replay.refund_id == part.refund_id) == ("refunded", False, True, True)
    assert mid == 13000 and rest.invoice["refunded_minor"] == 9000 and after["refunded_minor"] == 9000 and c["spendable"] == 20000 and report.ok


def test_a_card_paid_invoice_is_refunded_through_the_provider_and_booked_when_its_event_arrives(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        paid = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p1", actor="admin")
        pid = await payment_provider_id(rig, world, paid.payment_id)
        asked = await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=3000, key="r1", actor="admin")
        asked_again = await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=3000, key="r1", actor="admin")
        before_event = await invoices.get_invoice(db, world.a.tenant, inv["id"])
        ev = rig.events.charge_refunded(pid, 9000, 3000, currency="usd")
        first = await deliver(rig, ev)
        again = await deliver(rig, ev)
        older = await deliver(rig, rig.events.charge_refunded(pid, 9000, 1000, currency="usd"))
        await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=6000, key="r2", actor="admin")
        full = await deliver(rig, rig.events.charge_refunded(pid, 9000, 9000, currency="usd"))
        with pytest.raises(InvoiceStateError):
            await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=1, key="r9", actor="admin")
        return asked, asked_again, before_event, (first, again, older, full), await invoices.get_invoice(db, world.a.tenant, inv["id"]), rig.fake, await books(rig, world.a.tenant)
    asked, asked_again, before_event, events, after, fake, report = with_portal(go)
    assert (asked.status, asked.replayed, asked_again.replayed, asked.provider_refund_id == asked_again.provider_refund_id) == ("requested", False, True, True)
    assert before_event["refunded_minor"] == 0 and len(fake.refunds) == 2 and asked.provider_refund_id in fake.refunds       # linked to the provider's refund, asked once
    assert [e.status for e in events] == ["processed", "duplicate", "ignored", "processed"] and after["refunded_minor"] == 9000 and report.ok


def test_a_payment_made_for_an_invoice_is_refunded_through_its_invoice_and_not_as_a_top_up(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world)
        paid = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="card", payment_method="pm_fake_ok", key="p1", actor="admin")
        with pytest.raises(Exception, match="through its invoice"):
            await payments.request_refund(rig.db, rig.provider, world.a.tenant, paid.payment_id, amount_minor=100, key="r1")
        return True
    assert with_portal(go)


# ---- commissions -------------------------------------------------------------------------------------------------------------------------------

def test_the_commission_is_the_rate_of_the_total_rounded_down_and_is_reversed_to_the_rate_of_what_is_still_paid(world):
    async def go(rig):
        await fund(rig, world, 50000)
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world, clinic="c2", lines=[{"description": "Kit", "quantity": 1, "unit_amount_minor": 3333}])      # c2 pays rep1 1000 bp: 333.3 rounds to 333
        await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p1", actor="admin")
        accrued = [dict(r) for r in await commission_rows(rig, world, inv["id"])]
        await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=1000, key="r1", actor="admin")          # 2333 left: 233.3 -> 233, so 100 reversed
        after_part = [r["amount_minor"] for r in await commission_rows(rig, world, inv["id"])]
        await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=2333, key="r2", actor="admin")
        after_full = [r["amount_minor"] for r in await commission_rows(rig, world, inv["id"])]
        return accrued, after_part, after_full, await books(rig, world.a.tenant)
    accrued, after_part, after_full, report = with_portal(go)
    assert [(r["kind"], r["amount_minor"], r["basis_minor"], r["rate_bp"]) for r in accrued] == [("accrual", 333, 3333, 1000)]
    assert after_part == [333, -100] and after_full == [333, -100, -233] and sum(after_full) == 0 and report.ok


def test_a_clinic_with_no_rep_earns_nobody_a_commission_and_a_later_rate_change_does_not_touch_a_paid_invoice(world):
    async def go(rig):
        await fund(rig, world, 50000)
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world, clinic="c1")
        await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="wallet", key="p1", actor="admin")
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("update sales_rep_assignment set commission_bp = 5000 where clinic_id = %s", (world.a.clinics["c1"],))
            cur.execute("insert into clinic (tenant_id, code, name) values (%s, 'norep', 'No Rep') returning id", (world.a.tenant,))
            norep = str(cur.fetchone()[0])
        await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=4500, key="r1", actor="admin")           # reversed at the rate the invoice was paid at (500 bp)
        rows = [(r["kind"], r["amount_minor"], r["rate_bp"]) for r in await commission_rows(rig, world, inv["id"])]
        lonely, _ = await invoices.create_draft(db, world.a.tenant, clinic_id=norep, currency=USD, lines=LINES, key="norep", actor="admin")
        await invoices.finalize(db, world.a.tenant, lonely["id"], actor="admin")
        await invoices.pay(db, rig.provider, world.a.tenant, lonely["id"], method="wallet", key="p2", actor="admin")
        return rows, await commission_rows(rig, world, lonely["id"]), await books(rig, world.a.tenant)
    rows, none, report = with_portal(go)
    assert rows == [("accrual", 450, 500), ("reversal", -225, 500)] and none == [] and report.ok


def test_a_commission_follows_a_bank_debit_paid_invoice_and_its_refund_by_event(world):
    async def go(rig):
        db = rig.acting(world.a, "admin")
        inv = await open_invoice(rig, world, clinic="c3")                       # rep2 at 250 bp
        res = await invoices.pay(db, rig.provider, world.a.tenant, inv["id"], method="us_bank_account", payment_method=ACH_PM, customer=CUSTOMER, mandate=MANDATE, key="p1", actor="admin")
        none_yet = await commission_rows(rig, world, inv["id"])
        pid = await payment_provider_id(rig, world, res.payment_id)
        await deliver(rig, rig.events.payment_intent("payment_intent.succeeded", rig.fake.settle_ach(pid)))
        paid_rows = [r["amount_minor"] for r in await commission_rows(rig, world, inv["id"])]
        await invoices.refund(db, rig.provider, world.a.tenant, inv["id"], amount_minor=9000, key="r1", actor="admin")
        await deliver(rig, rig.events.charge_refunded(pid, 9000, 9000, currency="usd"))
        return none_yet, paid_rows, [r["amount_minor"] for r in await commission_rows(rig, world, inv["id"])], await books(rig, world.a.tenant)
    none_yet, paid_rows, final, report = with_portal(go)
    assert none_yet == [] and paid_rows == [225] and final == [225, -225] and report.ok


def test_commission_for_is_integer_floor_never_above_the_exact_percentage_and_monotone():
    rng = random.Random(7)
    for _ in range(3000):
        basis, bp = rng.randrange(0, 10 ** 9), rng.randrange(0, 10001)
        got = commissions.commission_for(basis, bp)
        assert isinstance(got, int) and got * 10000 <= basis * bp < (got + 1) * 10000
        assert commissions.commission_for(basis + 1, bp) >= got and commissions.commission_for(basis, min(bp + 1, 10000)) >= got
    assert commissions.commission_for(0, 500) == 0 and commissions.commission_for(10000, 10000) == 10000 and commissions.commission_for(1, 9999) == 0
    for bad in ((-1, 5), (5, -1), (5, 10001)):
        with pytest.raises(ValueError):
            commissions.commission_for(*bad)


# ---- the books, over many seeded worlds, against a model ---------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(12))
def test_over_seeded_random_worlds_the_wallet_the_invoices_and_the_commissions_agree_with_a_model_and_the_books_reconcile(world, seed):
    """Each world plays 14 steps: fund, make invoices on random clinics, pay by wallet, card or bank debit, clear or return a debit, refund part or all, void, and replay events. A
    model of the money (wallet, per invoice paid and refunded, commission) is kept beside it and compared at the end; the books must reconcile after every world."""
    rng = random.Random(1000 + seed)

    async def go(rig):
        db = rig.acting(world.a, "admin")
        wallet_model, model = 0, {}              # invoice id -> {"total", "status", "method", "refunded", "bp", "pid"}
        rates = {"c1": 500, "c2": 1000, "c3": 250}
        events: list = []
        for step in range(14):
            action = rng.choice(["fund", "make", "make", "pay", "pay", "clear", "refund", "void", "replay"])
            open_ids = [i for i, m in model.items() if m["status"] == "open" and not m.get("pid")]
            paid_ids = [i for i, m in model.items() if m["status"] == "paid" and m["refunded"] < m["total"]]
            if action == "fund":
                amount = rng.choice([5000, 12000, 30000])
                await fund(rig, world, amount, key="f-%d-%d" % (seed, step))
                wallet_model += amount
            elif action == "make":
                clinic = rng.choice(["c1", "c2", "c3"])
                lines = [{"description": "L%d" % i, "quantity": rng.randrange(1, 4), "unit_amount_minor": rng.randrange(101, 4000)} for i in range(rng.randrange(1, 4))]
                inv = await open_invoice(rig, world, clinic=clinic, lines=lines)
                model[inv["id"]] = {"total": inv["total_minor"], "status": "open", "method": None, "refunded": 0, "bp": rates[clinic], "pid": None}
                assert inv["total_minor"] == sum(l["quantity"] * l["unit_amount_minor"] for l in lines)
            elif action == "pay" and open_ids:
                iid = rng.choice(open_ids)
                m = model[iid]
                method = rng.choice(["wallet", "card", "us_bank_account"])
                kw = {"payment_method": ACH_PM, "customer": CUSTOMER, "mandate": MANDATE} if method == "us_bank_account" else {"payment_method": "pm_fake_ok"} if method == "card" else {}
                res = await invoices.pay(db, rig.provider, world.a.tenant, iid, method=method, key="p-%d-%d" % (seed, step), actor="admin", **kw)
                if method == "wallet":
                    if m["total"] <= wallet_model:
                        assert res.status == "paid"
                        wallet_model -= m["total"]
                        m.update(status="paid", method="wallet")
                    else:
                        assert res.status == "insufficient_funds"
                elif method == "card":
                    assert res.status == "paid"
                    m.update(status="paid", method="card", pid=(await payment_provider_id(rig, world, res.payment_id)))
                else:
                    assert res.status == "processing"
                    m["pid"] = await payment_provider_id(rig, world, res.payment_id)
                    m["debit"] = "processing"
            elif action == "clear":
                debits = [i for i, m in model.items() if m.get("debit") == "processing"]
                if debits:
                    iid = rng.choice(debits)
                    m = model[iid]
                    if rng.random() < 0.6:
                        pi = rig.fake.settle_ach(m["pid"])
                        ev = rig.events.payment_intent("payment_intent.succeeded", pi)
                        m.update(status="paid", method="us_bank_account", debit="cleared")
                    else:
                        pi = rig.fake.fail_ach(m["pid"])
                        ev = rig.events.payment_intent("payment_intent.payment_failed", pi)
                        m.update(debit="returned", pid=None)
                    events.append(ev)
                    await deliver(rig, ev)
            elif action == "refund" and paid_ids:
                iid = rng.choice(paid_ids)
                m = model[iid]
                amount = rng.randrange(1, m["total"] - m["refunded"] + 1)
                res = await invoices.refund(db, rig.provider, world.a.tenant, iid, amount_minor=amount, key="r-%d-%d" % (seed, step), actor="admin")
                m["refunded"] += amount
                if m["method"] == "wallet":
                    wallet_model += amount
                else:
                    ev = rig.events.charge_refunded(m["pid"], m["total"], m["refunded"], currency="usd")
                    events.append(ev)
                    await deliver(rig, ev)
            elif action == "void" and open_ids:
                iid = rng.choice(open_ids)
                await invoices.void(db, world.a.tenant, iid, actor="admin")
                model[iid]["status"] = "void"
            elif action == "replay" and events:
                await deliver(rig, rng.choice(events))        # a repeat of any event ever sent changes nothing
        net = {}
        for r in await commission_rows(rig, world, None):
            net[r["invoice_id"]] = net.get(r["invoice_id"], 0) + r["amount_minor"]
        got_invoices = {i["id"]: i for i in await invoices.list_invoices(db, world.a.tenant)}
        return wallet_model, model, net, got_invoices, (await counts(rig.db, world.a.tenant))["spendable"], await books(rig, world.a.tenant)

    wallet_model, model, net, got, spendable, report = with_portal(go)
    assert spendable == wallet_model, "the wallet is what the model says"
    for iid, m in model.items():
        inv = got[iid]
        assert (inv["status"], inv["refunded_minor"]) == (m["status"], m["refunded"]), iid
        expected = (m["total"] - m["refunded"]) * m["bp"] // 10000 if m["status"] == "paid" else 0
        assert net.get(iid, 0) == expected, "the commission of %s is the rate of what is still paid" % iid
    assert report.ok, report.discrepancies
