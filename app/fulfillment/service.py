"""Orders sent to a fulfilment partner: create one, send it (with retries and a dead letter), apply the partner's status events, and verify the audit chain.

WHAT AN ORDER IS HERE. An opaque reference (`ord_` and 24 random hex digits), the clinic it belongs to, a status, and once shipped a carrier and a tracking number. NOTHING ELSE: no patient, no
address, no drug. The partner is given the reference and the clinic's short code and matches them to the prescription through the clinic's own secure channel, outside this system. No function in
this package takes a free-text field about the order, so a personal or health detail has no way in; the audit detail is whitelisted as well (audit.clean_detail).

THE FLOW of a send (submit_order): a short transaction reads the order and its submission row (the Idempotency-Key is made once, at creation, so every attempt and every later retry of the
request carries the same one); the partner is called with NO transaction open (a slow network call holds no lock); a second short transaction records the answer. A 2xx moves the order to
`submitted`. A permanent refusal, or retries that ran out, write a dead letter row (a reason CODE, the HTTP status, the attempts) and leave the order `created`; `requeue` puts it back to be sent
again with the same key. Two people pressing send at once both call the partner with the same key, the partner makes one order, and the second finds the first's result.

THE FLOW of a partner event (apply_event): one transaction takes the order's row lock, looks the event id up (a second delivery of an id changes nothing and writes nothing), asks
states.decide what the event is against the order's current status, and either applies it (status, carrier, tracking number, one audit row), ignores it (recorded, nothing moves) or refuses it
(audited, nothing moves). Every outcome other than a repeat writes an audit row, in the same transaction as the change.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.access.engine import SYSTEM_USER_ID, ActingDatabase
from app.db import Database

from . import audit, states
from .client import PartnerClient
from .errors import EventRejected, InvalidOrder, OrderNotFound, OrderStateError, PartnerRejected, PartnerUnavailable

log = logging.getLogger("fulfillment")

_CARRIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,39}$")
_TRACKING = re.compile(r"^[A-Za-z0-9-]{6,40}$")
_PARTNER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class PartnerEvent:
    """A partner status event reduced to the fields this system keeps. Anything else the partner sent is not read into this object, so it cannot be stored or logged."""
    event_id: str
    order_ref: str
    status: str
    carrier: str | None = None
    tracking_number: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    """`status` is submitted (sent now, or already sent: `replayed` says which) or dead_letter (could not be sent; `reason` says why, and the order is still `created`)."""
    status: str
    attempts: int
    replayed: bool
    order: dict
    reason: str | None = None


@dataclass(frozen=True)
class EventResult:
    """`status` is applied, ignored, refused or duplicate (this event id was seen before: nothing was written)."""
    status: str
    order_id: str
    order_status: str
    detail: str


def system_db(db: Database) -> ActingDatabase:
    """The Database opened as the engine itself (admin, the system user): for a partner event, which has no portal user behind it."""
    return ActingDatabase(db, "admin", SYSTEM_USER_ID)


def _view(row: asyncpg.Record) -> dict:
    return {"id": str(row["id"]), "order_ref": row["order_ref"], "clinic_id": str(row["clinic_id"]), "status": row["status"], "carrier": row["carrier"],
            "tracking_number": row["tracking_number"], "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}


async def _lock(conn: asyncpg.Connection, tenant_id: str, order_id: str) -> asyncpg.Record | None:
    try:
        return await conn.fetchrow("select * from fulfillment_order where tenant_id = $1::uuid and id = $2::uuid for update", tenant_id, order_id)
    except asyncpg.DataError:        # an id that is not a uuid is the same answer as one that does not exist
        return None


# ----------------------------------------------------------------------------------------------------- create and read

async def create_order(db: Database, tenant_id: str, *, clinic_id: str, actor: str) -> dict:
    """A new order for a clinic. The reference is made here, from random bytes; there is no other input."""
    ref = "ord_" + secrets.token_hex(12)
    async with db.tenant(tenant_id) as conn:
        try:
            row = await conn.fetchrow("insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by) values ($1::uuid, $2::uuid, $3, $4) returning *",
                                      tenant_id, str(clinic_id), ref, actor)
        except asyncpg.ForeignKeyViolationError as exc:
            raise InvalidOrder("no such clinic") from exc
        await conn.execute("insert into fulfillment_submission (tenant_id, order_id, clinic_id, idempotency_key) values ($1::uuid, $2::uuid, $3::uuid, $4)",
                           tenant_id, str(row["id"]), str(clinic_id), "sub-%s" % uuid.uuid4())
        await audit.append(conn, tenant_id, str(row["id"]), str(clinic_id), "created", actor, to_status="created")
        return _view(row)


async def get_order(db: Database, tenant_id: str, order_id: str) -> dict:
    """One order with its audit rows, as the CALLER may see it: another clinic's order answers OrderNotFound, like one that does not exist."""
    async with db.tenant(tenant_id) as conn:
        try:
            row = await conn.fetchrow("select * from fulfillment_order where tenant_id = $1::uuid and id = $2::uuid", tenant_id, order_id)
        except asyncpg.DataError:
            row = None
        if row is None:
            raise OrderNotFound("no such order")
        rows = await conn.fetch("select * from fulfillment_audit where order_id = $1::uuid order by seq", order_id)
        return {**_view(row), "audit": [{"seq": r["seq"], "kind": r["kind"], "from_status": r["from_status"], "to_status": r["to_status"], "event_id": r["event_id"],
                                         "actor": r["actor"], "at": r["at"].isoformat(), "detail": json.loads(r["detail"]) if isinstance(r["detail"], str) else dict(r["detail"])}
                                        for r in rows]}


