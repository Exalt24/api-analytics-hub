"""The invented demo and scripts/billing.py, run for real against Postgres: the week's story ends with the facts the README claims (one duplicate webhook written once,
one failed payment credited nothing, one refund reversed exactly, a charge the wallet could not cover recorded as nothing) and the books reconcile; the command
line refuses what it should and exits 1 when the books do not agree."""
from __future__ import annotations

import csv
import io
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from billing_fixtures import CURRENCY, DSN, needs_db, new_tenants, psycopg, reset_billing_schema, with_rig  # noqa: E402

pytestmark = needs_db

import billing as cli  # noqa: E402  (scripts/billing.py)
from app.billing import demo, reports  # noqa: E402
from app.billing.errors import BillingError  # noqa: E402


class World:
    pass


@pytest.fixture()
def world(monkeypatch):
    reset_billing_schema()
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("BILLING_TENANT", raising=False)
    w = World()
    w.a, w.b = new_tenants("demo-a", "demo-b")
    return w


def run_cli(*argv):
    return cli.main(list(argv))


def test_the_story_ends_with_the_facts_it_claims_and_both_tenants_reconcile(world):
    async def go(rig):
        story = await demo.run_story(rig.db, rig.router, world.a, world.b)
        out = {}
        for label, t in (("A", world.a), ("B", world.b)):
            async with rig.db.tenant(t) as conn:
                out[label] = (await reports.reconcile(conn, t), await reports.statement(conn, t, CURRENCY, story.today - timedelta(days=6), story.today),
                              await conn.fetchval("select count(*) from billing_webhook_event"), await conn.fetchval("select count(*) from billing_notification"),
                              await conn.fetchval("select status from billing_subscription"))
        return story, out
    story, out = with_rig(go)
    assert story.duplicate_status == "duplicate"
    assert (story.failed_payment_status, story.failed_payment_code) == ("failed", "insufficient_funds")
    assert (story.refund_status, story.refund_minor) == ("processed", 2000)
    assert story.insufficient_status == "insufficient_funds" and story.insufficient_shortfall_minor > 0
    assert story.proration_net_minor > 0 and "past_due" in story.past_due_status
    for label in ("A", "B"):
        rec, st, events, notes, sub_status = out[label]
        assert rec.ok and st.agrees_with(story.spendable[label]) and story.spendable[label] > 0
    assert out["B"][4] == "past_due" and out["B"][3] == 1 and out["A"][3] == 1          # a failed card notice for Orchard, a past due notice for Birchwood


def test_the_demo_refuses_a_tenant_that_already_has_history(world):
    async def go(rig):
        await demo.run_story(rig.db, rig.router, world.a, world.b)
        with pytest.raises(BillingError, match="already has billing history"):
            await demo.run_story(rig.db, rig.router, world.a, world.b)
    with_rig(go)


def test_the_two_tenants_pay_their_own_prices_for_the_same_event(world):
    async def go(rig):
        await demo.run_story(rig.db, rig.router, world.a, world.b)
        out = {}
        for label, t in (("A", world.a), ("B", world.b)):
            async with rig.db.tenant(t) as conn:
                out[label] = {r["event_type"]: r["unit_price_minor"] for r in await conn.fetch("select distinct event_type, unit_price_minor from billing_usage_event")}
        return out
    out = with_rig(go)
    assert out["A"] == demo.USAGE_PRICES["A"] and out["B"] == demo.USAGE_PRICES["B"] and out["A"] != out["B"]


def test_demo_through_the_command_line_writes_the_files_and_exits_zero(world, tmp_path, capsys):
    code = run_cli("demo", "--tenant-a", world.a, "--tenant-b", world.b, "--out-dir", str(tmp_path))
    out = capsys.readouterr().out
    assert code == 0 and "THE INVENTED WEEK" in out and out.count("BOOKS AGREE") == 2 and "second duplicate" in out
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted("billing-demo-%s-%s" % (x, y) for x in "ab" for y in ("statement.md", "statement.csv", "daily.md", "daily.csv", "reconciliation.md"))
    rows = list(csv.reader(io.StringIO((tmp_path / "billing-demo-a-statement.csv").read_text(encoding="utf-8"))))
    assert rows[1][1] == "opening" and rows[-1][1] == "closing"


