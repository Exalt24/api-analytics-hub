"""A Stripe-style payment provider client: create a PaymentIntent, retrieve it, find one by metadata, create a refund. The HTTP client and the sleep are
injected, so every test runs against fake.py with no network and no waiting.

WHAT IS AND IS NOT KNOWN. The requests are written from Stripe's official pages, saved under docs/vendor-contract/stripe/ (README there lists
each URL and the lines quoted below). They have NEVER been sent to a live Stripe account; the only server this has talked to is fake_provider.py, which
was written from the same pages and so cannot catch a misreading of them. The first run against a real account is the real test.

THE LINES RELIED ON (quoted from the saved pages):

  Authentication. The cURL examples send the secret key as the user of HTTP Basic auth: `curl https://api.stripe.com/v1/payment_intents -u "sk_test_...:"`.
  Create. "POST /v1/payment_intents" "Creates a PaymentIntent object."; `amount` is "A positive integer representing how much to charge in the smallest
  currency unit"; `currency` is "Three-letter ISO currency code, in lowercase"; `confirm` "Set to true to attempt to confirm this PaymentIntent
  immediately"; `payment_method` is "The ID of a PaymentMethod to attach to this PaymentIntent."; `metadata` is a "Set of key-value pairs that you can
  attach to an object." The cURL example posts form fields (`-d amount=2000 -d currency=usd`), with nested ones written `metadata[key]`.
  Idempotency. "provide an additional IdempotencyKey element to the request options" and the cURL example `-H "Idempotency-Key: KG5LxwFBepaKHyUD"`;
  "Retries with the same key return the saved response, including 500 errors."; "Idempotency keys are up to 255 characters long."; the replayed
  answer carries the header "Idempotent-Replayed: true"; "The idempotency layer compares incoming parameters to those of the original request and
  errors if they're not the same" (error type idempotency_error); "For API v1, all POST requests accept idempotency keys. Don't send idempotency keys
  in GET and DELETE requests".
  Retrieve. "GET /v1/payment_intents/:id" "Retrieves the details of a PaymentIntent that has previously been created."
  Search. "GET /v1/payment_intents/search" with `query`, written metadata["key"]:"value"; "Don't use search in read-after-write flows where strict
  consistency is necessary. Under normal operating conditions, data is searchable in less than a minute." So a search that finds nothing proves nothing.
  Refund. "POST /v1/refunds" "you must specify a Charge or a PaymentIntent object"; `amount` "Can refund only up to the remaining, unrefunded amount of
  the charge."; "Raises an error if the Charge/PaymentIntent has already been refunded".
  Errors. HTTP 402 "Request Failed" ("The parameters were valid but the request failed."); error `type` is one of api_error, card_error, idempotency_error,
  invalid_request_error; card errors carry `code` ("card_declined") and `decline_code` ("the card issuer's reason for the decline"); 409 "The request
  conflicts with another request (perhaps due to using the same idempotent key)"; 429 "We recommend an exponential backoff of your requests."; 500, 502,
  503, 504 are server errors.
  Network errors. "When a connection fails, you might not know whether Stripe received or executed the request. Retry with the same idempotency key
  and parameters within the first 24 hours after the original request." "Treat the outcome of a request that returns 500 as indeterminate ... Don't
  retry with a new idempotency key until you've resolved the original outcome." "If a 409 indicates that another request with the same key is still
  executing, wait and retry with that key and the same parameters. A new key can start a second operation." The Stripe-Should-Retry header: "false:
  Don't retry the request."

THE DOUBLE CHARGE TRAP, and what this does about it. A request times out after it was sent. The charge may or may not exist. The wrong move is to create
again with a new key. So `create_payment_intent` never makes a new key: every attempt carries the key it was given. When the attempts run out
with the outcome still unknown, it looks for the intent by the metadata it put on it (a search finding it is proof; a search finding nothing is not, so
it says nothing is known) and raises OutcomeUnknown, which the caller records as an unknown payment to settle later with the SAME key. A 4xx card error
is a clear answer, not a fault: it is raised as CardDeclined with the provider's code, and never retried.

UNVERIFIED from the saved pages (a live account will say): whether confirm=true with a payment method needs more parameters (for example a return_url),
which API version header to send, and the exact fields of every error body beyond the ones named above.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import httpx

DEFAULT_BASE_URL = "https://api.stripe.com"
MAX_ATTEMPTS = 4
BASE_DELAY = 0.5
MAX_DELAY = 30.0

Sleep = Callable[[float], Awaitable[None]]


class ProviderError(Exception):
    """Base for what the provider (or the road to it) can do wrong."""


class CardDeclined(ProviderError):
    """A card_error: the provider answered, and the answer is no. Carries the provider's own code and decline_code. Never retried."""

    def __init__(self, code: str | None, decline_code: str | None, message: str, payment_intent_id: str | None = None, status: int = 402):
        super().__init__("%s%s: %s" % (code or "card_error", " / %s" % decline_code if decline_code else "", message))
        self.code, self.decline_code, self.message, self.payment_intent_id, self.status = code, decline_code, message, payment_intent_id, status


