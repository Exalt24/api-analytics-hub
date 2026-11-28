"""The receiver for the partner's status events: verify the signature and the time, read only the fields we keep, find whose order it is, apply it once.

THE ORDER OF THE STEPS, as for the billing webhook (app/billing/webhooks.py) and for the same reasons:
  1. VERIFY FIRST. The header `X-Partner-Signature` is `t=<unix time>,v1=<hex>`: an HMAC-SHA-256 over `<t>.<raw body>` under the shared secret, the scheme of app/billing/signature.py (several
     secrets may be active while one is rolled). Nothing is parsed, looked up or written until the signature checks out over the RAW bytes and `t` is within the replay window (300 seconds,
     both directions). A captured event replayed later is refused by the window; a replay inside the window is harmless because the event id is deduplicated.
  2. READ ONLY WHAT WE KEEP. The body is parsed into a PartnerEvent (event id, order reference, status, and for shipped a carrier and a tracking number), each checked against a strict
     pattern. Every other field the partner sent (a recipient name, an address, a note) is never read into anything: it is not stored, not audited and not logged.
  3. ROUTE. The order reference is looked up through the narrow `fulfillment_router` role, which can read the id, tenant and reference of an order and nothing else. A reference that is not ours
     is a 404, never a guess.
  4. APPLY. service.apply_event, as the system user, in one transaction (see its doc: apply, ignore, refuse, or a repeat that writes nothing).

THE ANSWERS (handle_http): 200 applied, ignored or duplicate (the partner may stop sending it); 400 a rejected event (the reason goes to OUR log as a code and is never sent back); 404 an order
that is not ours; 409 an event that is not legal now (the partner may send it again later: a `shipped` that overtook its `packed` is applied when it comes round again).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Sequence

from app.billing import signature
from app.billing.errors import WebhookRejected
from app.db import Database

from . import service
from .errors import EventRejected, EventUnroutable, OrderNotFound

log = logging.getLogger("fulfillment.webhook")

SIGNATURE_HEADER = "X-Partner-Signature"
_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_ORDER_REF = re.compile(r"^ord_[0-9a-f]{24}$")


def parse_event(body: Any) -> service.PartnerEvent:
    """Reduce a verified JSON body to a PartnerEvent. Raises EventRejected (with a code that is safe to log) for anything that is not a status event of ours."""
    if not isinstance(body, dict):
        raise EventRejected("body_not_an_object")
    event_id, ref, status = body.get("event_id"), body.get("order_ref"), body.get("status")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        raise EventRejected("bad_event_id")
    if not isinstance(ref, str) or not _ORDER_REF.match(ref):
        raise EventRejected("bad_order_ref")
    if not isinstance(status, str):
        raise EventRejected("bad_status")
    carrier, tracking = body.get("carrier"), body.get("tracking_number")
    for value in (carrier, tracking):
        if value is not None and not isinstance(value, str):
            raise EventRejected("bad_shipping_field")
    event = service.PartnerEvent(event_id, ref, status, carrier, tracking)
    service.check_event(event)
    return event


async def find_order(router: Database, order_ref: str) -> tuple[str, str] | None:
    """(tenant id, order id) for an order reference, through the narrow router role, or None when the reference is not ours."""
    async with router.unscoped() as conn:
        row = await conn.fetchrow("select tenant_id, id from fulfillment_order where order_ref = $1", order_ref)
    return (str(row["tenant_id"]), str(row["id"])) if row else None


Verifier = Callable[..., Any]


async def receive(db: Database, router: Database, payload: bytes, header: str | None, secrets: Sequence[str], *, now: float | None = None,
                  tolerance: int = signature.DEFAULT_TOLERANCE_SECONDS, verify: Verifier = signature.verify) -> service.EventResult:
    """The whole receiver. Raises WebhookRejected (not authentic, stale, or not an event), EventRejected (authentic, but not a valid status event) or EventUnroutable (not our order)."""
    verify(payload, header, secrets, now=time.time() if now is None else now, tolerance=tolerance)
    try:
        body = json.loads(payload)
    except ValueError as exc:
        raise WebhookRejected("body is not JSON") from exc
    event = parse_event(body)
    found = await find_order(router, event.order_ref)
    if found is None:
        raise EventUnroutable("no order of ours has this reference")
    tenant_id, order_id = found
    return await service.apply_event(db, tenant_id, order_id, event)


async def handle_http(db: Database, router: Database, payload: bytes, header: str | None, secrets: Sequence[str], *, now: float | None = None,
                      tolerance: int = signature.DEFAULT_TOLERANCE_SECONDS, verify: Verifier = signature.verify) -> tuple[int, dict[str, Any]]:
    """`receive` as an HTTP answer (status, JSON body). A reason for a rejection goes to our log as a code and never into the body; the body is never logged."""
    try:
        got = await receive(db, router, payload, header, secrets, now=now, tolerance=tolerance, verify=verify)
    except WebhookRejected as exc:
        log.warning("partner event rejected: %s", exc.reason)
        return 400, {"error": "invalid event"}
    except EventRejected as exc:
        log.warning("partner event rejected: %s", exc.reason)
        return 400, {"error": "invalid event"}
    except (EventUnroutable, OrderNotFound):
        log.warning("partner event for an order that is not ours")
        return 404, {"error": "unknown order"}
    if got.status == "refused":
        log.info("partner event refused: not legal now")
        return 409, {"error": "not legal now", "result": "refused"}
    return 200, {"received": True, "result": got.status}
