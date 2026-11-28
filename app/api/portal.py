"""The ordering portal over HTTP: orders sent to a fulfilment partner, invoices (draft, finalize, pay by wallet, card or bank debit, void, refund), payments, commissions, and the partner's
status webhook. Who may do what is the access matrix (app/access/matrix.py), and it is enforced twice: each route carries `requires_access(resource, action)`, and each route then opens its
transactions as the caller (`principal.acting(db)`), so the row level security policies that read the same table filter and refuse every statement a second time.

TENANT, CLINIC AND ROLE come from the credential and nowhere else (app/access/principal.py). A clinic user's order or invoice is always for their own clinic; the clinic is not a field they
can set. A clinic id in a body for another clinic is 403, and an id that belongs to another clinic or tenant answers 404 exactly as one that does not exist.

STATUSES. 200 done (a repeat with the same key says so in `replayed`). 201 created. 401 no or unknown credential. 403 the role may not (the matrix, or a row level security refusal). 404 no such
order, invoice or payment for this caller. 409 the state does not allow it (paying a draft, a payment already in flight, a refund beyond the total, a key reused for other content). 422 a body that
cannot be stored (including any field this API does not define: an order has no free text, so a name in a body is refused, never kept). 503 a provider or the partner is not configured here.

IDEMPOTENCY. Every call that creates or moves money takes an `Idempotency-Key` header (1 to 255 characters); the same key with the same content returns the first answer.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.access.principal import PortalAuthenticator, PortalPrincipal, requires_access
from app.billing import commissions, invoices, payments
from app.billing.errors import BillingError, IdempotencyConflict, InvoiceNotFound, InvoicePaymentPending, InvoiceStateError, InvalidInvoice
from app.billing.provider import PaymentProvider, ProviderError
from app.db import Database
from app.fulfillment import service, webhook
from app.fulfillment.client import PartnerClient
from app.fulfillment.errors import FulfillmentError, InvalidOrder, OrderNotFound, OrderStateError

log = logging.getLogger("hub")
router = APIRouter(prefix="/api/portal")


@dataclass
class PortalRuntime:
    """What the portal routes need: the request pool, the authenticator, and (when configured) the payment provider, the partner client and the narrow router for the partner's events."""
    db: Database
    authenticator: PortalAuthenticator
    provider: PaymentProvider | None = None
    partner: PartnerClient | None = None
    fulfillment_router: Database | None = None
    partner_secrets: tuple[str, ...] = ()
    _owned: tuple = ()


async def build_portal_runtime(db: Database) -> PortalRuntime:
    """From the environment. Anything not configured stays None and its routes answer 503, so the hub starts the same with none of it set (and on a database without the portal migrations)."""
    runtime = PortalRuntime(db, PortalAuthenticator(db))
    if os.environ.get("PORTAL_STRIPE_API_KEY"):
        from app.billing.stripe_sdk import StripeSdkProvider
        runtime.provider = StripeSdkProvider(os.environ["PORTAL_STRIPE_API_KEY"], api_base=os.environ.get("PORTAL_STRIPE_API_BASE") or None)
    if os.environ.get("PORTAL_PARTNER_BASE_URL") and os.environ.get("PORTAL_PARTNER_API_KEY"):
        http = httpx.AsyncClient(timeout=10)
        runtime.partner = PartnerClient(http, os.environ["PORTAL_PARTNER_BASE_URL"], os.environ["PORTAL_PARTNER_API_KEY"])
        runtime._owned = (http,)
    secrets = tuple(s for s in os.environ.get("PORTAL_PARTNER_WEBHOOK_SECRETS", "").split(",") if s)
    if secrets:
        runtime.partner_secrets = secrets
        runtime.fulfillment_router = Database(role="fulfillment_router", min_size=1, max_size=2)
        await runtime.fulfillment_router.connect()
    return runtime


async def close_portal_runtime(runtime: PortalRuntime) -> None:
    if runtime.fulfillment_router is not None:
        await runtime.fulfillment_router.close()
    for owned in runtime._owned:
        await owned.aclose()


def rt(request: Request) -> PortalRuntime:
    return request.app.state.portal


