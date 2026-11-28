"""Invoices: a clinic's bill with line items, paid from the wallet, by card or by bank debit (ACH), voided, or refunded in full or in part.

THE LIFE OF AN INVOICE.  draft (lines may change) -> open (finalized: the total is the sum of the lines and is fixed) -> paid, or void from draft or open. A paid invoice is never voided; it is
refunded, and its refunded total only grows and never passes the total. The moves are the rows of billing_invoice_transition, enforced by a trigger, and the trigger asks the access matrix
(migration 022) for the action each move needs: finalize and void are `update`, paying is `settle`, refunding is `refund`.

THREE WAYS TO PAY, one rule: an invoice is paid exactly once (a unique ledger reference for the invoice and a unique index that allows one live payment for it).
  wallet         the wallet is debited and revenue credited in one transaction with the status change; not enough credit writes nothing and answers insufficient_funds.
  card           a PaymentIntent for the invoice's total through the provider interface (billing/payments.py, the same idempotency key, the same retries). The provider answers succeeded at once for a
                 card; the invoice is settled from that answer or from the signed payment_intent.succeeded event, whichever comes first, and the other finds it already paid.
  bank debit     the same, with `us_bank_account`: the provider answers `processing`, the invoice stays OPEN and the payment is `processing`, and the invoice is settled only when the signed
                 succeeded event arrives (days later). A return (payment_failed) records a failed payment, leaves the invoice open and books nothing, so it can be paid again with a new key.
                 While a payment is in flight the invoice cannot be paid again or voided.
The ledger moves only when the money is known to have arrived: invoice_payment lines are provider_clearing (or wallet) against platform_revenue.

REFUNDS REUSE THE ONES THAT EXIST. An invoice paid from the wallet is refunded to the wallet at once. An invoice paid by card or bank debit asks the provider for a refund of the payment's intent
(billing.provider create_refund, a key derived from ours, so asking twice refunds once) and the ledger moves when the provider's charge.refunded event arrives, as for a wallet top-up; the
request is logged in billing_invoice_refund with the provider's refund id. Each refund reverses the sales rep's commission (billing/commissions.py).

WHO RUNS WHAT. The functions take a Database, or an ActingDatabase (app/access/engine.py) that opens every transaction as one portal user, so the matrix filters each statement. The steps that follow
a provider event and have no person behind them (settling a payment, reversing a commission) run inside `engine_actor`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import asyncpg

from app.access.engine import engine_actor
from app.db import Database

from . import commissions, ledger, payments, wallet
from .errors import BillingError, IdempotencyConflict, InvalidInvoice, InvoiceNotFound, InvoicePaymentPending, InvoiceStateError
from .provider import PaymentProvider

MAX_LINES = 50
MAX_AMOUNT_MINOR = 10 ** 12          # one line's unit amount or quantity; the sum of 50 of the products still fits a bigint
METHODS = ("wallet", "card", "us_bank_account")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class PayResult:
    """`status` is paid, processing (a bank debit that has not cleared, or a card answer not yet settled), failed or insufficient_funds."""
    status: str
    invoice: dict
    payment_id: str | None
    replayed: bool
    shortfall_minor: int = 0
    failure_code: str | None = None


@dataclass(frozen=True)
class RefundResult:
    """`status` is refunded (the ledger has moved) or requested (asked of the provider; the ledger moves on its charge.refunded event)."""
    status: str
    refund_id: str
    amount_minor: int
    provider_refund_id: str | None
    replayed: bool
    invoice: dict


def _view(row: asyncpg.Record, lines: Sequence[asyncpg.Record] | None = None) -> dict:
    out = {"id": str(row["id"]), "number": "INV-%06d" % row["number"], "clinic_id": str(row["clinic_id"]), "currency": row["currency"].strip(), "status": row["status"],
           "total_minor": row["total_minor"], "refunded_minor": row["refunded_minor"], "pay_method": row["pay_method"],
           "payment_id": str(row["payment_id"]) if row["payment_id"] else None,
           "created_at": row["created_at"].isoformat(), "finalized_at": row["finalized_at"].isoformat() if row["finalized_at"] else None,
           "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None, "voided_at": row["voided_at"].isoformat() if row["voided_at"] else None}
    if lines is not None:
        out["lines"] = [{"position": r["position"], "description": r["description"], "quantity": r["quantity"], "unit_amount_minor": r["unit_amount_minor"],
                         "amount_minor": r["amount_minor"]} for r in lines]
    return out


def _check_lines(lines: Sequence[Mapping[str, Any]]) -> list[tuple[str, int, int]]:
    if not isinstance(lines, (list, tuple)) or not 1 <= len(lines) <= MAX_LINES:
        raise InvalidInvoice("an invoice has 1 to %d lines" % MAX_LINES)
    out = []
    for line in lines:
        description, quantity, unit = line.get("description"), line.get("quantity"), line.get("unit_amount_minor")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
            raise InvalidInvoice("a line needs a description of 1 to 200 characters")
        for name, value in (("quantity", quantity), ("unit_amount_minor", unit)):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= MAX_AMOUNT_MINOR:
                raise InvalidInvoice("%s is a positive integer (minor units for an amount), got %r" % (name, value))
        out.append((description.strip(), quantity, unit))
    return out


async def _lock(conn: asyncpg.Connection, tenant_id: str, invoice_id: str) -> asyncpg.Record | None:
    try:
        return await conn.fetchrow("select * from billing_invoice where tenant_id = $1::uuid and id = $2::uuid for update", tenant_id, invoice_id)
    except asyncpg.DataError:       # an id that is not a uuid is the same answer as one that does not exist
        return None


async def _live_payment(conn: asyncpg.Connection, invoice_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("select id, status, idempotency_key from billing_payment where invoice_id = $1::uuid and status in ('created', 'processing', 'unknown', 'succeeded')", invoice_id)


# ----------------------------------------------------------------------------------------------------- draft, finalize, void

async def create_draft(db: Database, tenant_id: str, *, clinic_id: str, currency: str, lines: Sequence[Mapping[str, Any]], key: str, actor: str) -> tuple[dict, bool]:
    """A draft invoice for a clinic with its lines. Returns (invoice, replayed). The same key with the same content returns the first invoice; with other content it raises IdempotencyConflict."""
    cleaned = _check_lines(lines)
    currency = str(currency).upper()
    if not _CURRENCY.match(currency):
        raise InvalidInvoice("a currency is a three letter code")
    if not 0 < len(key) <= 255:
        raise ValueError("an idempotency key is 1 to 255 characters")
    req = ledger.request_hash("create_invoice", clinic_id=str(clinic_id), currency=currency, lines=cleaned)
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from billing_invoice where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        created = False
        if row is None:
            try:
                row = await conn.fetchrow(
                    """insert into billing_invoice (tenant_id, clinic_id, currency, idempotency_key, request_hash, created_by)
                       values ($1::uuid, $2::uuid, $3, $4, $5, $6) on conflict (tenant_id, idempotency_key) do nothing returning *""",
                    tenant_id, str(clinic_id), currency, key, req, actor)
            except asyncpg.ForeignKeyViolationError as exc:
                raise InvalidInvoice("no such clinic") from exc
            created = row is not None
            if row is None:
                row = await conn.fetchrow("select * from billing_invoice where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        if row["request_hash"] != req:
            raise IdempotencyConflict(key)
        if created:
            await conn.executemany(
                "insert into billing_invoice_line (tenant_id, invoice_id, clinic_id, position, description, quantity, unit_amount_minor, amount_minor) values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)",
                [(tenant_id, str(row["id"]), str(clinic_id), i, d, q, u, q * u) for i, (d, q, u) in enumerate(cleaned, start=1)])
        stored = await conn.fetch("select * from billing_invoice_line where invoice_id = $1::uuid order by position", str(row["id"]))
        return _view(row, stored), not created


async def finalize(db: Database, tenant_id: str, invoice_id: str, *, actor: str) -> dict:
    """draft -> open: the total becomes the sum of the lines and is fixed. Finalizing an open invoice again returns it unchanged."""
    async with db.tenant(tenant_id) as conn:
        inv = await _lock(conn, tenant_id, invoice_id)
        if inv is None:
            raise InvoiceNotFound("no such invoice")
        if inv["status"] == "open":
            return _view(inv)
        if inv["status"] != "draft":
            raise InvoiceStateError("invoice is %s, so it cannot be finalized" % inv["status"])
        total = await conn.fetchval("select coalesce(sum(amount_minor), 0) from billing_invoice_line where invoice_id = $1::uuid", invoice_id)
        if total <= 0:
            raise InvalidInvoice("an invoice with no lines cannot be finalized")
        updated = await conn.fetchrow("update billing_invoice set status = 'open', total_minor = $2, finalized_at = now(), updated_at = now() where id = $1::uuid returning *", invoice_id, total)
        return _view(updated)


async def void(db: Database, tenant_id: str, invoice_id: str, *, actor: str) -> dict:
    """draft or open -> void. A paid invoice is refunded, not voided; an invoice with a payment in flight cannot be voided until that payment ends. Voiding a void invoice returns it."""
    async with db.tenant(tenant_id) as conn:
        inv = await _lock(conn, tenant_id, invoice_id)
        if inv is None:
            raise InvoiceNotFound("no such invoice")
        if inv["status"] == "void":
            return _view(inv)
        if inv["status"] not in ("draft", "open"):
            raise InvoiceStateError("invoice is %s, so it cannot be voided (refund it instead)" % inv["status"])
        if await _live_payment(conn, invoice_id) is not None:
            raise InvoicePaymentPending("a payment for this invoice is in flight")
        updated = await conn.fetchrow("update billing_invoice set status = 'void', voided_at = now(), updated_at = now() where id = $1::uuid returning *", invoice_id)
        return _view(updated)


# ----------------------------------------------------------------------------------------------------- reading

async def get_invoice(db: Database, tenant_id: str, invoice_id: str) -> dict:
    """One invoice with its lines, as the CALLER may see it: another clinic's invoice answers InvoiceNotFound, like one that does not exist."""
    async with db.tenant(tenant_id) as conn:
        try:
            row = await conn.fetchrow("select * from billing_invoice where tenant_id = $1::uuid and id = $2::uuid", tenant_id, invoice_id)
        except asyncpg.DataError:
            row = None
        if row is None:
            raise InvoiceNotFound("no such invoice")
        return _view(row, await conn.fetch("select * from billing_invoice_line where invoice_id = $1::uuid order by position", invoice_id))


