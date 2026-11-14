"""Signing requests: create one, show it, sign it, decline it, verify it, and write its certificate.

EVERY FUNCTION OPENS `db.tenant(tenant_id)`, so the database filters by tenant and nothing here writes a tenant WHERE clause for isolation. A request id that is not
the caller's tenant's is invisible, and `RequestNotFound` is raised for it exactly as for an id that never existed.

THE ORDER OF THE RULES when a request is signed (sign): an existing signature first (a retry gets the first result, any other attempt is refused), then declined,
then expired (which is written to the audit log and committed before the refusal is raised), then consent, the consent version, the typed name and the document hash.
The whole signing is one transaction: the consent row, the signature, the signed row and the status change land together or not at all. Two signers at the same moment
are ordered by an advisory lock on the request id, and the unique key on `esign_signature (request_id)` is the backstop under it.

TIME comes in as `now` (a timezone aware datetime) so a test can run a request past its expiry without waiting; the routes leave it unset.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import certificate
from .chain import Event, Problem, canonical, check_record, event_hash, fmt_time, genesis_hash, head_of, pii_hash, sha256_hex, signature_hash
from .consent import CURRENT_VERSION, consent_text, consent_text_sha256, names_match
from .errors import (AlreadySigned, CertificateRefused, ConsentRequired, ConsentVersionMismatch, DocumentChanged, InvalidRequest, NameMismatch, NameRequired, NotSigned,
                     RequestDeclined, RequestExpired, RequestNotFound)

log = logging.getLogger("hub")

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_LIFETIME = timedelta(days=365)
MAX_TYPED_NAME = 400
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

_COLUMNS = ("id, tenant_id, document_name, document_sha256, signer_name, signer_email, consent_version, status, created_by, created_at, expires_at, "
            "octet_length(document) as document_bytes")


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _uuid(value: Any) -> str:
    """A request id as a canonical uuid string. Text that is not a uuid is the same refusal as an id nobody has."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise RequestNotFound() from None


@dataclass(frozen=True)
class SignResult:
    request_id: str
    signature_id: str
    signed_at: datetime
    signature_hash: str
    document_sha256: str
    signer_name_typed: str
    replayed: bool

    def as_dict(self) -> dict:
        return {"request_id": self.request_id, "signature_id": self.signature_id, "signed_at": fmt_time(self.signed_at), "signature_hash": self.signature_hash,
                "document_sha256": self.document_sha256, "signer_name_typed": self.signer_name_typed, "replayed": self.replayed}


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    problems: list[Problem]
    events_checked: int
    document_sha256: str
    chain_head: str | None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "problems": [p.as_dict() for p in self.problems], "events_checked": self.events_checked, "document_sha256": self.document_sha256,
                "chain_head": self.chain_head}


# ---------------------------------------------------------------------------------------------------------------------------- helpers

async def _lock(conn: asyncpg.Connection, request_id: str) -> None:
    """Order every command for one request: the advisory lock lasts until the transaction ends."""
    await conn.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", "esign:" + request_id)


async def _load(conn: asyncpg.Connection, request_id: str, *, with_document: bool = False) -> asyncpg.Record:
    row = await conn.fetchrow("select %s%s from esign_request where id = $1::uuid" % (_COLUMNS, ", document" if with_document else ""), request_id)
    if row is None:
        raise RequestNotFound()
    return row


def _event_from_row(row: asyncpg.Record) -> Event:
    return Event(seq=row["seq"], kind=row["kind"], at=row["at"], actor=row["actor"], detail=json.loads(row["detail"]), prev_hash=row["prev_hash"], entry_hash=row["entry_hash"])


async def _events(conn: asyncpg.Connection, request_id: str) -> list[Event]:
    rows = await conn.fetch("select seq, kind, at, actor, detail::text as detail, prev_hash, entry_hash from esign_event where request_id = $1::uuid order by id", request_id)
    return [_event_from_row(r) for r in rows]


async def _append(conn: asyncpg.Connection, tenant_id: str, request_id: str, kind: str, actor: str, detail: dict, at: datetime) -> Event:
    """Add one row to the request's chain. The caller holds the request's lock, so the row before it is the last one."""
    last = await conn.fetchrow("select seq, entry_hash from esign_event where request_id = $1::uuid order by seq desc limit 1", request_id)
    seq = last["seq"] + 1 if last else 1
    prev = last["entry_hash"] if last else genesis_hash(tenant_id, request_id)
    digest = event_hash(tenant_id=tenant_id, request_id=request_id, seq=seq, kind=kind, at=at, actor=actor, detail=detail, prev_hash=prev)
    await conn.execute(
        "insert into esign_event (tenant_id, request_id, seq, kind, at, actor, detail, prev_hash, entry_hash) values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8, $9)",
        tenant_id, request_id, seq, kind, at, actor, canonical(detail), prev, digest)
    log.info("esign %s %s", kind, request_id, extra={"event": "esign", "tenant_id": tenant_id})
    return Event(seq=seq, kind=kind, at=at, actor=actor, detail=detail, prev_hash=prev, entry_hash=digest)


