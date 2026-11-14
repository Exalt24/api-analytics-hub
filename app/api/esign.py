"""Signing requests as HTTP calls: a firm creates one, the client opens, reads and signs it, and either side can verify it and fetch the certificate.

WHO MAY DO WHAT, by the key's role (app/api/auth.py): an operator or admin creates a request (`write:esign`); every role can list, open, read the document, sign,
decline and verify the requests of its own tenant (`read:esign`, `sign:esign`). The tenant comes from the key and nowhere else, as everywhere in this service. A request id
that belongs to another tenant answers 404 with exactly the body of an id that does not exist.

WHAT IS RECORDED ABOUT THE CALLER. The user agent and the client address are hashed with a keyed hash (ESIGN_PII_KEY) inside app/esign/service.py before anything is stored;
this file passes them in and never logs them. The address is the connection's, or the first X-Forwarded-For entry when ESIGN_TRUST_FORWARDED=1 (set it only when a proxy you
control sets that header). With no ESIGN_PII_KEY a random key is made for the life of the process and a warning is logged, so the hashes then cannot be compared across restarts.

STATUSES. 200 done (or a retry of a done signing, with `replayed` true). 201 created. 401 and 403 as everywhere. 404 no such request for this tenant. 409 already signed, declined,
the consent text or the document is not the one on the request. 410 expired. 422 a request that cannot be stored, a missing consent, a name that is not the signer's.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, requires
from app.esign import service
from app.esign.certificate import render_text
from app.esign.errors import (AlreadySigned, CertificateRefused, ConsentRequired, ConsentVersionMismatch, DocumentChanged, EsignError, InvalidRequest, NameMismatch,
                              NameRequired, NotSigned, RequestDeclined, RequestExpired, RequestNotFound)

log = logging.getLogger("hub")
router = APIRouter()

_STATUS_OF = {
    RequestNotFound: 404, AlreadySigned: 409, RequestDeclined: 409, ConsentVersionMismatch: 409, DocumentChanged: 409, RequestExpired: 410,
    InvalidRequest: 422, ConsentRequired: 422, NameRequired: 422, NameMismatch: 422, NotSigned: 409, CertificateRefused: 409,
}

_process_key: bytes | None = None


def pii_key() -> bytes:
    """The keyed-hash key for personal values: ESIGN_PII_KEY when set, else one random key for this process (and a warning, once)."""
    global _process_key
    configured = os.environ.get("ESIGN_PII_KEY", "")
    if configured:
        return configured.encode("utf-8")
    if _process_key is None:
        _process_key = secrets.token_bytes(32)
        log.warning("ESIGN_PII_KEY is not set: signature hashes use a key made for this process only", extra={"event": "esign_key_unset"})
    return _process_key


def client_address(request: Request) -> str:
    if os.environ.get("ESIGN_TRUST_FORWARDED") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def refuse(exc: EsignError) -> HTTPException:
    """The HTTP form of a refusal. The text is the refusal's own and never includes another tenant's data."""
    code = _STATUS_OF.get(type(exc), 400)
    if isinstance(exc, CertificateRefused):
        return HTTPException(status_code=code, detail={"message": str(exc), "problems": [p.as_dict() for p in exc.problems]})
    return HTTPException(status_code=code, detail=str(exc))


class CreateRequest(BaseModel):
    document_b64: str = Field(description="the document's bytes, base64", max_length=7_500_000)
    document_name: str = Field(max_length=200)
    signer_name: str = Field(max_length=200)
    signer_email: str = Field(max_length=254)
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 365)


class SignBody(BaseModel):
    typed_name: str = Field(max_length=1000)
    consent: bool
    consent_version: str = Field(max_length=60)
    document_sha256: str = Field(max_length=64, description="the hash shown on the page the signer read")


@router.post("/api/esign/requests", status_code=status.HTTP_201_CREATED)
async def create_request(body: CreateRequest, request: Request, principal: Principal = Depends(requires("write:esign"))):
    try:
        document = base64.b64decode(body.document_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="document_b64 is not valid base64") from None
    try:
        return await service.create_request(request.app.state.db, principal.tenant_id, actor=principal.key_id, document=document, document_name=body.document_name,
                                            signer_name=body.signer_name, signer_email=body.signer_email,
                                            expires_at=datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours))
    except EsignError as exc:
        raise refuse(exc) from None


@router.get("/api/esign/requests")
async def list_requests(request: Request, principal: Principal = Depends(requires("read:esign"))):
    return {"requests": await service.list_requests(request.app.state.db, principal.tenant_id)}


@router.post("/api/esign/expire")
async def expire_due(request: Request, principal: Principal = Depends(requires("write:esign"))):
    """Mark the tenant's pending requests that are past their expiry. Safe to call at any time, so a scheduler can call it."""
    return {"expired": await service.expire_due(request.app.state.db, principal.tenant_id)}


@router.get("/api/esign/requests/{request_id}")
async def open_request(request_id: str, request: Request, principal: Principal = Depends(requires("read:esign"))):
    try:
        return await service.view_request(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id, role=principal.role.value)
    except EsignError as exc:
        raise refuse(exc) from None


@router.get("/api/esign/requests/{request_id}/document")
async def document(request_id: str, request: Request, principal: Principal = Depends(requires("read:esign"))):
    try:
        name, data, digest = await service.get_document(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id, role=principal.role.value)
    except EsignError as exc:
        raise refuse(exc) from None
    safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:100] or "document"
    return Response(content=data, media_type="application/octet-stream", headers={"X-Document-SHA256": digest, "Content-Disposition": 'attachment; filename="%s"' % safe})


@router.post("/api/esign/requests/{request_id}/sign")
async def sign(request_id: str, body: SignBody, request: Request, principal: Principal = Depends(requires("sign:esign"))):
    try:
        result = await service.sign(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id, typed_name=body.typed_name, consent=body.consent,
                                    consent_version=body.consent_version, document_sha256=body.document_sha256, user_agent=request.headers.get("user-agent", ""),
                                    client_addr=client_address(request), pii_key=pii_key())
    except EsignError as exc:
        raise refuse(exc) from None
    return result.as_dict()


@router.post("/api/esign/requests/{request_id}/decline")
async def decline(request_id: str, request: Request, principal: Principal = Depends(requires("sign:esign"))):
    try:
        return {"status": await service.decline(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id)}
    except EsignError as exc:
        raise refuse(exc) from None


@router.post("/api/esign/requests/{request_id}/verify")
async def verify(request_id: str, request: Request, principal: Principal = Depends(requires("read:esign"))):
    try:
        return (await service.verify(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id)).as_dict()
    except EsignError as exc:
        raise refuse(exc) from None


@router.get("/api/esign/requests/{request_id}/certificate")
async def certificate(request_id: str, request: Request, format: str = "json", principal: Principal = Depends(requires("read:esign"))):
    if format not in ("json", "text"):
        raise HTTPException(status_code=400, detail="format must be json or text")
    try:
        cert = await service.issue_certificate(request.app.state.db, principal.tenant_id, request_id, actor=principal.key_id)
    except EsignError as exc:
        raise refuse(exc) from None
    if format == "text":
        return Response(content=render_text(cert), media_type="text/plain; charset=utf-8")
    return cert
