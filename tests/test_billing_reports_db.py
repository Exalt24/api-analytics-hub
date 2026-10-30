"""Plan changes, and the reports: a statement whose opening plus lines is its closing and equals the wallet, a daily report that adds up, CSV cells that a spreadsheet
cannot run as formulas, markdown that cannot be broken by a pipe, and a reconciliation report that says so when the books are wrong."""
from __future__ import annotations

import csv
import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import CURRENCY, DSN, needs_db, new_tenants, psycopg, reset_billing_schema, set_price, with_rig  # noqa: E402

pytestmark = needs_db

from app.billing import ledger, reports, subscriptions, wallet  # noqa: E402
from app.billing.errors import IdempotencyConflict, NoSubscription, UnknownPlan  # noqa: E402

DAY = date(2026, 10, 20)


def at(day_offset: int, hour: int = 12) -> datetime:
    return datetime.combine(DAY + timedelta(days=day_offset), datetime.min.time(), timezone.utc) + timedelta(hours=hour)


class World:
    pass


@pytest.fixture()
def world():
    reset_billing_schema()
    w = World()
    w.a, w.b = new_tenants("tenant-a", "tenant-b")
    return w


# ---- plan changes -------------------------------------------------------------------------------------------------------------------------

async def subscribe(db, tenant, plan="starter", start=None):
    start = start or at(0, 0)
    async with db.tenant(tenant) as conn:
        await subscriptions.start_subscription(conn, tenant, plan_code=plan, provider_customer_id="cus_" + tenant[:6], provider_subscription_id="sub_" + tenant[:6],
                                               period_start=start, period_end=start + timedelta(days=30))


def test_a_plan_change_stores_its_proration_and_moves_the_plan(world):
    async def go(rig):
        await subscribe(rig.db, world.a)
        async with rig.db.tenant(world.a) as conn:
            pro, replayed = await subscriptions.change_plan(conn, world.a, new_plan_code="growth", at=at(15, 0), key="chg-1", actor="admin")
            row = await subscriptions.get_subscription(conn, world.a)
            stored = await conn.fetchrow("select * from billing_plan_change")
        return pro, replayed, row, stored
    pro, replayed, row, stored = with_rig(go)
    assert (pro.credit_minor, pro.charge_minor, pro.net_minor, replayed) == (1450, 4950, 3500, False)       # half of 29.00, half of 99.00
    assert row["plan_code"] == "growth" and (stored["credit_minor"], stored["charge_minor"], stored["net_minor"]) == (1450, 4950, 3500)


def test_the_same_plan_change_key_returns_the_first_proration_and_a_different_request_under_it_is_refused(world):
    async def go(rig):
        await subscribe(rig.db, world.a)
        async with rig.db.tenant(world.a) as conn:
            first, _ = await subscriptions.change_plan(conn, world.a, new_plan_code="growth", at=at(15, 0), key="chg-1", actor="admin")
            again, replayed = await subscriptions.change_plan(conn, world.a, new_plan_code="growth", at=at(15, 0), key="chg-1", actor="admin")
            with pytest.raises(IdempotencyConflict):
                await subscriptions.change_plan(conn, world.a, new_plan_code="starter", at=at(15, 0), key="chg-1", actor="admin")
            with pytest.raises(IdempotencyConflict):
                await subscriptions.change_plan(conn, world.a, new_plan_code="growth", at=at(16, 0), key="chg-1", actor="admin")
            return first, again, replayed, await conn.fetchval("select count(*) from billing_plan_change")
    first, again, replayed, n = with_rig(go)
    assert (again.net_minor, replayed, n) == (first.net_minor, True, 1)