async def _expire_if_due(conn: asyncpg.Connection, req: asyncpg.Record, now: datetime) -> bool:
    """A pending request past its expiry gets an 'expired' row and the status moves. True when it did."""
    if req["status"] != "pending" or now < req["expires_at"]:
        return False
    rid, tenant = str(req["id"]), str(req["tenant_id"])
    await _append(conn, tenant, rid, "expired", "system", {"expires_at": fmt_time(req["expires_at"])}, now)
    await conn.execute("update esign_request set status = 'expired' where id = $1::uuid and status = 'pending'", rid)
    return True


def _view(req: asyncpg.Record, status: str, signature: asyncpg.Record | None) -> dict:
    return {
        "id": str(req["id"]), "document_name": req["document_name"], "document_sha256": req["document_sha256"], "document_bytes": req["document_bytes"],
        "signer_name": req["signer_name"], "signer_email": req["signer_email"], "status": status, "created_at": fmt_time(req["created_at"]),
        "expires_at": fmt_time(req["expires_at"]), "consent_version": req["consent_version"], "consent_text": consent_text(req["consent_version"]),
        "signature": None if signature is None else {"id": str(signature["id"]), "signed_at": fmt_time(signature["signed_at"]), "signature_hash": signature["signature_hash"],
                                                     "signer_name_typed": signature["signer_name_typed"]},
    }


def _result(sig: asyncpg.Record, replayed: bool) -> SignResult:
    return SignResult(str(sig["request_id"]), str(sig["id"]), sig["signed_at"], sig["signature_hash"], sig["document_sha256"], sig["signer_name_typed"], replayed)


# ---------------------------------------------------------------------------------------------------------------------------- the commands

async def create_request(db, tenant_id: str, *, actor: str, document: bytes, document_name: str, signer_name: str, signer_email: str, expires_at: datetime,
                         now: datetime | None = None) -> dict:
    """Store the document bytes and their SHA-256, name the signer, set the expiry, and write the 'created' row that pins all of it."""
    now = _now(now)
    if not document:
        raise InvalidRequest("the document is empty")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise InvalidRequest("the document is over %d bytes" % MAX_DOCUMENT_BYTES)
    if not document_name.strip() or len(document_name) > 200:
        raise InvalidRequest("the document name must be 1 to 200 characters")
    if not signer_name.strip() or len(signer_name) > 200:
        raise InvalidRequest("the signer's name must be 1 to 200 characters")
    if not EMAIL_RE.match(signer_email) or len(signer_email) > 254:
        raise InvalidRequest("the signer's email is not an address")
    if expires_at.tzinfo is None:
        raise InvalidRequest("the expiry needs a time zone")
    if expires_at <= now or expires_at - now > MAX_LIFETIME:
        raise InvalidRequest("the expiry must be in the future and within a year")
    digest = sha256_hex(document)
    rid = str(uuid.uuid4())
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            """insert into esign_request (id, tenant_id, document_name, document, document_sha256, signer_name, signer_email, consent_version, created_by, created_at, expires_at)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
            rid, tenant_id, document_name, document, digest, signer_name, signer_email, CURRENT_VERSION, actor, now, expires_at)
        await _append(conn, tenant_id, rid, "created", actor,
                      {"document_sha256": digest, "document_bytes": len(document), "signer_name_sha256": sha256_hex(signer_name.encode("utf-8")),
                       "signer_email_sha256": sha256_hex(signer_email.encode("utf-8")), "consent_version": CURRENT_VERSION, "expires_at": fmt_time(expires_at)}, now)
        req = await _load(conn, rid)
    return _view(req, "pending", None)


async def list_requests(db, tenant_id: str) -> list[dict]:
    """The tenant's requests, newest first, without the consent text and without a single audit row written."""
    async with db.tenant(tenant_id) as conn:
        rows = await conn.fetch("select id, document_name, document_sha256, signer_name, status, created_at, expires_at from esign_request order by created_at desc, id")
    return [{"id": str(r["id"]), "document_name": r["document_name"], "document_sha256": r["document_sha256"], "signer_name": r["signer_name"], "status": r["status"],
             "created_at": fmt_time(r["created_at"]), "expires_at": fmt_time(r["expires_at"])} for r in rows]


