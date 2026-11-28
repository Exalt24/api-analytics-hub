"""Sales rep commissions: a percentage of a paid invoice, in integer minor units, reversed when the invoice is refunded.

THE RATE is the commission_bp (basis points, 100 = 1%) of the sales_rep_assignment that covers the invoice's clinic, read once when the invoice is paid and stored on the accrual row, so
a later change of the rate never touches an invoice already paid. A clinic with no rep earns nobody anything.

THE ARITHMETIC is integer only: commission = basis * rate_bp // 10000, rounded down, so it is never more than the exact percentage and never a float. An ACCRUAL is the commission on the
whole total, written once when the invoice is paid (a unique index). A REVERSAL follows each refund: with R refunded so far the commission that should remain is the commission on
(total - R), so the reversal is that minus what is held now. It is worked from the cumulative refund and not from each refund's own share, so rounding cannot drift: after any run of
partial refunds the net is exactly the commission on what is still paid, and after a full refund it is zero. A repeat of the same refund (same refunded total) writes nothing.

WHO WRITES. The rows are written by the billing engine inside `engine_actor` (app/access/engine.py), never by a request: the access matrix gives a commission `create` and `refund` to an
admin only, and the policies of migration 023 check that for the database too. The table is append only, and the net of an invoice can never go below zero (a trigger).
"""
from __future__ import annotations

import asyncpg

from app.access.engine import engine_actor


def commission_for(basis_minor: int, rate_bp: int) -> int:
    """The commission on `basis_minor` at `rate_bp` basis points, rounded down. Pure integer arithmetic."""
    if basis_minor < 0 or not 0 <= rate_bp <= 10000:
        raise ValueError("a basis is not negative and a rate is 0 to 10000 basis points")
    return basis_minor * rate_bp // 10000


async def accrue(conn: asyncpg.Connection, tenant_id: str, invoice: asyncpg.Record) -> int | None:
    """The invoice has just been paid: write its commission for the clinic's rep. Returns the amount, or None when the clinic has no rep. Safe to call twice."""
    async with engine_actor(conn):
        assignment = await conn.fetchrow("select rep_user_id, commission_bp from sales_rep_assignment where tenant_id = $1::uuid and clinic_id = $2::uuid", tenant_id, invoice["clinic_id"])
        if assignment is None:
            return None
        amount = commission_for(invoice["total_minor"], assignment["commission_bp"])
        await conn.execute(
            """insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref)
               values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'accrual', $5, $6, $7, $8, 'accrual') on conflict do nothing""",
            tenant_id, invoice["id"], invoice["clinic_id"], assignment["rep_user_id"], amount, invoice["total_minor"], assignment["commission_bp"], invoice["currency"].strip())
        return amount


async def reverse(conn: asyncpg.Connection, tenant_id: str, invoice: asyncpg.Record, refunded_total_minor: int) -> int:
    """`refunded_total_minor` is what has been refunded on the invoice so far. Reduce the commission to the commission on what is still paid. Returns the amount reversed (a
    negative number, or 0 when there was no commission or nothing to take back)."""
    async with engine_actor(conn):
        accrual = await conn.fetchrow("select rep_user_id, rate_bp from commission where tenant_id = $1::uuid and invoice_id = $2::uuid and kind = 'accrual'", tenant_id, invoice["id"])
        if accrual is None:
            return 0
        held = await conn.fetchval("select coalesce(sum(amount_minor), 0) from commission where tenant_id = $1::uuid and invoice_id = $2::uuid", tenant_id, invoice["id"])
        delta = commission_for(invoice["total_minor"] - refunded_total_minor, accrual["rate_bp"]) - held
        if delta >= 0:
            return 0
        await conn.execute(
            """insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref)
               values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'reversal', $5, $6, $7, $8, $9) on conflict do nothing""",
            tenant_id, invoice["id"], invoice["clinic_id"], accrual["rep_user_id"], delta, refunded_total_minor, accrual["rate_bp"], invoice["currency"].strip(),
            "refund:%d" % refunded_total_minor)
        return delta


async def list_commissions(conn: asyncpg.Connection, *, invoice_id: str | None = None) -> list[dict]:
    """The commission rows the CALLER may see (the policies filter them: a rep sees only their own). Newest last."""
    rows = await conn.fetch(
        "select id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref, created_at from commission "
        "where ($1::uuid is null or invoice_id = $1::uuid) order by created_at, id", invoice_id)
    return [{**dict(r), "id": str(r["id"]), "invoice_id": str(r["invoice_id"]), "clinic_id": str(r["clinic_id"]), "rep_user_id": str(r["rep_user_id"]),
             "currency": r["currency"].strip()} for r in rows]