def test_a_plan_change_to_the_plan_already_held_or_an_unknown_plan_or_with_no_subscription_is_refused(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            with pytest.raises(NoSubscription):
                await subscriptions.change_plan(conn, world.a, new_plan_code="growth", at=at(5), key="k0", actor="admin")
        await subscribe(rig.db, world.a)
        async with rig.db.tenant(world.a) as conn:
            for code in ("starter", "enterprise"):
                with pytest.raises(UnknownPlan):
                    await subscriptions.change_plan(conn, world.a, new_plan_code=code, at=at(5), key="k-" + code, actor="admin")
    with_rig(go)


# ---- the statement ------------------------------------------------------------------------------------------------------------------------

async def story(rig, w):
    await set_price(rig.db, w.a, "sync_run", 25)
    async with rig.db.tenant(w.a) as conn:
        await wallet.top_up(conn, w.a, amount_minor=5000, currency=CURRENCY, provider_ref="pi_1", at=at(0, 9))
        await wallet.debit_usage(conn, w.a, event_type="sync_run", quantity=10, currency=CURRENCY, key="u1", at=at(1, 10))
        await wallet.debit_usage(conn, w.a, event_type="sync_run", quantity=20, currency=CURRENCY, key="u2", at=at(2, 10))
        await wallet.adjust(conn, w.a, amount_minor=300, currency=CURRENCY, key="a1", actor="ops", reason="goodwill", at=at(2, 15))
        await wallet.refund_topup(conn, w.a, amount_minor=1000, currency=CURRENCY, provider_ref="pi_1:refund:1000", at=at(3, 8))
        await wallet.refund_usage(conn, w.a, amount_minor=50, currency=CURRENCY, key="svc", actor="ops", reason="double run", at=at(3, 9))


def test_a_statement_opens_where_the_range_starts_and_opening_plus_lines_is_the_closing_balance(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.a) as conn:
            whole = await reports.statement(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=3))
            later = await reports.statement(conn, world.a, CURRENCY, DAY + timedelta(days=2), DAY + timedelta(days=3))
            nothing = await reports.statement(conn, world.a, CURRENCY, DAY + timedelta(days=10), DAY + timedelta(days=12))
            spend = await ledger.spendable(conn, world.a, CURRENCY)
        return whole, later, nothing, spend
    whole, later, nothing, spend = with_rig(go)
    assert whole.opening_minor == 0 and len(whole.lines) == 6 and whole.closing_minor == whole.opening_minor + whole.total_minor == spend == 5000 - 250 - 500 + 300 - 1000 + 50
    assert later.opening_minor == 5000 - 250 and later.closing_minor == spend and len(later.lines) == 4
    assert (nothing.opening_minor, nothing.closing_minor, nothing.lines) == (spend, spend, ())
    assert whole.agrees_with(spend) and not whole.agrees_with(spend + 1)
    balances = [l.balance_minor for l in whole.lines]
    assert balances[-1] == whole.closing_minor and balances == sorted(balances, key=lambda b: balances.index(b))


def test_the_statement_orders_by_when_it_happened_not_by_when_it_was_written(world):
    async def go(rig):
        await set_price(rig.db, world.a, "sync_run", 25)
        async with rig.db.tenant(world.a) as conn:
            await wallet.top_up(conn, world.a, amount_minor=1000, currency=CURRENCY, provider_ref="pi_1", at=at(2, 9))
            await wallet.top_up(conn, world.a, amount_minor=500, currency=CURRENCY, provider_ref="pi_2", at=at(0, 9))      # written second, happened first
            return await reports.statement(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=3))
    st = with_rig(go)
    assert [l.description for l in st.lines] == ["pi_2", "pi_1"] and [l.balance_minor for l in st.lines] == [500, 1500]


def test_a_range_ending_before_it_starts_is_refused(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            with pytest.raises(ValueError):
                await reports.statement(conn, world.a, CURRENCY, DAY, DAY - timedelta(days=1))
    with_rig(go)


def test_one_tenants_statement_never_holds_another_tenants_lines(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.b) as conn:
            return await reports.statement(conn, world.b, CURRENCY, DAY, DAY + timedelta(days=10))
    st = with_rig(go)
    assert st.lines == () and st.closing_minor == 0


def test_the_statement_markdown_and_csv_show_opening_every_line_and_closing(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.a) as conn:
            return await reports.statement(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=3))
    st = with_rig(go)
    md, text = reports.statement_markdown(st, tenant_name="Orchard"), reports.statement_csv(st)
    assert "# Statement for Orchard, 2026-10-20 to 2026-10-23 (AUD)" in md and "| | opening | | | 0.00 | |" in md and "| | closing | | | 36.00 | |" in md
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:5] == ["when_utc", "kind", "description", "amount", "balance"] and rows[1][1] == "opening" and rows[-1][1] == "closing" and len(rows) == 3 + 6
    assert [r[3] for r in rows[2:5]] == ["50.00", "-2.50", "-5.00"]          # a negative amount stays a number


# ---- CSV and markdown cells --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["=1+1", "+1", "-1+1", "@SUM(A1)", "\t=1", "\r=1", "=HYPERLINK(\"http://x\",\"y\")"])
def test_a_text_cell_that_a_spreadsheet_would_run_as_a_formula_gets_an_apostrophe(text):
    assert reports.cell(text) == "'" + text


@pytest.mark.parametrize("text", ["plain", "a=1", "usage", "", "1 + 1", "12 x sync_run"])
def test_other_text_is_left_alone(text):
    assert reports.cell(text) == text
    assert reports.cell(None) == ""


def test_a_reason_a_provider_ref_or_an_actor_that_starts_with_a_formula_is_neutralised_in_the_statement_csv_and_the_daily_csv(world):
    async def go(rig):
        await set_price(rig.db, world.a, "=cmd|' /C calc'!A0", 25)
        async with rig.db.tenant(world.a) as conn:
            await wallet.top_up(conn, world.a, amount_minor=1000, currency=CURRENCY, provider_ref="=evil_ref", at=at(0))
            await wallet.adjust(conn, world.a, amount_minor=100, currency=CURRENCY, key="a1", actor="@actor", reason="-2+3 bad reason | with a pipe", at=at(0))
            await wallet.debit_usage(conn, world.a, event_type="=cmd|' /C calc'!A0", quantity=2, currency=CURRENCY, key="u1", at=at(0))
            return await reports.statement(conn, world.a, CURRENCY, DAY, DAY), await reports.daily_report(conn, world.a, CURRENCY, DAY, DAY)
    st, daily = with_rig(go)
    for text in (reports.statement_csv(st), reports.daily_csv(daily)):
        for row in csv.reader(io.StringIO(text)):
            for value in row:
                assert not value or (value[0] not in ("=", "@") and not (value[0] in "+-" and not value[1:2].isdigit())), value
    s = reports.statement_csv(st)
    assert "'=evil_ref" in s and "'@actor" in s and "'-2+3 bad reason | with a pipe" in s and "'=cmd|" in reports.daily_csv(daily)
    assert "\\|" in reports.statement_markdown(st) and "-2+3 bad reason \\| with a pipe" in reports.statement_markdown(st)