async def view_request(db, tenant_id: str, request_id: str, *, actor: str, role: str, now: datetime | None = None) -> dict:
    """What the client sees when the request is opened: the document's hash, the consent text, the status. Writes a 'viewed' row."""
    rid, now = _uuid(request_id), _now(now)
    async with db.tenant(tenant_id) as conn:
        await _lock(conn, rid)
        req = await _load(conn, rid)
        status = "expired" if await _expire_if_due(conn, req, now) else req["status"]
        await _append(conn, tenant_id, rid, "viewed", actor, {"what": "request", "role": role}, now)
        sig = await conn.fetchrow("select * from esign_signature where request_id = $1::uuid", rid)
    return _view(req, status, sig)


async def get_document(db, tenant_id: str, request_id: str, *, actor: str, role: str, now: datetime | None = None) -> tuple[str, bytes, str]:
    """The document's name, bytes and hash. The bytes must still hash to the recorded hash, or nothing is handed out."""
    rid, now = _uuid(request_id), _now(now)
    async with db.tenant(tenant_id) as conn:
        await _lock(conn, rid)
        req = await _load(conn, rid, with_document=True)
        data = bytes(req["document"])
        if sha256_hex(data) != req["document_sha256"]:
            raise DocumentChanged("the stored document no longer matches its recorded hash")
        await _append(conn, tenant_id, rid, "viewed", actor, {"what": "document", "role": role}, now)
    return req["document_name"], data, req["document_sha256"]