async def list_orders(db: Database, tenant_id: str) -> list[dict]:
    async with db.tenant(tenant_id) as conn:
        return [_view(r) for r in await conn.fetch("select * from fulfillment_order where tenant_id = $1::uuid order by created_at, id", tenant_id)]


async def list_dead_letters(db: Database, tenant_id: str, *, open_only: bool = True) -> list[dict]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.fetch("select d.*, o.order_ref from fulfillment_dead_letter d join fulfillment_order o on o.id = d.order_id "
                                "where d.tenant_id = $1::uuid and (not $2 or d.requeued_at is null) order by d.created_at, d.id", tenant_id, open_only)
        return [{"id": str(r["id"]), "order_id": str(r["order_id"]), "order_ref": r["order_ref"], "reason_code": r["reason_code"], "http_status": r["http_status"],
                 "attempts": r["attempts"], "requeued": r["requeued_at"] is not None} for r in rows]


# ----------------------------------------------------------------------------------------------------- sending

async def cancel_order(db: Database, tenant_id: str, order_id: str, *, actor: str) -> dict:
    """created -> cancelled, before the partner has it. An order the partner already has is cancelled by the partner's own event, not here. Cancelling a cancelled order returns it."""
    async with db.tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order is None:
            raise OrderNotFound("no such order")
        if order["status"] == "cancelled":
            return _view(order)
        if order["status"] != "created":
            raise OrderStateError("order is %s, so only the partner can cancel it" % order["status"])
        updated = await conn.fetchrow("update fulfillment_order set status = 'cancelled', updated_at = now() where id = $1::uuid returning *", order_id)
        await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "transition", actor, from_status="created", to_status="cancelled")
        return _view(updated)