async def list_invoices(db: Database, tenant_id: str) -> list[dict]:
    async with db.tenant(tenant_id) as conn:
        return [_view(r) for r in await conn.fetch("select * from billing_invoice where tenant_id = $1::uuid order by number", tenant_id)]


# ----------------------------------------------------------------------------------------------------- paying

async def pay(db: Database, provider: PaymentProvider | None, tenant_id: str, invoice_id: str, *, method: str, key: str, actor: str, payment_method: str | None = None,
              customer: str | None = None, mandate: str | None = None) -> PayResult:
    """Pay an open invoice from the wallet (`method` wallet) or through the provider (card, or us_bank_account with its customer and mandate). Safe to call again with the same key."""
    if method not in METHODS:
        raise ValueError("a method is one of %s" % (METHODS,))
    async with db.tenant(tenant_id) as conn:
        inv = await _lock(conn, tenant_id, invoice_id)
        if inv is None:
            raise InvoiceNotFound("no such invoice")
        if inv["status"] == "paid":
            if inv["pay_method"] != method:
                raise InvoiceStateError("invoice is already paid, by %s" % inv["pay_method"])
            return PayResult("paid", _view(inv), str(inv["payment_id"]) if inv["payment_id"] else None, replayed=True)
        if inv["status"] != "open":
            raise InvoiceStateError("invoice is %s, so it cannot be paid" % inv["status"])
        live = await _live_payment(conn, invoice_id)
        if live is not None and (method == "wallet" or live["idempotency_key"] != key):
            raise InvoicePaymentPending("a payment for this invoice is already in flight")
        if method == "wallet":
            outcome = await wallet.pay_invoice(conn, tenant_id, invoice_id=invoice_id, amount_minor=inv["total_minor"], currency=inv["currency"].strip(), from_wallet=True, actor=actor)
            if not outcome.ok:
                return PayResult("insufficient_funds", _view(inv), None, replayed=False, shortfall_minor=outcome.shortfall_minor)
            paid = await conn.fetchrow("update billing_invoice set status = 'paid', pay_method = 'wallet', paid_at = now(), updated_at = now() where id = $1::uuid returning *", invoice_id)
            await commissions.accrue(conn, tenant_id, paid)
            return PayResult("paid", _view(paid), None, replayed=False)
    if provider is None:
        raise BillingError("no payment provider is configured")
    if not payment_method:
        raise ValueError("a payment method token is required to pay by card or bank debit")
    try:
        res = await payments.start_topup(db, provider, tenant_id, amount_minor=inv["total_minor"], currency=inv["currency"].strip(), payment_method=payment_method, key=key,
                                         method_type=method, customer=customer, mandate=mandate, purpose="invoice", invoice_id=invoice_id, clinic_id=str(inv["clinic_id"]))
    except asyncpg.UniqueViolationError as exc:       # a concurrent payment for the same invoice under another key won the unique index
        raise InvoicePaymentPending("a payment for this invoice is already in flight") from exc
    if res.status == "succeeded":
        # the provider has the money (a card, at once): settle from its answer; the signed event that follows finds the invoice paid and writes nothing. Settling is the engine's
        # step, not the user's, so it uses the Database underneath (the user's right to pay was proved above, by locking the invoice as them)
        res = await payments.refresh_payment(getattr(db, "engine", db), provider, tenant_id, res.payment_id)
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from billing_invoice where tenant_id = $1::uuid and id = $2::uuid", tenant_id, invoice_id)
    status = "paid" if row["status"] == "paid" else ("failed" if res.status == "failed" else "processing")
    return PayResult(status, _view(row), res.payment_id, replayed=res.replayed, failure_code=res.failure_code)


