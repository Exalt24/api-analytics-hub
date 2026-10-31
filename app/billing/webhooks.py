"""The webhook receiver: verify a signed provider event, find whose it is, write it once, and apply it in the same transaction as the ledger lines it causes.

THE ORDER OF THE STEPS, and why each is where it is:

  1. VERIFY FIRST (`verify`: signature.verify by default, or stripe_sdk.verify_webhook, which is the SDK's own construct_event). Nothing is read from the body, looked up or written until the Stripe-Signature header checks out over the raw bytes
     and the timestamp is recent. Recording an event id before this would let a forged event poison the dedupe table.
  2. ROUTE. The event arrives before anyone knows whose it is. `find_tenant` asks the narrow `billing_router` role, which can read the payments and the
     subscriptions for their provider ids and nothing else. It routes by OUR rows (the payment's provider id or our payment id, the subscription's provider
     customer or subscription id), never by a tenant id the body names: a body that names another tenant than the one our row belongs to is refused.
     An event with no row to route by (a refund that beat its payment) raises WebhookUnroutable and is answered with a non-2xx, so the provider sends
     it again.
  3. WRITE ONCE. One tenant transaction inserts the event row (`unique (tenant_id, provider_event_id)`), applies the handler and marks the row's final
     status, so the event and its ledger lines commit together or not at all. A second delivery of the same id meets the unique row and writes nothing.
     A handler that raises (a database fault) rolls everything back, the answer is a 5xx, and the provider's retry applies it cleanly.

WHAT THE SAVED PAGES SAY (docs/vendor-contract/stripe/webhooks-signatures.txt, billing-subscriptions-webhooks.txt):
  "Webhook endpoints might occasionally receive the same event more than once. You can guard against duplicated event receipts by logging the event IDs
  you've processed, and then not processing already-logged events."
  "In some cases, two separate Event objects are generated and sent. To identify these duplicates, use the ID of the object in data.object along with the
  event.type."  Every money step here is keyed on the OBJECT (the payment intent, the invoice, the refunded total), so two events for one object credit once.
  "Stripe doesn't guarantee the delivery of events in the order that they're generated." and "Don't use created to determine event order or whether
  you've already processed an event. Track event IDs to identify duplicate deliveries instead."  So an event that cannot be applied yet is HELD, not
  dropped, and reprocess_held applies it when what it waited for has arrived.
  "Quickly returns a successful status code (2xx) before any complex logic that might cause a timeout." Not met: this receiver applies the event before it
  answers (one short database transaction). A provider with a slow handler would queue the event first; that is not done here.
  "In live mode, if your webhook endpoint doesn't respond properly, Stripe continues retrying the webhook notification for up to 3 days with an
  exponential back off." which is why a fault here is a 5xx and a held event is not.

THE FIVE ANSWERS (handle_http): 200 processed, duplicate, ignored or held; 400 a rejected event (the reason is logged by the caller, never sent); 503
unroutable, so the provider retries. A held event is a 200: it is stored and will be applied, and a retry would only repeat the wait.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import asyncpg

from app.db import Database

from . import payments, signature, subscriptions
from .errors import NoSubscription, UnknownPlan, WebhookRejected, WebhookUnroutable

log = logging.getLogger("billing.webhooks")

#: the event types this module acts on. Any other verified event is stored as ignored.
HANDLED = ("payment_intent.succeeded", "payment_intent.payment_failed", "charge.refunded", "invoice.paid", "invoice.payment_failed",
           "customer.subscription.updated", "customer.subscription.deleted")


@dataclass(frozen=True)
class Received:
    """`status`: processed (applied now), duplicate (this event id was seen; nothing written), ignored (a type or an older state we do not act on) or
    held (stored; waiting for something and retried by reprocess_held)."""
    status: str
    event_id: str
    event_type: str
    tenant_id: str
    detail: str


def _shape(event: Any) -> dict[str, Any]:
    """The few fields every handler needs, checked once. A verified body that is not an event is rejected, not guessed at."""
    if not isinstance(event, dict):
        raise WebhookRejected("body is not an object")
    obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
    if not isinstance(event.get("id"), str) or not event["id"] or not isinstance(event.get("type"), str) or not isinstance(obj, dict):
        raise WebhookRejected("not an event")
    if not isinstance(event.get("created"), int) or isinstance(event.get("created"), bool):
        raise WebhookRejected("no created time")
    if len(event["id"]) > 255:
        raise WebhookRejected("event id too long")
    return event


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


async def find_tenant(router: Database, event_type: str, obj: Mapping[str, Any]) -> str | None:
    """Whose event is this, from our own rows. `router` is a Database connected as billing_router. None when nothing of ours matches yet."""
    async with router.unscoped() as conn:
        if event_type.startswith("payment_intent."):
            rows = await conn.fetch("select tenant_id from billing_payment where provider_payment_id = $1", obj.get("id"))
            if not rows and _uuid_or_none((obj.get("metadata") or {}).get("payment_id")):
                rows = await conn.fetch("select tenant_id from billing_payment where id = $1::uuid", _uuid_or_none(obj["metadata"]["payment_id"]))
        elif event_type.startswith("charge."):
            rows = await conn.fetch("select tenant_id from billing_payment where provider_payment_id = $1", obj.get("payment_intent")) if obj.get("payment_intent") else []
        elif event_type.startswith("invoice."):
            sub_id = (((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")) or obj.get("subscription")
            rows = await conn.fetch("select tenant_id from billing_subscription where provider_customer_id = $1 or provider_subscription_id = $2",
                                    obj.get("customer"), sub_id)
        elif event_type.startswith("customer.subscription."):
            rows = await conn.fetch("select tenant_id from billing_subscription where provider_subscription_id = $1 or provider_customer_id = $2",
                                    obj.get("id"), obj.get("customer"))
        else:
            rows = []
        tenants = {str(r["tenant_id"]) for r in rows}
    if len(tenants) != 1:
        return None
    tenant = tenants.pop()
    named = (obj.get("metadata") or {}).get("tenant_id") if isinstance(obj.get("metadata"), dict) else None
    if named and _uuid_or_none(named) != tenant:
        raise WebhookUnroutable("the event names a different tenant than the one its payment belongs to")
    return tenant


async def _dispatch(conn: asyncpg.Connection, tenant_id: str, event: Mapping[str, Any]) -> tuple[str, str]:
    """Apply one event. Returns (status, detail) with status processed, ignored or held. Runs inside a savepoint, so a refusal leaves nothing behind."""
    et, obj, created = event["type"], event["data"]["object"], int(event["created"])
    at = datetime.fromtimestamp(created, tz=timezone.utc)
    if et == "payment_intent.succeeded":
        out = await payments.settle_succeeded(conn, tenant_id, obj, at=at)
        return "processed", "credited %d %s (%s)" % (out.amount_minor, out.currency, out.status)
    if et == "payment_intent.payment_failed":
        result = await payments.settle_failed(conn, tenant_id, obj)
        return ("processed", "recorded the failure, credited nothing") if result == "failed" else ("ignored", "the payment had already succeeded or failed")
    if et == "charge.refunded":
        return await payments.apply_refund(conn, tenant_id, obj, at=at)
    if et == "invoice.paid":
        return await subscriptions.on_invoice_paid(conn, tenant_id, obj, created, at=at)
    if et == "invoice.payment_failed":
        return await subscriptions.on_invoice_payment_failed(conn, tenant_id, obj, created)
    if et == "customer.subscription.updated":
        return await subscriptions.on_subscription_updated(conn, tenant_id, obj, created)
    if et == "customer.subscription.deleted":
        return await subscriptions.on_subscription_deleted(conn, tenant_id, obj, created)
    return "ignored", "an event type this module does not act on"


async def _apply(conn: asyncpg.Connection, tenant_id: str, event: Mapping[str, Any]) -> tuple[str, str]:
    """_dispatch inside a savepoint. A known refusal (a payment that does not match what we asked for, a subscription we do not hold, a body missing a
    field) becomes a HELD event with its reason; anything else (a database fault, a ledger invariant) propagates and rolls the whole delivery back."""
    try:
        async with conn.transaction():
            return await _dispatch(conn, tenant_id, event)
    except (payments.PaymentMismatch, NoSubscription, UnknownPlan) as exc:
        return "held", str(exc)[:300]
    except (KeyError, TypeError, ValueError) as exc:
        return "held", ("malformed %s: %s" % (event["type"], exc))[:300]


async def process(db: Database, tenant_id: str, event: Mapping[str, Any]) -> Received:
    """Write one verified event once and apply it, in one tenant transaction. A repeat of a processed or ignored id writes nothing; a repeat of a held id
    tries it again."""
    async with db.tenant(tenant_id) as conn:
        inserted = await conn.fetchrow(
            """insert into billing_webhook_event (tenant_id, provider_event_id, event_type, status, detail, payload)
               values ($1::uuid, $2, $3, 'held', 'being applied', $4::jsonb) on conflict (tenant_id, provider_event_id) do nothing returning id""",
            tenant_id, event["id"], event["type"], json.dumps(event))
        if inserted is None:
            seen = await conn.fetchrow("select * from billing_webhook_event where tenant_id = $1::uuid and provider_event_id = $2 for update", tenant_id, event["id"])
            if seen["status"] != "held":
                return Received("duplicate", event["id"], event["type"], tenant_id, "already %s: %s" % (seen["status"], seen["detail"] or ""))
            event = json.loads(seen["payload"]) if isinstance(seen["payload"], str) else dict(seen["payload"])
        status, detail = await _apply(conn, tenant_id, event)
        await conn.execute(
            "update billing_webhook_event set status = $3, detail = $4, processed_at = case when $3 = 'held' then null else now() end "
            "where tenant_id = $1::uuid and provider_event_id = $2", tenant_id, event["id"], status, detail)
        if status == "processed" and event["type"] in ("payment_intent.succeeded", "invoice.paid"):
            await _reprocess_held(conn, tenant_id, skip=event["id"])
        return Received(status, event["id"], event["type"], tenant_id, detail)


async def _reprocess_held(conn: asyncpg.Connection, tenant_id: str, skip: str | None = None) -> list[Received]:
    rows = await conn.fetch("select provider_event_id, payload from billing_webhook_event where tenant_id = $1::uuid and status = 'held' and provider_event_id <> $2 "
                            "order by id for update", tenant_id, skip or "")
    done: list[Received] = []
    for row in rows:
        event = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        status, detail = await _apply(conn, tenant_id, event)
        if status != "held":
            await conn.execute("update billing_webhook_event set status = $3, detail = $4, processed_at = now() where tenant_id = $1::uuid and provider_event_id = $2",
                               tenant_id, row["provider_event_id"], status, detail)
            done.append(Received(status, row["provider_event_id"], event["type"], tenant_id, detail))
    return done


async def reprocess_held(db: Database, tenant_id: str) -> list[Received]:
    """Try every held event of the tenant again, oldest first (a refund that waited for its payment, a refund the wallet could not yet cover). Returns the
    ones that moved."""
    async with db.tenant(tenant_id) as conn:
        return await _reprocess_held(conn, tenant_id)


#: A verifier takes (payload, header, secrets, now=, tolerance=) and raises WebhookRejected for an event that is not authentic or not recent.
Verifier = Callable[..., Any]


async def receive(db: Database, router: Database, payload: bytes, header: str | None, secrets: Sequence[str], *, now: float | None = None,
                  tolerance: int = signature.DEFAULT_TOLERANCE_SECONDS, verify: Verifier = signature.verify) -> Received:
    """The whole receiver. Raises WebhookRejected (not authentic, stale or malformed) or WebhookUnroutable (nobody's event yet)."""
    verify(payload, header, secrets, now=time.time() if now is None else now, tolerance=tolerance)
    try:
        event = _shape(json.loads(payload))
    except ValueError as exc:       # not JSON; WebhookRejected is not a ValueError, so it passes through untouched
        raise WebhookRejected("body is not JSON") from exc
    tenant = await find_tenant(router, event["type"], event["data"]["object"])
    if tenant is None:
        raise WebhookUnroutable("no payment or subscription of ours matches %s %s" % (event["type"], event["id"]))
    return await process(db, tenant, event)


async def handle_http(db: Database, router: Database, payload: bytes, header: str | None, secrets: Sequence[str], *, now: float | None = None,
                      tolerance: int = signature.DEFAULT_TOLERANCE_SECONDS, verify: Verifier = signature.verify) -> tuple[int, dict[str, Any]]:
    """`receive` as an HTTP answer (status, JSON body). The reason for a rejection goes to our log and never into the body."""
    try:
        got = await receive(db, router, payload, header, secrets, now=now, tolerance=tolerance, verify=verify)
    except WebhookRejected as exc:
        log.warning("webhook rejected: %s", exc.reason)
        return 400, {"error": "invalid webhook"}
    except WebhookUnroutable as exc:
        log.warning("webhook not routable yet: %s", exc)
        return 503, {"error": "not routable yet, send again"}
    return 200, {"received": True, "status": got.status, "event": got.event_id}

