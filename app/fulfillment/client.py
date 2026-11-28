"""The client for the partner's order API: send one order with an Idempotency-Key, retry what is safe to retry with backoff, and give up with a reason a person can act on.

THE CONTRACT is stated in docs/fulfillment.md and written once more as the stand-in server tests/fulfillment_stand_in.py. It is OUR contract with a partner, written by us and not taken from
a real partner's documentation, so a real partner's own pages decide what changes. In short: `POST {base}/v1/orders` with `Authorization: Bearer <key>`, `Content-Type: application/json`,
`Idempotency-Key: <key>` and the body {"order_ref", "clinic_ref"}; 201 with {"partner_order_id"} the first time, 200 with the same body and `Idempotent-Replayed: true` for a repeat.

WHAT IS RETRIED, and what is not.
  429, 408, 425, 500, 502, 503, 504 and 409 (the partner is still busy with the first copy of this key), a timeout, a dropped connection:  retried, with the SAME key, up to `max_attempts`
      in all, waiting base_delay * 2^n (a little jitter on top, none in the tests) or the partner's own Retry-After, never more than max_delay. A request whose answer was lost may well have
      been made, which is exactly why the key is the same on every attempt: the partner returns the first answer and makes nothing twice.
  Any other 4xx (400, 401, 403, 404, 422 and the rest):  permanent, never retried. Saying it again will not change it; it becomes PartnerRejected with the partner's error CODE.
  A 2xx whose body has no usable partner_order_id is permanent too (`bad_response`): the partner said yes but not to what.
When the attempts run out the answer is PartnerUnavailable (with the last HTTP status), so the caller can put the order in the dead letter.

WHAT IS NEVER LOGGED OR KEPT: the request and response bodies. Only the status code, the partner's short error code (checked against a safe alphabet) and counts leave this module.
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .errors import PartnerRejected, PartnerUnavailable

MAX_ATTEMPTS = 4
BASE_DELAY = 0.5
MAX_DELAY = 30.0
RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_CODE = re.compile(r"^[a-z0-9_]{1,40}$")
_PARTNER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Receipt:
    partner_order_id: str
    replayed: bool
    attempts: int


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        raw = resp.headers.get("Retry-After")
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def _error_code(resp: httpx.Response) -> str:
    """The partner's short error code if it sent one in the documented shape and it is plain text of a safe alphabet, else `error`. Nothing else of the body is read."""
    try:
        body = resp.json()
        code = body.get("code") if isinstance(body, dict) else None
    except ValueError:
        return "error"
    return code if isinstance(code, str) and _CODE.match(code) else "error"


class PartnerClient:
    """`http` is an httpx.AsyncClient; `api_key` is held and sent but never logged or stored. `sleep` and `rand` are injected so a test waits for nothing and knows each delay exactly."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str, *, sleep: Sleep = asyncio.sleep, max_attempts: int = MAX_ATTEMPTS, base_delay: float = BASE_DELAY,
                 max_delay: float = MAX_DELAY, rand: Callable[[], float] = random.random):
        if max_attempts < 1:
            raise ValueError("at least one attempt")
        self._http, self._base, self._key, self._sleep = http, base_url.rstrip("/"), api_key, sleep
        self._max_attempts, self._base_delay, self._max_delay, self._rand = max_attempts, base_delay, max_delay, rand

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        hinted = _retry_after(resp) if resp is not None else None
        wait = hinted if hinted is not None else self._base_delay * (2 ** attempt) * (1 + 0.25 * self._rand())
        return min(wait, self._max_delay)

    async def submit_order(self, *, order_ref: str, clinic_ref: str, idempotency_key: str) -> Receipt:
        """Send the order. Raises PartnerRejected for a permanent no and PartnerUnavailable when the attempts run out."""
        if not 8 <= len(idempotency_key) <= 120:
            raise ValueError("an idempotency key is 8 to 120 characters")
        headers = {"Authorization": "Bearer " + self._key, "Idempotency-Key": idempotency_key, "Content-Type": "application/json"}
        body = {"order_ref": order_ref, "clinic_ref": clinic_ref}
        last_status: int | None = None
        for attempt in range(self._max_attempts):
            resp: httpx.Response | None = None
            try:
                resp = await self._http.post(self._base + "/v1/orders", json=body, headers=headers)
            except httpx.TransportError:
                last_status = None
            else:
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    try:
                        partner_id = resp.json().get("partner_order_id")
                    except (ValueError, AttributeError):
                        partner_id = None
                    if not isinstance(partner_id, str) or not _PARTNER_ID.match(partner_id):
                        raise PartnerRejected(resp.status_code, "bad_response", attempt + 1)
                    return Receipt(partner_id, resp.headers.get("Idempotent-Replayed") == "true", attempt + 1)
                if resp.status_code not in RETRYABLE:
                    raise PartnerRejected(resp.status_code, _error_code(resp), attempt + 1)
            if attempt + 1 < self._max_attempts:
                await self._sleep(self._delay(attempt, resp))
        raise PartnerUnavailable(self._max_attempts, last_status)
