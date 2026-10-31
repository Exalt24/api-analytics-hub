"""The payment provider on the official `stripe` Python SDK: create a PaymentIntent for a wallet top-up, retrieve it, find one by metadata, refund it, and verify
a webhook with the SDK's own `stripe.Webhook.construct_event`. It implements the same `PaymentProvider` protocol as provider.py, so payments.py and wallet.py do not change.

WHAT IT WAS RUN AGAINST. stripe-mock (Stripe's own local mock server, https://github.com/stripe/stripe-mock) and an httpx stand-in on the SDK's transport. No Stripe account
or key exists for this project, so nothing here has ever reached api.stripe.com. stripe-mock checks each request against Stripe's OpenAPI spec (a wrong parameter name or
type is refused) and answers with canned objects that echo only a few request fields; it keeps no state, so a retrieve does not return what a create made, and it
sends no webhooks. The stand-in (fake_provider.py) is what scripts a card decline, a 429, a 500 and a lost response; it was written from the saved pages, so it shares
their misreadings. The SDK's own behaviour (the same Idempotency-Key on its own retry, the error classes) is the real SDK in both cases.

WHAT THE SDK DOES FOR US, and what we still do:
  - It retries a POST that failed with a connection error, a 409 or a 5xx, up to `max_network_retries`, and sends the SAME Idempotency-Key on every attempt. The
    key is ours (stored on the payment row before the call), passed in `options`, so a retry can never charge twice. It does NOT retry a 429.
  - It cannot tell "never connected" from "sent and the answer was lost", so every connection error and 5xx is OutcomeUnknown here, settled later with the same key.
    A 429 is ProviderUnavailable (nothing ran, so the payment stays "created" and is asked again with the same key). A card_error is CardDeclined and a refusal of the request itself is RequestRejected; neither is retried.
  - A search that finds nothing after an unknown outcome proves nothing (Stripe says search can lag), so create_payment_intent looks the intent up by the metadata it
    put on it and, finding nothing, still raises OutcomeUnknown.

THE WEBHOOK CHECK is `stripe.Webhook.construct_event(payload, header, secret, tolerance)`: the SDK parses the header, computes the HMAC over the raw bytes, compares in
constant time and applies the recency window. `verify_webhook` adds only what the SDK leaves open: a tolerance of zero or less (the SDK treats it as "no recency check"),
no secret or no header, several secrets while one is rolled, and the conversion of the SDK's errors to WebhookRejected, whose reason goes to our log and never to the sender.
The SDK reads its own clock, so `now` is accepted for the verifier interface and ignored.
"""
from __future__ import annotations

from typing import Any

import stripe

from . import signature
from .errors import WebhookRejected
from .provider import (CardDeclined, OutcomeUnknown, PaymentIntent, ProviderUnavailable, Refund, RequestRejected, _intent)

DEFAULT_MAX_NETWORK_RETRIES = 2


def _error_body(exc: stripe.StripeError) -> dict[str, Any]:
    body = exc.json_body if isinstance(exc.json_body, dict) else {}
    err = body.get("error")
    return err if isinstance(err, dict) else {}


def _translate(exc: stripe.StripeError) -> Exception:
    """The SDK's exception as this module's own. Never returns a retryable error for a card decline."""
    err = _error_body(exc)
    if isinstance(exc, stripe.CardError):
        pi = err.get("payment_intent") if isinstance(err.get("payment_intent"), dict) else {}
        return CardDeclined(err.get("code"), err.get("decline_code"), str(err.get("message") or exc), pi.get("id"), exc.http_status or 402)
    if isinstance(exc, stripe.RateLimitError):
        return ProviderUnavailable("rate limited (429) and the SDK's retries ran out")
    if isinstance(exc, (stripe.APIConnectionError, stripe.APIError)) and not isinstance(exc, stripe.InvalidRequestError):
        return OutcomeUnknown("no answer after the SDK's retries (%s); the same idempotency key must be used to settle it" % type(exc).__name__)
    return RequestRejected(exc.http_status or 0, err.get("type") or type(exc).__name__, err.get("code"), str(err.get("message") or exc))


def _replayed(obj: Any) -> bool:
    response = getattr(obj, "last_response", None)
    headers = getattr(response, "headers", None)
    return bool(headers) and headers.get("Idempotent-Replayed") == "true"