async def submit_order(db: Database, client: PartnerClient, tenant_id: str, order_id: str, *, actor: str) -> SubmitResult:
    """Send an order to the partner. Safe to call again: an order already sent returns as it is, and a send repeated after a crash carries the same Idempotency-Key."""
    async with db.tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order is None:
            raise OrderNotFound("no such order")
        if order["status"] != "created":
            if order["status"] == "cancelled":
                raise OrderStateError("order is cancelled, so it is not sent")
            return SubmitResult("submitted", 0, True, _view(order))
        sub = await conn.fetchrow("select * from fulfillment_submission where order_id = $1::uuid for update", order_id)
        if sub["state"] == "dead":
            raise OrderStateError("the order is in the dead letter; requeue it before sending again")
        clinic_code = await conn.fetchval("select code from clinic where id = $1::uuid", str(order["clinic_id"]))
    try:
        receipt = await client.submit_order(order_ref=order["order_ref"], clinic_ref=clinic_code, idempotency_key=sub["idempotency_key"])
    except (PartnerRejected, PartnerUnavailable) as exc:
        reason = "permanent_rejection" if isinstance(exc, PartnerRejected) else "retries_exhausted"
        status = exc.status
        if isinstance(exc, PartnerUnavailable) and exc.status is None:
            reason = "unreachable"
        code = exc.code if isinstance(exc, PartnerRejected) else ("no_answer" if exc.status is None else "http_%d" % exc.status)
        async with db.tenant(tenant_id) as conn:
            order = await _lock(conn, tenant_id, order_id)
            if order["status"] == "created":
                await conn.execute("update fulfillment_submission set state = 'dead', attempts = attempts + $2, last_error_code = $3, updated_at = now() where order_id = $1::uuid",
                                   order_id, exc.attempts, code if re.match(r"^[a-z0-9_]{1,40}$", code) else "error")
                await conn.execute("insert into fulfillment_dead_letter (tenant_id, order_id, clinic_id, reason_code, http_status, attempts) values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6)",
                                   tenant_id, order_id, str(order["clinic_id"]), reason, status, exc.attempts)
                await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "submit_attempt", actor, detail={"attempts": exc.attempts, "http_status": status, "reason": reason})
                await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "dead_lettered", actor, detail={"reason": reason, "attempts": exc.attempts})
            return SubmitResult("dead_letter", exc.attempts, False, _view(order), reason)
    async with db.tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order["status"] != "created":                       # a second sender, or the partner's own event, got here first
            return SubmitResult("submitted", receipt.attempts, True, _view(order))
        updated = await conn.fetchrow("update fulfillment_order set status = 'submitted', partner_order_id = $2, updated_at = now() where id = $1::uuid returning *",
                                      order_id, receipt.partner_order_id)
        await conn.execute("update fulfillment_submission set state = 'sent', attempts = attempts + $2, last_error_code = null, updated_at = now() where order_id = $1::uuid",
                           order_id, receipt.attempts)
        await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "submit_attempt", actor, detail={"attempts": receipt.attempts, "result": "sent"})
        await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "submitted", actor, from_status="created", to_status="submitted",
                           detail={"partner_order_id": receipt.partner_order_id})
        return SubmitResult("submitted", receipt.attempts, False, _view(updated))


async def requeue(db: Database, tenant_id: str, order_id: str, *, actor: str) -> dict:
    """Put a dead lettered order back to be sent again (with the same Idempotency-Key: the partner may already hold it). The dead letter row stays as the record."""
    async with db.tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order is None:
            raise OrderNotFound("no such order")
        sub = await conn.fetchrow("select * from fulfillment_submission where order_id = $1::uuid for update", order_id)
        if order["status"] != "created" or sub["state"] != "dead":
            raise OrderStateError("only an order in the dead letter can be requeued")
        await conn.execute("update fulfillment_submission set state = 'pending', updated_at = now() where order_id = $1::uuid", order_id)
        await conn.execute("update fulfillment_dead_letter set requeued_at = now() where order_id = $1::uuid and requeued_at is null", order_id)
        await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "requeued", actor)
        return _view(order)


# ----------------------------------------------------------------------------------------------------- the partner's events

def check_event(event: PartnerEvent) -> None:
    """A status event that names a status the partner may report, and for shipped a carrier and a tracking number of the right shape. Raises EventRejected otherwise."""
    if event.status not in states.PARTNER_STATUSES:
        raise EventRejected("unknown status")
    if event.status == "shipped" and not (event.carrier and event.tracking_number and _CARRIER.match(event.carrier) and _TRACKING.match(event.tracking_number)):
        raise EventRejected("shipped needs a carrier and a tracking number")
    if event.status != "shipped" and (event.carrier or event.tracking_number):
        raise EventRejected("a carrier and a tracking number belong to the shipped event only")