async def settle_invoice_payment(conn: asyncpg.Connection, tenant_id: str, payment: asyncpg.Record, intent_id: str, at: datetime | None = None) -> wallet.Outcome:
    """The provider says the payment made for an invoice succeeded: pay the invoice once. Called by payments.settle_succeeded for a payment whose purpose is `invoice`, from the signed event
    or from refresh_payment. An invoice already paid by this payment is a replay and writes nothing; one that is not open (voided by hand) holds the event instead of booking money to it."""
    async with engine_actor(conn):
        inv = await conn.fetchrow("select * from billing_invoice where id = $1::uuid for update", str(payment["invoice_id"]))
        currency = payment["currency"].strip()
        if inv["status"] == "paid" and inv["payment_id"] == payment["id"]:
            return wallet.Outcome("replayed", None, payment["amount_minor"], currency, await ledger.spendable(conn, tenant_id, currency))
        if inv["status"] != "open":
            raise payments.PaymentMismatch("payment %s was made for invoice %s, which is %s" % (intent_id, inv["id"], inv["status"]))
        if inv["total_minor"] != payment["amount_minor"] or inv["currency"].strip() != currency:
            raise payments.PaymentMismatch("payment %s is for %d %s but the invoice is %d %s" % (intent_id, payment["amount_minor"], currency, inv["total_minor"], inv["currency"].strip()))
        out = await wallet.pay_invoice(conn, tenant_id, invoice_id=str(inv["id"]), amount_minor=inv["total_minor"], currency=currency, from_wallet=False, actor="payment provider", at=at)
        paid = await conn.fetchrow(
            """update billing_invoice set status = 'paid', pay_method = $2, payment_id = $3::uuid, paid_at = coalesce($4::timestamptz, now()), updated_at = now()
               where id = $1::uuid returning *""", str(inv["id"]), payment["payment_method_type"], str(payment["id"]), at)
        await conn.execute("update billing_payment set status = 'succeeded', provider_payment_id = $2, failure_code = null, failure_message = null, updated_at = now() where id = $1::uuid",
                           str(payment["id"]), intent_id)
        await commissions.accrue(conn, tenant_id, paid)
        return out


