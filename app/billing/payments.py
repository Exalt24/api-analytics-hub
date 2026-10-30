"""Payments: ask the provider for money, record what happened, and credit the wallet only when the provider says the money arrived.

THE FLOW OF A TOP-UP, and where each protection sits:

  1. One short transaction writes a `billing_payment` row with OUR idempotency key and the key we will send the provider. It is committed BEFORE the
     provider is called, so a crash after the call can always find the row and call again with the same key. A second request with the same key and the
     same content finds that row and does not make another; with different content it raises IdempotencyConflict.
  2. The provider is called with NO database transaction open (a slow network call must not hold a lock). The client retries with the same key.
  3. A second short transaction records the answer. A card error is a clear no: the row becomes `failed` with the provider's code, a notification is
     queued, and NOTHING is credited (the table also forbids a failed payment with credit). A timeout that ran out of attempts becomes `unknown` and is
     settled later with the same key, never by creating again.
  4. The wallet is credited by `settle_succeeded`, called from the `payment_intent.succeeded` event or from `refresh_payment` (which asks the provider),
     whichever comes first. Both end in wallet.top_up, which is keyed on the provider's payment id, so the second route finds the first's transaction and
     writes nothing.

A refund is asked of the provider with a key derived from ours (`request_refund`), and the ledger moves only when the provider's `charge.refunded` event
arrives (`apply_refund`), reversing exactly the amount newly refunded. Posting from the answer to the request as well would be a second route to the same
reversal.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import asyncpg

from app.db import Database

from . import ledger, wallet
from .errors import BillingError, IdempotencyConflict
from .provider import CardDeclined, OutcomeUnknown, PaymentIntent, PaymentProvider, ProviderUnavailable, RequestRejected


class PaymentMismatch(BillingError):
    """The provider reports a payment that does not match what we asked for (another amount or currency). Not credited; the event is held."""


@dataclass(frozen=True)
class PaymentResult:
    """`status` is the payment row's: created (not yet answered, safe to ask again with the same key), processing, succeeded, failed or unknown."""
    payment_id: str
    status: str
    provider_payment_id: str | None
    failure_code: str | None
    failure_message: str | None
    replayed: bool


def _result(row: asyncpg.Record, replayed: bool) -> PaymentResult:
    return PaymentResult(str(row["id"]), row["status"], row["provider_payment_id"], row["failure_code"], row["failure_message"], replayed)


# status moves forward only. succeeded is final; a late "failed" or "processing" never undoes it.
_ADVANCE = """case when status = 'succeeded' then status
                    when $NEW = 'succeeded' then 'succeeded'
                    when status = 'failed' then status
                    else $NEW end"""


async def _set_status(conn: asyncpg.Connection, payment_id: str, new: str, *, provider_payment_id: str | None = None, code: str | None = None,
                      message: str | None = None) -> asyncpg.Record:
    return await conn.fetchrow(
        """update billing_payment set status = %s, provider_payment_id = coalesce(provider_payment_id, $3),
               failure_code = case when status = 'succeeded' then failure_code else coalesce($4, failure_code) end,
               failure_message = case when status = 'succeeded' then failure_message else coalesce($5, failure_message) end,
               updated_at = now()
         where id = $1::uuid returning *""" % _ADVANCE.replace("$NEW", "$2"), payment_id, new, provider_payment_id, code, message)


