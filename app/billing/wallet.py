"""The wallet: top-up, usage debit, refund, adjustment. Each is one balanced ledger transaction, and each can be repeated safely.

THE RULES EVERY COMMAND FOLLOWS, in this order, inside the caller's tenant transaction:

  1. Take the wallet row's lock (ledger.lock_wallet). The commands of one tenant and currency now run one after the other.
  2. Look for the key. The same key with the same request returns the first result and writes nothing (status 'replayed'); the same key with a
     different request raises IdempotencyConflict. A command that comes from a provider object (a payment, a refund) is also looked up by that
     object, so two routes to the same payment cannot both credit it.
  3. Check the funds. A debit that would take the wallet below zero returns status 'insufficient_funds' and records nothing at all.
  4. Write one transaction: the lines add up to zero, and the database checks it again.

Money is integer minor units; a quantity times a price is an integer product, never a float.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from . import ledger
from .errors import IdempotencyConflict, NoUsagePrice
from .ledger import Line


@dataclass(frozen=True)
class Outcome:
    """The result of a wallet command. `status` is applied (written now), replayed (the key was seen; nothing written) or insufficient_funds
    (nothing written). `balance_minor` is what the customer can spend afterwards."""
    status: str
    transaction_id: str | None
    amount_minor: int
    currency: str
    balance_minor: int
    shortfall_minor: int = 0

    @property
    def ok(self) -> bool:
        return self.status != "insufficient_funds"


async def _replayed(conn: asyncpg.Connection, tenant_id: str, row: asyncpg.Record) -> Outcome:
    tx = str(row["id"])
    currency = row["currency"].strip()
    effect = await ledger.wallet_effect(conn, tx)
    return Outcome("replayed", tx, abs(effect), currency, await ledger.spendable(conn, tenant_id, currency))


def _check_amount(amount_minor: int) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("an amount is a positive integer in minor units, got %r" % (amount_minor,))


async def _seen(conn: asyncpg.Connection, tenant_id: str, kind: str, key: str, req: str, provider_ref: str | None) -> asyncpg.Record | None:
    """The transaction already written for this key, or for this provider object. Either way the request must be the same one."""
    row = await ledger.find_by_key(conn, tenant_id, key, req)
    if row is None and provider_ref is not None:
        row = await ledger.find_by_provider_ref(conn, tenant_id, kind, provider_ref)
        if row is not None and row["request_hash"] != req:
            raise IdempotencyConflict(key)
    return row


async def top_up(conn: asyncpg.Connection, tenant_id: str, *, amount_minor: int, currency: str, provider_ref: str, key: str | None = None,
                 actor: str = "payment provider", at: datetime | None = None) -> Outcome:
    """Credit the wallet for a payment the provider says succeeded. The same provider payment credits once, however many routes reach it."""
    _check_amount(amount_minor)
    key = key or "topup:%s" % provider_ref
    req = ledger.request_hash("topup", provider_ref=provider_ref, amount_minor=amount_minor, currency=currency)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "topup", key, req, provider_ref)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    tx = await ledger.post(conn, tenant_id, kind="topup", currency=currency, accounts=accounts, key=key, req_hash=req, actor=actor, provider_ref=provider_ref, at=at,
                           lines=[Line("provider_clearing", amount_minor), Line("wallet", -amount_minor)])
    return Outcome("applied", tx, amount_minor, currency, available + amount_minor)


async def debit_usage(conn: asyncpg.Connection, tenant_id: str, *, event_type: str, quantity: int, currency: str, key: str, actor: str = "metering",
                      at: datetime | None = None) -> Outcome:
    """Charge one billable event (a sync run, a report) at the tenant's own price. Not enough credit: insufficient_funds, and nothing is recorded."""
    _check_amount(quantity)
    req = ledger.request_hash("usage", event_type=event_type, quantity=quantity, currency=currency)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "usage", key, req, None)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    unit = await conn.fetchval("select unit_price_minor from billing_usage_price where tenant_id = $1::uuid and event_type = $2 and currency = $3",
                               tenant_id, event_type, currency)
    if unit is None:
        raise NoUsagePrice("this tenant has no %s price for %r" % (currency, event_type))
    amount = unit * quantity
    if amount > available:
        return Outcome("insufficient_funds", None, amount, currency, available, shortfall_minor=amount - available)
    tx = await ledger.post(conn, tenant_id, kind="usage", currency=currency, accounts=accounts, key=key, req_hash=req, actor=actor, at=at,
                           lines=[Line("wallet", amount), Line("platform_revenue", -amount)])
    await conn.execute(
        """insert into billing_usage_event (tenant_id, transaction_id, event_type, quantity, unit_price_minor, amount_minor, currency, occurred_at)
           values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, coalesce($8::timestamptz, now()))""", tenant_id, tx, event_type, quantity, unit, amount, currency, at)
    return Outcome("applied", tx, amount, currency, available - amount)


