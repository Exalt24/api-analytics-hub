#!/usr/bin/env python
# LIFESPAN: permanent
"""The prepaid wallet and billing ledger from the command line, against the FAKE payment provider. Nothing real is contacted and no card exists.

    DATABASE_URL=postgresql://... python scripts/billing.py --tenant <tenant uuid> <command>

    price --event-type E --unit-minor N [--currency AUD]       set this tenant's price for a billable event
    topup --amount-minor N [--currency AUD] [--payment-method pm_fake_ok|pm_fake_declined] [--key K]
                                                               ask the in-process fake provider for money, then deliver its signed event to the receiver
    debit --event-type E --quantity N --key K [--currency AUD]  meter usage at the tenant's price; not enough credit is refused and records nothing
    refund --payment ID --amount-minor N [--key K]             refund part of a payment at the fake provider, then deliver its charge.refunded event
    refund --service-credit --amount-minor N --reason TEXT --key K   give usage credit back (no provider involved)
    adjust --amount-minor N --reason TEXT --actor WHO --key K   an admin correction (negative removes credit, never below zero)
    statement --currency AUD [--from D --to D] [--out-dir DIR]  the tenant's statement: opening, lines, closing (markdown, and CSV with --out-dir)
    report --currency AUD [--from D --to D] [--out-dir DIR]     the daily revenue and usage report
    reconcile                                                  prove the books agree; exit 1 and name every disagreement when they do not
    simulate-webhook --type T [--payment ID] [--amount-minor N] [--repeat N] [--tamper] [--age-seconds S] [--event-id ID] [--status S]
                                                               build a signed event, deliver it to the receiver and print the HTTP answer for each delivery
    demo --tenant-a ID --tenant-b ID [--out-dir DIR]           play the invented week for two seeded tenants (seed_billing_demo.py), then reconcile both

THE PROVIDER IS FAKE. Each run starts its own fake (app/billing/fake_provider.py) written from Stripe's saved pages, with the invented tokens pm_fake_ok and
pm_fake_declined. A payment's record is in the database; the fake's copy of it is not, so `refund` re-teaches the fake that payment before asking. The
signing secret is the fake's own. Nothing here has been run against a live Stripe account.

Exit 0: done. Exit 1: the books do not agree (reconcile and demo). Exit 2: refused or failed, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.billing import demo, payments, reports, subscriptions, wallet, webhooks  # noqa: E402
from app.billing.errors import BillingError, ReconciliationFailed  # noqa: E402
from app.billing.fake_provider import FAKE_API_KEY, FAKE_WEBHOOK_SECRET, Events, FakeStripe  # noqa: E402
from app.billing.provider import StripeStyleClient  # noqa: E402
from app.db import DSN_ENV, Database, UnsafeDatabaseRole  # noqa: E402
from app.price_watch.money import format_minor  # noqa: E402

SECRETS = (FAKE_WEBHOOK_SECRET,)
EVENT_TYPES = ("payment_intent.succeeded", "payment_intent.payment_failed", "charge.refunded", "invoice.paid", "invoice.payment_failed",
               "customer.subscription.updated", "customer.subscription.deleted")


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=os.environ.get("BILLING_TENANT"), help="tenant uuid (or BILLING_TENANT)")
    sub = p.add_subparsers(dest="command", required=True)
    cur = lambda x: x.add_argument("--currency", default="AUD")  # noqa: E731
    s = sub.add_parser("price")
    s.add_argument("--event-type", required=True)
    s.add_argument("--unit-minor", type=int, required=True)
    cur(s)
    s = sub.add_parser("topup")
    s.add_argument("--amount-minor", type=int, required=True)
    s.add_argument("--payment-method", default="pm_fake_ok")
    s.add_argument("--key", default=None, help="the idempotency key (default: a new one for each run)")
    cur(s)
    s = sub.add_parser("debit")
    s.add_argument("--event-type", required=True)
    s.add_argument("--quantity", type=int, required=True)
    s.add_argument("--key", required=True)
    cur(s)
    s = sub.add_parser("refund")
    s.add_argument("--payment", default=None, help="a payment id, or its first 8 characters")
    s.add_argument("--amount-minor", type=int, required=True)
    s.add_argument("--key", default=None)
    s.add_argument("--service-credit", action="store_true")
    s.add_argument("--reason", default=None)
    cur(s)
    s = sub.add_parser("adjust")
    s.add_argument("--amount-minor", type=int, required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--actor", required=True)
    s.add_argument("--key", required=True)
    cur(s)
    for name in ("statement", "report"):
        s = sub.add_parser(name)
        s.add_argument("--from", dest="start", type=_date, default=None)
        s.add_argument("--to", dest="end", type=_date, default=None)
        s.add_argument("--out-dir", default=None)
        cur(s)
    sub.add_parser("reconcile")
    s = sub.add_parser("simulate-webhook")
    s.add_argument("--type", required=True, choices=EVENT_TYPES)
    s.add_argument("--payment", default=None, help="a payment id or its first 8 characters (payment_intent and charge events)")
    s.add_argument("--amount-minor", type=int, default=None, help="invoice.paid: the amount paid; charge.refunded: the TOTAL refunded so far")
    s.add_argument("--status", default="active", help="customer.subscription.updated: the provider's status word")
    s.add_argument("--event-id", default=None)
    s.add_argument("--repeat", type=int, default=1, help="deliver the same signed body this many times (a provider retry)")
    s.add_argument("--tamper", action="store_true", help="change one byte after signing")
    s.add_argument("--age-seconds", type=int, default=0, help="sign with a timestamp this many seconds in the past")
    cur(s)
    s = sub.add_parser("demo")
    s.add_argument("--tenant-a", required=True)
    s.add_argument("--tenant-b", required=True)
    s.add_argument("--out-dir", default=None)
    return p


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _fresh_start() -> int:
    """Each run numbers its fake ids from somewhere new, so the ids of one run never meet the ids another run already stored."""
    return random.randrange(10_000, 900_000)


async def _fresh_provider() -> tuple[FakeStripe, StripeStyleClient, object]:
    fake = FakeStripe(first_id=_fresh_start())
    http = fake.client()
    return fake, StripeStyleClient(http, FAKE_API_KEY, sleep=demo._no_sleep), http


async def _resolve_payment(db: Database, tenant: str, prefix: str) -> asyncpg.Record:
    async with db.tenant(tenant) as conn:
        rows = await conn.fetch("select * from billing_payment where id::text like $1 || '%' or provider_payment_id = $1", prefix.lower())
    if len(rows) != 1:
        raise LookupError("%d payments match %r" % (len(rows), prefix))
    return rows[0]


def _write(out_dir: str | None, name: str, text: str) -> None:
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text, encoding="utf-8", newline="")
        print("wrote", out / name)


async def _deliver_pi(db: Database, router: Database, events: Events, fake: FakeStripe, tenant: str, payment: asyncpg.Record, event_type: str) -> webhooks.Received:
    pi = fake.payment_intents[payment["provider_payment_id"]]
    event = events.payment_intent(event_type, pi)
    body, header = events.signed(event, timestamp=int(event["created"]))
    return await webhooks.receive(db, router, body, header, SECRETS, now=event["created"])


async def run(args: argparse.Namespace) -> int:
    if args.command != "demo" and not args.tenant:
        print("no tenant: pass --tenant or set BILLING_TENANT", file=sys.stderr)
        return 2
    try:
        db, router = Database(max_size=3), Database(max_size=2, role="billing_router")
    except RuntimeError as exc:
        print("cannot start: %s (set %s)" % (exc, DSN_ENV), file=sys.stderr)
        return 2
    try:
        await db.connect()
        await router.connect()
    except UnsafeDatabaseRole as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, asyncpg.PostgresError) as exc:
        print("cannot reach the database: %s" % exc, file=sys.stderr)
        return 2
    try:
        return await dispatch(db, router, args)
    except ReconciliationFailed as exc:
        print(reports.reconciliation_markdown(exc.report))
        print("THE BOOKS DO NOT AGREE", file=sys.stderr)
        return 1
    except (BillingError, LookupError, ValueError, asyncpg.PostgresError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    finally:
        await router.close()
        await db.close()


def _say(out: wallet.Outcome) -> None:
    print("%s: %s %s, the wallet can spend %s" % (out.status, format_minor(out.amount_minor, out.currency), out.currency, format_minor(out.balance_minor, out.currency)))
    if out.shortfall_minor:
        print("   %s short; nothing was recorded" % format_minor(out.shortfall_minor, out.currency))


async def dispatch(db: Database, router: Database, args: argparse.Namespace) -> int:
    tenant, cmd = args.tenant, args.command
    if cmd == "price":
        async with db.tenant(tenant) as conn:
            await conn.execute("insert into billing_usage_price (tenant_id, event_type, currency, unit_price_minor) values ($1::uuid, $2, $3, $4) "
                               "on conflict (tenant_id, event_type, currency) do update set unit_price_minor = excluded.unit_price_minor",
                               tenant, args.event_type, args.currency, args.unit_minor)
        print("price for %s set to %s %s" % (args.event_type, format_minor(args.unit_minor, args.currency), args.currency))
        return 0
    if cmd == "topup":
        fake, provider, http = await _fresh_provider()
        events, key = Events(SECRETS, first_id=_fresh_start()), args.key or "cli-topup-%s" % uuid.uuid4()
        try:
            res = await payments.start_topup(db, provider, tenant, amount_minor=args.amount_minor, currency=args.currency, payment_method=args.payment_method, key=key)
            print("payment %s: %s%s" % (res.payment_id[:8], res.status, " (%s)" % res.failure_code if res.failure_code else ""))
            if res.provider_payment_id and res.status in ("succeeded", "failed"):
                pay = await _resolve_payment(db, tenant, res.payment_id)
                got = await _deliver_pi(db, router, events, fake, tenant, pay, "payment_intent.succeeded" if res.status == "succeeded" else "payment_intent.payment_failed")
                print("signed event delivered: %s, %s" % (got.status, got.detail))
        finally:
            await http.aclose()
        async with db.tenant(tenant) as conn:
            print("the wallet can spend %s %s" % (format_minor(await ledger_spendable(conn, tenant, args.currency), args.currency), args.currency))
        return 0
    if cmd == "debit":
        async with db.tenant(tenant) as conn:
            _say(await wallet.debit_usage(conn, tenant, event_type=args.event_type, quantity=args.quantity, currency=args.currency, key=args.key))
        return 0
    if cmd == "refund" and args.service_credit:
        if not args.key or not args.reason:
            raise ValueError("a service credit needs --key and --reason")
        async with db.tenant(tenant) as conn:
            _say(await wallet.refund_usage(conn, tenant, amount_minor=args.amount_minor, currency=args.currency, key=args.key, actor="cli", reason=args.reason))
        return 0
    if cmd == "refund":
        if not args.payment:
            raise ValueError("pass --payment (or --service-credit)")
        pay = await _resolve_payment(db, tenant, args.payment)
        fake, provider, http = await _fresh_provider()
        try:
            fake.adopt_succeeded(pay["provider_payment_id"], pay["amount_minor"], pay["currency"].strip(), {"tenant_id": tenant, "payment_id": str(pay["id"])})
            key = args.key or "cli-refund-%s" % uuid.uuid4()
            refund = await payments.request_refund(db, provider, tenant, str(pay["id"]), amount_minor=args.amount_minor, key=key)
            print("provider refund %s of %s" % (refund.id, format_minor(refund.amount, pay["currency"].strip())))
            events = Events(SECRETS, first_id=_fresh_start())
            total = pay["refunded_minor"] + refund.amount
            event = events.charge_refunded(pay["provider_payment_id"], pay["amount_minor"], total, pay["currency"].strip())
            body, header = events.signed(event, timestamp=int(event["created"]))
            got = await webhooks.receive(db, router, body, header, SECRETS, now=event["created"])
            print("charge.refunded delivered: %s, %s" % (got.status, got.detail))
        finally:
            await http.aclose()
        return 0
    if cmd == "adjust":
        async with db.tenant(tenant) as conn:
            _say(await wallet.adjust(conn, tenant, amount_minor=args.amount_minor, currency=args.currency, key=args.key, actor=args.actor, reason=args.reason))
        return 0
    if cmd in ("statement", "report"):
        end = args.end or _utc_today()
        start = args.start or end - timedelta(days=6)
        async with db.tenant(tenant) as conn:
            if cmd == "statement":
                st = await reports.statement(conn, tenant, args.currency, start, end)
                print(reports.statement_markdown(st))
                _write(args.out_dir, "statement-%s-%s.md" % (start, end), reports.statement_markdown(st))
                _write(args.out_dir, "statement-%s-%s.csv" % (start, end), reports.statement_csv(st))
                if end >= _utc_today() and not st.agrees_with(await ledger_spendable(conn, tenant, args.currency)):
                    print("the closing balance does not match the wallet", file=sys.stderr)
                    return 1
            else:
                rep = await reports.daily_report(conn, tenant, args.currency, start, end)
                print(reports.daily_markdown(rep))
                _write(args.out_dir, "daily-%s-%s.md" % (start, end), reports.daily_markdown(rep))
                _write(args.out_dir, "daily-%s-%s.csv" % (start, end), reports.daily_csv(rep))
        return 0
    if cmd == "reconcile":
        async with db.tenant(tenant) as conn:
            report = await reports.assert_reconciled(conn, tenant)
        print(reports.reconciliation_markdown(report))
        return 0
    if cmd == "simulate-webhook":
        return await simulate(db, router, args)
    if cmd == "demo":
        return await play_demo(db, router, args)
    return 2


async def ledger_spendable(conn: asyncpg.Connection, tenant: str, currency: str) -> int:
    from app.billing import ledger
    return await ledger.spendable(conn, tenant, currency)


async def simulate(db: Database, router: Database, args: argparse.Namespace) -> int:
    tenant, et = args.tenant, args.type
    events = Events(SECRETS, first_id=_fresh_start())
    kw = {"event_id": args.event_id} if args.event_id else {}
    if et.startswith(("payment_intent.", "charge.")):
        if not args.payment:
            raise ValueError("%s needs --payment" % et)
        pay = await _resolve_payment(db, tenant, args.payment)
        if not pay["provider_payment_id"]:
            raise ValueError("that payment has no provider id yet")
        cur = pay["currency"].strip()
        if et == "charge.refunded":
            event = events.charge_refunded(pay["provider_payment_id"], pay["amount_minor"], args.amount_minor if args.amount_minor is not None else pay["amount_minor"], cur, **kw)
        else:
            pi = {"id": pay["provider_payment_id"], "object": "payment_intent", "amount": pay["amount_minor"], "currency": cur.lower(), "metadata": {"tenant_id": tenant, "payment_id": str(pay["id"])},
                  "status": "succeeded" if et.endswith("succeeded") else "requires_payment_method",
                  "last_payment_error": None if et.endswith("succeeded") else {"type": "card_error", "code": "card_declined", "decline_code": "insufficient_funds", "message": "Your card has insufficient funds."}}
            event = events.payment_intent(et, pi, **kw)
    else:
        async with db.tenant(tenant) as conn:
            sub = await subscriptions.get_subscription(conn, tenant)
        if et.startswith("invoice."):
            event = events.invoice(et, sub["provider_customer_id"], args.amount_minor or 0, args.currency, "in_sim_%s" % uuid.uuid4().hex[:8], **kw)
        else:
            event = events.subscription(et, sub["provider_subscription_id"], sub["provider_customer_id"], args.status, **kw)
    body, header = events.signed(event, timestamp=int(datetime.now(timezone.utc).timestamp()) - args.age_seconds)
    if args.tamper:
        body = body.replace(b'"livemode":false', b'"livemode":true ', 1)
    print("event %s %s, delivering %d time(s)%s%s" % (event["id"], et, args.repeat, ", body changed after signing" if args.tamper else "",
                                                       ", signed %d s ago" % args.age_seconds if args.age_seconds else ""))
    for i in range(args.repeat):
        code, answer = await webhooks.handle_http(db, router, body, header, SECRETS)
        print("  delivery %d: HTTP %d %s" % (i + 1, code, json.dumps(answer)))
    return 0


async def play_demo(db: Database, router: Database, args: argparse.Namespace) -> int:
    story = await demo.run_story(db, router, args.tenant_a, args.tenant_b)
    print("THE INVENTED WEEK (fake provider, invented tenants, nothing real)\n")
    for line in story.log:
        print(" ", line)
    bad = 0
    for label, tenant in (("A", args.tenant_a), ("B", args.tenant_b)):
        end = story.today
        start = end - timedelta(days=6)
        async with db.tenant(tenant) as conn:
            st = await reports.statement(conn, tenant, demo.CURRENCY, start, end)
            rep = await reports.daily_report(conn, tenant, demo.CURRENCY, start, end)
            rec = await reports.reconcile(conn, tenant)
        print("\n" + reports.statement_markdown(st, tenant_name=demo.TENANT_NAMES[label]))
        print(reports.daily_markdown(rep, tenant_name=demo.TENANT_NAMES[label]))
        print(reports.reconciliation_markdown(rec))
        stem = "billing-demo-%s" % label.lower()
        _write(args.out_dir, stem + "-statement.md", reports.statement_markdown(st, tenant_name=demo.TENANT_NAMES[label]))
        _write(args.out_dir, stem + "-statement.csv", reports.statement_csv(st))
        _write(args.out_dir, stem + "-daily.md", reports.daily_markdown(rep, tenant_name=demo.TENANT_NAMES[label]))
        _write(args.out_dir, stem + "-daily.csv", reports.daily_csv(rep))
        _write(args.out_dir, stem + "-reconciliation.md", reports.reconciliation_markdown(rec))
        bad += (not rec.ok) + (not st.agrees_with(story.spendable[label]))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