def key_header(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if not idempotency_key or not 0 < len(idempotency_key) <= 255:
        raise HTTPException(status_code=400, detail="an Idempotency-Key header of 1 to 255 characters is required")
    return idempotency_key


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateOrder(Body):
    clinic_id: str | None = Field(default=None, max_length=40)


class Line(Body):
    description: str = Field(max_length=200)
    quantity: int
    unit_amount_minor: int


class CreateInvoice(Body):
    clinic_id: str = Field(max_length=40)
    currency: str = Field(default="USD", max_length=3)
    lines: list[Line] = Field(max_length=invoices.MAX_LINES)


class PayBody(Body):
    method: Literal["wallet", "card", "us_bank_account"]
    payment_method: str | None = Field(default=None, max_length=120)
    customer: str | None = Field(default=None, max_length=120)
    mandate: str | None = Field(default=None, max_length=120)


class RefundBody(Body):
    amount_minor: int


class TopupBody(Body):
    amount_minor: int
    currency: str = Field(max_length=3)
    payment_method: str = Field(max_length=120)
    method_type: Literal["card", "us_bank_account"] = "card"
    customer: str | None = Field(default=None, max_length=120)
    mandate: str | None = Field(default=None, max_length=120)


def refuse(exc: Exception) -> HTTPException:
    """The HTTP form of a refusal from the billing, fulfilment or database layers. The text is the refusal's own and never holds another tenant's data."""
    if isinstance(exc, (InvoiceNotFound, OrderNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidInvoice, InvalidOrder, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (InvoiceStateError, InvoicePaymentPending, OrderStateError, IdempotencyConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (asyncpg.InsufficientPrivilegeError,)):
        return HTTPException(status_code=403, detail="not allowed")
    if isinstance(exc, asyncpg.RestrictViolationError):
        return HTTPException(status_code=409, detail="not allowed in the current state")
    if isinstance(exc, ProviderError):
        return HTTPException(status_code=502, detail="the payment provider refused or did not answer")
    if isinstance(exc, (BillingError, FulfillmentError)):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


def needs(value: Any, what: str) -> Any:
    if value is None:
        raise HTTPException(status_code=503, detail="%s is not configured here" % what)
    return value


# ----------------------------------------------------------------------------------------------------- orders

@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def create_order(body: CreateOrder, request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "create"))):
    clinic = principal.clinic_id or body.clinic_id
    if principal.clinic_id and body.clinic_id and body.clinic_id != principal.clinic_id:
        raise HTTPException(status_code=403, detail="an order is for your own clinic")
    if not clinic:
        raise HTTPException(status_code=422, detail="clinic_id is required")
    try:
        return await service.create_order(principal.acting(rt(request).db), principal.tenant_id, clinic_id=clinic, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.get("/orders")
async def list_orders(request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "read"))):
    return {"orders": await service.list_orders(principal.acting(rt(request).db), principal.tenant_id)}