async def refund_topup(conn: asyncpg.Connection, tenant_id: str, *, amount_minor: int, currency: str, provider_ref: str, key: str | None = None,
                       actor: str = "payment provider", reason: str | None = None, at: datetime | None = None) -> Outcome:
    """Take refunded money back out of the wallet: exactly the amount the provider refunded, in the same currency. If the customer has already
    spent it, the wallet cannot go negative, so this returns insufficient_funds and records nothing (the webhook receiver holds the event)."""
    _check_amount(amount_minor)
    key = key or "refund:%s" % provider_ref
    req = ledger.request_hash("refund", provider_ref=provider_ref, amount_minor=amount_minor, currency=currency)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "refund", key, req, provider_ref)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    if amount_minor > available:
        return Outcome("insufficient_funds", None, amount_minor, currency, available, shortfall_minor=amount_minor - available)
    tx = await ledger.post(conn, tenant_id, kind="refund", currency=currency, accounts=accounts, key=key, req_hash=req, actor=actor, reason=reason,
                           provider_ref=provider_ref, at=at,
                           lines=[Line("wallet", amount_minor), Line("provider_clearing", -amount_minor)])
    return Outcome("applied", tx, amount_minor, currency, available - amount_minor)


async def refund_usage(conn: asyncpg.Connection, tenant_id: str, *, amount_minor: int, currency: str, key: str, actor: str, reason: str, at: datetime | None = None) -> Outcome:
    """Give back credit for usage that should not have been charged (a service credit): the wallet goes up, the refunds account takes the debit."""
    _check_amount(amount_minor)
    if not reason or len(reason.strip()) < 3:
        raise ValueError("a usage refund needs a reason")
    req = ledger.request_hash("usage_refund", amount_minor=amount_minor, currency=currency, reason=reason)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "usage_refund", key, req, None)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    tx = await ledger.post(conn, tenant_id, kind="usage_refund", currency=currency, accounts=accounts, key=key, req_hash=req, actor=actor, reason=reason, at=at,
                           lines=[Line("refunds", amount_minor), Line("wallet", -amount_minor)])
    return Outcome("applied", tx, amount_minor, currency, available + amount_minor)


async def adjust(conn: asyncpg.Connection, tenant_id: str, *, amount_minor: int, currency: str, key: str, actor: str, reason: str, at: datetime | None = None) -> Outcome:
    """An admin's correction: positive adds credit, negative removes it (never below zero). The reason is required, here and in the table."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor == 0:
        raise ValueError("an adjustment is a non-zero integer in minor units, got %r" % (amount_minor,))
    if not actor or not actor.strip():
        raise ValueError("an adjustment is attributed to someone")
    if not reason or len(reason.strip()) < 3:
        raise ValueError("an adjustment needs a reason")
    req = ledger.request_hash("adjustment", amount_minor=amount_minor, currency=currency, reason=reason)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "adjustment", key, req, None)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    if amount_minor < 0 and -amount_minor > available:
        return Outcome("insufficient_funds", None, -amount_minor, currency, available, shortfall_minor=-amount_minor - available)
    tx = await ledger.post(conn, tenant_id, kind="adjustment", currency=currency, accounts=accounts, key=key, req_hash=req, actor=actor.strip(), reason=reason.strip(), at=at,
                           lines=[Line("adjustments", amount_minor), Line("wallet", -amount_minor)])
    return Outcome("applied", tx, abs(amount_minor), currency, available + amount_minor)


async def credit_subscription(conn: asyncpg.Connection, tenant_id: str, *, paid_minor: int, credits_minor: int, currency: str, provider_ref: str,
                              key: str | None = None, at: datetime | None = None) -> Outcome:
    """A paid subscription invoice. The money received is `paid_minor`; `credits_minor` of it goes to the wallet as included credits and the rest is
    revenue. If the credits are worth more than the payment, the difference is booked to adjustments (a bonus)."""
    _check_amount(paid_minor)
    if credits_minor < 0:
        raise ValueError("credits cannot be negative")
    key = key or "invoice:%s" % provider_ref
    req = ledger.request_hash("subscription_payment", provider_ref=provider_ref, paid_minor=paid_minor, credits_minor=credits_minor, currency=currency)
    accounts, available = await ledger.lock_wallet(conn, tenant_id, currency)
    seen = await _seen(conn, tenant_id, "subscription_payment", key, req, provider_ref)
    if seen is not None:
        return await _replayed(conn, tenant_id, seen)
    lines = [Line("provider_clearing", paid_minor)]
    if credits_minor:
        lines.append(Line("wallet", -credits_minor))
    if paid_minor > credits_minor:
        lines.append(Line("platform_revenue", -(paid_minor - credits_minor)))
    elif credits_minor > paid_minor:
        lines.append(Line("adjustments", credits_minor - paid_minor))
    tx = await ledger.post(conn, tenant_id, kind="subscription_payment", currency=currency, accounts=accounts, key=key, req_hash=req, actor="payment provider",
                           provider_ref=provider_ref, at=at,
                           lines=lines)
    return Outcome("applied", tx, paid_minor, currency, available + credits_minor)