class RequestRejected(ProviderError):
    """Another 4xx (a bad parameter, a bad key, an idempotency_error). Saying it again will not change it."""

    def __init__(self, status: int, error_type: str | None, code: str | None, message: str):
        super().__init__("HTTP %d %s%s: %s" % (status, error_type or "error", " / %s" % code if code else "", message))
        self.status, self.error_type, self.code, self.message = status, error_type, code, message


class ProviderUnavailable(ProviderError):
    """Rate limited or refused before the provider did anything (a 429), and the attempts ran out. Nothing was created, so trying again later is safe."""


class OutcomeUnknown(ProviderError):
    """The request may have reached the provider and the attempts ran out without an answer. The caller must settle it with the SAME idempotency
    key (or by looking the intent up), and must never create again with a new key."""


@dataclass(frozen=True)
class PaymentIntent:
    id: str
    amount: int
    currency: str
    status: str
    metadata: dict[str, str] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


@dataclass(frozen=True)
class Refund:
    id: str
    amount: int
    payment_intent: str
    status: str
    replayed: bool = False


class PaymentProvider(Protocol):
    async def create_payment_intent(self, *, amount_minor: int, currency: str, payment_method: str, metadata: dict[str, str], idempotency_key: str) -> PaymentIntent: ...
    async def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent: ...
    async def find_payment_intent(self, metadata_key: str, metadata_value: str) -> PaymentIntent | None: ...
    async def create_refund(self, *, payment_intent_id: str, amount_minor: int, idempotency_key: str, metadata: dict[str, str] | None = None) -> Refund: ...