def test_demo_on_a_tenant_that_already_ran_it_exits_two_with_the_reason(world, capsys):
    assert run_cli("demo", "--tenant-a", world.a, "--tenant-b", world.b) == 0
    capsys.readouterr()
    assert run_cli("demo", "--tenant-a", world.a, "--tenant-b", world.b) == 2
    assert "already has billing history" in capsys.readouterr().err


def test_debit_twice_with_one_key_charges_once_and_a_changed_request_under_it_is_refused(world, capsys):
    assert run_cli("--tenant", world.a, "price", "--event-type", "sync_run", "--unit-minor", "25") == 0
    assert run_cli("--tenant", world.a, "topup", "--amount-minor", "1000") == 0
    capsys.readouterr()
    assert run_cli("--tenant", world.a, "debit", "--event-type", "sync_run", "--quantity", "4", "--key", "k1") == 0
    first = capsys.readouterr().out
    assert run_cli("--tenant", world.a, "debit", "--event-type", "sync_run", "--quantity", "4", "--key", "k1") == 0
    second = capsys.readouterr().out
    assert first.startswith("applied: 1.00 AUD") and second.startswith("replayed: 1.00 AUD") and "can spend 9.00" in second
    assert run_cli("--tenant", world.a, "debit", "--event-type", "sync_run", "--quantity", "5", "--key", "k1") == 2
    assert "already used for a different request" in capsys.readouterr().err


def test_a_debit_the_wallet_cannot_cover_says_so_and_records_nothing(world, capsys):
    run_cli("--tenant", world.a, "price", "--event-type", "sync_run", "--unit-minor", "100")
    assert run_cli("--tenant", world.a, "debit", "--event-type", "sync_run", "--quantity", "3", "--key", "poor") == 0
    out = capsys.readouterr().out
    assert "insufficient_funds" in out and "3.00 short" in out and "nothing was recorded" in out
    assert run_cli("--tenant", world.a, "reconcile") == 0


def test_topup_declined_then_topup_ok_then_a_refund_through_the_command_line(world, capsys):
    assert run_cli("--tenant", world.a, "topup", "--amount-minor", "3000", "--payment-method", "pm_fake_declined", "--key", "t-bad") == 0
    declined = capsys.readouterr().out
    assert "failed (insufficient_funds)" in declined and "can spend 0.00" in declined
    assert run_cli("--tenant", world.a, "topup", "--amount-minor", "3000", "--key", "t-ok") == 0
    assert "can spend 30.00" in capsys.readouterr().out
    async def pay_id(rig):
        async with rig.db.tenant(world.a) as conn:
            return await conn.fetchval("select id from billing_payment where status = 'succeeded'")
    pid = str(with_rig(pay_id))
    assert run_cli("--tenant", world.a, "refund", "--payment", pid[:8], "--amount-minor", "1000", "--key", "r1") == 0
    out = capsys.readouterr().out
    assert "charge.refunded delivered: processed" in out and "reversed 1000" in out
    assert run_cli("--tenant", world.a, "refund", "--payment", pid[:8], "--amount-minor", "99999") == 2
    assert "still refundable" in capsys.readouterr().err
    assert run_cli("--tenant", world.a, "reconcile") == 0


def test_service_credit_and_adjustment_need_their_reasons(world, capsys):
    assert run_cli("--tenant", world.a, "adjust", "--amount-minor", "500", "--reason", "goodwill credit", "--actor", "ops", "--key", "a1") == 0
    assert "applied: 5.00 AUD" in capsys.readouterr().out
    assert run_cli("--tenant", world.a, "adjust", "--amount-minor", "500", "--reason", "x", "--actor", "ops", "--key", "a2") == 2
    assert "needs a reason" in capsys.readouterr().err
    assert run_cli("--tenant", world.a, "refund", "--service-credit", "--amount-minor", "50") == 2
    assert run_cli("--tenant", world.a, "refund", "--service-credit", "--amount-minor", "50", "--reason", "double run", "--key", "s1") == 0


