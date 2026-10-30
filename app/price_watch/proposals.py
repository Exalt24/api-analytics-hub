"""The review queue: proposed price changes, and the only ways a proposal may change state.

    proposed -> approved | rejected          a person decides (approve, reject)
    approved -> applied | failed             the sync finishes (sync.py)
    failed   -> applied | failed             the sync is run again; only the failed shop is tried again

Every move goes through `move`, which locks the row, refuses a move the table above does not list, changes the state and writes
its audit row (who, when, from, to, why) in the same transaction. So a double approval is an error and writes nothing, two people
approving at the same moment cannot both succeed (the second waits for the lock, then finds the state already moved), and
there is no state change without an audit row. The request role can read and append to the audit table and cannot change or
delete a row of it (migration 011), so the trail cannot be edited from here either.

Callers pass an asyncpg connection from Database.tenant(...), so row level security scopes everything below to one tenant.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import asyncpg

from .report import ReportLine

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "rejected"}),
    "approved": frozenset({"applied", "failed"}),
    "failed": frozenset({"applied", "failed"}),
    "rejected": frozenset(),
    "applied": frozenset(),
}


class InvalidTransition(Exception):
    pass


class ProposalNotFound(Exception):
    pass


async def create_proposal(conn: asyncpg.Connection, tenant_id: str, line: ReportLine, report_date: date, *, actor: str = "price-watch report") -> str | None:
    """Queue the proposal for a report line. Returns its id, or None when the product already has an open one (proposed, approved or
    failed): a second report must not stack a new price on top of one a person is reviewing."""
    if line.proposed_price_minor is None:
        return None
    product = await conn.fetchrow("select shopify_variant_gid, zoho_item_id from catalogue_product where id = $1::uuid", line.product_id)
    if product is None:
        return None
    pid = uuid.uuid4()
    evidence = {"competitor_url": line.url, "competitor_price_minor": line.comp_price_minor, "competitor_shipping_minor": line.comp_shipping_minor,
                "our_landed_minor": line.our_landed_minor, "floor_limited": line.floor_limited, "note": line.note}
    row = await conn.fetchrow(
        """insert into price_change_proposal
               (id, tenant_id, catalogue_product_id, report_date, currency, current_price_minor, proposed_price_minor, floor_minor,
                competitor, competitor_landed_minor, shipping_source, evidence, idempotency_key, shopify_status, zoho_status)
           values ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15)
           on conflict do nothing
           returning id""",
        pid, tenant_id, line.product_id, report_date, line.currency, line.our_price_minor, line.proposed_price_minor, line.floor_minor,
        line.competitor, line.comp_landed_minor, line.shipping_source, json.dumps(evidence), "price-watch:%s" % pid,
        "pending" if product["shopify_variant_gid"] else "skipped", "pending" if product["zoho_item_id"] else "skipped")
    if row is None:
        return None
    await conn.execute(
        "insert into price_change_audit (tenant_id, proposal_id, from_state, to_state, actor, note) values ($1::uuid, $2, null, 'proposed', $3, $4)",
        tenant_id, pid, actor, "from the %s report" % report_date)
    return str(pid)


async def move(conn: asyncpg.Connection, tenant_id: str, proposal_id: str, to_state: str, actor: str, note: str | None = None,
               *, decision: bool = False, **columns: Any) -> asyncpg.Record:
    """Change a proposal's state and audit it. `decision` marks an approve or reject, which records who decided and when;
    `columns` are other proposal columns to set in the same update (the sync's per shop statuses)."""
    if not actor or not actor.strip():
        raise ValueError("an actor is required: every state change is attributed")
    if to_state not in TRANSITIONS:
        raise ValueError("unknown state %r" % to_state)
    row = await conn.fetchrow("select state from price_change_proposal where id = $1::uuid for update", proposal_id)
    if row is None:
        raise ProposalNotFound(proposal_id)
    current = row["state"]
    if to_state not in TRANSITIONS[current]:
        raise InvalidTransition("proposal %s is %s and cannot go to %s" % (proposal_id, current, to_state))
    sets = ["state = $2", "updated_at = now()"]
    args: list[Any] = [proposal_id, to_state]
    if decision:
        sets += ["decided_by = $%d" % (len(args) + 1), "decided_at = now()", "decision_note = $%d" % (len(args) + 2)]
        args += [actor, note]
    for name, value in columns.items():
        if name not in _MUTABLE:
            raise ValueError("column %r may not be set through move()" % name)
        args.append(value)
        sets.append("%s = $%d" % (name, len(args)))
    updated = await conn.fetchrow("update price_change_proposal set %s where id = $1::uuid returning *" % ", ".join(sets), *args)
    await conn.execute(
        "insert into price_change_audit (tenant_id, proposal_id, from_state, to_state, actor, note) values ($1::uuid, $2::uuid, $3, $4, $5, $6)",
        tenant_id, proposal_id, current, to_state, actor.strip(), note)
    return updated


_MUTABLE = frozenset({"shopify_status", "shopify_error", "zoho_status", "zoho_error", "sync_attempts"})


async def approve(conn: asyncpg.Connection, tenant_id: str, proposal_id: str, actor: str, note: str | None = None) -> asyncpg.Record:
    return await move(conn, tenant_id, proposal_id, "approved", actor, note, decision=True)


async def reject(conn: asyncpg.Connection, tenant_id: str, proposal_id: str, actor: str, note: str | None = None) -> asyncpg.Record:
    return await move(conn, tenant_id, proposal_id, "rejected", actor, note, decision=True)


async def list_proposals(conn: asyncpg.Connection, state: str | None = None) -> list[asyncpg.Record]:
    return await conn.fetch(
        """select p.*, c.sku, c.title from price_change_proposal p join catalogue_product c on c.id = p.catalogue_product_id
            where ($1::text is null or p.state = $1) order by p.created_at, p.id""", state)


async def audit_trail(conn: asyncpg.Connection, proposal_id: str) -> list[asyncpg.Record]:
    return await conn.fetch("select from_state, to_state, actor, at, note from price_change_audit where proposal_id = $1::uuid order by id", proposal_id)
