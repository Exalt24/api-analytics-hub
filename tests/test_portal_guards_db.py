"""The guards of the ordering portal that the per cell tests cannot reach, against real Postgres:

  - every new table has row level security enabled AND forced;
  - each of the two layers under a table (the policies and the triggers) refuses on its own: the triggers are switched off and the policies must still refuse, then the policies are
    switched off and the triggers must still refuse. A table guarded twice would otherwise pass every test with one of its guards removed;
  - the policies read the matrix table at query time: a cell changed in the database table changes what is allowed, with no other edit;
  - the constraints of the invoice, its lines, its refunds and the commissions that no ordinary flow reaches (a refund beyond the total, a decreasing refunded total, a second live
    payment for one invoice, a moved clinic);
  - the proof functions: `billing_invoice_discrepancies` reports each of the four ways the stored invoices can disagree with the ledger.
"""
from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from portal_fixtures import needs_db, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN  # noqa: E402

pytestmark = needs_db

NEW_TABLES = ("clinic", "portal_user", "sales_rep_assignment", "billing_invoice", "billing_invoice_line", "billing_invoice_refund", "commission", "fulfillment_order",
              "fulfillment_submission", "fulfillment_dead_letter", "fulfillment_event", "fulfillment_audit")


class Facts:
    pass


@pytest.fixture()
def world():
    f = Facts()
    f.a, f.b = reset_world("tenant-a", "tenant-b")
    return f


def owner(sql: str, *args):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0] if cur.description else None


def invoice(world, clinic="c1", status="draft", total=0, method=None, refunded=0):
    paid_at = "now()" if status == "paid" else "null"
    return str(owner("insert into billing_invoice (tenant_id, clinic_id, currency, status, total_minor, pay_method, paid_at, refunded_minor, idempotency_key, request_hash, created_by) "
                     "values (%%s, %%s, 'USD', %%s, %%s, %%s, %s, %%s, %%s, 'h', 'owner') returning id" % paid_at,
                     world.a.tenant, world.a.clinics[clinic], status, total, method, refunded, uuid.uuid4().hex))


def order(world, clinic="c1"):
    return str(owner("insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by) values (%s, %s, %s, 'owner') returning id", world.a.tenant, world.a.clinics[clinic],
                     "ord_" + uuid.uuid4().hex[:24]))


@contextmanager
def switched(table: str, what: str):
    """Switch off a trigger (what = 'trigger <name>') or the row level security (what = 'rls') of a table as the owner, and switch it back on afterwards."""
    off, on = (("disable trigger %s" % what.split(" ", 1)[1], "enable trigger %s" % what.split(" ", 1)[1]) if what.startswith("trigger") else ("disable row level security", "enable row level security"))
    owner("alter table %s %s" % (table, off))
    try:
        yield
    finally:
        owner("alter table %s %s" % (table, on))


async def update_as(rig, world, label: str, sql: str, *args) -> str:
    """'ok n' when the statement ran (n rows), else the name of the refusal, as the user `label` on a fresh transaction."""
    role = {"clinic1": "clinic", "clinic2": "clinic", "prescriber1": "prescriber", "rep1": "sales_rep", "rep2": "sales_rep", "admin": "admin"}[label]
    try:
        async with rig.db.session(world.a.tenant, role, world.a.users[label]) as conn:
            status = await conn.execute(sql, *args)
        return "ok " + status.split()[-1]
    except asyncpg.PostgresError as exc:
        return type(exc).__name__


# ---- row level security is on, and forced, on every new table ---------------------------------------------------------------------------------

def test_every_new_table_has_row_level_security_enabled_and_forced_and_a_policy():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select c.relname, c.relrowsecurity, c.relforcerowsecurity, (select count(*) from pg_policy p where p.polrelid = c.oid) from pg_class c where c.relname = any(%s)", (list(NEW_TABLES),))
        rows = {r[0]: r[1:] for r in cur.fetchall()}
        cur.execute("select count(*) from pg_policy p join pg_class c on c.oid = p.polrelid where c.relname = 'billing_payment' and p.polpermissive = false")
        restrictive = cur.fetchone()[0]
    assert set(rows) == set(NEW_TABLES)
    assert all(enabled and forced and policies >= 1 for enabled, forced, policies in rows.values()), rows
    assert restrictive == 3, "billing_payment keeps its tenant policy and gains three restrictive ones (select, insert, update)"


