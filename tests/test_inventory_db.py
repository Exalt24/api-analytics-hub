"""Inventory planning against real Postgres, as the REQUEST role: tenant isolation on every new table, what a sync stores and what it keeps, the stored plan against numbers worked on
paper, each shortage raised once, and delivery through the hub's own outbox code.

The isolation tests connect through `Database`, which drops into app_request and refuses to start as a superuser. That matters: the first isolation run of this project had tenant A
reading tenant B's rows with every policy correct, because the test connected as the bootstrap superuser (see db.py). Skips cleanly when Postgres is not running; like the other
database suites it drops and recreates the `public` schema it points at.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from inventory_fixtures import DSN, TODAY, add_tenant, admin_rows, needs_db, psycopg, reset_schema, run  # noqa: E402

pytestmark = needs_db

from app.changes import SinkAnswer, deliver_pending  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import alerts, store  # noqa: E402
from app.inventory.model import PlanParams, date_range  # noqa: E402
from app.inventory.reader import LocationLevel, OrderLine, VariantStock  # noqa: E402

TABLES = ("inventory_supplier", "inventory_variant", "inventory_snapshot", "inventory_order_line", "inventory_promo", "inventory_plan_run", "inventory_plan_line",
          "inventory_alert", "inventory_alert_outbox")
WINDOW = date_range(TODAY - timedelta(days=28), TODAY)          # 2026-10-27 .. 2026-11-23


def day(n: int) -> date:
    return WINDOW[n - 1]


def stock(n: int, product: str, sku: str | None, available: int, *, tracked: bool = True, incoming: int = 0, cost: int | None = None) -> VariantStock:
    return VariantStock("gid://shopify/ProductVariant/%d" % n, "gid://shopify/InventoryItem/%d" % n, "gid://shopify/Product/%d" % n, product, "Default Title", sku, tracked,
                        cost, "USD" if cost is not None else None, (LocationLevel("gid://shopify/Location/1", "Main", available, available, 0, incoming),))


def sale(order: int, variant: int, on: date, units: int, price_minor: int) -> OrderLine:
    return OrderLine("gid://shopify/Order/%d" % order, "gid://shopify/LineItem/%d" % order, "gid://shopify/ProductVariant/%d" % variant, None, on, units, units * price_minor, "USD")


class World:
    pass


async def seed(db: Database, tenant: str, *, tee_today: int = 10) -> dict[str, str]:
    """The Tee is the worked example: stock on 10 days, none for 8, stock again; 2 sold on each day it was on sale; 10 on the shelf today. The Mug sells steadily and has no
    supplier, the Hat sold once 40 days ago, the Gift card is not tracked. Returns sku (or title) -> variant id."""
    variants = [stock(1, "Tee", "TEE-1", tee_today), stock(2, "Mug", "MUG-1", 50), stock(3, "Hat", "HAT-1", 30), stock(4, "Gift card", None, 0, tracked=False)]
    lines: list[OrderLine] = []
    n = 0
    for d in range(1, 29):
        if not 11 <= d <= 18:
            n += 1
            lines.append(sale(n, 1, day(d), 2, 2500))
        if d % 2 == 0:
            n += 1
            lines.append(sale(n, 2, day(d), 1, 1500))
    n += 1
    lines.append(sale(n, 3, TODAY - timedelta(days=40), 1, 900))
    async with db.tenant(tenant) as conn:
        ids = await store.upsert_variants(conn, tenant, variants)
        for d in range(1, 29):
            level = 5 if d <= 10 else 0 if d <= 18 else 20
            snaps = [stock(1, "Tee", "TEE-1", level), stock(2, "Mug", "MUG-1", 50), stock(3, "Hat", "HAT-1", 30), stock(4, "Gift card", None, 0, tracked=False)]
            await store.save_snapshots(conn, tenant, day(d), snaps, ids)
        await store.save_snapshots(conn, tenant, TODAY, variants, ids)
        await store.replace_order_lines(conn, tenant, TODAY - timedelta(days=120), TODAY, lines, ids)
        await store.upsert_supplier(conn, tenant, name="Acme Goods", lead_time_days=9, lead_time_sd_days=0.0)
        await store.assign_supplier(conn, "TEE-1", "Acme Goods", moq=50, pack_size=12, unit_cost_minor=800, currency="USD")
        await store.add_promo(conn, tenant, ref="HAT-1", starts_on=TODAY + timedelta(days=60), ends_on=TODAY + timedelta(days=70), factor=1.2, entered_by="seed", note="a promotion far out")
    return {"tee": ids["gid://shopify/ProductVariant/1"], "mug": ids["gid://shopify/ProductVariant/2"], "hat": ids["gid://shopify/ProductVariant/3"],
            "gift": ids["gid://shopify/ProductVariant/4"]}


@pytest.fixture()
def world():
    """Two tenants. Tenant A has the worked example; tenant B has one variant of its own."""
    reset_schema()
    w = World()
    w.a, w.b = str(add_tenant("tenant-a")), str(add_tenant("tenant-b"))

    async def build():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            w.ids = await seed(db, w.a)
            async with db.tenant(w.b) as conn:
                ids = await store.upsert_variants(conn, w.b, [stock(1, "Other tenant's tee", "TEE-1", 99)])
                await store.save_snapshots(conn, w.b, TODAY, [stock(1, "Other tenant's tee", "TEE-1", 99)], ids)
                await store.upsert_supplier(conn, w.b, name="Acme Goods", lead_time_days=3)
                await store.add_promo(conn, w.b, ref=None, starts_on=TODAY, ends_on=TODAY + timedelta(days=5), factor=2.0, entered_by="b-owner")
            async with db.tenant(w.a) as conn:
                await store.make_plan(conn, w.a, TODAY)
        finally:
            await db.close()

    run(build())
    return w


def with_db(fn):
    """Run `fn(db)` with a connected request-role Database, closing it after."""
    async def go():
        db = Database(DSN, max_size=4)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()
    return run(go())


# ------------------------------------------------------------------------------------------------ tenant isolation

def test_row_level_security_is_enabled_and_forced_on_every_new_table():
    reset_schema()
    rows = admin_rows("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(%s)", (list(TABLES),))
    assert sorted(r[0] for r in rows) == sorted(TABLES)
    assert all(r[1] and r[2] for r in rows), rows


def test_every_new_table_has_a_tenant_column_and_a_policy():
    reset_schema()
    cols = {r[0] for r in admin_rows("select table_name from information_schema.columns where column_name = 'tenant_id' and table_name = any(%s)", (list(TABLES),))}
    assert cols == set(TABLES)
    policies = {r[0] for r in admin_rows("select tablename from pg_policies where tablename = any(%s) and policyname like '%%_by_tenant'", (list(TABLES),))}
    assert policies == set(TABLES)


def test_tenant_b_sees_none_of_tenant_as_rows_in_any_table(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
            counts_a = {t: await conn.fetchval("select count(*) from %s" % t) for t in TABLES}
        async with db.tenant(world.b) as conn:
            counts_b = {t: await conn.fetchval("select count(*) from %s" % t) for t in TABLES}
            skus = [r["sku"] for r in await conn.fetch("select sku from inventory_variant")]
            promos = await conn.fetchval("select count(*) from inventory_promo")
        return counts_a, counts_b, skus, promos
    counts_a, counts_b, skus, promos = with_db(go)
    assert all(counts_a[t] > 0 for t in TABLES), counts_a            # tenant A really has rows in every table, so zero for B means the policy, not an empty table
    assert counts_b["inventory_alert"] == 0 and counts_b["inventory_plan_line"] == 0 and counts_b["inventory_order_line"] == 0 and counts_b["inventory_plan_run"] == 0
    assert counts_b["inventory_variant"] == 1 and skus == ["TEE-1"] and promos == 1


def test_a_row_for_another_tenant_is_refused_by_the_policy(world):
    async def go(db):
        async with db.tenant(world.b) as conn:
            try:
                await conn.execute("insert into inventory_supplier (tenant_id, name, lead_time_days) values ($1::uuid, 'Smuggled', 5)", world.a)
            except asyncpg.PostgresError as exc:
                return type(exc).__name__
        return "inserted"
    assert with_db(go) == "InsufficientPrivilegeError"


def test_without_a_tenant_nothing_is_visible(world):
    async def go(db):
        async with db.unscoped() as conn:
            return {t: await conn.fetchval("select count(*) from %s" % t) for t in TABLES}
    assert set(with_db(go).values()) == {0}


# ------------------------------------------------------------------------------------------------ what a sync stores and keeps

def test_a_second_sync_the_same_day_corrects_the_snapshot_and_keeps_what_a_person_set(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            before = await conn.fetchval("select count(*) from inventory_snapshot where observed_on = $1", TODAY)
            ids = await store.upsert_variants(conn, world.a, [stock(1, "Tee renamed", "TEE-1", 7, cost=900)])
            await store.save_snapshots(conn, world.a, TODAY, [stock(1, "Tee renamed", "TEE-1", 7)], ids)
            after = await conn.fetchval("select count(*) from inventory_snapshot where observed_on = $1", TODAY)
            snap = await conn.fetchrow("select available from inventory_snapshot where variant_id = $1::uuid and observed_on = $2", world.ids["tee"], TODAY)
            v = await conn.fetchrow("select v.product_title, v.moq, v.pack_size, v.unit_cost_minor, s.name from inventory_variant v join inventory_supplier s on s.id = v.supplier_id where v.id = $1::uuid", world.ids["tee"])
            keep = await store.upsert_variants(conn, world.a, [stock(1, "Tee renamed", "TEE-1", 7)])            # a refresh whose shop holds no cost
            cost = await conn.fetchval("select unit_cost_minor from inventory_variant where id = $1::uuid", keep["gid://shopify/ProductVariant/1"])
        return before, after, snap["available"], dict(v), cost
    before, after, available, v, cost = with_db(go)
    assert before == after == 4 and available == 7
    assert v == {"product_title": "Tee renamed", "moq": 50, "pack_size": 12, "unit_cost_minor": 900, "name": "Acme Goods"}      # renamed and re-costed by the shop; supplier, moq and pack kept
    assert cost == 900                                                                                                     # a shop with no cost does not erase ours


def test_replacing_a_window_of_order_lines_leaves_no_stale_line(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            ids = {"gid://shopify/ProductVariant/1": world.ids["tee"]}
            await store.replace_order_lines(conn, world.a, day(1), day(8), [sale(900, 1, day(2), 5, 100)], ids)
            inside = await conn.fetchval("select count(*) from inventory_order_line where processed_on >= $1 and processed_on < $2", day(1), day(8))
            outside = await conn.fetchval("select count(*) from inventory_order_line where processed_on >= $1", day(8))
            kept = await conn.fetchval("select quantity from inventory_order_line where line_gid = 'gid://shopify/LineItem/900'")
        return inside, outside, kept
    inside, outside, kept = with_db(go)
    assert inside == 1 and kept == 5                                  # the seven earlier lines of the window are gone, the new one is in
    assert outside > 20                                               # lines after the window are untouched


def test_the_variant_reference_must_name_exactly_one_variant(world):
    async def go(db):
        out = []
        async with db.tenant(world.a) as conn:
            for ref in ("NO-SUCH", "gid://shopify/ProductVariant/2", "2", "MUG-1"):
                try:
                    out.append((await store.find_variant(conn, ref))["sku"])
                except LookupError as exc:
                    out.append(str(exc))
        return out
    got = with_db(go)
    assert got[0].startswith("no variant matches") and got[1:] == ["MUG-1", "MUG-1", "MUG-1"]


# ------------------------------------------------------------------------------------------------ the stored plan

def test_the_stored_plan_matches_the_worked_example(world):
    """The Tee: 40 units over the 20 days it was on sale is 2.0 a day (not 40 / 28 = 1.43), 10 on the shelf is 5 days of cover against a 9 day lead time, reorder point 18 (no variation),
    target 2 * (9 + 14) = 46 less 10 on hand is 36, raised to the minimum 50 and to whole packs of 12: 60."""
    async def go(db):
        async with db.tenant(world.a) as conn:
            return {r["sku"] or r["product_title"]: dict(r) for r in await store.plan_lines(conn, TODAY)}
    by = with_db(go)
    tee = by["TEE-1"]
    assert (tee["velocity"], tee["counted_days"], tee["out_days"], tee["unknown_days"], tee["units_recent"]) == (pytest.approx(2.0), 20, 8, 0, 40)
    assert tee["velocity_sd"] == pytest.approx(0.0) and tee["cover_days"] == pytest.approx(5.0)
    assert (tee["lead_time_days"], tee["safety_stock"], tee["reorder_point"], tee["needs_reorder"], tee["suggested_qty"]) == (9, 0, 18, True, 60)
    assert (tee["abc_class"], tee["label"], tee["status"], tee["supplier_name"], tee["unit_cost_minor"], tee["stock_read_on"]) == ("A", "best_seller", "ok", "Acme Goods", 800, TODAY)
    mug, hat, gift = by["MUG-1"], by["HAT-1"], by["Gift card"]
    assert mug["status"] == "no_supplier" and mug["suggested_qty"] == 0 and mug["reorder_point"] is None and mug["cover_days"] == pytest.approx(50 / (14 / 28))
    assert hat["label"] == "slow_mover" and hat["units_all"] == 1 and hat["units_prior"] == 1     # its one sale was 40 days ago, in the prior window
    assert gift["status"] == "untracked" and gift["abc_class"] == "C"
    assert {r["abc_class"] for r in by.values()} <= {"A", "B", "C"} and {r["label"] for r in by.values()} <= {"best_seller", "rising", "slow_mover", "steady"}


def test_running_the_plan_again_the_same_day_replaces_that_run(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await store.make_plan(conn, world.a, TODAY, PlanParams(review_days=30))
            runs = await conn.fetchval("select count(*) from inventory_plan_run")
            lines = await conn.fetchval("select count(*) from inventory_plan_line")
            params = await conn.fetchval("select params->>'review_days' from inventory_plan_run")
            qty = await conn.fetchval("select suggested_qty from inventory_plan_line where sku = 'TEE-1'")
        return runs, lines, params, qty
    runs, lines, params, qty = with_db(go)
    assert (runs, lines, params) == (1, 4, "30")
    assert qty == 72                                                   # 2 * (9 + 30) = 78 less 10 is 68, raised to whole packs of 12
    assert admin_rows("select count(*) from inventory_plan_line")[0][0] == 4


def test_a_promotion_a_person_entered_moves_the_forecast_and_the_order(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await store.add_promo(conn, world.a, ref="TEE-1", starts_on=TODAY + timedelta(days=3), ends_on=TODAY + timedelta(days=40), factor=2.0, entered_by="ads team", note="spring launch")
            await store.make_plan(conn, world.a, TODAY)
            tee = await conn.fetchrow("select * from inventory_plan_line where sku = 'TEE-1'")
            mug = await conn.fetchrow("select promo_factor from inventory_plan_line where sku = 'MUG-1'")
        return dict(tee), mug["promo_factor"]
    tee, mug_factor = with_db(go)
    assert tee["cover_days"] == pytest.approx(5.0) and tee["forecast_cover_days"] < 5.0
    assert tee["promo_factor"] > 1.0 and tee["suggested_qty"] >= 60 and mug_factor == 1.0          # only the Tee has the promotion


def test_a_promotion_with_a_bad_factor_or_dates_or_no_author_is_refused(world):
    async def go(db):
        out = []
        async with db.tenant(world.a) as conn:
            for kwargs in (dict(factor=0.0), dict(factor=25.0), dict(factor=1.5, ends_on=TODAY - timedelta(days=1)), dict(factor=1.5, entered_by="  ")):
                args = dict(ref=None, starts_on=TODAY, ends_on=TODAY + timedelta(days=5), factor=1.5, entered_by="x") | kwargs
                try:
                    async with conn.transaction():
                        await store.add_promo(conn, world.a, **args)
                    out.append("stored")
                except (asyncpg.PostgresError, ValueError) as exc:
                    out.append(type(exc).__name__)
        return out
    assert with_db(go) == ["CheckViolationError", "CheckViolationError", "CheckViolationError", "ValueError"]


# ------------------------------------------------------------------------------------------------ alerts, once

def test_a_shortage_is_raised_once_however_often_the_plan_and_the_alerts_run(world):
    async def go(db):
        out = []
        for _ in range(3):
            async with db.tenant(world.a) as conn:
                await store.make_plan(conn, world.a, TODAY)
                raised = await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
                out.append((raised.new, raised.already_raised))
        async with db.tenant(world.a) as conn:
            rows = [dict(r) for r in await conn.fetch("select * from inventory_alert")]
            outbox = [dict(r) for r in await conn.fetch("select * from inventory_alert_outbox")]
        return out, rows, outbox
    out, rows, outbox = with_db(go)
    assert out == [(1, 0), (0, 1), (0, 1)]
    [alert] = rows
    assert alert["kind"] == "cover_below_lead_time" and alert["event_ref"] == day(19).isoformat()        # the cycle that began when the Tee was restocked on day 19
    [box] = outbox
    assert box["status"] == "pending" and box["idempotency_key"] == "%s:%s:cover_below_lead_time:%s" % (world.a, world.ids["tee"], day(19).isoformat())
    payload = json.loads(box["payload"]) if isinstance(box["payload"], str) else box["payload"]
    assert payload["sku"] == "TEE-1" and payload["suggested_qty"] == 60 and payload["lead_time_days"] == 9 and payload["cover_days"] == 5.0 and payload["supplier"] == "Acme Goods"


def test_a_later_day_in_the_same_stock_cycle_does_not_alert_again_but_a_restock_then_a_new_shortage_does(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
        tomorrow = TODAY + timedelta(days=1)
        tee = world.ids["tee"]
        async with db.tenant(world.a) as conn:                      # one more day, 2 units gone: still the same cycle
            await conn.execute("insert into inventory_snapshot (tenant_id, variant_id, observed_on, available) values ($1::uuid, $2::uuid, $3, 8)", world.a, tee, tomorrow)
            await store.make_plan(conn, world.a, tomorrow)
            again = await alerts.raise_alerts(conn, world.a, tomorrow, await store.plan_lines(conn, tomorrow), await store.snapshot_series(conn, tomorrow))
        third = TODAY + timedelta(days=2)
        async with db.tenant(world.a) as conn:                      # a delivery arrives: 80 on the shelf
            await conn.execute("insert into inventory_snapshot (tenant_id, variant_id, observed_on, available) values ($1::uuid, $2::uuid, $3, 80)", world.a, tee, third)
            await store.make_plan(conn, world.a, third)
            healthy = await alerts.raise_alerts(conn, world.a, third, await store.plan_lines(conn, third), await store.snapshot_series(conn, third))
        fourth = TODAY + timedelta(days=3)
        async with db.tenant(world.a) as conn:                      # and it sells down to 6 within the lead time: a new cycle, so a new alert
            await conn.execute("insert into inventory_snapshot (tenant_id, variant_id, observed_on, available) values ($1::uuid, $2::uuid, $3, 6)", world.a, tee, fourth)
            await store.make_plan(conn, world.a, fourth)
            later = await alerts.raise_alerts(conn, world.a, fourth, await store.plan_lines(conn, fourth), await store.snapshot_series(conn, fourth))
            refs = [r["event_ref"] for r in await conn.fetch("select event_ref from inventory_alert order by id")]
        return again, healthy, later, refs
    again, healthy, later, refs = with_db(go)
    assert (again.new, again.already_raised) == (0, 1)
    assert healthy.new == 0 and healthy.already_raised == 0           # 80 on the shelf is 40 days of cover: nothing short
    assert later.new == 1
    assert refs == [day(19).isoformat(), (TODAY + timedelta(days=2)).isoformat()]


def test_out_of_stock_alerts_for_a_variant_that_sells_and_not_for_one_that_does_not(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await conn.execute("update inventory_snapshot set available = 0 where observed_on = $1 and variant_id in ($2::uuid, $3::uuid)", TODAY, world.ids["mug"], world.ids["gift"])
            await store.make_plan(conn, world.a, TODAY)
            await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
            return [(r["sku"], r["kind"]) for r in await conn.fetch("select v.sku, a.kind from inventory_alert a join inventory_variant v on v.id = a.variant_id order by v.sku")]
    got = with_db(go)
    assert got == [("MUG-1", "out_of_stock"), ("TEE-1", "cover_below_lead_time")]       # the untracked gift card is silent, the supplier-less mug is still out of stock


def test_the_database_itself_refuses_a_second_alert_for_one_shortage_and_any_rewrite(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
            errors = []
            for sql in ("insert into inventory_alert (tenant_id, variant_id, kind, event_ref, as_of, detail) select tenant_id, variant_id, kind, event_ref, as_of, detail from inventory_alert",
                        "update inventory_alert set event_ref = 'x'", "delete from inventory_alert"):
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                    errors.append("done")
                except asyncpg.PostgresError as exc:
                    errors.append(type(exc).__name__)
            return errors
    errors = with_db(go)
    assert errors[0] == "UniqueViolationError" and errors[1:] == ["InsufficientPrivilegeError", "InsufficientPrivilegeError"]       # the request role cannot even try
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update inventory_alert set event_ref = 'x'")                                                              # and the superuser is stopped by the trigger
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("truncate inventory_alert, inventory_alert_outbox")


# ------------------------------------------------------------------------------------------------ delivery through the existing outbox code

def _raised(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            await alerts.raise_alerts(conn, world.a, TODAY, await store.plan_lines(conn, TODAY), await store.snapshot_series(conn, TODAY))
    with_db(go)


def test_a_pending_alert_is_delivered_once_with_the_payload_and_key_that_were_stored(world):
    _raised(world)
    seen = []

    def sink(payload, key):
        seen.append((payload, key))
        return SinkAnswer("ok", "ref-1")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("set role sync_worker")                                          # the delivery role: crosses tenants, cannot be a superuser
        first = deliver_pending(conn, sink, table=alerts.OUTBOX_TABLE)
        second = deliver_pending(conn, sink, table=alerts.OUTBOX_TABLE)
    assert (first.delivered, first.dead, second.delivered) == (1, 0, 0) and len(seen) == 1
    stored = admin_rows("select payload, idempotency_key, status, sink_ref, attempts from inventory_alert_outbox")
    payload, key, status, ref, attempts = stored[0]
    assert (payload, key) == seen[0] and status == "delivered" and ref == "ref-1" and attempts == 1
    assert "Tee" in alerts.describe_alert(payload) and "5.0 days of cover" in alerts.describe_alert(payload) and "Suggested order: 60 units from Acme Goods" in alerts.describe_alert(payload)


def test_a_failing_sink_is_retried_with_the_same_key(world):
    _raised(world)
    keys = []
    answers = iter([SinkAnswer("ambiguous", None, "HTTP 503"), SinkAnswer("ok", "done")])

    def flaky(payload, key):
        keys.append(key)
        return next(answers)

    with psycopg.connect(DSN, autocommit=True) as conn:
        first = deliver_pending(conn, flaky, table=alerts.OUTBOX_TABLE, backoff_seconds=0)
        second = deliver_pending(conn, flaky, table=alerts.OUTBOX_TABLE, backoff_seconds=0)
    assert (first.retrying, second.delivered) == (1, 1) and len(set(keys)) == 1 and len(keys) == 2
    assert admin_rows("select status, attempts from inventory_alert_outbox") == [("delivered", 2)]


def test_a_refusal_is_permanent_and_the_alert_is_not_pushed_again(world):
    _raised(world)
    with psycopg.connect(DSN, autocommit=True) as conn:
        dead = deliver_pending(conn, lambda p, k: SinkAnswer("rejected", None, "HTTP 400"), table=alerts.OUTBOX_TABLE)
        again = deliver_pending(conn, lambda p, k: SinkAnswer("ok", "x"), table=alerts.OUTBOX_TABLE)
    assert (dead.dead, again.delivered) == (1, 0)
    assert admin_rows("select status, last_outcome from inventory_alert_outbox") == [("dead", "rejected: HTTP 400")]


def test_a_table_name_that_is_not_an_outbox_is_refused_before_any_sql(world):
    with psycopg.connect(DSN, autocommit=True) as conn:
        for bad in ("tenant", "change_outbox; drop table tenant", "inventory_alert", ""):
            with pytest.raises(ValueError, match="not an outbox table"):
                deliver_pending(conn, lambda p, k: SinkAnswer("ok", "x"), table=bad)
    assert admin_rows("select count(*) from tenant")[0][0] == 2


def test_the_worker_role_reads_the_outbox_across_tenants_and_the_request_role_only_its_own(world):
    _raised(world)

    async def go(db):
        async with db.tenant(world.b) as conn:
            return await conn.fetchval("select count(*) from inventory_alert_outbox")
    assert with_db(go) == 0
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("set role sync_worker")
        assert conn.execute("select count(*) from inventory_alert_outbox").fetchone()[0] == 1
