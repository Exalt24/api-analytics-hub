"""Subscriptions: one for each tenant, in one of three states, moved by the provider's invoice and subscription events and by a plan change.

STATES.  active (paid up), past_due (an invoice payment failed), canceled (the provider ended it; final). The provider's own status string is kept as
sent in `provider_status`. The mapping from a provider status to ours is the table STATUS_MAP below; a status not in it (incomplete, paused) changes
nothing but `provider_status`, because guessing would be worse than waiting for the next event. From the saved subscription page: "Possible values are
incomplete, incomplete_expired, trialing, active, past_due, canceled, unpaid, or paused."

THE EVENTS (docs/vendor-contract/stripe/billing-subscriptions-webhooks.txt and api-events-types.txt):
  invoice.paid                   "Sent when the invoice is successfully paid." -> the payment is booked (money in, included credits to the wallet, the rest
                                 revenue), the subscription is active again.
  invoice.payment_failed         "A payment for an invoice failed." -> past_due, a notification queued, nothing credited.
  customer.subscription.updated  "Sent when a subscription starts or changes" -> the provider status and, when the price id is one of our plans, the plan.
  customer.subscription.deleted  "Sent when a customer's subscription ends." -> canceled.
From the saved page: "When a subscription changes to past_due, notify the customer directly and ask them to update their payment details" and "When a
subscription changes to canceled or unpaid, revoke access to your product." Revoking access is not done here (this module has no access control to revoke).

ORDER. The provider does not promise order ("Stripe doesn't guarantee the delivery of events in the order that they're generated") and says not to rely on
`created` ("Snapshot events record created in seconds, so distinct events can share a timestamp. Don't use created to determine event order"). So this does
two things that do not need an order: the MONEY side (booking an invoice) is keyed on the invoice id and applies whenever it arrives, and a canceled
subscription is final, so no late event revives it. For the STATE it applies an event only when its `created` is not older than the newest one applied;
two events in the same second are applied in arrival order, which can be wrong, and the remedy the page names (retrieving the subscription from the
API) is not implemented. That is a stated limit, in docs/billing.md.

CREDITS. A paid invoice grants the plan's included credits in proportion to what was paid, rounded DOWN (never more than was paid for):
    credits = included_credits * min(paid, monthly_price) // monthly_price
A full cycle payment grants all of them; the payment for a mid-cycle upgrade (see proration.py) grants the same share of the new plan's credits as it is of
the new plan's price. An invoice for a plan with a zero price books nothing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import asyncpg

from . import ledger, payments, wallet
from .errors import IdempotencyConflict, NoSubscription, UnknownPlan
from .proration import Proration, prorate

#: the provider's subscription status -> ours. Anything else leaves our state alone.
STATUS_MAP = {"active": "active", "trialing": "active", "past_due": "past_due", "unpaid": "past_due", "canceled": "canceled", "incomplete_expired": "canceled"}


def credits_for_payment(included_credits_minor: int, monthly_price_minor: int, paid_minor: int) -> int:
    """The credits a payment earns: the plan's included credits in proportion to the share of the price paid, rounded down, capped at the full amount."""
    if monthly_price_minor <= 0 or paid_minor <= 0:
        return 0
    return included_credits_minor * min(paid_minor, monthly_price_minor) // monthly_price_minor


async def get_plan(conn: asyncpg.Connection, code: str) -> asyncpg.Record:
    row = await conn.fetchrow("select * from billing_plan where code = $1 and active", code)
    if row is None:
        raise UnknownPlan("no active plan %r" % code)
    return row