def test_simulate_webhook_shows_a_replay_a_tampered_body_and_a_stale_signature(world, capsys):
    run_cli("--tenant", world.a, "topup", "--amount-minor", "1000", "--key", "sim-pay")
    capsys.readouterr()
    pid = None

    async def pay_id(rig):
        async with rig.db.tenant(world.a) as conn:
            return str(await conn.fetchval("select id from billing_payment"))
    pid = with_rig(pay_id)
    assert run_cli("--tenant", world.a, "simulate-webhook", "--type", "charge.refunded", "--payment", pid[:8], "--amount-minor", "300", "--repeat", "2") == 0
    out = capsys.readouterr().out
    assert 'delivery 1: HTTP 200 {"received": true, "status": "processed"' in out and 'delivery 2: HTTP 200 {"received": true, "status": "duplicate"' in out
    assert run_cli("--tenant", world.a, "simulate-webhook", "--type", "payment_intent.succeeded", "--payment", pid[:8], "--tamper") == 0
    assert 'HTTP 400 {"error": "invalid webhook"}' in capsys.readouterr().out
    assert run_cli("--tenant", world.a, "simulate-webhook", "--type", "payment_intent.succeeded", "--payment", pid[:8], "--age-seconds", "4000") == 0
    assert 'HTTP 400' in capsys.readouterr().out
    assert run_cli("--tenant", world.a, "reconcile") == 0


def test_simulate_webhook_for_invoices_and_subscriptions_needs_a_subscription(world, capsys):
    assert run_cli("--tenant", world.a, "simulate-webhook", "--type", "invoice.paid", "--amount-minor", "2900") == 2
    assert "no subscription" in capsys.readouterr().err
    assert run_cli("--tenant", world.a, "simulate-webhook", "--type", "charge.refunded") == 2
    assert "needs --payment" in capsys.readouterr().err


def test_reconcile_exits_one_and_names_the_disagreement_when_the_books_are_wrong(world, capsys):
    run_cli("--tenant", world.a, "topup", "--amount-minor", "1000", "--key", "rec-1")
    capsys.readouterr()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("update billing_account set balance_minor = balance_minor + 9 where kind = 'platform_revenue' and tenant_id = %s", (world.a,))
        conn.execute("update billing_account set balance_minor = balance_minor - 9 where kind = 'wallet' and tenant_id = %s", (world.a,))
    assert run_cli("--tenant", world.a, "reconcile") == 1
    cap = capsys.readouterr()
    assert "BOOKS DO NOT AGREE" in cap.out and "account balance" in cap.out and "THE BOOKS DO NOT AGREE" in cap.err


def test_statement_and_report_print_and_write_files_and_a_missing_tenant_is_refused(world, tmp_path, capsys):
    run_cli("--tenant", world.a, "topup", "--amount-minor", "1000", "--key", "st-1")
    capsys.readouterr()
    assert run_cli("--tenant", world.a, "statement", "--currency", "AUD", "--out-dir", str(tmp_path)) == 0
    assert "# Statement" in capsys.readouterr().out and len(list(tmp_path.glob("statement-*.csv"))) == 1
    assert run_cli("--tenant", world.a, "report", "--currency", "AUD", "--out-dir", str(tmp_path)) == 0
    assert "# Daily revenue and usage" in capsys.readouterr().out and len(list(tmp_path.glob("daily-*.csv"))) == 1
    assert run_cli("statement") == 2
    assert "no tenant" in capsys.readouterr().err


def test_the_command_line_refuses_to_start_without_a_database_url_or_as_a_superuser(world, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL")
    assert run_cli("--tenant", world.a, "reconcile") == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    monkeypatch.setenv("DATABASE_ROLE", "analytics")
    monkeypatch.setenv("DATABASE_URL", DSN)
    assert run_cli("--tenant", world.a, "reconcile") == 2
    err = capsys.readouterr().err
    assert "SUPERUSER" in err or "BYPASSRLS" in err
