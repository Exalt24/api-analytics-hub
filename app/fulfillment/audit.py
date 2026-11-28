"""The audit log of an order: one hash chained, append only row for every step, and the checks that prove the chain is intact.

THE CHAIN works as in app/esign/chain.py. Each row of an order holds the hash of the row before it (`prev_hash`; the first holds a genesis value tied to the tenant and the order) and its own
hash, which covers its position, kind, statuses, event id, time, actor, detail and `prev_hash`. Recomputing the chain shows an edited row (its hash no longer matches), a deleted row (a gap and a
broken link) and a reordered row. THE LIMIT is the one esign states: whoever can rewrite every later row and recompute every hash makes a chain that checks out, and a truncated tail leaves no
gap; the head hash, kept somewhere else, catches both.

WHAT A ROW MAY HOLD. A personal or health detail must not get into this log by accident, so the detail is not free text: `clean_detail` keeps only keys from DETAIL_KEYS, with values that are
short strings in a restricted alphabet, integers, booleans or null, and DROPS everything else. A partner that puts a name or an address in a status event, or a caller that passes one, finds
it gone before it is hashed or stored. (The rows are also checked by the database: detail is an object of at most 2000 characters.)

WHO MAY WRITE. `append` must be called with the order row locked (`select ... for update`), which is how the next number is made without a race; the policies of migration 024 let a role that may
update orders write rows, and the one that records the creation is also open to a role that may create them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import asyncpg

from app.esign.chain import canonical, fmt_time, sha256_hex

GENESIS_LABEL = "fulfillment-genesis-v1"
KINDS = ("created", "submit_attempt", "submitted", "dead_lettered", "requeued", "transition", "ignored", "refused", "verified")

#: the only keys a detail may carry, each with the one shape its text may have. Nothing here can hold a name, an address or a health detail: counts, codes in lower case and underscores, the
#: statuses of the state machine, a carrier and a tracking number in the shapes the table checks, an opaque partner id. A key outside this table is dropped, and so is a value that is not in
#: its key's shape (a name under `reason` is not a code), so neither a stray key nor a stray value gets into a hash or a row.
_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_STATUS = re.compile(r"^(" + "|".join(("created", "submitted", "accepted", "packed", "shipped", "delivered", "rejected", "cancelled")) + r")$")
DETAIL_TEXT = {
    "reason": _CODE, "result": _CODE, "current": _STATUS, "target": _STATUS,
    "carrier": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$"), "tracking_number": re.compile(r"^[A-Za-z0-9-]{6,40}$"), "partner_order_id": re.compile(r"^[A-Za-z0-9_-]{1,80}$"),
}
DETAIL_NUMBERS = ("attempts", "http_status", "problems", "occurred_at")
DETAIL_FLAGS = ("ok",)
DETAIL_KEYS = tuple(DETAIL_TEXT) + DETAIL_NUMBERS + DETAIL_FLAGS


def clean_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """The detail with every key outside DETAIL_KEYS dropped and every value that is not in its key's shape dropped too (null is kept for any key)."""
    out: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key in DETAIL_TEXT and (value is None or (isinstance(value, str) and DETAIL_TEXT[key].match(value))):
            out[key] = value
        elif key in DETAIL_NUMBERS and (value is None or (isinstance(value, int) and not isinstance(value, bool) and abs(value) < 10 ** 12)):
            out[key] = value
        elif key in DETAIL_FLAGS and (value is None or isinstance(value, bool)):
            out[key] = value
    return out


def genesis_hash(tenant_id: str, order_id: str) -> str:
    return sha256_hex(("%s:%s:%s" % (GENESIS_LABEL, tenant_id, order_id)).encode("utf-8"))