async def start_subscription(conn: asyncpg.Connection, tenant_id: str, *, plan_code: str, provider_customer_id: str, provider_subscription_id: str,
                             period_start: datetime, period_end: datetime) -> asyncpg.Record:
    plan = await get_plan(conn, plan_code)
    return await conn.fetchrow(
        """insert into billing_subscription (tenant_id, plan_id, provider_customer_id, provider_subscription_id, provider_status, period_start, period_end)
           values ($1::uuid, $2, $3, $4, 'active', $5, $6)
           on conflict (tenant_id) do update set plan_id = excluded.plan_id, provider_customer_id = excluded.provider_customer_id,
               provider_subscription_id = excluded.provider_subscription_id, status = 'active', provider_status = 'active', canceled_at = null,
               past_due_since = null, period_start = excluded.period_start, period_end = excluded.period_end, updated_at = now()
           returning *""",
        tenant_id, plan["id"], provider_customer_id, provider_subscription_id, period_start, period_end)


async def get_subscription(conn: asyncpg.Connection, tenant_id: str) -> asyncpg.Record:
    row = await conn.fetchrow("select s.*, p.code as plan_code, p.monthly_price_minor, p.included_credits_minor, p.currency as plan_currency "
                              "from billing_subscription s join billing_plan p on p.id = s.plan_id where s.tenant_id = $1::uuid", tenant_id)
    if row is None:
        raise NoSubscription("this tenant has no subscription")
    return row