def form_fields(params: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten nested parameters the way Stripe's examples write them: {"metadata": {"k": "v"}} becomes metadata[k]=v. Booleans are true or false."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, dict):
            out.extend(("%s[%s]" % (key, sub), _text(v)) for sub, v in value.items())
        elif value is not None:
            out.append((key, _text(value)))
    return out


def _text(value: Any) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


class StripeStyleClient:
    """The client. `http` is an httpx.AsyncClient (a MockTransport in the tests); `api_key` is a secret key, held and sent but never logged or stored."""

    def __init__(self, http: httpx.AsyncClient, api_key: str, *, base_url: str = DEFAULT_BASE_URL, sleep: Sleep = asyncio.sleep,
                 max_attempts: int = MAX_ATTEMPTS, base_delay: float = BASE_DELAY):
        self._http, self._key, self._base, self._sleep = http, api_key, base_url.rstrip("/"), sleep
        self._attempts, self._base_delay = max_attempts, base_delay

    # ------------------------------------------------------------------------------------------------- one request, with the retry rule

    async def _send(self, method: str, path: str, *, params: dict[str, Any] | None = None, idempotency_key: str | None = None,
                    retry: bool = True) -> tuple[dict[str, Any], httpx.Response]:
        """Send one request, retrying with the SAME key on 429, 5xx, 409, 424, and on a timeout or a dropped connection. Returns (json, response).
        Raises CardDeclined for a card_error, RequestRejected for another 4xx, ProviderUnavailable when only 429s were seen, OutcomeUnknown when the last
        failure left the outcome unknown."""
        if idempotency_key is not None and method != "POST":
            raise ValueError("an idempotency key is sent on POST only")
        headers = {}
        if idempotency_key is not None:
            if not 0 < len(idempotency_key) <= 255:
                raise ValueError("an idempotency key is 1 to 255 characters")
            headers["Idempotency-Key"] = idempotency_key
        last_unknown: str | None = None      # set when the last failure may have reached the provider
        last_reason = ""
        for attempt in range(self._attempts if retry else 1):
            try:
                if method == "POST":
                    resp = await self._http.post(self._base + path, content=_encode(form_fields(params or {})), auth=(self._key, ""),
                                                 headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
                else:
                    resp = await self._http.get(self._base + path, params=params, auth=(self._key, ""), headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_unknown, last_reason = None, "could not connect: %s" % type(exc).__name__      # never sent: safe to say nothing happened
            except httpx.TransportError as exc:
                last_unknown, last_reason = "after the request was sent: %s" % type(exc).__name__, "timeout or dropped connection"
            else:
                body = _json(resp)
                code = resp.status_code
                if 200 <= code < 300:
                    return body, resp
                if resp.headers.get("Stripe-Should-Retry", "").lower() == "false" and code >= 400:
                    raise _error_for(resp, body)
                if code == 402 or (400 <= code < 500 and code not in (408, 409, 424, 429)):
                    raise _error_for(resp, body)
                if code == 429:
                    last_unknown, last_reason = None, "rate limited (429)"
                else:
                    last_unknown, last_reason = "HTTP %d" % code, "HTTP %d" % code      # 5xx, 409 and 424: the outcome is indeterminate
                if attempt + 1 < self._attempts and retry:
                    await self._sleep(min(_retry_after(resp) if _retry_after(resp) is not None else self._base_delay * (2 ** attempt), MAX_DELAY))
                    continue
            if attempt + 1 < self._attempts and retry:
                await self._sleep(min(self._base_delay * (2 ** attempt), MAX_DELAY))
        if last_unknown is not None:
            raise OutcomeUnknown("no answer after %d attempts (%s); the same idempotency key must be used to settle it" % (self._attempts, last_unknown))
        raise ProviderUnavailable("gave up after %d attempts: %s" % (self._attempts, last_reason))

    # ------------------------------------------------------------------------------------------------- the four calls

    async def create_payment_intent(self, *, amount_minor: int, currency: str, payment_method: str, metadata: dict[str, str], idempotency_key: str) -> PaymentIntent:
        params = {"amount": amount_minor, "currency": currency.lower(), "payment_method": payment_method, "confirm": True, "metadata": metadata}
        try:
            body, resp = await self._send("POST", "/v1/payment_intents", params=params, idempotency_key=idempotency_key)
        except OutcomeUnknown:
            # the attempts ran out and the intent may exist. A search that finds it is proof; one that finds nothing is not (search lags), so say so.
            found = await self._find_by_all_metadata(metadata)
            if found is not None:
                return found
            raise
        return _intent(body, replayed=resp.headers.get("Idempotent-Replayed") == "true")

    async def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        body, _ = await self._send("GET", "/v1/payment_intents/%s" % payment_intent_id)
        return _intent(body)

    async def find_payment_intent(self, metadata_key: str, metadata_value: str) -> PaymentIntent | None:
        if '"' in metadata_key or '"' in metadata_value or "\\" in metadata_key + metadata_value:
            raise ValueError("a metadata search value cannot hold a quote or a backslash")
        body, _ = await self._send("GET", "/v1/payment_intents/search", params={"query": 'metadata["%s"]:"%s"' % (metadata_key, metadata_value)})
        data = body.get("data") or []
        return _intent(data[0]) if data else None

    async def _find_by_all_metadata(self, metadata: dict[str, str]) -> PaymentIntent | None:
        try:
            # the payment id is unique to this attempt, so one key is enough to find it
            key = "payment_id" if "payment_id" in metadata else next(iter(metadata))
            return await self.find_payment_intent(key, metadata[key])
        except (ProviderError, StopIteration):
            return None

    async def create_refund(self, *, payment_intent_id: str, amount_minor: int, idempotency_key: str, metadata: dict[str, str] | None = None) -> Refund:
        params: dict[str, Any] = {"payment_intent": payment_intent_id, "amount": amount_minor}
        if metadata:
            params["metadata"] = metadata
        body, resp = await self._send("POST", "/v1/refunds", params=params, idempotency_key=idempotency_key)
        return Refund(body["id"], int(body["amount"]), body.get("payment_intent") or payment_intent_id, body.get("status", "succeeded"),
                      replayed=resp.headers.get("Idempotent-Replayed") == "true")


# ----------------------------------------------------------------------------------------------------- helpers

def _encode(fields: list[tuple[str, str]]) -> bytes:
    from urllib.parse import urlencode
    return urlencode(fields).encode("ascii")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_for(resp: httpx.Response, body: dict[str, Any]) -> ProviderError:
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    etype, code, message = err.get("type"), err.get("code"), str(err.get("message") or resp.text[:200] or "HTTP %d" % resp.status_code)
    if etype == "card_error" or resp.status_code == 402:
        pi = err.get("payment_intent") if isinstance(err.get("payment_intent"), dict) else {}
        return CardDeclined(code, err.get("decline_code"), message, pi.get("id"), resp.status_code)
    return RequestRejected(resp.status_code, etype, code, message)


def _intent(body: dict[str, Any], replayed: bool = False) -> PaymentIntent:
    err = body.get("last_payment_error") if isinstance(body.get("last_payment_error"), dict) else {}
    return PaymentIntent(body["id"], int(body["amount"]), str(body["currency"]).upper(), body["status"], dict(body.get("metadata") or {}),
                         err.get("code"), err.get("message"), replayed)
