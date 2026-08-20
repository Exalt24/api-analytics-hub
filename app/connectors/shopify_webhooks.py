"""Shopify webhook verification and the reconciliation problem behind it.

WHY EVERY REAL SHOPIFY ENGAGEMENT NEEDS THIS. Polling alone is either stale or
wasteful: a five-minute sync means five-minute-old numbers, and a one-minute sync
burns the rate budget asking whether anything changed. Webhooks invert it, and
every Shopify client project ends up needing them, so this is the piece that
transfers.

FIVE THINGS THAT ARE WRONG IN MOST IMPLEMENTATIONS, each learned somewhere real:

  1. HMAC IS OVER THE RAW BODY. Re-serialising parsed JSON changes key order and
     whitespace, the digest changes, and the failure looks exactly like a wrong
     secret. This is the single most common Shopify webhook bug.

  2. COMPARE IN CONSTANT TIME, and SANITISE THE HEADER FIRST. I originally wrote
     that `compare_digest` raises on mismatched lengths; measured 2026-08-20, it
     does not, it returns False. What it DOES raise on is a str containing
     non-ASCII characters, so an attacker sending a signature header with one
     byte of UTF-8 turns a rejection into a TypeError and a 500. The guard that
     matters is therefore an ASCII check, not a length check.

  3. IDEMPOTENCY IS RECORDED AFTER AUTHENTICATION, never before. Recording an
     unverified id lets an attacker poison the cache with the id of a real event
     they expect, and the genuine delivery is then discarded as a duplicate.

  4. HANDLER ERRORS MUST PROPAGATE. Returning 200 to Shopify cancels its retry,
     so swallowing an exception loses the event permanently. Shopify retries over
     roughly 48 hours; that window is the only safety net there is.

  5. THE DEDUPLICATION KEY IS NOT JUST THE EVENT ID. An order updated twice
     produces two deliveries with the same topic and the same resource id, and
     keying on that alone silently drops the second, real update. Shopify sends
     X-Shopify-Webhook-Id per delivery, which is what actually distinguishes them.

Pairs with the same lesson from a legal-practice API, where the live payload
disproved three assumptions the tests shared with the code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping

#: Shopify's headers. Lowercased because header access should never be
#: case-sensitive: a plain dict from a test harness and a real ASGI scope
#: disagree, and that difference has caused a signature check to silently fall
#: back to "no signature present" before.
SIG_HEADER = "x-shopify-hmac-sha256"
TOPIC_HEADER = "x-shopify-topic"
SHOP_HEADER = "x-shopify-shop-domain"
WEBHOOK_ID_HEADER = "x-shopify-webhook-id"
API_VERSION_HEADER = "x-shopify-api-version"


class WebhookRejected(Exception):
    """Failed verification. Never tell the caller which check failed."""


class DuplicateDelivery(Exception):
    """Already processed this delivery. Answer 200 so Shopify stops retrying."""


def _get(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header read."""
    if name in headers:
        return headers[name]
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name)


def verify_signature(raw_body: bytes, headers: Mapping[str, str], secret: str) -> None:
    """Raise unless the HMAC over the RAW body matches.

    `raw_body` must be the bytes as received. Accepting a str here would invite
    the caller to hand over a re-serialised payload, which is the bug this
    function exists to prevent, so the type is enforced.
    """
    if not isinstance(raw_body, (bytes, bytearray)):
        raise TypeError(
            "raw_body must be bytes as received; a re-serialised payload will not verify"
        )

    provided = _get(headers, SIG_HEADER)
    if not provided:
        raise WebhookRejected("missing signature header")

    digest = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")

    # ASCII first. compare_digest returns False on a length mismatch (verified,
    # it does NOT raise as an earlier version of this comment claimed), but it
    # DOES raise TypeError on a str with non-ASCII characters. Without this a
    # single UTF-8 byte in the header becomes an unhandled exception and a 500
    # instead of a clean rejection, which is a free denial-of-service and hides
    # the attempt from anything watching for rejections.
    if not provided.isascii():
        raise WebhookRejected("signature header is not ascii")
    if not hmac.compare_digest(provided, expected):
        raise WebhookRejected("signature mismatch")


@dataclass
class DeliveryLog:
    """Bounded FIFO of seen delivery ids.

    Bounded because a long-lived receiver with an unbounded set leaks memory
    until it is killed, and the restart looks like an unrelated crash.
    """

    capacity: int = 4096
    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict, repr=False)

    def seen(self, delivery_id: str) -> bool:
        return delivery_id in self._seen

    def record(self, delivery_id: str) -> None:
        if delivery_id in self._seen:
            self._seen.move_to_end(delivery_id)
            return
        self._seen[delivery_id] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class WebhookReceiver:
    secret: str
    handler: Callable[[str, dict, Mapping[str, str]], Awaitable[None]]
    log: DeliveryLog = field(default_factory=DeliveryLog)

    async def handle(self, raw_body: bytes, headers: Mapping[str, str], parsed: dict) -> str:
        """Verify, deduplicate, dispatch. Returns the topic handled.

        Order is deliberate and the tests pin it: verification strictly before
        any dedup bookkeeping, so an unverified request cannot touch the log.
        """
        verify_signature(raw_body, headers, self.secret)

        # AFTER verification, never before.
        delivery_id = _get(headers, WEBHOOK_ID_HEADER)
        if delivery_id:
            if self.log.seen(delivery_id):
                raise DuplicateDelivery(delivery_id)
            self.log.record(delivery_id)

        topic = _get(headers, TOPIC_HEADER) or "unknown"

        # Deliberately NOT wrapped in try/except. A handler failure must surface
        # as a non-200 so Shopify retries; swallowing it loses the event forever.
        await self.handler(topic, parsed, headers)
        return topic


#: Topics worth subscribing to for an analytics sync, and why. Kept here so a
#: future engagement starts from a reasoned list rather than guessing.
ANALYTICS_TOPICS = {
    "orders/create": "the primary signal; makes near-real-time revenue possible",
    "orders/updated": "totals change after the fact via edits, so create alone drifts",
    "orders/cancelled": "a cancellation that never arrives leaves revenue overstated",
    "refunds/create": "separates gross from net, and refunds arrive days later",
    "products/update": "price changes, which historical line items must NOT inherit",
    "app/uninstalled": "the only reliable signal that a token is dead; without it "
                       "the sync retries a revoked credential forever",
    "shop/update": "currency and timezone changes silently reshape every metric",
}