# ---- the daily report --------------------------------------------------------------------------------------------------------------------------

def test_the_daily_report_adds_up_to_the_ledger_for_every_day_and_every_column(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.a) as conn:
            rep = await reports.daily_report(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=4))
            spend = await ledger.spendable(conn, world.a, CURRENCY)
            revenue = await conn.fetchval("select -balance_minor from billing_account where kind = 'platform_revenue'")
        return rep, spend, revenue
    rep, spend, revenue = with_rig(go)
    assert len(rep.days) == 5 and [d.day for d in rep.days] == [DAY + timedelta(days=i) for i in range(5)]
    d0, d1, d2, d3, d4 = rep.days
    assert (d0.topups_minor, d1.usage_minor, d2.usage_minor, d2.adjustments_minor, d3.refunds_minor, d3.service_credits_minor) == (5000, 250, 500, 300, 1000, 50)
    assert d4 == reports.DayRow(d4.day) and rep.total("revenue_minor") == revenue == 750
    assert rep.total("topups_minor") - rep.total("usage_minor") + rep.total("adjustments_minor") - rep.total("refunds_minor") + rep.total("service_credits_minor") == spend
    assert [(u.event_type, u.quantity, u.amount_minor) for u in rep.usage] == [("sync_run", 30, 750)]


def test_the_daily_report_buckets_by_utc_day_at_the_edges(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            await wallet.top_up(conn, world.a, amount_minor=100, currency=CURRENCY, provider_ref="pi_edge_a", at=datetime(2026, 10, 20, 23, 59, 59, tzinfo=timezone.utc))
            await wallet.top_up(conn, world.a, amount_minor=200, currency=CURRENCY, provider_ref="pi_edge_b", at=datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc))
            return await reports.daily_report(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=1))
    rep = with_rig(go)
    assert [d.topups_minor for d in rep.days] == [100, 200]


def test_failed_payments_are_counted_in_the_daily_report(world):
    async def go(rig):
        from app.billing import payments
        await payments.start_topup(rig.db, rig.provider, world.a, amount_minor=500, currency=CURRENCY, payment_method="pm_fake_declined", key="bad-1")
        today = datetime.now(timezone.utc).date()
        async with rig.db.tenant(world.a) as conn:
            return await reports.daily_report(conn, world.a, CURRENCY, today, today)
    assert with_rig(go).days[0].failed_payments == 1


def test_the_daily_markdown_and_csv_carry_every_day_and_the_usage_table(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.a) as conn:
            return await reports.daily_report(conn, world.a, CURRENCY, DAY, DAY + timedelta(days=4))
    rep = with_rig(go)
    md, text = reports.daily_markdown(rep, tenant_name="Orchard"), reports.daily_csv(rep)
    assert md.count("| 2026-10-2") == 5 and "| sync_run | 30 | 7.50 |" in md and "# Daily revenue and usage for Orchard" in md
    assert "day,topups,subscription_credits,usage,refunds,service_credits,adjustments,revenue,failed_payments,currency" in text and "sync_run,30,7.50,AUD" in text


def test_an_empty_range_says_there_was_no_usage(world):
    async def go(rig):
        async with rig.db.tenant(world.a) as conn:
            return await reports.daily_report(conn, world.a, CURRENCY, DAY, DAY)
    assert "No usage in this range." in reports.daily_markdown(with_rig(go))


# ---- the reconciliation report -----------------------------------------------------------------------------------------------------------

def test_the_reconciliation_markdown_says_the_books_agree_or_names_every_disagreement(world):
    async def go(rig):
        await story(rig, world)
        async with rig.db.tenant(world.a) as conn:
            good = await reports.reconcile(conn, world.a)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update billing_account set balance_minor = balance_minor - 3 where kind = 'platform_revenue' and tenant_id = %s", (world.a,))
        async with rig.db.tenant(world.a) as conn:
            bad = await reports.reconcile(conn, world.a)
        return good, bad
    good, bad = with_rig(go)
    assert good.ok and "BOOKS AGREE" in reports.reconciliation_markdown(good)
    md = reports.reconciliation_markdown(bad)
    assert not bad.ok and "BOOKS DO NOT AGREE (1 problem(s))" in md and "| account balance | platform_revenue AUD | -753 | -750 |" in md
    assert reports.reconciliation_csv(bad).splitlines()[1] == "account balance,platform_revenue AUD,-753,-750"