async def apply_event(db: Database, tenant_id: str, order_id: str, event: PartnerEvent) -> EventResult:
    """Apply one verified partner event to its order, as the system user. See the module doc for the three outcomes and the repeat."""
    check_event(event)
    actor = "partner"
    async with system_db(db).tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order is None:
            raise OrderNotFound("no such order")
        clinic = str(order["clinic_id"])
        seen = await conn.fetchrow("select result from fulfillment_event where tenant_id = $1::uuid and partner_event_id = $2", tenant_id, event.event_id)
        if seen is not None:
            return EventResult("duplicate", order_id, order["status"], "this event id was already %s" % seen["result"])
        verdict = states.decide(order["status"], event.status)
        if verdict == "refuse":
            await audit.append(conn, tenant_id, order_id, clinic, "refused", actor, from_status=order["status"], to_status=event.status, event_id=event.event_id,
                               detail={"reason": "not_legal_now", "current": order["status"], "target": event.status})
            return EventResult("refused", order_id, order["status"], "a %s event is not legal for an order that is %s" % (event.status, order["status"]))
        await conn.execute("insert into fulfillment_event (tenant_id, order_id, clinic_id, partner_event_id, event_status, result) values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6)",
                           tenant_id, order_id, clinic, event.event_id, event.status, "applied" if verdict == "apply" else "ignored")
        if verdict == "ignore":
            await audit.append(conn, tenant_id, order_id, clinic, "ignored", actor, from_status=order["status"], to_status=event.status, event_id=event.event_id,
                               detail={"reason": "nothing_new" if event.status == order["status"] else "older_than_current", "current": order["status"], "target": event.status})
            return EventResult("ignored", order_id, order["status"], "nothing new: the order is already %s" % order["status"])
        updated = await conn.fetchrow(
            "update fulfillment_order set status = $2, carrier = coalesce($3, carrier), tracking_number = coalesce($4, tracking_number), updated_at = now() where id = $1::uuid returning *",
            order_id, event.status, event.carrier, event.tracking_number)
        await audit.append(conn, tenant_id, order_id, clinic, "transition", actor, from_status=order["status"], to_status=event.status, event_id=event.event_id,
                           detail={"carrier": event.carrier, "tracking_number": event.tracking_number})
        return EventResult("applied", order_id, updated["status"], "moved from %s to %s" % (order["status"], updated["status"]))


# ----------------------------------------------------------------------------------------------------- the proof

async def verify(db: Database, tenant_id: str, order_id: str, *, actor: str) -> list[audit.Problem]:
    """Recompute the order's audit chain and compare it with the order itself: an edited, deleted or reordered row, a status the log does not explain, a shipped order with no tracking number.
    Returns the problems (empty is the proof)."""
    async with db.tenant(tenant_id) as conn:
        order = await _lock(conn, tenant_id, order_id)
        if order is None:
            raise OrderNotFound("no such order")
        rows = [audit.row_from_record(r) for r in await conn.fetch("select * from fulfillment_audit where order_id = $1::uuid order by id", order_id)]
        problems = audit.check_chain(tenant_id, order_id, rows)
        moves = [r for r in rows if r.kind in ("created", "transition", "submitted")]
        last = max(moves, key=lambda r: r.seq).to_status if moves else None
        if last != order["status"]:
            problems.append(audit.Problem("status_unexplained", "the order is %s but the log's last move ended in %s" % (order["status"], last)))
        if order["status"] in ("shipped", "delivered") and not (order["carrier"] and order["tracking_number"]):
            problems.append(audit.Problem("tracking_missing", "a shipped order has no carrier or tracking number"))
        await audit.append(conn, tenant_id, order_id, str(order["clinic_id"]), "verified", actor, detail={"ok": not problems, "problems": len(problems)})
        return problems