@router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "read"))):
    try:
        return await service.get_order(principal.acting(rt(request).db), principal.tenant_id, order_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/orders/{order_id}/submit")
async def submit_order(order_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "update"))):
    runtime = rt(request)
    try:
        res = await service.submit_order(principal.acting(runtime.db), needs(runtime.partner, "the fulfilment partner"), principal.tenant_id, order_id, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None
    return {"status": res.status, "attempts": res.attempts, "replayed": res.replayed, "reason": res.reason, "order": res.order}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "update"))):
    try:
        return await service.cancel_order(principal.acting(rt(request).db), principal.tenant_id, order_id, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/orders/{order_id}/requeue")
async def requeue_order(order_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "update"))):
    try:
        return await service.requeue(principal.acting(rt(request).db), principal.tenant_id, order_id, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/webhooks/fulfillment")
async def partner_webhook(request: Request):
    """The partner's status events. No credential: the signature over the raw body is the authentication (webhook.py has the order of the checks)."""
    runtime = rt(request)
    needs(runtime.fulfillment_router, "the partner webhook")
    code, body = await webhook.handle_http(runtime.db, runtime.fulfillment_router, await request.body(), request.headers.get(webhook.SIGNATURE_HEADER), runtime.partner_secrets)
    return JSONResponse(status_code=code, content=body)


# ----------------------------------------------------------------------------------------------------- invoices

@router.post("/invoices", status_code=status.HTTP_201_CREATED)
async def create_invoice(body: CreateInvoice, request: Request, key: str = Depends(key_header), principal: PortalPrincipal = Depends(requires_access("invoices", "create"))):
    try:
        invoice, replayed = await invoices.create_draft(principal.acting(rt(request).db), principal.tenant_id, clinic_id=body.clinic_id, currency=body.currency,
                                                        lines=[l.model_dump() for l in body.lines], key=key, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None
    return {**invoice, "replayed": replayed}


@router.get("/invoices")
async def list_invoices(request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "read"))):
    return {"invoices": await invoices.list_invoices(principal.acting(rt(request).db), principal.tenant_id)}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "read"))):
    try:
        return await invoices.get_invoice(principal.acting(rt(request).db), principal.tenant_id, invoice_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/invoices/{invoice_id}/finalize")
async def finalize_invoice(invoice_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "update"))):
    try:
        return await invoices.finalize(principal.acting(rt(request).db), principal.tenant_id, invoice_id, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/invoices/{invoice_id}/void")
async def void_invoice(invoice_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "update"))):
    try:
        return await invoices.void(principal.acting(rt(request).db), principal.tenant_id, invoice_id, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None


@router.post("/invoices/{invoice_id}/pay")
async def pay_invoice(invoice_id: str, body: PayBody, request: Request, key: str = Depends(key_header), principal: PortalPrincipal = Depends(requires_access("invoices", "settle"))):
    runtime = rt(request)
    if body.method != "wallet":
        needs(runtime.provider, "the payment provider")
    try:
        res = await invoices.pay(principal.acting(runtime.db), runtime.provider, principal.tenant_id, invoice_id, method=body.method, key=key, actor=principal.user_id,
                                 payment_method=body.payment_method, customer=body.customer, mandate=body.mandate)
    except Exception as exc:
        raise refuse(exc) from None
    return {"status": res.status, "replayed": res.replayed, "shortfall_minor": res.shortfall_minor, "failure_code": res.failure_code, "invoice": res.invoice}


@router.post("/invoices/{invoice_id}/refund")
async def refund_invoice(invoice_id: str, body: RefundBody, request: Request, key: str = Depends(key_header),
                         principal: PortalPrincipal = Depends(requires_access("invoices", "refund"))):
    runtime = rt(request)
    try:
        res = await invoices.refund(principal.acting(runtime.db), runtime.provider, principal.tenant_id, invoice_id, amount_minor=body.amount_minor, key=key, actor=principal.user_id)
    except Exception as exc:
        raise refuse(exc) from None
    return {"status": res.status, "refund_id": res.refund_id, "amount_minor": res.amount_minor, "provider_refund_id": res.provider_refund_id, "replayed": res.replayed, "invoice": res.invoice}


# ----------------------------------------------------------------------------------------------------- payments and commissions

@router.get("/payments")
async def list_payments(request: Request, principal: PortalPrincipal = Depends(requires_access("payments", "read"))):
    async with principal.acting(rt(request).db).tenant(principal.tenant_id) as conn:
        rows = await conn.fetch("select id, status, amount_minor, currency, payment_method_type, purpose, invoice_id, clinic_id, failure_code, created_at from billing_payment order by created_at, id")
    return {"payments": [{"id": str(r["id"]), "status": r["status"], "amount_minor": r["amount_minor"], "currency": r["currency"].strip(), "method": r["payment_method_type"],
                          "purpose": r["purpose"], "invoice_id": str(r["invoice_id"]) if r["invoice_id"] else None, "clinic_id": str(r["clinic_id"]) if r["clinic_id"] else None,
                          "failure_code": r["failure_code"], "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/payments", status_code=status.HTTP_201_CREATED)
async def create_payment(body: TopupBody, request: Request, key: str = Depends(key_header), principal: PortalPrincipal = Depends(requires_access("payments", "create"))):
    """A wallet top-up by card or bank debit. A clinic user's payment is for their clinic; a bank debit stays `processing` until the provider's signed event."""
    runtime = rt(request)
    needs(runtime.provider, "the payment provider")
    try:
        res = await payments.start_topup(principal.acting(runtime.db), runtime.provider, principal.tenant_id, amount_minor=body.amount_minor, currency=body.currency.upper(),
                                         payment_method=body.payment_method, key=key, method_type=body.method_type, customer=body.customer, mandate=body.mandate,
                                         clinic_id=principal.clinic_id)
    except Exception as exc:
        raise refuse(exc) from None
    return {"payment_id": res.payment_id, "status": res.status, "replayed": res.replayed, "failure_code": res.failure_code}


@router.get("/commissions")
async def list_commissions(request: Request, principal: PortalPrincipal = Depends(requires_access("commissions", "read"))):
    async with principal.acting(rt(request).db).tenant(principal.tenant_id) as conn:
        return {"commissions": await commissions.list_commissions(conn)}
