"""The ledger and the wallet against real Postgres, as the REQUEST role: the balanced-transaction constraint, append only, no overdraft, the wallet lock under a
race, idempotency (same key same result, same key different request refused), forced row level security with negative tests, and the reconciliation that proves
the books agree (and fails loudly when they do not).

Skips cleanly when Postgres is not running; like the other database suites it drops and recreates the `public` schema it points at.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import CURRENCY, DSN, counts, needs_db, new_tenants, psycopg, reset_billing_schema, run, set_price, with_rig  # noqa: E402

pytestmark = needs_db

from app.billing import ledger, reports, wallet  # noqa: E402
from app.billing.errors import IdempotencyConflict, NoUsagePrice, ReconciliationFailed  # noqa: E402

TABLES = ("billing_account", "billing_transaction", "billing_entry", "billing_usage_price", "billing_usage_event", "billing_payment", "billing_subscription",
          "billing_plan_change", "billing_webhook_event", "billing_notification")


class World:
    pass


@pytest.fixture()
def world():
    reset_billing_schema()
    w = World()
    w.a, w.b = new_tenants("tenant-a", "tenant-b")
    return w


async def fund(db, tenant, amount, ref):
    async with db.tenant(tenant) as conn:
        return await wallet.top_up(conn, tenant, amount_minor=amount, currency=CURRENCY, provider_ref=ref)


async def debit(db, tenant, qty, key, event_type="sync_run"):
    async with db.tenant(tenant) as conn:
        return await wallet.debit_usage(conn, tenant, event_type=event_type, quantity=qty, currency=CURRENCY, key=key)


async def seed(rig, w, amount=1000):
    for t in (w.a, w.b):
        await set_price(rig.db, t, "sync_run", 25)
        await fund(rig.db, t, amount, "pi_seed_" + t[:6])


# ---- the migration and the role ----------------------------------------------------------------------------------------------------

def test_the_migration_is_safe_to_run_twice(world):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute((ROOT / "migrations" / "012_billing.sql").read_text(encoding="utf-8"))


def test_every_billing_table_has_row_level_security_enabled_forced_and_a_policy(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(%s)", (list(TABLES),))
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute("select tablename, count(*) from pg_policies where schemaname = 'public' and tablename = any(%s) group by tablename", (list(TABLES),))
        policies = dict(cur.fetchall())
    assert set(rows) == set(TABLES)
    assert all(v == (True, True) for v in rows.values()), rows
    assert all(policies.get(t, 0) >= 1 for t in TABLES), policies


def test_the_connection_really_is_the_request_role_and_not_a_superuser(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            return await conn.fetchrow("select current_user as who, r.rolsuper, r.rolbypassrls from pg_roles r where r.rolname = current_user")
    row = with_rig(go)
    assert (row["who"], row["rolsuper"], row["rolbypassrls"]) == ("app_request", False, False)


# ---- tenant isolation, negative, as the request role ----------------------------------------------------------------------------------

def test_tenant_a_sees_none_of_tenant_bs_rows_in_any_table_and_all_of_its_own(world):
    async def go(rig):
        await seed(rig, world)
        await debit(rig.db, world.b, 4, "b-use-1")
        out = {}
        for t in TABLES:
            async with rig.db.tenant(world.a) as conn:
                out[t] = (await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, world.b),
                          await conn.fetchval("select count(*) from %s" % t),
                          await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, world.a))
        return out
    got = with_rig(go)
    for t, (of_b, visible, of_a) in got.items():
        assert of_b == 0, t
        assert visible == of_a, t
    assert got["billing_entry"][2] > 0 and got["billing_account"][2] > 0 and got["billing_usage_price"][2] == 1


def test_a_connection_with_no_tenant_set_sees_nothing_at_all(world):
    async def go(rig):
        await seed(rig, world)
        async with rig.db.unscoped() as conn:
            return [await conn.fetchval("select count(*) from %s" % t) for t in TABLES]
    assert with_rig(go) == [0] * len(TABLES)


def test_tenant_a_cannot_write_a_row_for_tenant_b_in_any_money_table(world):
    async def go(rig):
        await seed(rig, world)
        errors = []
        async with rig.db.tenant(world.b) as conn:
            b_wallet = await conn.fetchval("select id from billing_account where kind = 'wallet'")
        inserts = {
            "billing_account": ("insert into billing_account (tenant_id, kind, currency) values ($1::uuid, 'wallet', 'USD')", (world.b,)),
            "billing_transaction": ("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values ($1::uuid, 'topup', 'AUD', 'x', 'h', 'a')", (world.b,)),
            "billing_usage_price": ("insert into billing_usage_price (tenant_id, event_type, currency, unit_price_minor) values ($1::uuid, 'x', 'AUD', 1)", (world.b,)),
            "billing_notification": ("insert into billing_notification (tenant_id, kind, message) values ($1::uuid, 'payment_failed', 'x')", (world.b,)),
        }
        for name, (sql, args) in inserts.items():
            with pytest.raises(asyncpg.PostgresError):
                async with rig.db.tenant(world.a) as conn:
                    await conn.execute(sql, *args)
            errors.append(name)
        # an entry for B's wallet written under A's tenant is refused as well, however it is dressed
        with pytest.raises(asyncpg.PostgresError):
            async with rig.db.tenant(world.a) as conn:
                tx = await conn.fetchval("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values ($1::uuid, 'topup', 'AUD', 'z', 'h', 'a') returning id", world.a)
                await conn.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values ($1::uuid, $2, $3, 'AUD', 5)", world.a, tx, b_wallet)
        return errors
    assert len(with_rig(go)) == 4


def test_tenant_a_cannot_move_tenant_bs_balance_or_read_its_wallet_by_id(world):
    async def go(rig):
        await seed(rig, world)
        async with rig.db.tenant(world.b) as conn:
            b_wallet = await conn.fetchval("select id from billing_account where kind = 'wallet'")
        async with rig.db.tenant(world.a) as conn:
            seen = await conn.fetchval("select count(*) from billing_account where id = $1::uuid", b_wallet)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with rig.db.tenant(world.a) as conn:
                await conn.execute("update billing_account set balance_minor = 0 where id = $1::uuid", b_wallet)
        return seen, (await counts(rig.db, world.b))["spendable"]
    seen, b_spend = with_rig(go)
    assert (seen, b_spend) == (0, 1000)


def test_the_request_role_cannot_set_a_balance_edit_a_plan_or_touch_the_ledger_after_the_fact(world):
    async def go(rig):
        await seed(rig, world)
        bad = [
            "update billing_account set balance_minor = 0",
            "insert into billing_account (tenant_id, kind, currency, balance_minor) values ('%s'::uuid, 'refunds', 'USD', -5)" % world.a,
            "update billing_entry set amount_minor = 1",
            "delete from billing_entry",
            "update billing_transaction set actor = 'x'",
            "delete from billing_transaction",
            "update billing_usage_event set quantity = 1",
            "delete from billing_usage_event",
            "insert into billing_plan (code, name, currency, monthly_price_minor) values ('x', 'x', 'AUD', 1)",
            "update billing_plan set monthly_price_minor = 0",
        ]
        denied = 0
        for sql in bad:
            try:
                async with rig.db.tenant(world.a) as conn:
                    await conn.execute(sql)
            except asyncpg.InsufficientPrivilegeError:
                denied += 1
        return denied, len(bad), await counts(rig.db, world.a)
    denied, total, c = with_rig(go)
    assert denied == total and c["spendable"] == 1000


def test_the_router_role_reads_payments_and_subscriptions_and_nothing_else_and_writes_nothing(world):
    async def go(rig):
        await seed(rig, world)
        async with rig.router.unscoped() as conn:
            for table in ("billing_payment", "billing_subscription"):
                await conn.fetchval("select count(*) from %s" % table)
            denied = 0
            for sql in ("select count(*) from billing_entry", "select count(*) from billing_account", "select count(*) from billing_transaction",
                        "select count(*) from billing_webhook_event"):
                try:
                    async with conn.transaction():
                        await conn.fetchval(sql)
                except asyncpg.InsufficientPrivilegeError:
                    denied += 1
            for sql in ("insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency) values (gen_random_uuid(), 'k', 'h', 'p', 1, 'AUD')",
                        "update billing_payment set status = 'failed'", "delete from billing_payment"):
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                except asyncpg.InsufficientPrivilegeError:
                    denied += 1
        return denied
    assert with_rig(go) == 7


def test_deleting_a_tenant_removes_its_ledger_but_an_entry_is_never_deleted_while_the_tenant_exists(world):
    async def go(rig):
        await seed(rig, world)
    with_rig(go)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("delete from billing_entry where tenant_id = %s", (world.a,))
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update billing_entry set amount_minor = amount_minor + 1 where tenant_id = %s", (world.a,))
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update billing_transaction set actor = 'tampered' where tenant_id = %s", (world.a,))
        cur.execute("delete from tenant where id = %s", (world.a,))
        cur.execute("select count(*) from billing_entry where tenant_id = %s", (world.a,))
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from billing_entry where tenant_id = %s", (world.b,))
        assert cur.fetchone()[0] > 0


# ---- balanced transactions, enforced by the database ----------------------------------------------------------------------------------

async def raw_transaction(db, tenant, lines, kind="topup", currency=CURRENCY, key="raw-1"):
    """Insert a transaction and the given (account kind, amount) lines directly, bypassing the module, and commit."""
    async with db.tenant(tenant) as conn:
        accounts = await ledger.ensure_accounts(conn, tenant, CURRENCY)
        tx = await conn.fetchval("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values ($1::uuid, $2, $3, $4, 'h', 'raw') returning id",
                                 tenant, kind, currency, key)
        for account, amount in lines:
            await conn.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values ($1::uuid, $2, $3::uuid, $4, $5)",
                               tenant, tx, accounts[account], currency, amount)


def test_a_transaction_whose_lines_do_not_add_up_to_zero_cannot_commit(world):
    async def go(rig):
        with pytest.raises(asyncpg.CheckViolationError, match="out of balance by 1"):
            await raw_transaction(rig.db, world.a, [("provider_clearing", 500), ("wallet", -499)])
        return await counts(rig.db, world.a)
    c = with_rig(go)
    assert (c["billing_transaction"], c["billing_entry"], c["spendable"]) == (0, 0, 0)


def test_a_transaction_with_one_line_or_none_cannot_commit(world):
    async def go(rig):
        with pytest.raises(asyncpg.CheckViolationError, match="at least two"):
            await raw_transaction(rig.db, world.a, [("provider_clearing", 0 + 1)], key="one")
        with pytest.raises(asyncpg.CheckViolationError, match="0 line"):
            await raw_transaction(rig.db, world.a, [], key="none")
        return await counts(rig.db, world.a)
    c = with_rig(go)
    assert c["billing_transaction"] == 0


def test_a_balanced_transaction_written_line_by_line_commits_and_moves_the_balance(world):
    async def go(rig):
        await raw_transaction(rig.db, world.a, [("provider_clearing", 700), ("wallet", -300), ("platform_revenue", -400)])
        return await counts(rig.db, world.a)
    c = with_rig(go)
    assert (c["billing_transaction"], c["billing_entry"], c["spendable"]) == (1, 3, 300)


def test_a_line_in_another_currency_than_its_transaction_or_its_account_is_refused(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            accounts = await ledger.ensure_accounts(conn, world.a, CURRENCY)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with rig.db.tenant(world.a) as conn:
                tx = await conn.fetchval("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values ($1::uuid, 'topup', 'USD', 'cur-1', 'h', 'raw') returning id", world.a)
                await conn.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values ($1::uuid, $2, $3::uuid, 'USD', 5)", world.a, tx, accounts["wallet"])
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with rig.db.tenant(world.a) as conn:
                tx = await conn.fetchval("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values ($1::uuid, 'topup', 'AUD', 'cur-2', 'h', 'raw') returning id", world.a)
                await conn.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values ($1::uuid, $2, $3::uuid, 'USD', 5)", world.a, tx, accounts["wallet"])
    with_rig(go)


def test_a_zero_amount_line_is_refused(world):
    async def go(rig):
        with pytest.raises(asyncpg.CheckViolationError):
            await raw_transaction(rig.db, world.a, [("provider_clearing", 0), ("wallet", 0)])
    with_rig(go)


# ---- the wallet never goes below zero ------------------------------------------------------------------------------------------------------

def test_the_database_itself_refuses_an_entry_that_would_take_a_wallet_below_zero(world):
    async def go(rig):
        await fund(rig.db, world.a, 100, "pi_one")
        with pytest.raises(asyncpg.CheckViolationError, match="wallet_never_overdrawn"):
            await raw_transaction(rig.db, world.a, [("wallet", 101), ("platform_revenue", -101)], kind="usage")
        return await counts(rig.db, world.a)
    assert with_rig(go)["spendable"] == 100


def test_a_debit_larger_than_the_balance_is_insufficient_funds_and_records_nothing_at_all(world):
    async def go(rig):
        await seed(rig, world, 100)
        before = await counts(rig.db, world.a)
        out = await debit(rig.db, world.a, 5, "too-big")          # 5 x 25 = 125 against 100
        return before, out, await counts(rig.db, world.a)
    before, out, after = with_rig(go)
    assert (out.status, out.ok, out.transaction_id, out.amount_minor, out.shortfall_minor, out.balance_minor) == ("insufficient_funds", False, None, 125, 25, 100)
    assert after == before


def test_a_debit_of_exactly_the_balance_is_allowed_and_leaves_zero(world):
    async def go(rig):
        await seed(rig, world, 100)
        out = await debit(rig.db, world.a, 4, "exact")
        again = await debit(rig.db, world.a, 1, "one-more")
        return out, again, await counts(rig.db, world.a)
    out, again, c = with_rig(go)
    assert (out.status, out.balance_minor) == ("applied", 0) and again.status == "insufficient_funds" and c["spendable"] == 0


def test_an_admin_adjustment_cannot_take_a_wallet_below_zero_either(world):
    async def go(rig):
        await seed(rig, world, 100)
        async with rig.db.tenant(world.a) as conn:
            return await wallet.adjust(conn, world.a, amount_minor=-101, currency=CURRENCY, key="adj-1", actor="admin", reason="clawback")
    out = with_rig(go)
    assert (out.status, out.shortfall_minor) == ("insufficient_funds", 1)


def test_a_debit_with_no_price_for_the_event_is_refused_and_a_price_is_per_tenant(world):
    async def go(rig):
        await set_price(rig.db, world.a, "sync_run", 25)
        await set_price(rig.db, world.b, "sync_run", 40)
        await fund(rig.db, world.a, 1000, "pi_a")
        await fund(rig.db, world.b, 1000, "pi_b")
        with pytest.raises(NoUsagePrice):
            await debit(rig.db, world.a, 1, "x", event_type="never_priced")
        a, b = await debit(rig.db, world.a, 10, "k1"), await debit(rig.db, world.b, 10, "k1")
        return a, b
    a, b = with_rig(go)
    assert (a.amount_minor, b.amount_minor) == (250, 400)       # the same key in two tenants is two different commands


def test_usage_is_an_integer_price_times_quantity_and_the_event_row_cannot_say_otherwise(world):
    async def go(rig):
        await seed(rig, world)
        out = await debit(rig.db, world.a, 7, "u1")
        async with rig.db.tenant(world.a) as conn:
            row = await conn.fetchrow("select * from billing_usage_event")
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute("insert into billing_usage_event (tenant_id, transaction_id, event_type, quantity, unit_price_minor, amount_minor, currency) "
                                       "values ($1::uuid, $2, 'sync_run', 2, 25, 51, 'AUD')", world.a, row["transaction_id"])
        return out, row
    out, row = with_rig(go)
    assert (row["quantity"], row["unit_price_minor"], row["amount_minor"], out.amount_minor) == (7, 25, 175, 175)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "3", None])
def test_an_amount_or_quantity_that_is_not_a_positive_integer_is_refused_before_the_database(world, bad):
    async def go(rig):
        await seed(rig, world)
        async with rig.db.tenant(world.a) as conn:
            with pytest.raises(ValueError):
                await wallet.debit_usage(conn, world.a, event_type="sync_run", quantity=bad, currency=CURRENCY, key="bad")
            with pytest.raises(ValueError):
                await wallet.top_up(conn, world.a, amount_minor=bad, currency=CURRENCY, provider_ref="pi_bad")
    with_rig(go)


# ---- the other wallet commands and their ledger lines --------------------------------------------------------------------------------------

def test_each_command_writes_the_documented_balanced_lines(world):
    async def go(rig):
        await seed(rig, world, 1000)
        await debit(rig.db, world.a, 4, "use")
        async with rig.db.tenant(world.a) as conn:
            await wallet.refund_topup(conn, world.a, amount_minor=300, currency=CURRENCY, provider_ref="pi_seed_x:refund:300")
            await wallet.refund_usage(conn, world.a, amount_minor=50, currency=CURRENCY, key="svc-1", actor="ops", reason="the sync ran twice")
            await wallet.adjust(conn, world.a, amount_minor=-20, currency=CURRENCY, key="adj-1", actor="ops", reason="correction")
            rows = await conn.fetch("select t.kind, a.kind as account, e.amount_minor from billing_entry e join billing_transaction t on t.id = e.transaction_id "
                                    "join billing_account a on a.id = e.account_id order by e.id")
        return [(r["kind"], r["account"], r["amount_minor"]) for r in rows]
    lines = with_rig(go)
    assert lines == [("topup", "provider_clearing", 1000), ("topup", "wallet", -1000),
                     ("usage", "wallet", 100), ("usage", "platform_revenue", -100),
                     ("refund", "wallet", 300), ("refund", "provider_clearing", -300),
                     ("usage_refund", "refunds", 50), ("usage_refund", "wallet", -50),
                     ("adjustment", "adjustments", -20), ("adjustment", "wallet", 20)]


def test_an_adjustment_needs_an_actor_and_a_reason_in_the_module_and_in_the_table(world):
    async def go(rig):
        await seed(rig, world)
        async with rig.db.tenant(world.a) as conn:
            for kw in ({"reason": ""}, {"reason": "  "}, {"reason": "ab"}, {"actor": " "}):
                args = dict(amount_minor=10, currency=CURRENCY, key="adj-x", actor="ops", reason="a real reason") | kw
                with pytest.raises(ValueError):
                    await wallet.adjust(conn, world.a, **args)
            with pytest.raises(ValueError):
                await wallet.adjust(conn, world.a, amount_minor=0, currency=CURRENCY, key="adj-z", actor="ops", reason="a real reason")
            with pytest.raises(ValueError):
                await wallet.refund_usage(conn, world.a, amount_minor=5, currency=CURRENCY, key="svc-x", actor="ops", reason="")
        with pytest.raises(asyncpg.CheckViolationError, match="adjustment_needs_a_reason"):
            await raw_transaction(rig.db, world.a, [("adjustments", 5), ("wallet", -5)], kind="adjustment", key="raw-adj")
    with_rig(go)


# ---- idempotency: same key same result, same key different request refused ---------------------------------------------------------------

def test_the_same_key_and_the_same_request_returns_the_first_result_and_writes_nothing(world):
    async def go(rig):
        await seed(rig, world)
        first = await debit(rig.db, world.a, 3, "idem-1")
        mid = await counts(rig.db, world.a)
        second = await debit(rig.db, world.a, 3, "idem-1")
        third = await debit(rig.db, world.a, 3, "idem-1")
        return first, second, third, mid, await counts(rig.db, world.a)
    first, second, third, mid, after = with_rig(go)
    assert (first.status, second.status, third.status) == ("applied", "replayed", "replayed")
    assert first.transaction_id == second.transaction_id == third.transaction_id
    assert second.amount_minor == first.amount_minor == 75 and second.balance_minor == first.balance_minor == 925
    assert after == mid


def test_the_same_key_with_a_different_request_is_refused_loudly_and_writes_nothing(world):
    async def go(rig):
        await seed(rig, world)
        await debit(rig.db, world.a, 3, "idem-2")
        before = await counts(rig.db, world.a)
        for qty, event in ((4, "sync_run"), (3, "report_run")):
            await set_price(rig.db, world.a, "report_run", 100)
            with pytest.raises(IdempotencyConflict) as exc:
                await debit(rig.db, world.a, qty, "idem-2", event_type=event)
            assert exc.value.key == "idem-2"
        async with rig.db.tenant(world.a) as conn:
            with pytest.raises(IdempotencyConflict):          # even a different command under the same key
                await wallet.adjust(conn, world.a, amount_minor=75, currency=CURRENCY, key="idem-2", actor="ops", reason="same number, other command")
        return before, await counts(rig.db, world.a)
    before, after = with_rig(go)
    assert after["billing_transaction"] == before["billing_transaction"] and after["spendable"] == before["spendable"]


def test_a_provider_payment_credits_once_whatever_key_or_route_reaches_it(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            a = await wallet.top_up(conn, world.a, amount_minor=500, currency=CURRENCY, provider_ref="pi_dup")
            b = await wallet.top_up(conn, world.a, amount_minor=500, currency=CURRENCY, provider_ref="pi_dup", key="another-key")
            with pytest.raises(IdempotencyConflict):
                await wallet.top_up(conn, world.a, amount_minor=600, currency=CURRENCY, provider_ref="pi_dup")
        return a, b, await counts(rig.db, world.a)
    a, b, c = with_rig(go)
    assert (a.status, b.status, c["billing_transaction"], c["spendable"]) == ("applied", "replayed", 1, 500)


def test_the_same_key_means_two_things_in_two_tenants_and_is_scoped_to_each(world):
    async def go(rig):
        await seed(rig, world, 1000)
        return await debit(rig.db, world.a, 2, "shared-key"), await debit(rig.db, world.b, 2, "shared-key")
    a, b = with_rig(go)
    assert (a.status, b.status) == ("applied", "applied") and a.transaction_id != b.transaction_id


# ---- concurrency: the wallet lock ---------------------------------------------------------------------------------------------------------

def test_thirty_concurrent_debits_against_a_wallet_that_covers_ten_leave_exactly_zero_and_no_overdraft(world):
    async def go(rig):
        await set_price(rig.db, world.a, "sync_run", 100)
        await fund(rig.db, world.a, 1000, "pi_race")
        results = await asyncio.gather(*[debit(rig.db, world.a, 1, "race-%d" % i) for i in range(30)], return_exceptions=True)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.reconcile(conn, world.a)
        return results, await counts(rig.db, world.a), rec
    results, c, rec = with_rig(go, size=12)
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == []
    assert sorted(r.status for r in results).count("applied") == 10 and sorted(r.status for r in results).count("insufficient_funds") == 20
    assert c["spendable"] == 0 and c["billing_usage_event"] == 10 and rec.ok


def test_twenty_concurrent_copies_of_one_debit_write_it_once(world):
    async def go(rig):
        await seed(rig, world)
        results = await asyncio.gather(*[debit(rig.db, world.a, 5, "same-key") for _ in range(20)], return_exceptions=True)
        return results, await counts(rig.db, world.a)
    results, c = with_rig(go, size=12)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert sorted(r.status for r in results).count("applied") == 1 and sorted(r.status for r in results).count("replayed") == 19
    assert c["billing_usage_event"] == 1 and c["spendable"] == 1000 - 125


def test_concurrent_top_ups_debits_and_refunds_of_one_wallet_never_overdraw_and_always_reconcile(world):
    async def go(rig):
        await set_price(rig.db, world.a, "sync_run", 10)
        await fund(rig.db, world.a, 500, "pi_base")

        async def refund(i):
            async with rig.db.tenant(world.a) as conn:
                return await wallet.refund_topup(conn, world.a, amount_minor=100, currency=CURRENCY, provider_ref="pi_base:refund:%d" % (100 * (i + 1)))

        async def topup(i):
            return await fund(rig.db, world.a, 50, "pi_more_%d" % i)

        jobs = [debit(rig.db, world.a, 3, "d-%d" % i) for i in range(25)] + [refund(i) for i in range(5)] + [topup(i) for i in range(10)]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.reconcile(conn, world.a)
        return results, await counts(rig.db, world.a), rec
    results, c, rec = with_rig(go, size=12)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert c["spendable"] >= 0 and rec.ok
    applied = {k: sum(1 for r in results if r.status == "applied") for k in ("n",)}
    assert applied["n"] + sum(1 for r in results if r.status == "insufficient_funds") == len(results)


# ---- reconciliation: it proves, and it fails loudly ------------------------------------------------------------------------------------------

def test_the_books_reconcile_after_ordinary_traffic_and_the_wallet_equals_the_sum_of_its_entries(world):
    async def go(rig):
        await seed(rig, world)
        for i in range(5):
            await debit(rig.db, world.a, i + 1, "r-%d" % i)
        async with rig.db.tenant(world.a) as conn:
            rec = await reports.assert_reconciled(conn, world.a)
            derived = await conn.fetchval("select -sum(e.amount_minor) from billing_entry e join billing_account a on a.id = e.account_id where a.kind = 'wallet'")
            return rec, derived, await ledger.spendable(conn, world.a, CURRENCY)
    rec, derived, spend = with_rig(go)
    assert rec.ok and derived == spend == 1000 - 25 * 15 and rec.trial_balance == {"AUD": 0}


def test_a_stored_balance_that_disagrees_with_the_entries_fails_the_reconciliation_loudly(world):
    async def go(rig):
        await seed(rig, world)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update billing_account set balance_minor = balance_minor + 7 where kind = 'wallet' and tenant_id = %s", (world.a,))
        async with rig.db.tenant(world.a) as conn:
            with pytest.raises(ReconciliationFailed) as exc:
                await reports.assert_reconciled(conn, world.a)
            return exc.value.report
    rep = with_rig(go)
    assert not rep.ok and [d.problem for d in rep.discrepancies] == ["account balance"]
    assert (rep.discrepancies[0].stored_minor, rep.discrepancies[0].derived_minor) == (-993, -1000)
    assert "BOOKS DO NOT AGREE" in reports.reconciliation_markdown(rep)


def test_an_unbalanced_transaction_slipped_past_the_triggers_is_found_by_the_reconciliation(world):
    async def go(rig):
        await seed(rig, world)
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("set session_replication_role = replica")           # what a bulk load or a bad restore could do
            cur.execute("select id from billing_account where kind = 'refunds' and tenant_id = %s", (world.a,))
            refunds = cur.fetchone()[0]
            cur.execute("insert into billing_transaction (tenant_id, kind, currency, idempotency_key, request_hash, actor) values (%s, 'usage_refund', 'AUD', 'smuggled', 'h', 'x') returning id",
                        (world.a,))
            tx = cur.fetchone()[0]
            cur.execute("insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values (%s, %s, %s, 'AUD', 9)", (world.a, tx, refunds))
        async with rig.db.tenant(world.a) as conn:
            return await reports.reconcile(conn, world.a)
    rep = with_rig(go)
    problems = {d.problem for d in rep.discrepancies}
    assert {"unbalanced transaction", "account balance", "trial balance"} <= problems and rep.trial_balance == {"AUD": 9}


def test_a_wallet_balance_above_zero_is_reported_as_a_wallet_below_zero(world):
    async def go(rig):
        await seed(rig, world)
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("alter table billing_account disable trigger all")
            cur.execute("alter table billing_account drop constraint wallet_never_overdrawn")
            cur.execute("update billing_account set balance_minor = 50 where kind = 'wallet' and tenant_id = %s", (world.a,))
        async with rig.db.tenant(world.a) as conn:
            return await reports.reconcile(conn, world.a)
    assert "wallet below zero" in {d.problem for d in with_rig(go).discrepancies}


def test_a_clean_tenant_with_no_entries_reconciles(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            return await reports.assert_reconciled(conn, world.a)
    rep = with_rig(go)
    assert rep.ok and (rep.accounts, rep.transactions, rep.entries) == (0, 0, 0)
