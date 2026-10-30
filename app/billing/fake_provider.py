"""A fake Stripe-style provider, as an httpx transport (no socket, nothing real), and a factory for the signed events it would send.

WRITTEN FROM THE SAVED STRIPE PAGES (docs/vendor-contract/stripe/), not from a live account, so it checks that a request has the shape those pages describe
(HTTP Basic auth with the key as the user, form fields with metadata[key], an Idempotency-Key header on POST only, the search query written
metadata["key"]:"value") and answers the way those pages say Stripe answers. It cannot catch a misreading shared with provider.py; only a live account can.

WHAT IT MODELS ABOUT IDEMPOTENCY, from the "Idempotent requests" page and the HTTP-level error handling page:
  - the first request with a key runs and its status and body are saved; a retry with the same key and the same parameters gets the saved answer and the
    header `Idempotent-Replayed: true`, and nothing runs a second time;
  - the same key with different parameters is refused with an idempotency_error (HTTP 400);
  - "Errors that occur before execution begins, such as invalid parameters, an invalid API key, or rate limiting, don't cache a result", so a scripted
    429 or 500 sent before execution saves nothing.
It counts what actually ran (`executions`, `charges`), so a test can say "exactly one charge" and mean it.

FAILURES YOU CAN SCRIPT, each used up in order:
    stripe.fail_before_execution(429, 500)     the next two POSTs are refused before anything runs
    stripe.lose_response_after_execution(1)    the next POST RUNS and is saved, then the client sees a read timeout (the double charge trap); a
                                               replay of a saved answer can be lost the same way
    stripe.search_lag = True                   search finds nothing yet, as the page says it may
The card tokens are invented: `pm_fake_ok` succeeds, `pm_fake_declined` is refused with card_declined / insufficient_funds. No card number appears anywhere.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import parse_qsl

import httpx

from . import signature

#: invented test values, deliberately not shaped like a real key or signing secret
FAKE_API_KEY = "fake-provider-key-invented-for-tests"
FAKE_WEBHOOK_SECRET = "fake-webhook-signing-secret-invented-for-tests"
FAKE_WEBHOOK_SECRET_NEW = "fake-webhook-signing-secret-after-rotation"

_SEARCH_RE = re.compile(r'^metadata\["([^"\\]+)"\]:"([^"\\]*)"$')


def _unflatten(fields: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields:
        m = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", key)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = value
        else:
            out[key] = value
    return out


class FakeStripe:
    def __init__(self, api_key: str = FAKE_API_KEY, *, first_id: int = 0) -> None:
        """`first_id` starts the numbering of the ids it hands out; the command line uses a fresh one for each run so a second run does not reuse the ids of the first."""
        self.api_key = api_key
        self.requests: list[dict[str, Any]] = []
        self.payment_intents: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.charges: list[dict[str, Any]] = []              # one per payment that really succeeded
        self.executions = 0                                  # how many POSTs actually ran (a replay does not count)
        self.search_lag = False
        self._saved: dict[str, tuple[str, int, dict[str, Any]]] = {}      # idempotency key -> (fingerprint, status, body)
        self._before: list[int] = []
        self._lose = 0
        self._counter = first_id

    # ------------------------------------------------------------------------------------------------- scripting

    def fail_before_execution(self, *statuses: int) -> None:
        self._before.extend(statuses)

    def lose_response_after_execution(self, times: int = 1) -> None:
        self._lose += times

    def adopt_succeeded(self, payment_intent_id: str, amount: int, currency: str, metadata: dict[str, str]) -> None:
        """Make this fake know a payment that really succeeded in an EARLIER process (the command line starts a fresh fake each run), so a refund can be asked of it."""
        self.payment_intents[payment_intent_id] = {"id": payment_intent_id, "object": "payment_intent", "amount": amount, "currency": currency.lower(), "metadata": dict(metadata),
                                                   "payment_method": None, "status": "succeeded", "last_payment_error": None, "latest_charge": None}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return "%s_fake%06d" % (prefix, self._counter)

    # ------------------------------------------------------------------------------------------------- the server

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        key = request.headers.get("Idempotency-Key")
        self.requests.append({"method": method, "path": path, "idempotency_key": key, "headers": dict(request.headers), "query": dict(request.url.params)})
        if method == "POST" and self._before:
            status = self._before.pop(0)
            return httpx.Response(status, json={"error": {"type": "api_error" if status >= 500 else "invalid_request_error", "message": "scripted %d" % status}},
                                  headers={"Retry-After": "0"} if status == 429 else {})
        auth = request.headers.get("Authorization", "")
        import base64
        try:
            user = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[0] if auth.startswith("Basic ") else ""
        except Exception:
            user = ""
        if user != self.api_key:
            return httpx.Response(401, json={"error": {"type": "invalid_request_error", "message": "No valid API key provided."}})
        if method == "GET" and key is not None:
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "idempotency keys belong on POST only"}})
        if method == "POST":
            return self._post(request, path, key)
        if method == "GET" and path == "/v1/payment_intents/search":
            return self._search(request)
        m = re.fullmatch(r"/v1/payment_intents/([^/]+)", path)
        if method == "GET" and m:
            pi = self.payment_intents.get(m.group(1))
            return httpx.Response(200, json=pi) if pi else _not_found(m.group(1))
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "unrecognized request URL"}})

    def _post(self, request: httpx.Request, path: str, key: str | None) -> httpx.Response:
        if request.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "form encoded body expected"}})
        fields = parse_qsl(request.content.decode("ascii"), keep_blank_values=True)
        params = _unflatten(fields)
        fingerprint = json.dumps([path, params], sort_keys=True)
        if key is not None:
            saved = self._saved.get(key)
            if saved is not None:
                if saved[0] != fingerprint:
                    return httpx.Response(400, json={"error": {"type": "idempotency_error",
                                                               "message": "Keys for idempotent requests can only be used with the same parameters they were first used with."}})
                self._maybe_lose(request)           # a replayed answer can be lost on the way back as well
                return httpx.Response(saved[1], json=saved[2], headers={"Idempotent-Replayed": "true"})
        if path == "/v1/payment_intents":
            status, body = self._create_intent(params)
        elif path == "/v1/refunds":
            status, body = self._create_refund(params)
        else:
            return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "unrecognized request URL"}})
        self.executions += 1
        if key is not None:
            self._saved[key] = (fingerprint, status, body)
        self._maybe_lose(request)
        return httpx.Response(status, json=body)

    def _maybe_lose(self, request: httpx.Request) -> None:
        if self._lose:
            self._lose -= 1
            raise httpx.ReadTimeout("scripted: the response was lost after the request ran", request=request)

    def _create_intent(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            amount = int(params["amount"])
        except (KeyError, ValueError):
            return 400, {"error": {"type": "invalid_request_error", "code": "parameter_invalid_integer", "param": "amount", "message": "amount must be an integer"}}
        if amount <= 0:
            return 400, {"error": {"type": "invalid_request_error", "param": "amount", "message": "amount must be positive"}}
        currency = params.get("currency", "")
        if currency != currency.lower() or len(currency) != 3:
            return 400, {"error": {"type": "invalid_request_error", "param": "currency", "message": "currency is a three letter lowercase code"}}
        pi_id = self._next("pi")
        pi = {"id": pi_id, "object": "payment_intent", "amount": amount, "currency": currency, "metadata": params.get("metadata") or {},
              "payment_method": params.get("payment_method"), "status": "requires_payment_method", "last_payment_error": None, "latest_charge": None}
        if params.get("confirm") == "true" and params.get("payment_method") == "pm_fake_ok":
            pi["status"] = "succeeded"
            pi["latest_charge"] = self._next("ch")
            self.charges.append({"id": pi["latest_charge"], "payment_intent": pi_id, "amount": amount})
        elif params.get("confirm") == "true":
            pi["last_payment_error"] = {"type": "card_error", "code": "card_declined", "decline_code": "insufficient_funds", "message": "Your card has insufficient funds."}
            self.payment_intents[pi_id] = pi
            return 402, {"error": {**pi["last_payment_error"], "payment_intent": pi}}
        self.payment_intents[pi_id] = pi
        return 200, pi

    def _create_refund(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        pi = self.payment_intents.get(params.get("payment_intent", ""))
        if pi is None or pi["status"] != "succeeded":
            return 400, {"error": {"type": "invalid_request_error", "message": "no such successful payment_intent"}}
        done = sum(r["amount"] for r in self.refunds.values() if r["payment_intent"] == pi["id"])
        amount = int(params.get("amount", pi["amount"] - done))
        if done >= pi["amount"]:
            return 400, {"error": {"type": "invalid_request_error", "code": "charge_already_refunded", "message": "The charge has already been refunded."}}
        if amount <= 0 or amount > pi["amount"] - done:
            return 400, {"error": {"type": "invalid_request_error", "code": "amount_too_large", "param": "amount",
                                   "message": "Refund amount is greater than the unrefunded amount on the charge."}}
        rid = self._next("re")
        refund = {"id": rid, "object": "refund", "amount": amount, "currency": pi["currency"], "payment_intent": pi["id"], "status": "succeeded",
                  "metadata": params.get("metadata") or {}}
        self.refunds[rid] = refund
        return 200, refund

    def _search(self, request: httpx.Request) -> httpx.Response:
        m = _SEARCH_RE.match(request.url.params.get("query", ""))
        if not m:
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "unsupported search query"}})
        hits = [] if self.search_lag else [p for p in self.payment_intents.values() if p["metadata"].get(m.group(1)) == m.group(2)]
        return httpx.Response(200, json={"object": "search_result", "url": "/v1/payment_intents/search", "has_more": False, "data": hits})


def _not_found(ident: str) -> httpx.Response:
    return httpx.Response(404, json={"error": {"type": "invalid_request_error", "code": "resource_missing", "message": "No such payment_intent: %r" % ident}})


# ----------------------------------------------------------------------------------------------------- events the provider would send

class Events:
    """Builds events shaped like the saved pages' (id, object "event", type, created, data.object, livemode, pending_webhooks, request) and signs them."""

    def __init__(self, secrets: tuple[str, ...] = (FAKE_WEBHOOK_SECRET,), *, first_id: int = 0) -> None:
        self.secrets = secrets
        self._n = first_id

    def event(self, event_type: str, obj: dict[str, Any], *, created: int | None = None, event_id: str | None = None) -> dict[str, Any]:
        self._n += 1
        return {"id": event_id or "evt_fake%06d" % self._n, "object": "event", "api_version": "fake", "created": int(created if created is not None else time.time()),
                "data": {"object": obj}, "livemode": False, "pending_webhooks": 1, "request": None, "type": event_type}

    def payment_intent(self, event_type: str, pi: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return self.event(event_type, pi, **kw)

    def charge_refunded(self, payment_intent_id: str, amount: int, amount_refunded: int, currency: str = "aud", **kw: Any) -> dict[str, Any]:
        charge = {"id": "ch_fake_for_" + payment_intent_id, "object": "charge", "amount": amount, "amount_refunded": amount_refunded, "currency": currency.lower(),
                  "payment_intent": payment_intent_id, "refunded": amount_refunded >= amount, "status": "succeeded"}
        return self.event("charge.refunded", charge, **kw)

    def invoice(self, event_type: str, customer: str, amount_paid: int, currency: str = "aud", invoice_id: str = "in_fake000001", **kw: Any) -> dict[str, Any]:
        inv = {"id": invoice_id, "object": "invoice", "customer": customer, "amount_paid": amount_paid, "currency": currency.lower(),
               "status": "paid" if event_type == "invoice.paid" else "open", "period_start": 0, "period_end": 0}
        return self.event(event_type, inv, **kw)

    def subscription(self, event_type: str, subscription_id: str, customer: str, status: str, price_id: str | None = None, **kw: Any) -> dict[str, Any]:
        sub = {"id": subscription_id, "object": "subscription", "customer": customer, "status": status,
               "items": {"object": "list", "data": [{"id": "si_fake000001", "price": {"id": price_id}}] if price_id else []}}
        return self.event(event_type, sub, **kw)

    @staticmethod
    def body(event: dict[str, Any]) -> bytes:
        """The raw bytes a sender posts. Compact JSON, so a test that changes one byte changes the signature."""
        return json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def signed(self, event: dict[str, Any], *, timestamp: int | None = None) -> tuple[bytes, str]:
        """(body, Stripe-Signature header) with one v1 signature for each active secret."""
        body = self.body(event)
        return body, signature.build_header(body, self.secrets, int(timestamp if timestamp is not None else time.time()))