# ---- two layers under a table: each refuses by itself ------------------------------------------------------------------------------------------

def test_the_invoice_policies_refuse_on_their_own_with_the_trigger_switched_off(world):
    inv = invoice(world)

    async def go(rig):
        return {label: await update_as(rig, world, label, "update billing_invoice set status = 'void', voided_at = now() where id = $1::uuid", inv) for label in ("rep1", "rep2", "prescriber1", "clinic2")}
    with switched("billing_invoice", "trigger billing_invoice_guard"):
        refused = with_portal(go, size=2)
        control = with_portal(lambda rig: update_as(rig, world, "admin", "update billing_invoice set status = 'void', voided_at = now() where id = $1::uuid", inv), size=2)
    assert refused == {"rep1": "ok 0", "rep2": "ok 0", "prescriber1": "ok 0", "clinic2": "ok 0"}          # the row is not there for them to update
    assert control == "ok 1"


def test_the_invoice_triggers_refuse_on_their_own_with_the_policies_switched_off(world):
    draft, open_inv, paid = invoice(world), invoice(world, status="open", total=1000), invoice(world, status="paid", total=1000, method="wallet")

    async def go(rig):
        return {"rep void": await update_as(rig, world, "rep1", "update billing_invoice set status = 'void', voided_at = now() where id = $1::uuid", draft),
                "clinic void": await update_as(rig, world, "clinic1", "update billing_invoice set status = 'void', voided_at = now() where id = $1::uuid", draft),
                "rep pay": await update_as(rig, world, "rep1", "update billing_invoice set status = 'paid', pay_method = 'wallet', paid_at = now() where id = $1::uuid", open_inv),
                "clinic refund": await update_as(rig, world, "clinic1", "update billing_invoice set refunded_minor = 100 where id = $1::uuid", paid),
                "clinic pay": await update_as(rig, world, "clinic1", "update billing_invoice set status = 'paid', pay_method = 'wallet', paid_at = now() where id = $1::uuid", open_inv),
                "admin refund": await update_as(rig, world, "admin", "update billing_invoice set refunded_minor = 100 where id = $1::uuid", paid)}
    with switched("billing_invoice", "rls"):
        got = with_portal(go, size=2)
    assert got["rep void"] == got["clinic void"] == got["rep pay"] == got["clinic refund"] == "InsufficientPrivilegeError"
    assert got["clinic pay"] == "ok 1" and got["admin refund"] == "ok 1"                                            # the controls: the right action by the right role


def test_the_order_policy_refuses_on_its_own_with_the_trigger_switched_off_and_the_trigger_on_its_own_with_the_policy_off(world):
    one, two = order(world), order(world)

    async def trigger_only(rig):
        return await update_as(rig, world, "rep1", "update fulfillment_order set status = 'cancelled' where id = $1::uuid", two)

    async def policy_only(rig):
        return {"rep": await update_as(rig, world, "rep1", "update fulfillment_order set status = 'cancelled' where id = $1::uuid", one),
                "other clinic": await update_as(rig, world, "clinic2", "update fulfillment_order set status = 'cancelled' where id = $1::uuid", one),
                "own clinic": await update_as(rig, world, "clinic1", "update fulfillment_order set status = 'cancelled' where id = $1::uuid", one)}
    with switched("fulfillment_order", "trigger fulfillment_order_guard"):
        by_policy = with_portal(policy_only, size=2)
    with switched("fulfillment_order", "rls"):
        by_trigger = with_portal(trigger_only, size=2)
    assert by_policy == {"rep": "ok 0", "other clinic": "ok 0", "own clinic": "ok 1"}
    assert by_trigger == "InsufficientPrivilegeError"


def test_an_order_cannot_be_inserted_in_any_status_but_created_even_by_an_admin(world):
    async def go(rig):
        out = {}
        for status in ("created", "submitted", "delivered"):
            out[status] = await update_as(rig, world, "admin", "insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by, status, carrier, tracking_number) "
                                                               "select $1::uuid, $2::uuid, $3, 'x', $4, case when $4 = 'delivered' then 'SwiftPost' end, case when $4 = 'delivered' then 'TRK-123456' end",
                                          world.a.tenant, world.a.clinics["c1"], "ord_" + uuid.uuid4().hex[:24], status)
        return out
    assert with_portal(go, size=2) == {"created": "ok 1", "submitted": "InsufficientPrivilegeError", "delivered": "InsufficientPrivilegeError"}