def entry_hash(*, tenant_id: str, order_id: str, seq: int, kind: str, from_status: str | None, to_status: str | None, event_id: str | None, at: datetime, actor: str,
               detail: dict, prev_hash: str) -> str:
    body = {"tenant_id": str(tenant_id), "order_id": str(order_id), "seq": seq, "kind": kind, "from_status": from_status, "to_status": to_status, "event_id": event_id,
            "at": fmt_time(at), "actor": actor, "detail": detail, "prev_hash": prev_hash}
    return sha256_hex(canonical(body).encode("utf-8"))


@dataclass(frozen=True)
class Row:
    seq: int
    kind: str
    from_status: str | None
    to_status: str | None
    event_id: str | None
    at: datetime
    actor: str
    detail: dict
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    seq: int | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "seq": self.seq}


def check_chain(tenant_id: str, order_id: str, rows: Sequence[Row]) -> list[Problem]:
    """Check rows given in the order they were inserted. Reports a gap, reordering, a broken link and an edited row, each with its number."""
    problems: list[Problem] = []
    seqs = [r.seq for r in rows]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        problems.append(Problem("rows_reordered", "audit rows are not in numbered order: %s" % seqs))
    expected = 1
    for r in sorted(rows, key=lambda x: x.seq):
        if r.seq > expected:
            problems.append(Problem("row_missing", "audit row(s) %s are missing before row %d" % (list(range(expected, r.seq)), r.seq), r.seq))
        expected = r.seq + 1
    prev = genesis_hash(tenant_id, order_id)
    for r in sorted(rows, key=lambda x: x.seq):
        if r.prev_hash != prev:
            problems.append(Problem("link_broken", "row %d does not link to the hash of the row before it" % r.seq, r.seq))
        again = entry_hash(tenant_id=tenant_id, order_id=order_id, seq=r.seq, kind=r.kind, from_status=r.from_status, to_status=r.to_status, event_id=r.event_id, at=r.at,
                           actor=r.actor, detail=r.detail, prev_hash=r.prev_hash)
        if again != r.entry_hash:
            problems.append(Problem("row_edited", "audit row %d (%s) was changed after it was written: its stored hash is not the hash of its content" % (r.seq, r.kind), r.seq))
        prev = r.entry_hash
    return problems


async def append(conn: asyncpg.Connection, tenant_id: str, order_id: str, clinic_id: str, kind: str, actor: str, *, from_status: str | None = None,
                 to_status: str | None = None, event_id: str | None = None, detail: Mapping[str, Any] | None = None) -> Row:
    """Write the next row of the order's chain. The caller holds the order row's lock."""
    if kind not in KINDS:
        raise ValueError("unknown audit kind %r" % kind)
    last = await conn.fetchrow("select seq, entry_hash from fulfillment_audit where order_id = $1::uuid order by seq desc limit 1", order_id)
    seq = last["seq"] + 1 if last else 1
    prev = last["entry_hash"] if last else genesis_hash(tenant_id, order_id)
    at = datetime.now(timezone.utc)
    clean = clean_detail(detail)
    digest = entry_hash(tenant_id=tenant_id, order_id=order_id, seq=seq, kind=kind, from_status=from_status, to_status=to_status, event_id=event_id, at=at, actor=actor, detail=clean,
                        prev_hash=prev)
    await conn.execute(
        """insert into fulfillment_audit (tenant_id, order_id, clinic_id, seq, kind, from_status, to_status, event_id, actor, at, detail, prev_hash, entry_hash)
           values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)""",
        tenant_id, order_id, clinic_id, seq, kind, from_status, to_status, event_id, actor, at, json.dumps(clean), prev, digest)
    return Row(seq, kind, from_status, to_status, event_id, at, actor, clean, prev, digest)


def row_from_record(rec: asyncpg.Record) -> Row:
    detail = rec["detail"]
    return Row(rec["seq"], rec["kind"], rec["from_status"], rec["to_status"], rec["event_id"], rec["at"], rec["actor"],
               json.loads(detail) if isinstance(detail, str) else dict(detail), rec["prev_hash"].strip(), rec["entry_hash"].strip())