async def sign(db, tenant_id: str, request_id: str, *, actor: str, typed_name: str, consent: bool, consent_version: str, document_sha256: str, user_agent: str,
               client_addr: str, pii_key: bytes, now: datetime | None = None) -> SignResult:
    """Sign a request. See the module note for the order of the rules. A retry of an earlier success returns that first result with `replayed` true."""
    rid, now = _uuid(request_id), _now(now)
    seen = document_sha256.strip().lower()
    expired = False
    async with db.tenant(tenant_id) as conn:
        await _lock(conn, rid)
        req = await _load(conn, rid, with_document=True)
        existing = await conn.fetchrow("select * from esign_signature where request_id = $1::uuid", rid)
        if existing is not None:
            if consent is True and consent_version == existing["consent_version"] and seen == existing["document_sha256"] and names_match(typed_name, existing["signer_name_typed"]):
                return _result(existing, True)
            raise AlreadySigned("this request is already signed")
        if req["status"] == "declined":
            raise RequestDeclined("this request was declined")
        if req["status"] == "expired":
            raise RequestExpired("this request has expired")
        expired = await _expire_if_due(conn, req, now)
        if not expired:
            if consent is not True:
                raise ConsentRequired("tick the consent box to sign")
            if consent_version != req["consent_version"]:
                raise ConsentVersionMismatch("the consent text shown is not the one on this request")
            if not typed_name.strip():
                raise NameRequired("type your full name to sign")
            if len(typed_name) > MAX_TYPED_NAME or not names_match(typed_name, req["signer_name"]):
                raise NameMismatch("the typed name is not the signer's name")
            if sha256_hex(bytes(req["document"])) != req["document_sha256"] or seen != req["document_sha256"]:
                raise DocumentChanged("the document is not the one that was shown")
            digest, tenant = req["document_sha256"], str(req["tenant_id"])
            consent_row = await _append(conn, tenant, rid, "consented", actor, {"consent_version": req["consent_version"],
                                                                                 "consent_text_sha256": consent_text_sha256(req["consent_version"])}, now)
            sig_id = str(uuid.uuid4())
            ua, addr = pii_hash(pii_key, "user-agent", user_agent), pii_hash(pii_key, "client-address", client_addr)
            sig_hash = signature_hash(signature_id=sig_id, tenant_id=tenant, request_id=rid, document_sha256=digest, signer_name_typed=typed_name,
                                      consent_version=req["consent_version"], consent_text_sha256=consent_text_sha256(req["consent_version"]), signed_at=now,
                                      user_agent_hash=ua, client_addr_hash=addr, prev_hash=consent_row.entry_hash)
            try:
                await conn.execute(
                    """insert into esign_signature (id, tenant_id, request_id, document_sha256, signer_name_typed, consent_version, consent_text_sha256, signed_at,
                                                    user_agent_hash, client_addr_hash, prev_hash, signature_hash)
                       values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
                    sig_id, tenant, rid, digest, typed_name, req["consent_version"], consent_text_sha256(req["consent_version"]), now, ua, addr, consent_row.entry_hash, sig_hash)
            except asyncpg.UniqueViolationError:
                raise AlreadySigned("this request is already signed") from None
            await _append(conn, tenant, rid, "signed", actor, {"signature_id": sig_id, "signature_hash": sig_hash, "signed_at": fmt_time(now), "document_sha256": digest}, now)
            await conn.execute("update esign_request set status = 'signed' where id = $1::uuid and status = 'pending'", rid)
            return SignResult(rid, sig_id, now, sig_hash, digest, typed_name, False)
    if expired:
        raise RequestExpired("this request has expired")
    raise AssertionError("unreachable")


async def decline(db, tenant_id: str, request_id: str, *, actor: str, now: datetime | None = None) -> str:
    """The signer declines. Returns the status, 'declined'; asking again changes nothing. A signed request cannot be declined, an expired one is refused."""
    rid, now = _uuid(request_id), _now(now)
    expired = False
    async with db.tenant(tenant_id) as conn:
        await _lock(conn, rid)
        req = await _load(conn, rid)
        if req["status"] == "signed":
            raise AlreadySigned("this request is already signed")
        if req["status"] == "declined":
            return "declined"
        if req["status"] == "expired":
            raise RequestExpired("this request has expired")
        expired = await _expire_if_due(conn, req, now)
        if not expired:
            await _append(conn, tenant_id, rid, "declined", actor, {}, now)
            await conn.execute("update esign_request set status = 'declined' where id = $1::uuid and status = 'pending'", rid)
            return "declined"
    if expired:
        raise RequestExpired("this request has expired")
    raise AssertionError("unreachable")


async def expire_due(db, tenant_id: str, *, now: datetime | None = None) -> int:
    """Mark every pending request of the tenant that is past its expiry. Returns how many. Safe to run at any time and from a schedule."""
    now = _now(now)
    async with db.tenant(tenant_id) as conn:
        ids = [str(r["id"]) for r in await conn.fetch("select id from esign_request where status = 'pending' and expires_at <= $1", now)]
    count = 0
    for rid in ids:
        async with db.tenant(tenant_id) as conn:
            await _lock(conn, rid)
            req = await _load(conn, rid)
            if await _expire_if_due(conn, req, now):
                count += 1
    return count


async def _read_record(conn: asyncpg.Connection, rid: str) -> tuple[dict, list[Event], dict | None, bytes]:
    req = await _load(conn, rid, with_document=True)
    sig = await conn.fetchrow("select * from esign_signature where request_id = $1::uuid", rid)
    request = {k: req[k] for k in ("id", "tenant_id", "document_sha256", "signer_name", "signer_email", "consent_version", "status", "expires_at")}
    return request, await _events(conn, rid), (dict(sig) if sig is not None else None), bytes(req["document"])


async def verify(db, tenant_id: str, request_id: str, *, actor: str, now: datetime | None = None) -> VerifyReport:
    """Recompute the document hash and the whole chain, compare the request, signature and audit rows, and say exactly what changed. Writes a 'verified' row."""
    rid, now = _uuid(request_id), _now(now)
    async with db.tenant(tenant_id) as conn:
        await _lock(conn, rid)
        request, events, signature, document = await _read_record(conn, rid)
        problems = check_record(request, events, signature, document)
        head = head_of(events)
        await _append(conn, tenant_id, rid, "verified", actor, {"ok": not problems, "problems": sorted({p.code for p in problems}), "chain_head": head or ""}, now)
    return VerifyReport(not problems, problems, len(events), sha256_hex(document), head)


async def issue_certificate(db, tenant_id: str, request_id: str, *, actor: str, now: datetime | None = None) -> dict:
    """The completion certificate of a signed request. The record is verified first (a 'verified' row is written) and a record that does not verify gets none."""
    rid, now = _uuid(request_id), _now(now)
    report = await verify(db, tenant_id, rid, actor=actor, now=now)
    if not report.ok:
        raise CertificateRefused(report.problems)
    async with db.tenant(tenant_id) as conn:
        req = await _load(conn, rid)
        sig = await conn.fetchrow("select * from esign_signature where request_id = $1::uuid", rid)
        if sig is None:
            raise NotSigned("this request has no signature")
        events = await _events(conn, rid)
    signed = [e for e in events if e.kind == "signed"]
    upto = [e for e in events if e.seq <= signed[0].seq]
    return certificate.build(dict(req), dict(sig), upto, consent_text(sig["consent_version"]), now)
