"""The hash chain and everything that checks it. Pure functions: no database and no clock, so the tests (and a third party holding a certificate) run it anywhere.

THE CHAIN. Each audit row of a request holds the hash of the row before it (`prev_hash`; the first row holds a genesis value tied to the tenant and the request)
and its own hash, which covers its position, kind, time, actor, detail and `prev_hash`. Recomputing the chain from the stored rows shows an edited row (its
own hash no longer matches), a deleted row (a gap in the numbers and a broken link), and a reordered row (numbers out of order). THE LIMIT: whoever can rewrite
every later row and recompute every hash makes a chain that checks out, and a truncated tail leaves no gap. The head hash in a certificate, kept somewhere else,
is what catches both.

THE SIGNATURE HASH covers every field of the signature record and the hash of the row before the signed row, so the record cannot be edited without the
signed row's detail (which holds the same hash) disagreeing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

KINDS = ("created", "viewed", "consented", "signed", "expired", "declined", "verified")
GENESIS_LABEL = "esign-genesis-v1"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj: Any) -> str:
    """Sorted keys, no spaces, ASCII only: the same value always gives the same bytes, however it was built or read back."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fmt_time(at: datetime) -> str:
    """One text form for a time, in UTC to the microsecond, so a time read back from the database hashes as it did when written."""
    if at.tzinfo is None:
        raise ValueError("a time without a zone cannot be hashed")
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pii_hash(key: bytes, label: str, value: str) -> str:
    """HMAC-SHA-256 of a personal value under a deployment key, with a label so the same text hashes differently as an address and as a user agent.
    A plain SHA-256 of an address is guessable (there are only so many addresses); the key is what stops that."""
    if not key:
        raise ValueError("a hashing key is required")
    return hmac.new(key, (label + "\x00" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def genesis_hash(tenant_id: str, request_id: str) -> str:
    return sha256_hex(("%s:%s:%s" % (GENESIS_LABEL, tenant_id, request_id)).encode("utf-8"))


def event_hash(*, tenant_id: str, request_id: str, seq: int, kind: str, at: datetime, actor: str, detail: dict, prev_hash: str) -> str:
    body = {"tenant_id": str(tenant_id), "request_id": str(request_id), "seq": seq, "kind": kind, "at": fmt_time(at), "actor": actor,
            "detail": detail, "prev_hash": prev_hash}
    return sha256_hex(canonical(body).encode("utf-8"))


def signature_hash(*, signature_id: str, tenant_id: str, request_id: str, document_sha256: str, signer_name_typed: str, consent_version: str,
                   consent_text_sha256: str, signed_at: datetime, user_agent_hash: str, client_addr_hash: str, prev_hash: str) -> str:
    body = {"signature_id": str(signature_id), "tenant_id": str(tenant_id), "request_id": str(request_id), "document_sha256": document_sha256,
            "signer_name_typed": signer_name_typed, "consent_version": consent_version, "consent_text_sha256": consent_text_sha256,
            "signed_at": fmt_time(signed_at), "user_agent_hash": user_agent_hash, "client_addr_hash": client_addr_hash, "prev_hash": prev_hash}
    return sha256_hex(canonical(body).encode("utf-8"))


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    at: datetime
    actor: str
    detail: dict
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class Problem:
    """One thing that does not check out. `code` is stable (tests and callers match on it); `message` says what changed."""
    code: str
    message: str
    seq: int | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "seq": self.seq}


def check_chain(tenant_id: str, request_id: str, events: Sequence[Event]) -> list[Problem]:
    """Check rows given in the order they were inserted. Reports a gap, reordering, a broken link and an edited row, each with its number."""
    problems: list[Problem] = []
    seqs = [e.seq for e in events]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        problems.append(Problem("rows_reordered", "audit rows are not in numbered order: %s" % seqs))
    ordered = sorted(events, key=lambda e: e.seq)
    expected = 1
    for e in ordered:
        if e.seq > expected:
            gap = list(range(expected, e.seq))
            problems.append(Problem("row_missing", "audit row(s) %s are missing before row %d" % (gap, e.seq), e.seq))
        expected = e.seq + 1
    prev = genesis_hash(tenant_id, request_id)
    first = True
    for e in ordered:
        if e.prev_hash != prev:
            what = "the first row does not start from this request's genesis value" if first else "row %d does not link to the hash of the row before it" % e.seq
            problems.append(Problem("link_broken", what, e.seq))
        recomputed = event_hash(tenant_id=tenant_id, request_id=request_id, seq=e.seq, kind=e.kind, at=e.at, actor=e.actor, detail=e.detail, prev_hash=e.prev_hash)
        if recomputed != e.entry_hash:
            problems.append(Problem("row_edited", "audit row %d (%s) was changed after it was written: its stored hash is not the hash of its content" % (e.seq, e.kind), e.seq))
        prev = e.entry_hash
        first = False
    return problems


def head_of(events: Iterable[Event]) -> str | None:
    """The hash of the row with the highest number, or None for no rows."""
    last = None
    for e in events:
        if last is None or e.seq > last.seq:
            last = e
    return last.entry_hash if last else None


def check_record(request: dict, events: Sequence[Event], signature: dict | None, document: bytes) -> list[Problem]:
    """Everything verify() reports. Recomputes the document hash and the chain, then compares the request, the signature and the audit rows with each other.

    `request` and `signature` are the stored columns as plain values; `events` are in the order they were inserted."""
    tenant, rid = str(request["tenant_id"]), str(request["id"])
    problems = check_chain(tenant, rid, events)
    by_kind: dict[str, list[Event]] = {}
    for e in sorted(events, key=lambda x: x.seq):
        by_kind.setdefault(e.kind, []).append(e)

    actual = sha256_hex(document)
    if actual != request["document_sha256"]:
        problems.append(Problem("document_changed", "the stored document bytes hash to %s, not to the recorded %s" % (actual, request["document_sha256"])))

    created = by_kind.get("created", [])
    if not created:
        problems.append(Problem("created_row_missing", "there is no 'created' audit row to pin the document and the signer"))
    else:
        d = created[0].detail
        pinned = {"document_sha256": request["document_sha256"], "document_bytes": len(document),
                  "signer_name_sha256": sha256_hex(request["signer_name"].encode("utf-8")), "signer_email_sha256": sha256_hex(request["signer_email"].encode("utf-8")),
                  "consent_version": request["consent_version"], "expires_at": fmt_time(request["expires_at"])}
        for key, value in pinned.items():
            if d.get(key) != value:
                problems.append(Problem("request_changed", "%s on the request is not what the 'created' audit row recorded" % key, created[0].seq))

    signed_rows = by_kind.get("signed", [])
    status = request["status"]
    if signature is None and (status == "signed" or signed_rows):
        problems.append(Problem("signature_missing", "the request is marked signed or the audit log has a 'signed' row, but no signature record is visible for this tenant "
                                "(it was deleted, or it was relabelled to another tenant)"))
    if signature is not None:
        sig = signature
        if str(sig["request_id"]) != rid or str(sig["tenant_id"]) != tenant:
            problems.append(Problem("signature_foreign", "the signature record names another request or tenant"))
        if sig["document_sha256"] != request["document_sha256"]:
            problems.append(Problem("signature_document_mismatch", "the signature was made over document %s, but the request holds %s" % (sig["document_sha256"], request["document_sha256"])))
        recomputed = signature_hash(signature_id=str(sig["id"]), tenant_id=str(sig["tenant_id"]), request_id=str(sig["request_id"]), document_sha256=sig["document_sha256"],
                                    signer_name_typed=sig["signer_name_typed"], consent_version=sig["consent_version"], consent_text_sha256=sig["consent_text_sha256"],
                                    signed_at=sig["signed_at"], user_agent_hash=sig["user_agent_hash"], client_addr_hash=sig["client_addr_hash"], prev_hash=sig["prev_hash"])
        if recomputed != sig["signature_hash"]:
            problems.append(Problem("signature_edited", "the signature record was changed after it was written: its stored hash is not the hash of its content"))
        if status != "signed":
            problems.append(Problem("status_mismatch", "a signature exists but the request is marked %s" % status))
        if not signed_rows:
            problems.append(Problem("signed_row_missing", "a signature exists but the audit log has no 'signed' row"))
        else:
            row = signed_rows[0]
            if row.detail.get("signature_hash") != sig["signature_hash"] or row.detail.get("signature_id") != str(sig["id"]):
                problems.append(Problem("signature_not_in_chain", "the 'signed' audit row does not carry this signature's hash", row.seq))
            earlier = [e for e in sorted(events, key=lambda x: x.seq) if e.seq < row.seq]
            if not earlier or earlier[-1].entry_hash != sig["prev_hash"]:
                problems.append(Problem("signature_link_broken", "the signature does not link to the audit row just before its 'signed' row", row.seq))
            if row.detail.get("signed_at") != fmt_time(sig["signed_at"]):
                problems.append(Problem("signature_time_changed", "the signing time differs between the signature record and its audit row", row.seq))
    if status == "declined" and not by_kind.get("declined"):
        problems.append(Problem("status_mismatch", "the request is marked declined but the audit log has no 'declined' row"))
    if status == "expired" and not by_kind.get("expired"):
        problems.append(Problem("status_mismatch", "the request is marked expired but the audit log has no 'expired' row"))
    if status == "pending" and (by_kind.get("declined") or by_kind.get("expired")):
        problems.append(Problem("status_mismatch", "the audit log shows the request ended, but it is still marked pending"))
    return problems
