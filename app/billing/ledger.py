"""The ledger: accounts, one balanced transaction for each business event, and the lock that orders them.

SIGNS. An entry is a signed integer in minor units: a debit is positive and a credit negative, so the lines of a transaction add up to zero.
The wallet is a liability, so a wallet holding 5,000 has a balance of -5,000; `spendable` (minus the balance) is what the customer sees.

    top-up of 5,000          debit provider_clearing +5,000, credit wallet -5,000
    usage of 700             debit wallet +700,           credit platform_revenue -700
    refund of a top-up 2,000 debit wallet +2,000,         credit provider_clearing -2,000
    usage refund 300         debit refunds +300,          credit wallet -300
    adjustment (admin)       debit adjustments +n, credit wallet -n   (or the reverse), with a reason

WHAT THE DATABASE ENFORCES (migration 012), whatever this file does: the lines of a transaction sum to zero per currency and there are at least two
(a deferred constraint trigger), an account and its lines share a currency and a tenant (composite foreign keys), a wallet balance never goes
above zero (wallet_never_overdrawn), a balance can only move through the entry trigger, and entries and transactions are never updated or
deleted. This file keeps ordinary traffic away from those refusals: it takes the wallet row's lock first, so two commands for one tenant run one
after the other and the second sees the first's result. If the database still refuses a write, that is a bug here, and it is raised as
LedgerInvariantViolation instead of being turned into a polite answer.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import asyncpg

from .errors import IdempotencyConflict, LedgerInvariantViolation, UnbalancedTransaction

ACCOUNT_KINDS = ("wallet", "platform_revenue", "provider_clearing", "refunds", "adjustments")


@dataclass(frozen=True)
class Line:
    account: str            # one of ACCOUNT_KINDS
    amount_minor: int       # debit positive, credit negative


def request_hash(command: str, **params: Any) -> str:
    """A digest of what a command asked for, so the same key with different content can be told from a retry. Sorted keys and no spaces, so two
    callers building the same request get the same digest."""
    body = json.dumps({"command": command, **params}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def ensure_accounts(conn: asyncpg.Connection, tenant_id: str, currency: str) -> dict[str, str]:
    """The tenant's five accounts in this currency, created if they are missing (safe to run at the same time as another caller)."""
    await conn.execute(
        """insert into billing_account (tenant_id, kind, currency) select $1::uuid, k, $2 from unnest($3::text[]) as k
           on conflict (tenant_id, kind, currency) do nothing""",
        tenant_id, currency, list(ACCOUNT_KINDS))
    rows = await conn.fetch("select kind, id from billing_account where tenant_id = $1::uuid and currency = $2", tenant_id, currency)
    return {r["kind"]: str(r["id"]) for r in rows}


async def lock_wallet(conn: asyncpg.Connection, tenant_id: str, currency: str) -> tuple[dict[str, str], int]:
    """Create the accounts if needed, then take a transaction-scoped advisory lock on the wallet (held until the transaction ends) and read the balance
    AFTER it is held. Returns the account ids and what the customer can spend. Every command that moves money starts here, so the commands of one
    tenant and currency run one after the other and the second reads what the first committed. An advisory lock and not `select ... for update`,
    because FOR UPDATE needs an UPDATE grant on the account table and the request role has none (it must never be able to write a balance)."""
    accounts = await ensure_accounts(conn, tenant_id, currency)
    await conn.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", "billing-wallet:" + accounts["wallet"])
    balance = await conn.fetchval("select balance_minor from billing_account where id = $1::uuid", accounts["wallet"])
    return accounts, -balance


async def spendable(conn: asyncpg.Connection, tenant_id: str, currency: str) -> int:
    """What the customer can spend, without locking anything."""
    balance = await conn.fetchval("select balance_minor from billing_account where tenant_id = $1::uuid and kind = 'wallet' and currency = $2", tenant_id, currency)
    return -(balance or 0)


async def find_by_key(conn: asyncpg.Connection, tenant_id: str, key: str, req_hash: str) -> asyncpg.Record | None:
    """The transaction already written for this key. Same content: it is returned and the caller writes nothing. Different content: refused."""
    row = await conn.fetchrow("select * from billing_transaction where tenant_id = $1::uuid and idempotency_key = $2", tenant_id, key)
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        raise IdempotencyConflict(key)
    return row


async def find_by_provider_ref(conn: asyncpg.Connection, tenant_id: str, kind: str, provider_ref: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "select * from billing_transaction where tenant_id = $1::uuid and kind = $2 and provider_ref = $3", tenant_id, kind, provider_ref)


async def wallet_effect(conn: asyncpg.Connection, transaction_id: str) -> int:
    """How much the transaction added to what the customer can spend (negative for a debit of the wallet)."""
    total = await conn.fetchval(
        """select coalesce(sum(e.amount_minor), 0) from billing_entry e join billing_account a on a.id = e.account_id
            where e.transaction_id = $1::uuid and a.kind = 'wallet'""", transaction_id)
    return -int(total)


async def post(conn: asyncpg.Connection, tenant_id: str, *, kind: str, currency: str, lines: Sequence[Line], accounts: dict[str, str],
               key: str, req_hash: str, actor: str, reason: str | None = None, provider_ref: str | None = None, at: datetime | None = None) -> str:
    """Write one transaction and its lines. The caller holds the wallet lock (lock_wallet) and has already looked for a replay. `at` is when the event
    happened (a provider event's own time, or a backdated demo); the database's now() when omitted. The no-overdraft check runs against the balance when
    the entry is WRITTEN, not at `at`."""
    total = sum(line.amount_minor for line in lines)
    if len(lines) < 2 or total != 0:
        raise UnbalancedTransaction("%s: %d line(s) summing to %d" % (kind, len(lines), total))
    tx_id = str(uuid.uuid4())
    try:
        await conn.execute(
            """insert into billing_transaction (id, tenant_id, kind, currency, idempotency_key, request_hash, provider_ref, reason, actor, occurred_at)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, coalesce($10::timestamptz, now()))""",
            tx_id, tenant_id, kind, currency, key, req_hash, provider_ref, reason, actor, at)
        await conn.executemany(
            "insert into billing_entry (tenant_id, transaction_id, account_id, currency, amount_minor) values ($1::uuid, $2::uuid, $3::uuid, $4, $5)",
            [(tenant_id, tx_id, accounts[line.account], currency, line.amount_minor) for line in lines])
        # run the deferred balance check now, so a refusal surfaces here with its context, then put it back to deferred for the next post
        await conn.execute("set constraints billing_entry_balanced, billing_transaction_has_lines immediate")
        await conn.execute("set constraints billing_entry_balanced, billing_transaction_has_lines deferred")
    except (asyncpg.CheckViolationError, asyncpg.RestrictViolationError, asyncpg.UniqueViolationError, asyncpg.ForeignKeyViolationError) as exc:
        raise LedgerInvariantViolation("%s refused by the database: %s" % (kind, exc)) from exc
    return tx_id