async def start_topup(db: Database, provider: PaymentProvider, tenant_id: str, *, amount_minor: int, currency: str, payment_method: str, key: str) -> PaymentResult:
    """Ask the provider for `amount_minor` with a payment method token (never a card number). Safe to call again with the same key."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("an amount is a positive integer in minor units, got %r" % (amount_minor,))
    req = ledger.request_hash("start_topup", amount_minor=amount_minor, currency=currency, payment_method=payment_method)
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from billing_payment where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        created_now = False
        if row is None:
            row = await conn.fetchrow(
                """insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, payment_method_ref)
                   values ($1::uuid, $2, $3, $4, $5, $6, $7) on conflict (tenant_id, idempotency_key) do nothing returning *""",
                tenant_id, key, req, "pay-%s" % uuid.uuid4(), amount_minor, currency, payment_method)
            created_now = row is not None
            if row is None:       # a concurrent copy of this very request won the insert; read what it wrote
                row = await conn.fetchrow("select * from billing_payment where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        if row["request_hash"] != req:
            raise IdempotencyConflict(key)
    # a row already answered (processing, succeeded, failed) is returned as it is; created and unknown are asked again, with the SAME provider key
    if row["status"] not in ("created", "unknown"):
        return _result(row, replayed=True)
    try:
        pi = await provider.create_payment_intent(
            amount_minor=amount_minor, currency=currency, payment_method=payment_method,
            metadata={"tenant_id": tenant_id, "payment_id": str(row["id"])}, idempotency_key=row["provider_idempotency_key"])
    except CardDeclined as exc:
        async with db.tenant(tenant_id) as conn:
            updated = await _set_status(conn, str(row["id"]), "failed", provider_payment_id=exc.payment_intent_id, code=exc.decline_code or exc.code, message=exc.message)
            if updated["status"] == "failed":
                await notify(conn, tenant_id, "payment_failed", exc.payment_intent_id or str(row["id"]),
                             "A payment of %d %s was declined: %s" % (amount_minor, currency, exc.decline_code or exc.code or "declined"))
        return _result(updated, replayed=not created_now)
    except RequestRejected as exc:
        async with db.tenant(tenant_id) as conn:
            updated = await _set_status(conn, str(row["id"]), "failed", code=exc.code or exc.error_type or "rejected", message=exc.message)
            if updated["status"] == "failed":
                await notify(conn, tenant_id, "payment_failed", str(row["id"]), "A payment of %d %s was refused by the provider: %s" % (amount_minor, currency, exc.message))
        return _result(updated, replayed=not created_now)
    except OutcomeUnknown as exc:
        async with db.tenant(tenant_id) as conn:
            updated = await _set_status(conn, str(row["id"]), "unknown", code="outcome_unknown", message=str(exc)[:300])
        return _result(updated, replayed=not created_now)
    except ProviderUnavailable:
        return _result(row, replayed=not created_now)       # nothing was created at the provider; still 'created', ask again with the same key
    async with db.tenant(tenant_id) as conn:
        updated = await _set_status(conn, str(row["id"]), "succeeded" if pi.succeeded else "processing", provider_payment_id=pi.id)
    return _result(updated, replayed=not created_now)


async def notify(conn: asyncpg.Connection, tenant_id: str, kind: str, subject_ref: str | None, message: str) -> None:
    await conn.execute("insert into billing_notification (tenant_id, kind, subject_ref, message) values ($1::uuid, $2, $3, $4)", tenant_id, kind, subject_ref, message)


async def _find_payment(conn: asyncpg.Connection, tenant_id: str, intent_id: str, metadata: Mapping[str, Any]) -> asyncpg.Record | None:
    row = await conn.fetchrow("select * from billing_payment where tenant_id = $1::uuid and provider_payment_id = $2 for update", tenant_id, intent_id)
    if row is None and metadata.get("payment_id"):
        try:
            uuid.UUID(str(metadata["payment_id"]))
        except ValueError:
            return None
        row = await conn.fetchrow("select * from billing_payment where tenant_id = $1::uuid and id = $2::uuid for update", tenant_id, str(metadata["payment_id"]))
    return row


async def settle_succeeded(conn: asyncpg.Connection, tenant_id: str, intent: Mapping[str, Any], at: datetime | None = None) -> wallet.Outcome:
    """The provider says this payment intent succeeded: credit the wallet once. `intent` is the PaymentIntent object from an event or from retrieve."""
    intent_id, amount, currency = intent["id"], int(intent["amount"]), str(intent["currency"]).upper()
    metadata = intent.get("metadata") or {}
    # the wallet lock first, like every money command, so the payment row and the ledger move together
    await ledger.lock_wallet(conn, tenant_id, currency)
    row = await _find_payment(conn, tenant_id, intent_id, metadata)
    if row is None:
        # a payment we did not start (or whose row is not visible yet): record it from the event, which is signed, so it can be credited
        row = await conn.fetchrow(
            """insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, status, provider_payment_id)
               values ($1::uuid, $2, $3, $4, $5, $6, 'processing', $7) returning *""",
            tenant_id, "provider:%s" % intent_id, ledger.request_hash("from_event", provider_ref=intent_id), "none", amount, currency, intent_id)
    if row["amount_minor"] != amount or row["currency"].strip() != currency:
        raise PaymentMismatch("payment %s asked for %d %s but the provider reports %d %s" % (intent_id, row["amount_minor"], row["currency"].strip(), amount, currency))
    outcome = await wallet.top_up(conn, tenant_id, amount_minor=amount, currency=currency, provider_ref=intent_id, at=at)
    await conn.execute(
        """update billing_payment set status = 'succeeded', provider_payment_id = $2, credited_minor = $3, failure_code = null, failure_message = null,
               updated_at = now() where id = $1::uuid""", str(row["id"]), intent_id, amount)
    return outcome


async def settle_failed(conn: asyncpg.Connection, tenant_id: str, intent: Mapping[str, Any]) -> str:
    """The provider says a payment attempt failed. Record the failure and queue a notice. Never credits, and never undoes a success that was already
    recorded (a late failure event for a payment that has since succeeded changes nothing). Returns 'failed' or 'ignored'."""
    intent_id = intent["id"]
    row = await _find_payment(conn, tenant_id, intent_id, intent.get("metadata") or {})
    err = intent.get("last_payment_error") or {}
    code = err.get("decline_code") or err.get("code") or "payment_failed"
    message = err.get("message") or "The payment failed."
    if row is None:
        row = await conn.fetchrow(
            """insert into billing_payment (tenant_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency, status, provider_payment_id)
               values ($1::uuid, $2, $3, $4, $5, $6, 'processing', $7) returning *""",
            tenant_id, "provider:%s" % intent_id, ledger.request_hash("from_event", provider_ref=intent_id), "none", int(intent["amount"]), str(intent["currency"]).upper(), intent_id)
    if row["status"] in ("succeeded", "failed"):
        return "ignored"
    await _set_status(conn, str(row["id"]), "failed", provider_payment_id=intent_id, code=code, message=message)
    await notify(conn, tenant_id, "payment_failed", intent_id, "A payment of %d %s failed: %s" % (row["amount_minor"], row["currency"].strip(), code))
    return "failed"


async def refresh_payment(db: Database, provider: PaymentProvider, tenant_id: str, payment_id: str) -> PaymentResult:
    """Ask the provider where a payment stands (for a lost webhook or an unknown outcome) and settle it. Uses the stored provider id, or, for an unknown
    outcome that never got one, the same idempotency key: the provider answers with the first result and creates nothing."""
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)
    if row is None:
        raise BillingError("no such payment")
    if row["provider_payment_id"]:
        pi = await provider.retrieve_payment_intent(row["provider_payment_id"])
    else:
        try:
            pi = await provider.create_payment_intent(
                amount_minor=row["amount_minor"], currency=row["currency"].strip(), payment_method=row["payment_method_ref"] or "",
                metadata={"tenant_id": tenant_id, "payment_id": payment_id}, idempotency_key=row["provider_idempotency_key"])
        except CardDeclined as exc:
            async with db.tenant(tenant_id) as conn:
                await settle_failed(conn, tenant_id, {"id": exc.payment_intent_id or "unknown-" + payment_id, "amount": row["amount_minor"], "currency": row["currency"],
                                                      "metadata": {"payment_id": payment_id},
                                                      "last_payment_error": {"code": exc.code, "decline_code": exc.decline_code, "message": exc.message}})
                row = await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)
            return _result(row, replayed=True)
    async with db.tenant(tenant_id) as conn:
        if pi.succeeded:
            await settle_succeeded(conn, tenant_id, {"id": pi.id, "amount": pi.amount, "currency": pi.currency, "metadata": pi.metadata})
        elif pi.status == "requires_payment_method" and pi.error_code:
            await settle_failed(conn, tenant_id, {"id": pi.id, "amount": pi.amount, "currency": pi.currency, "metadata": pi.metadata,
                                                  "last_payment_error": {"code": pi.error_code, "message": pi.error_message}})
        else:
            await _set_status(conn, payment_id, "processing", provider_payment_id=pi.id)
        row = await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)
    return _result(row, replayed=True)


async def request_refund(db: Database, provider: PaymentProvider, tenant_id: str, payment_id: str, *, amount_minor: int, key: str) -> Any:
    """Ask the provider to refund part or all of a succeeded payment. The provider key is derived from ours, so asking twice refunds once. The wallet is
    not touched here: it moves when the provider's charge.refunded event arrives (apply_refund)."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("an amount is a positive integer in minor units, got %r" % (amount_minor,))
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from billing_payment where id = $1::uuid", payment_id)
    if row is None or row["status"] != "succeeded" or not row["provider_payment_id"]:
        raise BillingError("only a succeeded payment can be refunded")
    if amount_minor > row["credited_minor"] - row["refunded_minor"]:
        raise BillingError("%d is more than the %d still refundable on this payment" % (amount_minor, row["credited_minor"] - row["refunded_minor"]))
    provider_key = "rf-" + hashlib.sha256(("%s|%s" % (tenant_id, key)).encode()).hexdigest()[:48]
    return await provider.create_refund(payment_intent_id=row["provider_payment_id"], amount_minor=amount_minor, idempotency_key=provider_key,
                                        metadata={"tenant_id": tenant_id, "payment_id": payment_id})


