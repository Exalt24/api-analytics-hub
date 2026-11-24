"""Purchase order tracking against real Postgres, as the REQUEST role (migrations/021_purchase_orders.sql, app/inventory/order_store.py).

What is proved here: every new table is under forced row level security and tenant B sees nothing of tenant A's orders; an order made from the plan's draft keeps its lines, quantities
and costs; the expected date is the order date plus the supplier's lead time at that moment and can be edited; an illegal status move is refused by the database itself, also when
it is a hand written UPDATE; a receipt moves the order to partly received or received and an over-receipt is refused unless accepted and then flagged; the plan reads the quantity still
to arrive on open orders as inbound stock (a variant with an order on the way suggests no second order); a late order is reported once, through the outbox; and, over seeded random
worlds, the database and the pure rules in app/inventory/orders.py agree on every move and every receipt, refusal for refusal.

Like the other database suites this drops and recreates the `public` schema it points at, and skips cleanly when Postgres is not running.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from inventory_fixtures import DSN, TODAY, admin_rows, needs_db, psycopg, reset_schema, run  # noqa: E402

pytestmark = needs_db

from app.changes import SinkAnswer, deliver_pending  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import alerts, order_store, orders, store  # noqa: E402
from test_inventory_db import World, seed, stock, with_db  # noqa: E402

NEW_TABLES = ("inventory_purchase_order", "inventory_purchase_order_line", "inventory_po_event", "inventory_po_alert", "inventory_po_alert_outbox")


@pytest.fixture()
def world():
    """Tenant A has the worked example (the Tee needs 60 from Acme Goods, lead time 9 days) and its stored plan; tenant B has a supplier of the same name and a variant of its own."""
    reset_schema()
    from inventory_fixtures import add_tenant
    w = World()
    w.a, w.b = str(add_tenant("tenant-a")), str(add_tenant("tenant-b"))

    async def build():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            w.ids = await seed(db, w.a)
            async with db.tenant(w.b) as conn:
                ids = await store.upsert_variants(conn, w.b, [stock(1, "Other tenant's tee", "TEE-1", 2)])
                await store.save_snapshots(conn, w.b, TODAY, [stock(1, "Other tenant's tee", "TEE-1", 2)], ids)
                await store.upsert_supplier(conn, w.b, name="Acme Goods", lead_time_days=3)
            async with db.tenant(w.a) as conn:
                await store.make_plan(conn, w.a, TODAY)
        finally:
            await db.close()

    run(build())
    return w


def place(world, *, on: date = TODAY, expected: date | None = None, by: str = "buyer") -> str:
    """A draft from the plan, placed on `on` (today by default, so the lead time puts the arrival nine days out). Returns the order id."""
    async def go(db):
        async with db.tenant(world.a) as conn:
            oid = await order_store.create_from_plan(conn, "Acme Goods", by)
            await order_store.move(conn, oid, "ordered", by, on=on, expected_on=expected)
            return oid
    return with_db(go)


def place_late(world, days_late: int = 3) -> str:
    """An order placed three weeks ago whose expected date was `days_late` days ago."""
    return place(world, on=TODAY - timedelta(days=21), expected=TODAY - timedelta(days=days_late))


def refused(world, fn):
    """The OrderError a call raises (each call in its own transaction, so a refusal rolls back), or None when it was accepted."""
    async def go(db):
        try:
            async with db.tenant(world.a) as conn:
                await fn(conn)
        except orders.OrderError as exc:
            return exc
        return None
    return with_db(go)


def order_of(world, oid):
    async def go(db):
        async with db.tenant(world.a) as conn:
            return await order_store.get_order(conn, oid, as_of=TODAY)
    return with_db(go)


# ------------------------------------------------------------------------------------------------ tenant isolation and grants

def test_row_level_security_is_enabled_and_forced_on_every_new_table():
    reset_schema()
    rows = admin_rows("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(%s)", (list(NEW_TABLES),))
    assert sorted(r[0] for r in rows) == sorted(NEW_TABLES)
    assert all(r[1] and r[2] for r in rows), rows


def test_every_new_table_has_a_tenant_column_and_a_policy():
    reset_schema()
    cols = {r[0] for r in admin_rows("select table_name from information_schema.columns where column_name = 'tenant_id' and table_name = any(%s)", (list(NEW_TABLES),))}
    assert cols == set(NEW_TABLES)
    policies = {r[0] for r in admin_rows("select tablename from pg_policies where tablename = any(%s) and policyname like '%%_by_tenant'", (list(NEW_TABLES),))}
    assert policies == set(NEW_TABLES)


def test_the_request_role_is_not_the_owner_and_cannot_delete_an_order_or_rewrite_history(world):
    oid = place(world)

    async def go(db):
        out = {}
        async with db.tenant(world.a) as conn:
            out["role"] = await conn.fetchval("select current_user")
            out["superuser"] = await conn.fetchval("select rolsuper from pg_roles where rolname = current_user")
            for name, sql in (("delete order", "delete from inventory_purchase_order"), ("rewrite event", "update inventory_po_event set by_whom = 'x'"),
                              ("delete event", "delete from inventory_po_event"), ("rewrite transition", "update inventory_po_transition set to_status = 'received'")):
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                    out[name] = "done"
                except asyncpg.PostgresError as exc:
                    out[name] = type(exc).__name__
        return out
    got = with_db(go)
    assert got["role"] == "app_request" and got["superuser"] is False
    assert [got[k] for k in ("delete order", "rewrite event", "delete event", "rewrite transition")] == ["InsufficientPrivilegeError"] * 4
    assert admin_rows("select count(*) from inventory_purchase_order where id = %s", (oid,))[0][0] == 1
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update inventory_po_event set by_whom = 'x'")                  # and the superuser is stopped by the trigger


def test_tenant_b_sees_none_of_tenant_as_orders_and_cannot_move_one(world):
    oid = place_late(world, 2)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.raise_late_alerts(conn, world.a, TODAY)
            counts_a = {t: await conn.fetchval("select count(*) from %s" % t) for t in NEW_TABLES}
        async with db.tenant(world.b) as conn:
            counts_b = {t: await conn.fetchval("select count(*) from %s" % t) for t in NEW_TABLES}
            listed = await order_store.list_orders(conn)
            problems = []
            for call in (lambda: order_store.move(conn, oid, "cancelled", "b-user"), lambda: order_store.set_expected(conn, oid, TODAY, "b-user"),
                         lambda: order_store.get_order(conn, oid)):
                try:
                    async with conn.transaction():
                        await call()
                    problems.append("done")
                except orders.NotFound:
                    problems.append("not found")
            try:
                async with conn.transaction():
                    await order_store.create_from_plan(conn, "Acme Goods", "b-user")
                problems.append("done")
            except orders.NothingToOrder:
                problems.append("nothing to order")                       # B has the supplier but a plan of its own: none stored, so no draft
            except orders.NotFound:
                problems.append("no plan")
        return counts_a, counts_b, listed, problems
    counts_a, counts_b, listed, problems = with_db(go)
    assert all(counts_a[t] > 0 for t in NEW_TABLES), counts_a                # A really has rows in every table, so zero for B is the policy, not an empty table
    assert set(counts_b.values()) == {0} and listed == []
    assert problems == ["not found", "not found", "not found", "no plan"]
    assert order_of(world, oid)["status"] == "ordered"                         # tenant B's attempt changed nothing


def test_a_line_for_another_tenant_is_refused_by_the_policy(world):
    oid = place(world)

    async def go(db):
        async with db.tenant(world.b) as conn:
            try:
                await conn.execute("insert into inventory_purchase_order_line (tenant_id, order_id, description, quantity_ordered) values ($1::uuid, $2::uuid, 'smuggled', 1)", world.a, oid)
            except asyncpg.PostgresError as exc:
                return type(exc).__name__
        return "inserted"
    assert with_db(go) == "InsufficientPrivilegeError"


def test_without_a_tenant_nothing_is_visible_and_the_draft_function_refuses(world):
    place(world)

    async def go(db):
        async with db.unscoped() as conn:
            counts = {t: await conn.fetchval("select count(*) from %s" % t) for t in NEW_TABLES}
            try:
                await conn.fetchrow("select inventory_po_create_from_plan('Acme Goods', 'x')")
            except asyncpg.PostgresError as exc:
                return counts, exc.sqlstate
        return counts, None
    counts, state = with_db(go)
    assert set(counts.values()) == {0} and state == "PO404"


def test_the_migration_can_run_twice_and_the_table_matches_the_python_rules():
    reset_schema()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute((ROOT / "migrations" / "021_purchase_orders.sql").read_text(encoding="utf-8"))
    rows = admin_rows("select from_status, to_status from inventory_po_transition")
    assert len(rows) == 15 and {(a, b) for a, b in rows} == {(a, b) for a, tos in orders.TRANSITIONS.items() for b in tos}


# ------------------------------------------------------------------------------------------------ an order from the plan's draft

def test_an_order_made_from_the_plans_draft_keeps_its_lines_quantities_and_costs(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            oid = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            return await order_store.get_order(conn, oid, as_of=TODAY)
    o = with_db(go)
    assert (o["status"], o["supplier"], o["currency"], o["plan_as_of"], o["lead_time_days"], o["created_by"]) == ("draft", "Acme Goods", "USD", TODAY, 9, "buyer")
    assert o["ordered_on"] is None and o["expected_on"] is None                                  # nothing was placed yet
    assert len(o["lines"]) == 1
    line = o["lines"][0]
    assert (line["sku"], line["description"], line["quantity_ordered"], line["quantity_received"], line["unit_cost_minor"], line["currency"]) == ("TEE-1", "Tee", 60, 0, 800, "USD")
    assert o["total_minor"] == 48000 and o["lines_without_cost"] == 0 and o["open_units"] == 60
    assert [e["kind"] for e in o["events"]] == ["created"] and o["next_statuses"] == ["ordered", "cancelled"]
    stored = admin_rows("select p.suggested_qty from inventory_plan_line p where p.sku = 'TEE-1' and p.tenant_id = %s", (world.a,))
    assert stored == [(60,)]                                                                      # the quantity is the plan's, not a number typed in


def test_a_draft_is_refused_when_nothing_is_due_the_supplier_is_unknown_or_a_draft_is_already_open(world):
    async def go(db):
        out = []
        for supplier in ("Acme Goods", "Acme Goods", "No Such Supplier"):
            try:
                async with db.tenant(world.a) as conn:
                    await order_store.create_from_plan(conn, supplier, "buyer")
                out.append("made")
            except orders.OrderError as exc:
                out.append(type(exc).__name__)
        async with db.tenant(world.a) as conn:
            await store.upsert_supplier(conn, world.a, name="Idle Supplier", lead_time_days=5)
        try:
            async with db.tenant(world.a) as conn:
                await order_store.create_from_plan(conn, "Idle Supplier", "buyer")
            out.append("made")
        except orders.OrderError as exc:
            out.append(type(exc).__name__)
        return out
    assert with_db(go) == ["made", "NothingToOrder", "NotFound", "NothingToOrder"]


def test_a_cancelled_draft_frees_the_supplier_for_a_new_one(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            first = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await order_store.move(conn, first, "cancelled", "buyer")
            second = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            return first, second
    first, second = with_db(go)
    assert first != second and order_of(world, first)["status"] == "cancelled" and order_of(world, second)["status"] == "draft"


def test_a_draft_with_lines_in_two_currencies_is_refused(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await conn.execute("update inventory_plan_line set status = 'ok', supplier_name = 'Acme Goods', needs_reorder = true, suggested_qty = 10, currency = 'EUR' where sku = 'MUG-1'")
            try:
                await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            except orders.NothingToOrder as exc:
                return str(exc)
        return "made"
    assert "more than one currency" in with_db(go)


def test_a_line_with_no_unit_cost_is_kept_blank_and_the_total_leaves_it_out(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await conn.execute("update inventory_plan_line set status = 'ok', supplier_name = 'Acme Goods', needs_reorder = true, suggested_qty = 10, unit_cost_minor = null where sku = 'MUG-1'")
            oid = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            return await order_store.get_order(conn, oid)
    o = with_db(go)
    by = {l["sku"]: l for l in o["lines"]}
    assert by["MUG-1"]["unit_cost_minor"] is None and by["MUG-1"]["currency"] is None            # a blank, never a zero
    assert o["total_minor"] == 48000 and o["lines_without_cost"] == 1


# ------------------------------------------------------------------------------------------------ placing, the expected date, the status moves

def test_placing_an_order_sets_the_expected_date_from_the_lead_time_at_that_moment(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            oid = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await store.upsert_supplier(conn, world.a, name="Acme Goods", lead_time_days=14)       # the supplier quotes longer before the order is placed
            await order_store.move(conn, oid, "ordered", "buyer", on=date(2026, 11, 25))
            first = await order_store.get_order(conn, oid)
            await store.upsert_supplier(conn, world.a, name="Acme Goods", lead_time_days=30)       # and a later change must not move an order already placed
            after = await order_store.get_order(conn, oid)
        return first, after
    first, after = with_db(go)
    assert (first["ordered_on"], first["lead_time_days"], first["expected_on"]) == (date(2026, 11, 25), 14, date(2026, 12, 9))
    assert (after["lead_time_days"], after["expected_on"]) == (14, date(2026, 12, 9))


def test_an_expected_date_given_when_placing_wins_and_one_can_be_edited_while_the_order_is_open(world):
    oid = place(world, expected=date(2026, 12, 20))
    assert order_of(world, oid)["expected_on"] == date(2026, 12, 20)

    async def go(db):
        async with db.tenant(world.a) as conn:
            return await order_store.set_expected(conn, oid, date(2026, 12, 2), "buyer")
    assert with_db(go) == date(2026, 12, 2)
    o = order_of(world, oid)
    assert o["expected_on"] == date(2026, 12, 2)
    changes = [e["detail"] for e in o["events"] if e["kind"] == "expected_on"]
    assert changes == [{"from": "2026-12-20", "to": "2026-12-02"}]                                # the old and the new date are on record


def test_the_expected_date_cannot_be_edited_on_a_draft_a_received_or_a_cancelled_order(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            draft = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
        out = []
        try:
            async with db.tenant(world.a) as conn:
                await order_store.set_expected(conn, draft, TODAY, "buyer")
        except orders.NotAllowedInStatus as exc:
            out.append(exc.code)
        async with db.tenant(world.a) as conn:
            await order_store.move(conn, draft, "cancelled", "buyer")
        try:
            async with db.tenant(world.a) as conn:
                await order_store.set_expected(conn, draft, TODAY, "buyer")
        except orders.NotAllowedInStatus as exc:
            out.append(exc.code)
        return out
    assert with_db(go) == ["PO003", "PO003"]


def test_the_status_moves_follow_the_table_and_each_one_is_on_record(world):
    oid = place(world)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.move(conn, oid, "confirmed", "buyer", on=date(2026, 11, 26), expected_on=date(2026, 12, 4), note="supplier confirmed by email")
            await order_store.move(conn, oid, "shipped", "buyer", on=date(2026, 12, 1))
    with_db(go)
    o = order_of(world, oid)
    assert (o["status"], o["confirmed_on"], o["shipped_on"], o["expected_on"], o["note"]) == ("shipped", date(2026, 11, 26), date(2026, 12, 1), date(2026, 12, 4), "supplier confirmed by email")
    assert [(e["from_status"], e["to_status"]) for e in o["events"] if e["kind"] == "status"] == [("draft", "ordered"), ("ordered", "confirmed"), ("confirmed", "shipped")]
    assert [e["by"] for e in o["events"]] == ["buyer"] * len(o["events"])


def test_an_illegal_move_is_refused_loudly_with_the_order_unchanged(world):
    oid = place(world)

    async def go(db):
        out = []
        for to in ("draft", "ordered", "partly_received", "received", "bogus"):
            try:
                async with db.tenant(world.a) as conn:
                    await order_store.move(conn, oid, to, "buyer")
                out.append("moved")
            except orders.IllegalMove as exc:
                out.append(exc.code)
            except asyncpg.PostgresError as exc:
                out.append(exc.sqlstate)
        return out
    assert with_db(go) == ["PO001"] * 5                                                           # back to draft, again, and the two a receipt makes, and a status that does not exist
    assert order_of(world, oid)["status"] == "ordered"


def test_the_database_refuses_an_illegal_move_made_by_a_hand_written_update(world):
    oid = place(world)

    async def go(db):
        out = []
        for to in ("draft", "received", "partly_received"):
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute("update inventory_purchase_order set status = $2 where id = $1::uuid", oid, to)
                out.append("moved")
            except asyncpg.PostgresError as exc:
                out.append(exc.sqlstate)
        # received needs every line in full, whoever says so
        return out
    assert with_db(go) == ["PO001", "PO001", "PO001"]
    assert order_of(world, oid)["status"] == "ordered"


def test_a_terminal_order_never_moves_again(world):
    oid = place(world)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.move(conn, oid, "cancelled", "buyer")
        out = []
        for to in orders.STATUSES:
            try:
                async with db.tenant(world.a) as conn:
                    await order_store.move(conn, oid, to, "buyer")
                out.append("moved")
            except orders.IllegalMove:
                out.append("refused")
        return out
    assert with_db(go) == ["refused"] * 7
    assert order_of(world, oid)["closed_on"] is not None


def test_an_order_with_no_lines_cannot_be_placed(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            sid = await conn.fetchval("select id from inventory_supplier where name = 'Acme Goods'")
            oid = await conn.fetchval("insert into inventory_purchase_order (tenant_id, supplier_id, supplier_name, created_by) values ($1::uuid, $2, 'Acme Goods', 'x') returning id", world.a, sid)
        try:
            async with db.tenant(world.a) as conn:
                await order_store.move(conn, str(oid), "ordered", "x")
        except orders.IllegalMove as exc:
            return str(exc)
        return "placed"
    assert "no lines" in with_db(go)


def test_lines_cannot_be_added_removed_or_changed_once_the_order_is_placed(world):
    oid = place(world)

    async def go(db):
        out = []
        for sql in ("insert into inventory_purchase_order_line (tenant_id, order_id, description, quantity_ordered) values ('%s'::uuid, '%s'::uuid, 'extra', 5)" % (world.a, oid),
                    "update inventory_purchase_order_line set quantity_ordered = 61 where order_id = '%s'::uuid" % oid,
                    "delete from inventory_purchase_order_line where order_id = '%s'::uuid" % oid):
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute(sql)
                out.append("done")
            except asyncpg.PostgresError as exc:
                out.append(exc.sqlstate)
        return out
    assert with_db(go) == ["PO003", "PO003", "PO003"]


def test_a_draft_line_can_be_removed_and_nothing_else_is_lost_with_it(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            oid = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await conn.execute("delete from inventory_purchase_order_line where order_id = $1::uuid", oid)
        try:
            async with db.tenant(world.a) as conn:
                await order_store.move(conn, oid, "ordered", "buyer")
        except orders.IllegalMove:
            return "refused"
        return "placed"
    assert with_db(go) == "refused"


def test_money_is_integer_minor_units_with_a_currency_and_a_cost_without_one_is_refused(world):
    async def go(db):
        out = []
        async with db.tenant(world.a) as conn:
            oid = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
        for sql in ("update inventory_purchase_order_line set unit_cost_minor = 100, currency = null where order_id = '%s'::uuid" % oid,
                    "update inventory_purchase_order_line set unit_cost_minor = -1 where order_id = '%s'::uuid" % oid,
                    "update inventory_purchase_order_line set unit_cost_minor = 150 where order_id = '%s'::uuid" % oid):
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute(sql)
                out.append("done")
            except asyncpg.PostgresError as exc:
                out.append(type(exc).__name__)
        col = await _scalar(db, world.a, "select data_type from information_schema.columns where table_name = 'inventory_purchase_order_line' and column_name = 'unit_cost_minor'")
        return out, col
    out, col = with_db(go)
    assert out == ["CheckViolationError", "CheckViolationError", "done"] and col == "bigint"


async def _scalar(db, tenant, sql, *args):
    async with db.tenant(tenant) as conn:
        return await conn.fetchval(sql, *args)


# ------------------------------------------------------------------------------------------------ receipts

def _line_id(world, oid) -> int:
    return order_of(world, oid)["lines"][0]["id"]


def test_a_partial_receipt_then_the_rest(world):
    oid = place(world)
    line = _line_id(world, oid)

    async def go(db):
        async with db.tenant(world.a) as conn:
            first = await order_store.receive(conn, oid, [(line, 25)], "dock", on=date(2026, 12, 2))
            mid = await order_store.get_order(conn, oid, as_of=TODAY)
        async with db.tenant(world.a) as conn:
            second = await order_store.receive(conn, oid, [(line, 35)], "dock", on=date(2026, 12, 9))
        return first, mid, second
    first, mid, second = with_db(go)
    assert first == "partly_received" and (mid["status"], mid["open_units"], mid["lines"][0]["quantity_received"], mid["lines"][0]["quantity_open"]) == ("partly_received", 35, 25, 35)
    done = order_of(world, oid)
    assert second == "received" and (done["status"], done["open_units"], done["closed_on"], done["next_statuses"], done["can_receive"]) == ("received", 0, date(2026, 12, 9), [], False)
    receipts = [e["detail"] for e in done["events"] if e["kind"] == "receipt"]
    assert [(r["quantity"], r["total_received"]) for r in receipts] == [(25, 25), (35, 60)]


def test_receiving_more_than_ordered_is_refused_unless_accepted_and_then_it_is_flagged(world):
    oid = place(world)
    line = _line_id(world, oid)
    err = refused(world, lambda conn: order_store.receive(conn, oid, [(line, 61)], "dock"))
    assert isinstance(err, orders.OverReceipt) and "61" in str(err) and "60" in str(err)
    assert order_of(world, oid)["lines"][0]["quantity_received"] == 0                              # the refusal recorded nothing

    async def go(db):
        async with db.tenant(world.a) as conn:
            return await order_store.receive(conn, oid, [(line, 64)], "dock", allow_over=True)
    assert with_db(go) == "received"
    o = order_of(world, oid)
    assert (o["lines"][0]["quantity_received"], o["lines"][0]["over_received"], o["lines"][0]["quantity_open"], o["open_units"]) == (64, True, 0, 0)       # open is never negative


def test_the_database_refuses_a_received_quantity_that_exceeds_the_order_without_the_flag(world):
    oid = place(world)

    async def go(db):
        out = []
        for sql in ("update inventory_purchase_order_line set quantity_received = 100 where order_id = '%s'::uuid" % oid,
                    "update inventory_purchase_order_line set over_received = true where order_id = '%s'::uuid" % oid):
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute(sql)
                out.append("done")
            except asyncpg.PostgresError as exc:
                out.append(type(exc).__name__)
        return out
    assert with_db(go) == ["CheckViolationError", "CheckViolationError"]


def test_a_receipt_of_nothing_a_negative_one_an_unknown_line_and_a_receipt_on_a_draft_are_refused(world):
    oid = place(world)
    line = _line_id(world, oid)
    assert isinstance(refused(world, lambda c: order_store.receive(c, oid, [(line, 0)], "dock")), orders.BadQuantity)
    assert isinstance(refused(world, lambda c: order_store.receive(c, oid, [(line, -5)], "dock", allow_over=True)), orders.BadQuantity)
    assert isinstance(refused(world, lambda c: order_store.receive(c, oid, [], "dock")), orders.BadQuantity)
    assert isinstance(refused(world, lambda c: order_store.receive(c, oid, [(line + 999, 1)], "dock")), orders.NotFound)

    async def draft(conn):
        d = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
        await order_store.receive(conn, d, [(await order_store.find_line(conn, d, "TEE-1"), 1)], "dock")
    assert isinstance(refused(world, draft), orders.NotAllowedInStatus)


def test_nothing_can_be_received_on_a_cancelled_or_a_received_order(world):
    done = place(world)
    line = _line_id(world, done)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.receive(conn, done, [(line, 60)], "dock")
            second = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await order_store.move(conn, second, "cancelled", "buyer")
        return second
    dropped = with_db(go)
    assert order_of(world, done)["status"] == "received"
    for oid in (done, dropped):
        err = refused(world, lambda c, oid=oid: order_store.receive(c, oid, [(line, 1)], "dock", allow_over=True))
        assert isinstance(err, orders.NotAllowedInStatus), oid


def test_a_received_quantity_is_never_reduced_by_a_direct_update(world):
    oid = place(world)
    line = _line_id(world, oid)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.receive(conn, oid, [(line, 10)], "dock")
        try:
            async with db.tenant(world.a) as conn:
                await conn.execute("update inventory_purchase_order_line set quantity_received = 3 where order_id = $1::uuid", oid)
        except asyncpg.PostgresError as exc:
            return exc.sqlstate
        return "reduced"
    assert with_db(go) == "PO004"


def test_a_partly_received_order_can_be_cancelled_and_its_remainder_stops_counting_as_inbound(world):
    oid = place(world, expected=TODAY + timedelta(days=3))
    line = _line_id(world, oid)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.receive(conn, oid, [(line, 20)], "dock")
            before = [r["incoming"] for r in await _replan(conn, world.a)]
            await order_store.move(conn, oid, "cancelled", "buyer")
            after = [r["incoming"] for r in await _replan(conn, world.a)]
        return before, after
    before, after = with_db(go)
    assert before == [40] and after == [0]                                                         # 40 still to arrive while open, nothing once the rest is cancelled
    assert order_of(world, oid)["lines"][0]["quantity_received"] == 20                              # what arrived stays on record


# ------------------------------------------------------------------------------------------------ the plan reads open orders as inbound stock

async def _replan(conn, tenant, as_of=TODAY):
    await store.make_plan(conn, tenant, as_of)
    return [r for r in await store.plan_lines(conn, as_of) if r["sku"] == "TEE-1"]


def test_without_an_order_the_tee_is_short_and_the_plan_suggests_sixty(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            return (await _replan(conn, world.a))[0]
    tee = with_db(go)
    assert (tee["suggested_qty"], tee["shortage"], tee["incoming"], tee["inbound_source"], tee["inbound_expected_on"], tee["inbound_late"]) == (60, "cover_below_lead_time", 0, "none", None, False)


def test_an_order_on_the_way_means_no_shortage_and_no_second_order_and_the_date_is_used_for_the_cover(world):
    oid = place(world, expected=TODAY + timedelta(days=3))

    async def go(db):
        async with db.tenant(world.a) as conn:
            return (await _replan(conn, world.a))[0]
    tee = with_db(go)
    assert (tee["incoming"], tee["inbound_source"], tee["inbound_expected_on"], tee["inbound_late"]) == (60, "orders", TODAY + timedelta(days=3), False)
    assert (tee["needs_reorder"], tee["suggested_qty"], tee["shortage"]) == (False, 0, None)
    assert tee["cover_days"] == pytest.approx(5.0)                                                   # the shelf alone still lasts 5 days
    assert tee["forecast_cover_days"] == pytest.approx(35.0)                                         # with the 60 landing on day 3 it lasts 35
    del oid


def test_the_same_order_landing_after_the_shelf_is_empty_still_shows_the_shortage_but_suggests_no_second_order(world):
    place(world, expected=TODAY + timedelta(days=8))

    async def go(db):
        async with db.tenant(world.a) as conn:
            return (await _replan(conn, world.a))[0]
    tee = with_db(go)
    assert (tee["shortage"], tee["needs_reorder"], tee["suggested_qty"], tee["incoming"]) == ("cover_below_lead_time", False, 0, 60)


def test_a_draft_is_not_inbound_and_a_late_order_stays_counted_without_being_assumed_to_land(world):
    async def draft(db):
        async with db.tenant(world.a) as conn:
            await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            return (await _replan(conn, world.a))[0]
    tee = with_db(draft)
    assert (tee["incoming"], tee["suggested_qty"]) == (0, 60)                                         # a draft orders nothing yet

    async def late(db):
        async with db.tenant(world.a) as conn:
            d = await conn.fetchval("select id from inventory_purchase_order where status = 'draft'")
            await order_store.move(conn, str(d), "ordered", "buyer", on=TODAY - timedelta(days=20), expected_on=TODAY - timedelta(days=4))
            return (await _replan(conn, world.a))[0]
    tee = with_db(late)
    assert (tee["incoming"], tee["inbound_late"], tee["inbound_expected_on"], tee["suggested_qty"]) == (60, True, TODAY - timedelta(days=4), 0)
    assert tee["shortage"] == "cover_below_lead_time" and tee["forecast_cover_days"] == pytest.approx(5.0)


def test_shopifys_incoming_is_replaced_by_the_orders_and_never_added_to_them(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            d = await order_store.create_from_plan(conn, "Acme Goods", "buyer")                 # the draft is made while the plan still says to order
            await conn.execute("update inventory_snapshot set incoming = 45 where observed_on = $1 and variant_id = $2::uuid", TODAY, world.ids["tee"])
            shopify_only = (await _replan(conn, world.a))[0]
            await order_store.move(conn, d, "ordered", "buyer", on=TODAY, expected_on=TODAY + timedelta(days=5))
            both = (await _replan(conn, world.a))[0]
        return shopify_only, both
    shopify_only, both = with_db(go)
    assert (shopify_only["incoming"], shopify_only["inbound_source"]) == (45, "shopify")
    assert (both["incoming"], both["inbound_source"]) == (60, "orders")


# ------------------------------------------------------------------------------------------------ the late alert

def _raise_late(world, as_of=TODAY):
    async def go(db):
        async with db.tenant(world.a) as conn:
            return await order_store.raise_late_alerts(conn, world.a, as_of)
    return with_db(go)


def test_a_late_order_is_reported_once_however_often_the_check_runs(world):
    oid = place_late(world, 3)
    first, second = _raise_late(world), _raise_late(world)
    assert (first.new, first.already_raised, second.new, second.already_raised) == (1, 0, 0, 1)
    assert _raise_late(world, TODAY + timedelta(days=5)).new == 0                                    # later days do not raise it again
    rows = admin_rows("select a.kind, a.detail->>'days_late', a.detail->>'open_units', o.idempotency_key from inventory_po_alert a join inventory_po_alert_outbox o on o.event_id = a.id")
    assert len(rows) == 1 and rows[0][0] == "po_late" and (rows[0][1], rows[0][2]) == ("3", "60") and rows[0][3] == "%s:%s:po_late" % (world.a, oid)


def test_moving_the_expected_date_later_does_not_raise_a_second_alert_for_the_order(world):
    oid = place_late(world, 3)
    _raise_late(world)

    async def go(db):
        async with db.tenant(world.a) as conn:
            await order_store.set_expected(conn, oid, TODAY + timedelta(days=2), "buyer")
    with_db(go)
    assert _raise_late(world, TODAY + timedelta(days=9)).new == 0                                    # late again, still the same order, still one alert
    assert admin_rows("select count(*) from inventory_po_alert")[0][0] == 1


def test_only_an_open_order_past_its_date_with_something_left_to_arrive_is_late(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            not_yet = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await order_store.move(conn, not_yet, "ordered", "buyer", on=TODAY, expected_on=TODAY)             # expected today is not late
        return not_yet
    with_db(go)
    assert _raise_late(world).new == 0
    assert _raise_late(world, TODAY + timedelta(days=1)).new == 1                                      # tomorrow it is


def test_a_received_a_cancelled_and_a_draft_order_are_never_late(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await conn.execute("update inventory_plan_line set status = 'ok', supplier_name = 'Acme Goods', needs_reorder = true, suggested_qty = 10 where sku = 'MUG-1'")
            d1 = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await order_store.move(conn, d1, "ordered", "buyer", on=TODAY - timedelta(days=30), expected_on=TODAY - timedelta(days=10))
            for ref in ("TEE-1", "MUG-1"):
                await order_store.receive(conn, d1, [(await order_store.find_line(conn, d1, ref), 60 if ref == "TEE-1" else 10)], "dock")
            d2 = await order_store.create_from_plan(conn, "Acme Goods", "buyer")
            await order_store.move(conn, d2, "ordered", "buyer", on=TODAY - timedelta(days=30), expected_on=TODAY - timedelta(days=10))
            await order_store.move(conn, d2, "cancelled", "buyer")
            d3 = await order_store.create_from_plan(conn, "Acme Goods", "buyer")                              # a draft, left a draft
            return d1, d2, d3
    d1, d2, d3 = with_db(go)
    assert order_of(world, d1)["status"] == "received" and order_of(world, d2)["status"] == "cancelled" and order_of(world, d3)["status"] == "draft"
    assert _raise_late(world).new == 0


def test_a_late_alert_is_delivered_once_through_the_existing_outbox_code_with_the_stored_payload_and_key(world):
    place_late(world, 3)
    _raise_late(world)
    seen = []

    def sink(payload, key):
        seen.append((payload, key))
        return SinkAnswer("ok", "ref-1")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("set role sync_worker")                                           # the delivery role crosses tenants and cannot be a superuser
        first = deliver_pending(conn, sink, table=alerts.LATE_OUTBOX_TABLE)
        second = deliver_pending(conn, sink, table=alerts.LATE_OUTBOX_TABLE)
    assert (first.delivered, first.dead, second.delivered) == (1, 0, 0) and len(seen) == 1
    stored = admin_rows("select payload, idempotency_key, status, attempts from inventory_po_alert_outbox")
    assert (stored[0][0], stored[0][1], stored[0][2], stored[0][3]) == (seen[0][0], seen[0][1], "delivered", 1)
    text = alerts.describe_alert(seen[0][0])
    assert "Acme Goods" in text and "3 day(s) late" in text and "60 unit(s)" in text
    assert admin_rows("select count(*) from inventory_alert_outbox")[0][0] == 0                       # the shortage outbox is untouched


def test_the_late_digest_lists_late_orders_for_the_hubs_alert_policy(world):
    place_late(world, 3)

    async def go(db):
        async with db.tenant(world.a) as conn:
            return await order_store.open_late_orders(conn, TODAY)
    late = with_db(go)
    assert len(late) == 1 and late[0].kind == "po_late" and "Acme Goods" in late[0].detail and late[0].key.endswith(":po_late")
    assert "1 inventory exception(s)" in alerts.describe_shortages(late)


def test_a_late_alert_is_append_only_for_everyone(world):
    place_late(world, 3)
    _raise_late(world)
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update inventory_po_alert set as_of = as_of")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("insert into inventory_po_alert (tenant_id, order_id, kind, as_of, detail) select tenant_id, order_id, kind, as_of, detail from inventory_po_alert")


# ------------------------------------------------------------------------------------------------ the database and the pure rules agree

async def _make_order(db, world, rng: random.Random) -> str:
    """A draft with two or three lines inserted directly (the plan only ever makes one line for the example), so a receipt can leave some lines open and others in full."""
    async with db.tenant(world.a) as conn:
        sid = await conn.fetchval("select id from inventory_supplier where name = 'Acme Goods'")
        oid = await conn.fetchval("insert into inventory_purchase_order (tenant_id, supplier_id, supplier_name, lead_time_days, created_by) values ($1::uuid, $2, 'Acme Goods', 9, 'sim') returning id",
                                  world.a, sid)
        for v in [world.ids["tee"], world.ids["mug"], world.ids["hat"]][:rng.randint(2, 3)]:
            await conn.execute("insert into inventory_purchase_order_line (tenant_id, order_id, variant_id, description, quantity_ordered) values ($1::uuid, $2, $3::uuid, 'sim', $4)",
                               world.a, oid, v, rng.randint(1, 30))
        return str(oid)


async def _attempt(db, world, fn):
    """The OrderError a call raises (in its own transaction, so a refusal rolls back), or None when it was accepted."""
    try:
        async with db.tenant(world.a) as conn:
            await fn(conn)
    except orders.OrderError as exc:
        return type(exc)
    return None


def test_property_the_database_and_the_pure_rules_agree_over_random_worlds(world):
    """Each seed drives one order with random operations (a move, a receipt of one line with or without the accept flag, a new expected date), against the database and against the pure
    rules in lockstep. They must agree on whether it was refused and with which code, on the status, on every line's received quantity, on the expected date and on the inbound quantity
    the plan would read."""
    async def go(db):
        steps = refusals = accepted = 0
        for seed in range(60):
            rng = random.Random(seed)
            oid = await _make_order(db, world, rng)
            async with db.tenant(world.a) as conn:
                first = await order_store.get_order(conn, oid)
            status = first["status"]
            lines = {l["id"]: [l["quantity_ordered"], 0, False] for l in first["lines"]}
            expected_on = None
            for _ in range(30):
                op = rng.choice(("move", "move", "receive", "receive", "receive", "expected"))
                steps += 1
                want: type[orders.OrderError] | None = None
                new_status, new_lines, new_expected = status, lines, expected_on
                if op == "move":
                    to = rng.choice((*orders.STATUSES, "ordered", "confirmed", "cancelled"))
                    try:
                        orders.check_move(status, to)
                        new_status = to
                        if to == "ordered":
                            new_expected = TODAY + timedelta(days=9)                       # the order date plus the supplier's nine days
                    except orders.OrderError as exc:
                        want = type(exc)
                    got = await _attempt(db, world, lambda c: order_store.move(c, oid, to, "sim", on=TODAY))
                elif op == "receive":
                    lid = rng.choice(list(lines))
                    qty = rng.choice((1, 3, 7, lines[lid][0] - lines[lid][1], lines[lid][0], 0))
                    allow = rng.random() < 0.3
                    try:
                        if qty <= 0:
                            raise orders.BadQuantity("quantity")
                        if status not in orders.OPEN_STATUSES:
                            raise orders.NotAllowedInStatus("status")
                        total, over = orders.receive_line(lines[lid][0], lines[lid][1], qty, allow)
                        new_lines = {k: list(v) for k, v in lines.items()}
                        new_lines[lid] = [lines[lid][0], total, over]
                        new_status = orders.status_after_receipt(status, [(v[0], v[1]) for v in new_lines.values()])
                    except orders.OrderError as exc:
                        want = type(exc)
                    got = await _attempt(db, world, lambda c: order_store.receive(c, oid, [(lid, qty)], "sim", allow_over=allow))
                else:
                    target = TODAY + timedelta(days=rng.randint(1, 60))
                    if status in orders.OPEN_STATUSES:
                        new_expected = target
                    else:
                        want = orders.NotAllowedInStatus
                    got = await _attempt(db, world, lambda c: order_store.set_expected(c, oid, target, "sim"))
                assert got is want, (seed, op, status, got, want)
                if want is None:
                    accepted += 1
                    status, lines, expected_on = new_status, new_lines, new_expected
                else:
                    refusals += 1
                async with db.tenant(world.a) as conn:
                    now = await order_store.get_order(conn, oid)
                assert now["status"] == status and now["expected_on"] == expected_on, (seed, op, now["status"], status, now["expected_on"], expected_on)
                assert {l["id"]: [l["quantity_ordered"], l["quantity_received"], l["over_received"]] for l in now["lines"]} == {k: list(v) for k, v in lines.items()}, (seed, op)
                assert all(l["quantity_open"] >= 0 and (l["quantity_received"] <= l["quantity_ordered"] or l["over_received"]) for l in now["lines"]), seed
                inbound = orders.arrivals_by_variant([(l["variant_id"], now["status"], l["quantity_ordered"], l["quantity_received"], now["expected_on"]) for l in now["lines"]])
                assert sum(a.quantity for arr in inbound.values() for a in arr) == (now["open_units"] if status in orders.OPEN_STATUSES else 0), seed
                if status in orders.TERMINAL_STATUSES:
                    break
        return steps, accepted, refusals
    steps, accepted, refusals = with_db(go)
    assert steps > 600 and accepted > 200 and refusals > 200