# ---- the policies read the matrix table, so a cell changed in the database is a change of what is allowed -------------------------------------

def test_a_cell_changed_in_the_database_table_changes_what_the_policies_allow_with_no_other_edit(world):
    inv = invoice(world, status="paid", total=1000, method="wallet")
    owner("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) values (%s, %s, %s, %s, 'accrual', 50, 1000, 500, 'USD', 'accrual')",
          world.a.tenant, inv, world.a.clinics["c1"], world.a.users["rep1"])
    accrue = ("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) "
              "values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'accrual', 5, 1000, 500, 'USD', $5)")
    reverse = ("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) "
               "values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'reversal', -1, 1, 500, 'USD', $5)")

    async def go(rig):
        args = (world.a.tenant, inv, world.a.clinics["c1"], world.a.users["rep1"])
        before = (await update_as(rig, world, "admin", reverse, *args, "refund:1"), await update_as(rig, world, "admin", accrue, *args, "accrual-2"))
        return before

    async def after_change(rig):
        args = (world.a.tenant, inv, world.a.clinics["c1"], world.a.users["rep1"])
        return (await update_as(rig, world, "admin", reverse, *args, "refund:2"), await update_as(rig, world, "admin", accrue, *args, "accrual-3"))
    before = with_portal(go, size=2)
    assert before[0] == "ok 1" and before[1] != "ok 1"                      # a reversal is allowed; a second accrual is refused by the unique index, not the policy: see the next lines
    owner("update access_matrix set scope = 'none' where role = 'admin' and resource = 'commissions' and action = 'refund'")
    try:
        changed = with_portal(after_change, size=2)
    finally:
        owner("update access_matrix set scope = 'all' where role = 'admin' and resource = 'commissions' and action = 'refund'")
    assert changed[0] == "InsufficientPrivilegeError"                        # the same admin, the same SQL: refused now that the table's cell says none
    again = with_portal(lambda rig: update_as(rig, world, "admin", reverse, world.a.tenant, inv, world.a.clinics["c1"], world.a.users["rep1"], "refund:3"), size=2)
    assert again == "ok 1"


# ---- constraints no ordinary flow reaches --------------------------------------------------------------------------------------------------------

def test_a_refunded_total_beyond_the_total_or_going_down_and_a_moved_clinic_are_refused_by_the_database(world):
    paid = invoice(world, status="paid", total=1000, method="wallet", refunded=300)
    other = world.a.clinics["c2"]

    async def go(rig):
        return {"over": await update_as(rig, world, "admin", "update billing_invoice set refunded_minor = 1001 where id = $1::uuid", paid),
                "down": await update_as(rig, world, "admin", "update billing_invoice set refunded_minor = 100 where id = $1::uuid", paid),
                "moved": await update_as(rig, world, "admin", "update billing_invoice set clinic_id = $2::uuid where id = $1::uuid", paid, other),
                "renumbered": await update_as(rig, world, "admin", "update billing_invoice set number = number + 1000 where id = $1::uuid", paid),
                "up": await update_as(rig, world, "admin", "update billing_invoice set refunded_minor = 400 where id = $1::uuid", paid)}
    got = with_portal(go, size=2)
    assert got["over"] == "CheckViolationError" and got["down"] == got["moved"] == got["renumbered"] == "RestrictViolationError" and got["up"] == "ok 1"