async def change_plan(conn: asyncpg.Connection, tenant_id: str, *, new_plan_code: str, at: datetime, key: str, actor: str) -> tuple[Proration, bool]:
    """Move the tenant to another plan part way through the period and work out the proration. Returns (proration, replayed). The same key with the same
    request returns the first proration and changes nothing; with a different request it raises IdempotencyConflict. The money for the difference is
    billed by the provider; the wallet moves when that invoice is paid (invoice.paid)."""
    req = ledger.request_hash("change_plan", new_plan_code=new_plan_code, at=at.isoformat())
    await conn.execute("select 1 from billing_subscription where tenant_id = $1::uuid for update", tenant_id)
    seen = await conn.fetchrow("select * from billing_plan_change where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
    if seen is not None:
        if seen["request_hash"] != req:
            raise IdempotencyConflict(key)
        return Proration(seen["credit_minor"], seen["charge_minor"], seen["net_minor"], 0, 0), True
    sub = await get_subscription(conn, tenant_id)
    new = await get_plan(conn, new_plan_code)
    if new["id"] == sub["plan_id"]:
        raise UnknownPlan("the tenant is already on %r" % new_plan_code)
    if new["currency"] != sub["plan_currency"]:
        raise UnknownPlan("a plan change cannot change the currency (no conversion is done)")
    pro = prorate(sub["monthly_price_minor"], new["monthly_price_minor"], sub["period_start"], sub["period_end"], at)
    await conn.execute(
        """insert into billing_plan_change (tenant_id, idempotency_key, request_hash, from_plan_id, to_plan_id, changed_at, period_start, period_end,
                                            credit_minor, charge_minor, net_minor, actor)
           values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
        tenant_id, key, req, sub["plan_id"], new["id"], at, sub["period_start"], sub["period_end"], pro.credit_minor, pro.charge_minor, pro.net_minor, actor)
    await conn.execute("update billing_subscription set plan_id = $2, updated_at = now() where tenant_id = $1::uuid", tenant_id, new["id"])
    return pro, False


# ----------------------------------------------------------------------------------------------------- provider events

def _fresh(sub: asyncpg.Record, created: int) -> bool:
    return created >= sub["last_event_created"]


async def on_invoice_paid(conn: asyncpg.Connection, tenant_id: str, invoice: Mapping[str, Any], created: int, at: datetime | None = None) -> tuple[str, str]:
    sub = await get_subscription(conn, tenant_id)
    paid = int(invoice.get("amount_paid") or 0)
    currency = str(invoice.get("currency", "")).upper()
    if paid > 0:
        if currency != sub["plan_currency"].strip():
            return "held", "invoice is in %s and the plan in %s" % (currency, sub["plan_currency"].strip())
        credits = credits_for_payment(sub["included_credits_minor"], sub["monthly_price_minor"], paid)
        outcome = await wallet.credit_subscription(conn, tenant_id, paid_minor=paid, credits_minor=credits, currency=currency, provider_ref=invoice["id"], at=at)
        detail = "booked %d, %d credits (%s)" % (paid, credits, outcome.status)
    else:
        detail = "nothing was paid on this invoice, nothing booked"
    if sub["status"] != "canceled" and _fresh(sub, created):
        await conn.execute("update billing_subscription set status = 'active', past_due_since = null, last_event_created = greatest(last_event_created, $2), updated_at = now() "
                           "where tenant_id = $1::uuid", tenant_id, created)
    return "processed", detail


async def on_invoice_payment_failed(conn: asyncpg.Connection, tenant_id: str, invoice: Mapping[str, Any], created: int) -> tuple[str, str]:
    sub = await get_subscription(conn, tenant_id)
    if sub["status"] == "canceled" or not _fresh(sub, created):
        return "ignored", "older than the state held, or already canceled"
    await conn.execute("update billing_subscription set status = 'past_due', past_due_since = coalesce(past_due_since, now()), "
                       "last_event_created = greatest(last_event_created, $2), updated_at = now() where tenant_id = $1::uuid", tenant_id, created)
    await payments.notify(conn, tenant_id, "subscription_past_due", invoice.get("id"), "The subscription payment failed; the plan is past due until it is paid.")
    return "processed", "past_due"


async def on_subscription_updated(conn: asyncpg.Connection, tenant_id: str, subscription: Mapping[str, Any], created: int) -> tuple[str, str]:
    sub = await get_subscription(conn, tenant_id)
    if sub["status"] == "canceled":
        return "ignored", "already canceled, which is final"
    if not _fresh(sub, created):
        return "ignored", "older than the state held"
    raw = subscription.get("status")
    new_status = STATUS_MAP.get(raw, sub["status"])
    plan_id = sub["plan_id"]
    items = ((subscription.get("items") or {}).get("data")) or []
    price_id = ((items[0].get("price") or {}).get("id")) if items else None
    if price_id:
        plan = await conn.fetchrow("select id from billing_plan where provider_price_id = $1", price_id)
        if plan is not None:
            plan_id = plan["id"]
    await conn.execute(
        """update billing_subscription set status = $2, plan_id = $3, provider_status = $4, last_event_created = greatest(last_event_created, $5),
               past_due_since = case when $2 = 'past_due' then coalesce(past_due_since, now()) when $2 = 'active' then null else past_due_since end,
               canceled_at = case when $2 = 'canceled' then coalesce(canceled_at, now()) else canceled_at end, updated_at = now()
         where tenant_id = $1::uuid""", tenant_id, new_status, plan_id, raw, created)
    if new_status == "past_due" and sub["status"] != "past_due":
        await payments.notify(conn, tenant_id, "subscription_past_due", subscription.get("id"), "The subscription is past due at the provider.")
    if new_status == "canceled":
        await payments.notify(conn, tenant_id, "subscription_canceled", subscription.get("id"), "The subscription was canceled at the provider.")
    return "processed", "status %s -> %s%s" % (sub["status"], new_status, ", plan changed" if plan_id != sub["plan_id"] else "")


async def on_subscription_deleted(conn: asyncpg.Connection, tenant_id: str, subscription: Mapping[str, Any], created: int) -> tuple[str, str]:
    sub = await get_subscription(conn, tenant_id)
    if sub["status"] == "canceled":
        return "ignored", "already canceled"
    await conn.execute("update billing_subscription set status = 'canceled', provider_status = 'canceled', canceled_at = now(), "
                       "last_event_created = greatest(last_event_created, $2), updated_at = now() where tenant_id = $1::uuid", tenant_id, created)
    await payments.notify(conn, tenant_id, "subscription_canceled", subscription.get("id"), "The subscription ended at the provider.")
    return "processed", "canceled"