class StripeSdkProvider:
    """`api_key` is a secret key, held by the SDK and never logged or stored here. `api_base` points the SDK at stripe-mock (http://127.0.0.1:12111); left out, it is
    api.stripe.com. `http_client` is for a test that puts a transport under the SDK."""

    def __init__(self, api_key: str, *, api_base: str | None = None, max_network_retries: int = DEFAULT_MAX_NETWORK_RETRIES,
                 http_client: stripe.HTTPClient | None = None):
        self._client = stripe.StripeClient(api_key, base_addresses={"api": api_base} if api_base else None,
                                           max_network_retries=max_network_retries, http_client=http_client)

    # ------------------------------------------------------------------------------------------------- the four calls

    async def create_payment_intent(self, *, amount_minor: int, currency: str, payment_method: str, metadata: dict[str, str], idempotency_key: str) -> PaymentIntent:
        if not 0 < len(idempotency_key) <= 255:
            raise ValueError("an idempotency key is 1 to 255 characters")
        # allow_redirects "never": a confirmed intent with automatic payment methods otherwise wants a return_url, and a wallet top-up by card has no page to return to
        params = {"amount": amount_minor, "currency": currency.lower(), "payment_method": payment_method, "confirm": True, "metadata": metadata,
                  "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"}}
        try:
            pi = await self._client.v1.payment_intents.create_async(params=params, options={"idempotency_key": idempotency_key})
        except stripe.StripeError as exc:
            failure = _translate(exc)
            if isinstance(failure, OutcomeUnknown):
                found = await self._find_by_all_metadata(metadata)
                if found is not None:
                    return found
            raise failure from exc
        return _intent(pi.to_dict(), replayed=_replayed(pi))

    async def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        try:
            pi = await self._client.v1.payment_intents.retrieve_async(payment_intent_id)
        except stripe.StripeError as exc:
            raise _translate(exc) from exc
        return _intent(pi.to_dict())

    async def find_payment_intent(self, metadata_key: str, metadata_value: str) -> PaymentIntent | None:
        if '"' in metadata_key or '"' in metadata_value or "\\" in metadata_key + metadata_value:
            raise ValueError("a metadata search value cannot hold a quote or a backslash")
        try:
            found = await self._client.v1.payment_intents.search_async(params={"query": 'metadata["%s"]:"%s"' % (metadata_key, metadata_value)})
        except stripe.StripeError as exc:
            raise _translate(exc) from exc
        data = found.data or []
        return _intent(data[0].to_dict()) if data else None

    async def _find_by_all_metadata(self, metadata: dict[str, str]) -> PaymentIntent | None:
        try:
            # the payment id is unique to this attempt, so one key is enough to find it
            key = "payment_id" if "payment_id" in metadata else next(iter(metadata))
            return await self.find_payment_intent(key, metadata[key])
        except (Exception, StopIteration):
            return None

    async def create_refund(self, *, payment_intent_id: str, amount_minor: int, idempotency_key: str, metadata: dict[str, str] | None = None) -> Refund:
        if not 0 < len(idempotency_key) <= 255:
            raise ValueError("an idempotency key is 1 to 255 characters")
        params: dict[str, Any] = {"payment_intent": payment_intent_id, "amount": amount_minor}
        if metadata:
            params["metadata"] = metadata
        try:
            refund = await self._client.v1.refunds.create_async(params=params, options={"idempotency_key": idempotency_key})
        except stripe.StripeError as exc:
            raise _translate(exc) from exc
        return Refund(refund.id, int(refund.amount), refund.payment_intent or payment_intent_id, refund.status or "succeeded", replayed=_replayed(refund))


def verify_webhook(payload: bytes, header: str | None, secrets: tuple[str, ...] | list[str], *, now: float | None = None,
                   tolerance: int = signature.DEFAULT_TOLERANCE_SECONDS) -> None:
    """Return if `header` is a valid, recent Stripe signature of `payload` under any of `secrets`; raise WebhookRejected otherwise. Same shape as signature.verify,
    so webhooks.receive takes either."""
    if tolerance <= 0:
        raise ValueError("a tolerance of zero or less disables the recency check; use a positive number of seconds")
    if not secrets:
        raise WebhookRejected("no signing secret configured")
    if not header:
        raise WebhookRejected("no signature header")
    # the SDK compares with hmac.compare_digest, which raises TypeError on non-ASCII text and would turn a rejection into a 500; a signature is hex, so non-ASCII is simply wrong
    if not header.isascii():
        raise WebhookRejected("non-ascii header")
    reason = "signature mismatch"
    for secret in secrets:
        try:
            stripe.Webhook.construct_event(payload, header, secret, tolerance=tolerance)
            return
        except stripe.SignatureVerificationError as exc:
            reason = str(exc)
        except ValueError as exc:       # a signed body that is not JSON; the signature of a payload is only checked over its bytes
            raise WebhookRejected("body is not JSON") from exc
    raise WebhookRejected(reason)