def test_an_open_invoice_has_a_total_a_refund_row_needs_a_paid_invoice_and_one_invoice_has_one_live_payment(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        t, c = world.a.tenant, world.a.clinics["c1"]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("insert into billing_invoice (tenant_id, clinic_id, currency, status, idempotency_key, request_hash, created_by) values (%s, %s, 'USD', 'open', 'z', 'h', 'x')", (t, c))
        open_inv, paid = invoice(world, status="open", total=1000), invoice(world, status="paid", total=1000, method="wallet")
        refund = ("insert into billing_invoice_refund (tenant_id, invoice_id, clinic_id, amount_minor, method, idempotency_key, request_hash, created_by) values (%s, %s, %s, 10, 'wallet', %s, 'h', 'x')")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(refund, (t, open_inv, c, "r-open"))
        cur.execute(refund, (t, paid, c, "r-paid"))                                                       # the control
        pay = ("insert into billing_payment (tenant_id, clinic_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, purpose, invoice_id, status) "
               "values (%s, %s, %s, 'h', 'p', 1000, 'USD', 'invoice', %s, %s)")
        cur.execute(pay, (t, c, "p1", open_inv, "processing"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(pay, (t, c, "p2", open_inv, "created"))
        cur.execute(pay, (t, c, "p3", open_inv, "failed"))                                                 # a failed one does not count as live: the invoice can be paid again


# ---- the clinic constraint of a user -----------------------------------------------------------------------------------------------------------------

def test_a_clinic_user_and_a_prescriber_belong_to_a_clinic_and_a_rep_and_an_admin_do_not(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        t, c = world.a.tenant, world.a.clinics["c1"]
        ins = "insert into portal_user (tenant_id, role, clinic_id, label) values (%s, %s, %s, 'x')"
        for role, clinic in (("clinic", None), ("prescriber", None), ("sales_rep", c), ("admin", c)):
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(ins, (t, role, clinic))
        for role, clinic in (("clinic", c), ("prescriber", c), ("sales_rep", None), ("admin", None)):
            cur.execute(ins, (t, role, clinic))                                                              # the controls
        with pytest.raises(psycopg.errors.ForeignKeyViolation):                                              # a clinic of another tenant
            cur.execute(ins, (world.b.tenant, "clinic", c))


# ---- the proof functions find what they are for ----------------------------------------------------------------------------------------------------------

def test_billing_invoice_discrepancies_names_each_way_an_invoice_can_disagree_with_the_ledger(world):
    t = world.a.tenant
    clean = with_portal(lambda rig: _discrepancies(rig, world), size=2)
    assert clean == []
    with switched("billing_invoice", "trigger billing_invoice_guard"):
        paid_without_ledger = invoice(world, status="paid", total=1000, method="wallet")                    # paid, but nothing in the ledger
        refunded_without_ledger = invoice(world, status="paid", total=1000, method="wallet", refunded=300)   # refunded, but nothing in the ledger
        unpaid_with_ledger = invoice(world, status="open", total=700)
        accounts = {}
        for kind in ("wallet", "platform_revenue", "provider_clearing", "refunds", "adjustments"):
            owner("insert into billing_account (tenant_id, kind, currency) values (%s, %s, 'USD') on conflict do nothing", t, kind)
        with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
            cur.execute("select kind, id from billing_account where tenant_id = %s", (t,))
            accounts = dict(cur.fetchall())
            cur.execute("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, provider_ref, actor) values (%s, 'invoice_payment', 'USD', 'k', 'h', %s, 'test') returning id",
                        (t, "invoice:" + unpaid_with_ledger))
            tx = cur.fetchone()[0]
            for kind, amount in (("provider_clearing", 700), ("platform_revenue", -700)):
                cur.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values (%s, %s, %s, 'USD', %s)", (t, tx, accounts[kind], amount))
            conn.commit()
        wrong_commission = invoice(world, status="paid", total=1000, method="wallet")
        owner("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) values (%s, %s, %s, %s, 'accrual', 99, 1000, 500, 'USD', 'accrual')",
              t, wrong_commission, world.a.clinics["c1"], world.a.users["rep1"])
        found = with_portal(lambda rig: _discrepancies(rig, world), size=2)
    problems = {(p, ref) for p, ref, *_ in found}
    assert ("paid invoice not in the ledger", paid_without_ledger) in problems and ("paid invoice not in the ledger", wrong_commission) in problems
    assert ("invoice refunded differs from ledger", refunded_without_ledger) in problems
    assert ("unpaid invoice has a payment in the ledger", unpaid_with_ledger) in problems
    assert ("commission differs from the rate", wrong_commission) in problems


async def _discrepancies(rig, world):
    async with rig.db.session(world.a.tenant, "admin", world.a.users["admin"]) as conn:
        rows = await conn.fetch("select * from billing_invoice_discrepancies($1::uuid)", world.a.tenant)
    return [(r["problem"], r["ref"], r["stored_minor"], r["derived_minor"]) for r in rows]