# ----------------------------------------------------------------------------------------------------- refunding

async def refund(db: Database, provider: PaymentProvider | None, tenant_id: str, invoice_id: str, *, amount_minor: int, key: str, actor: str) -> RefundResult:
    """Refund part or all of a paid invoice. Wallet payment: back to the wallet now. Card or bank debit: asked of the provider (a key derived from ours, so twice refunds once), booked when its
    charge.refunded event arrives. The sum of the refunds asked for never passes the total."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("an amount is a positive integer in minor units, got %r" % (amount_minor,))
    req = ledger.request_hash("refund_invoice", invoice_id=str(invoice_id), amount_minor=amount_minor)
    provider_key = "inv-rf-" + hashlib.sha256(("%s|%s" % (tenant_id, key)).encode()).hexdigest()[:48]
    async with db.tenant(tenant_id) as conn:
        inv = await _lock(conn, tenant_id, invoice_id)
        if inv is None:
            raise InvoiceNotFound("no such invoice")
        existing = await conn.fetchrow("select * from billing_invoice_refund where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        if existing is not None:
            if existing["request_hash"] != req:
                raise IdempotencyConflict(key)
            fresh = await conn.fetchrow("select * from billing_invoice where id = $1::uuid", invoice_id)
            return RefundResult("refunded" if existing["method"] == "wallet" else "requested", str(existing["id"]), existing["amount_minor"], existing["provider_refund_id"], True, _view(fresh))
        if inv["status"] != "paid":
            raise InvoiceStateError("invoice is %s, so it cannot be refunded" % inv["status"])
        asked = await conn.fetchval("select coalesce(sum(amount_minor), 0) from billing_invoice_refund where invoice_id = $1::uuid", invoice_id)
        if amount_minor > inv["total_minor"] - asked:
            raise InvoiceStateError("%d is more than the %d still refundable on this invoice" % (amount_minor, inv["total_minor"] - asked))
        if inv["pay_method"] == "wallet":
            refund_id = str((await conn.fetchrow(
                """insert into billing_invoice_refund (tenant_id, invoice_id, clinic_id, amount_minor, method, idempotency_key, request_hash, created_by)
                   values ($1::uuid, $2::uuid, $3::uuid, $4, 'wallet', $5, $6, $7) returning id""", tenant_id, invoice_id, str(inv["clinic_id"]), amount_minor, key, req, actor))["id"])
            await wallet.refund_invoice(conn, tenant_id, invoice_id=invoice_id, amount_minor=amount_minor, currency=inv["currency"].strip(), to_wallet=True, ref=refund_id, actor=actor)
            total = inv["refunded_minor"] + amount_minor
            updated = await conn.fetchrow("update billing_invoice set refunded_minor = $2, updated_at = now() where id = $1::uuid returning *", invoice_id, total)
            await commissions.reverse(conn, tenant_id, updated, total)
            return RefundResult("refunded", refund_id, amount_minor, None, False, _view(updated))
        payment = await conn.fetchrow("select * from billing_payment where id = $1::uuid", str(inv["payment_id"]))
    if provider is None:
        raise BillingError("no payment provider is configured")
    if payment is None or not payment["provider_payment_id"]:
        raise BillingError("the invoice's payment has no provider id to refund")
    # the provider is called with no transaction open; the derived key makes a repeat of this call (after a crash, or a retry of the request) refund once
    prov = await provider.create_refund(payment_intent_id=payment["provider_payment_id"], amount_minor=amount_minor, idempotency_key=provider_key,
                                        metadata={"tenant_id": tenant_id, "invoice_id": invoice_id})
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow(
            """insert into billing_invoice_refund (tenant_id, invoice_id, clinic_id, amount_minor, method, idempotency_key, request_hash, provider_refund_id, created_by)
               values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9) on conflict (tenant_id, idempotency_key) do nothing returning *""",
            tenant_id, invoice_id, str(inv["clinic_id"]), amount_minor, inv["pay_method"], key, req, prov.id, actor)
        replayed = row is None
        if row is None:
            row = await conn.fetchrow("select * from billing_invoice_refund where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
        fresh = await conn.fetchrow("select * from billing_invoice where id = $1::uuid", invoice_id)
        return RefundResult("requested", str(row["id"]), amount_minor, row["provider_refund_id"], replayed, _view(fresh))


async def apply_provider_refund(conn: asyncpg.Connection, tenant_id: str, payment: asyncpg.Record, refunded_total_minor: int, at: datetime | None = None) -> tuple[str, str]:
    """A charge.refunded event for the payment of an invoice carries the TOTAL refunded so far. Book what is newly refunded and reverse the commission to match. A smaller or equal total
    (an older or repeated event) writes nothing; a total above the invoice total is held. Returns (status, detail): processed, ignored or held."""
    async with engine_actor(conn):
        inv = await conn.fetchrow("select * from billing_invoice where id = $1::uuid for update", str(payment["invoice_id"]))
        if inv["status"] != "paid":
            return "held", "the refund arrived before its invoice was paid"
        if refunded_total_minor > inv["total_minor"]:
            return "held", "refunded total %d is more than the invoice total %d" % (refunded_total_minor, inv["total_minor"])
        delta = refunded_total_minor - inv["refunded_minor"]
        if delta <= 0:
            return "ignored", "already reversed up to %d" % inv["refunded_minor"]
        await wallet.refund_invoice(conn, tenant_id, invoice_id=str(inv["id"]), amount_minor=delta, currency=inv["currency"].strip(), to_wallet=False, ref=str(refunded_total_minor),
                                    actor="payment provider", at=at)
        updated = await conn.fetchrow("update billing_invoice set refunded_minor = $2, updated_at = now() where id = $1::uuid returning *", str(inv["id"]), refunded_total_minor)
        await commissions.reverse(conn, tenant_id, updated, refunded_total_minor)
        return "processed", "reversed %d (total refunded %d)" % (delta, refunded_total_minor)