async def apply_refund(conn: asyncpg.Connection, tenant_id: str, charge: Mapping[str, Any], at: datetime | None = None) -> tuple[str, str]:
    """A charge.refunded event: reverse what is newly refunded. The event carries the TOTAL refunded so far (`amount_refunded`), so the amount to reverse is
    that total minus what was already reversed. An older event (a smaller total) reverses nothing, a repeat reverses nothing, and the sum of the
    reversals is always exactly the provider's total. Returns (status, detail): processed, ignored or held."""
    intent_id = charge.get("payment_intent")
    total = int(charge["amount_refunded"])
    if not intent_id:
        return "ignored", "charge has no payment intent"
    row = await conn.fetchrow("select * from billing_payment where tenant_id = $1::uuid and provider_payment_id = $2 for update", tenant_id, intent_id)
    if row is None or row["status"] != "succeeded":
        return "held", "the refund arrived before its payment was recorded as succeeded"
    currency = row["currency"].strip()
    if str(charge.get("currency", "")).upper() != currency:
        return "held", "the refund is in %s and the payment in %s" % (str(charge.get("currency", "")).upper(), currency)
    if total > row["credited_minor"]:
        return "held", "refunded total %d is more than the %d credited" % (total, row["credited_minor"])
    delta = total - row["refunded_minor"]
    if delta <= 0:
        return "ignored", "already reversed up to %d" % row["refunded_minor"]
    outcome = await wallet.refund_topup(conn, tenant_id, amount_minor=delta, currency=currency, provider_ref="%s:refund:%d" % (intent_id, total), at=at)
    if not outcome.ok:
        await notify(conn, tenant_id, "refund_held", intent_id,
                     "A refund of %d %s was made at the provider but the wallet holds only %d, so it was not reversed yet." % (delta, currency, outcome.balance_minor))
        return "held", "the wallet holds %d and the refund is %d" % (outcome.balance_minor, delta)
    await conn.execute("update billing_payment set refunded_minor = greatest(refunded_minor, $2), updated_at = now() where id = $1::uuid", str(row["id"]), total)
    return "processed", "reversed %d (total refunded %d)" % (delta, total)
