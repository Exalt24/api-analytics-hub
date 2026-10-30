"""The invented demo: two tenants, two plans, a week of usage, one duplicate webhook, one failed payment, one refund, a plan change and an admin credit.

Every name, id, amount and card token here is made up, and the provider is fake_provider.py, so nothing leaves the machine and no card number exists
anywhere. The story goes through the real routes: payments are asked of the fake provider, its signed events are delivered to the real receiver, the
usage is metered at each tenant's own price, and the days are backdated with the `at` argument so the reports have a week to show.

    seed_billing_demo.py  loads the plans and the two tenants (it drops the `public` schema of the database it points at)
    scripts/billing.py demo --tenant-a ID --tenant-b ID   plays this story through the request role and prints what happened

Run it on a tenant that has already been through it and it refuses: the fake provider's payments live in memory, so a second run could not settle them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import asyncpg

from app.db import Database

from . import payments, subscriptions, wallet, webhooks
from .errors import BillingError
from .fake_provider import FAKE_API_KEY, FAKE_WEBHOOK_SECRET, Events, FakeStripe
from .provider import StripeStyleClient

CURRENCY = "AUD"
SECRETS = (FAKE_WEBHOOK_SECRET,)

PLANS: list[dict[str, Any]] = [
    dict(code="starter", name="Starter (invented)", currency=CURRENCY, monthly_price_minor=2900, included_credits_minor=2500, provider_price_id="price_fake_starter"),
    dict(code="growth", name="Growth (invented)", currency=CURRENCY, monthly_price_minor=9900, included_credits_minor=9000, provider_price_id="price_fake_growth"),
]

#: each tenant sets its own price for what it meters, in minor units: the same event costs a different amount for each
USAGE_PRICES = {
    "A": {"sync_run": 25, "report_run": 150},
    "B": {"sync_run": 40, "report_run": 200},
}

TENANT_NAMES = {"A": "Orchard Analytics (invented)", "B": "Birchwood Outfitters (invented)"}


async def load_plans(conn: asyncpg.Connection) -> None:
    """The plan catalogue is global and the request role can only read it, so it is loaded with the owner's connection."""
    for p in PLANS:
        await conn.execute(
            """insert into billing_plan (code, name, currency, monthly_price_minor, included_credits_minor, provider_price_id)
               values ($1, $2, $3, $4, $5, $6) on conflict (code) do nothing""",
            p["code"], p["name"], p["currency"], p["monthly_price_minor"], p["included_credits_minor"], p["provider_price_id"])


@dataclass
class Story:
    """What the demo did, in numbers a test (or a person reading the output) can check."""
    today: date
    log: list[str] = field(default_factory=list)
    duplicate_status: str = ""
    failed_payment_status: str = ""
    failed_payment_code: str | None = None
    refund_status: str = ""
    refund_minor: int = 0
    insufficient_status: str = ""
    insufficient_shortfall_minor: int = 0
    proration_net_minor: int = 0
    past_due_status: str = ""
    spendable: dict[str, int] = field(default_factory=dict)

    def say(self, line: str) -> None:
        self.log.append(line)


def _ts(day: date, hour: int = 10) -> int:
    return int(datetime.combine(day, time(hour), timezone.utc).timestamp())


def _at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), timezone.utc)


async def _deliver(db: Database, router: Database, events: Events, event: dict[str, Any]) -> webhooks.Received:
    body, header = events.signed(event, timestamp=event["created"])
    return await webhooks.receive(db, router, body, header, SECRETS, now=event["created"])


async def _has_history(db: Database, tenant: str) -> bool:
    async with db.tenant(tenant) as conn:
        return bool(await conn.fetchval("select count(*) from billing_transaction"))


async def run_story(db: Database, router: Database, tenant_a: str, tenant_b: str, *, today: date | None = None) -> Story:
    today = today or datetime.now(timezone.utc).date()
    story = Story(today)
    days = [today - timedelta(days=6 - i) for i in range(7)]      # days[0] is six days ago, days[6] is today
    for tenant in (tenant_a, tenant_b):
        if await _has_history(db, tenant):
            raise BillingError("tenant %s already has billing history; run the demo on a fresh seed (seed_billing_demo.py)" % tenant)

    fake = FakeStripe()
    http = fake.client()
    provider = StripeStyleClient(http, FAKE_API_KEY, sleep=_no_sleep)
    events = Events(SECRETS)
    try:
        # --- prices and subscriptions ------------------------------------------------------------------------------------------------
        for label, tenant in (("A", tenant_a), ("B", tenant_b)):
            async with db.tenant(tenant) as conn:
                for event_type, unit in USAGE_PRICES[label].items():
                    await conn.execute("insert into billing_usage_price (tenant_id, event_type, currency, unit_price_minor) values ($1::uuid, $2, $3, $4) "
                                       "on conflict (tenant_id, event_type, currency) do update set unit_price_minor = excluded.unit_price_minor", tenant, event_type, CURRENCY, unit)
        period = (_at(days[0], 0), _at(days[0], 0) + timedelta(days=30))
        async with db.tenant(tenant_a) as conn:
            await subscriptions.start_subscription(conn, tenant_a, plan_code="starter", provider_customer_id="cus_fake_orchard", provider_subscription_id="sub_fake_orchard",
                                                   period_start=period[0], period_end=period[1])
        async with db.tenant(tenant_b) as conn:
            await subscriptions.start_subscription(conn, tenant_b, plan_code="growth", provider_customer_id="cus_fake_birchwood", provider_subscription_id="sub_fake_birchwood",
                                                   period_start=period[0], period_end=period[1])
        for customer, paid, inv, who in (("cus_fake_orchard", 2900, "in_fake_orchard_1", "Orchard"), ("cus_fake_birchwood", 9900, "in_fake_birchwood_1", "Birchwood")):
            got = await _deliver(db, router, events, events.invoice("invoice.paid", customer, paid, CURRENCY, inv, created=_ts(days[0], 9)))
            story.say("day 0  %s: first subscription invoice paid, %s" % (who, got.detail))

        # --- top-ups: asked of the provider, credited when its signed event arrives --------------------------------------------------
        async def topup(tenant: str, who: str, amount: int, key: str, day: date, method: str = "pm_fake_ok") -> payments.PaymentResult:
            result = await payments.start_topup(db, provider, tenant, amount_minor=amount, currency=CURRENCY, payment_method=method, key=key)
            return result

        async def deliver_payment_event(result: payments.PaymentResult, event_type: str, day: date) -> list[webhooks.Received]:
            pi = fake.payment_intents[result.provider_payment_id]
            event = events.payment_intent(event_type, pi, created=_ts(day))
            body, header = events.signed(event, timestamp=event["created"])
            return [await webhooks.receive(db, router, body, header, SECRETS, now=event["created"])]

        first_a = await topup(tenant_a, "Orchard", 5000, "demo-topup-a-1", days[0])
        got = await deliver_payment_event(first_a, "payment_intent.succeeded", days[0])
        story.say("day 0  Orchard: top-up of 50.00 asked of the provider (%s), event %s" % (first_a.status, got[0].detail))
        first_b = await topup(tenant_b, "Birchwood", 3000, "demo-topup-b-1", days[1])
        got = await deliver_payment_event(first_b, "payment_intent.succeeded", days[1])
        story.say("day 1  Birchwood: top-up of 30.00, event %s" % got[0].detail)

        # --- a week of usage, each charge keyed so a retry changes nothing ----------------------------------------------------------
        plan_a = [(12, 1), (30, 1), (22, 2), (41, 1), (18, 1), (26, 2)]
        plan_b = [(0, 0), (15, 1), (9, 1), (33, 2), (20, 1), (11, 1)]
        for label, tenant, schedule in (("A", tenant_a, plan_a), ("B", tenant_b, plan_b)):
            for i, (syncs, reports) in enumerate(schedule, start=1):
                day = days[i]
                for event_type, qty in (("sync_run", syncs), ("report_run", reports)):
                    if qty:
                        async with db.tenant(tenant) as conn:
                            out = await wallet.debit_usage(conn, tenant, event_type=event_type, quantity=qty, currency=CURRENCY,
                                                           key="demo-usage-%s-%d-%s" % (label, i, event_type), at=_at(day, 14))
                        if not out.ok:
                            raise BillingError("demo usage was refused: %s" % out)
        story.say("days 1 to 6  both tenants: usage metered at each tenant's own price (Orchard pays 0.25 a sync, Birchwood 0.40)")

        # --- the duplicate webhook: the same signed delivery twice -------------------------------------------------------------------
        second_a = await topup(tenant_a, "Orchard", 2000, "demo-topup-a-2", days[3])
        event = events.payment_intent("payment_intent.succeeded", fake.payment_intents[second_a.provider_payment_id], created=_ts(days[3]))
        body, header = events.signed(event, timestamp=event["created"])
        first = await webhooks.receive(db, router, body, header, SECRETS, now=event["created"])
        again = await webhooks.receive(db, router, body, header, SECRETS, now=event["created"] + 90)
        story.duplicate_status = again.status
        story.say("day 3  Orchard: top-up of 20.00; the provider delivered the same event twice: first %s, second %s (nothing written the second time)" % (first.status, again.status))

        # --- the failed payment ------------------------------------------------------------------------------------------------------
        bad = await topup(tenant_a, "Orchard", 3000, "demo-topup-a-3-declined", days[4], method="pm_fake_declined")
        story.failed_payment_status, story.failed_payment_code = bad.status, bad.failure_code
        late = await deliver_payment_event(bad, "payment_intent.payment_failed", days[4])
        story.say("day 4  Orchard: a top-up of 30.00 was declined (%s, code %s); credited nothing; the failure event after it was %s" % (bad.status, bad.failure_code, late[0].status))

        # --- a plan change in the middle of the period ------------------------------------------------------------------------------
        async with db.tenant(tenant_a) as conn:
            pro, _ = await subscriptions.change_plan(conn, tenant_a, new_plan_code="growth", at=_at(days[4], 11), key="demo-plan-a-1", actor="demo admin")
        story.proration_net_minor = pro.net_minor
        got = await _deliver(db, router, events, events.invoice("invoice.paid", "cus_fake_orchard", pro.net_minor, CURRENCY, "in_fake_orchard_2", created=_ts(days[4], 12)))
        story.say("day 4  Orchard: moved from Starter to Growth part way through the period: credit %d, charge %d, bill %d (minor units); invoice paid, %s"
                  % (pro.credit_minor, pro.charge_minor, pro.net_minor, got.detail))

        # --- the refund: asked of the provider, reversed when its charge.refunded event arrives ------------------------------------------
        refund = await payments.request_refund(db, provider, tenant_a, second_a.payment_id, amount_minor=2000, key="demo-refund-a-1")
        got = await _deliver(db, router, events, events.charge_refunded(second_a.provider_payment_id, 2000, 2000, CURRENCY, created=_ts(days[5])))
        story.refund_status, story.refund_minor = got.status, refund.amount
        story.say("day 5  Orchard: the 20.00 top-up was refunded at the provider (%s), reversed in the wallet: %s" % (refund.id, got.detail))

        # --- an admin credit, and a charge the wallet cannot cover ------------------------------------------------------------------------
        async with db.tenant(tenant_b) as conn:
            adj = await wallet.adjust(conn, tenant_b, amount_minor=500, currency=CURRENCY, key="demo-adjust-b-1", actor="demo admin",
                                      reason="Goodwill credit after the day 2 sync outage (invented)", at=_at(days[3], 16))
        story.say("day 3  Birchwood: admin credit of 5.00 with its reason (%s)" % adj.status)
        async with db.tenant(tenant_a) as conn:
            big = await wallet.debit_usage(conn, tenant_a, event_type="report_run", quantity=1000, currency=CURRENCY, key="demo-usage-a-big", at=_at(days[6], 9))
        story.insufficient_status, story.insufficient_shortfall_minor = big.status, big.shortfall_minor
        story.say("day 6  Orchard: a batch of 1000 reports (%d) was refused, %s short, and nothing was recorded" % (big.amount_minor, big.shortfall_minor))

        # --- a failed subscription payment on the other tenant ------------------------------------------------------------------------
        got = await _deliver(db, router, events, events.invoice("invoice.payment_failed", "cus_fake_birchwood", 0, CURRENCY, "in_fake_birchwood_2", created=_ts(days[6], 8)))
        story.past_due_status = got.detail
        story.say("day 6  Birchwood: the next subscription invoice failed, the plan is now %s, a notice is queued, nothing credited" % got.detail)

        for label, tenant in (("A", tenant_a), ("B", tenant_b)):
            async with db.tenant(tenant) as conn:
                story.spendable[label] = -await conn.fetchval("select coalesce(sum(balance_minor), 0) from billing_account where kind = 'wallet' and currency = $1", CURRENCY)
    finally:
        await http.aclose()
    return story


async def _no_sleep(_: float) -> None:
    return None
