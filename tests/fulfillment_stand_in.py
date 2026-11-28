# LIFESPAN: permanent (stand-in for a fulfilment partner's order API, used by the fulfilment tests and tests/mutation_check_fulfillment.py)
"""A stand-in for a fulfilment partner's REST API, on localhost, random port, no network. It is written from the CONTRACT in docs/fulfillment.md, which we wrote; it is not a real partner.

    POST /v1/orders     Authorization: Bearer <key>, Content-Type: application/json, Idempotency-Key: <8 to 120 characters>, body {"order_ref": "ord_...", "clinic_ref": "..."}
        201 {"partner_order_id": "po_..."}   the order was made
        200 the same body, header `Idempotent-Replayed: true`   the same key with the same body again: the first answer, nothing made twice
        400 {"code": "invalid_order"}  a body that is not the two fields, or an order_ref that is not `ord_` and 24 hex digits
        400 {"code": "idempotency_key_required"}
        401 {"code": "unauthorized"}
        415 {"code": "json_required"}
        422 {"code": "idempotency_key_reused"}  the same key with a different body
        422 {"code": "duplicate_order"}  an order_ref that exists, under another key
        409 {"code": "request_in_progress"} + Retry-After   the first copy of this key is still being handled (retry with the same key)
        429 + Retry-After   too many requests;  500 / 503   a fault on the partner's side

The status events go the other way: the partner POSTs them to us. `event()` builds one the way the partner would send it (JSON body, `X-Partner-Signature: t=<time>,v1=<hex>` over `<time>.<body>`
under the shared secret) and returns (body bytes, header), for a test to hand to the receiver. `extra` adds fields the contract does NOT define (a recipient name, an address): a real partner
may send more than it says, and the receiver must not keep any of it.

Switches, set as attributes before or between calls, each used up in order where it is a list:
    fail_posts   faults, one consumed per POST: "429", "503", "500", "401", "400", "422", "409", "lost" (the order IS made, then the connection drops: the answer is lost), "dropped"
                 (the connection drops BEFORE anything is made), "bad_body" (a 201 whose body has no partner_order_id)
    retry_after  the value of the Retry-After header on a 429 or 409 (None for no header)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIGNATURE_HEADER = "X-Partner-Signature"
_REF = re.compile(r"^ord_[0-9a-f]{24}$")


class StandIn:
    def __init__(self, api_key: str, signing_secret: str):
        self.api_key, self.signing_secret = api_key, signing_secret
        self.fail_posts: list[str] = []
        self.retry_after: str | None = "2"
        self.requests: list[dict] = []          # every request seen, whatever the answer
        self.orders: dict[str, dict] = {}       # order_ref -> what was made
        self._by_key: dict[str, tuple[str, dict]] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0

    # -- lifecycle

    def start(self) -> "StandIn":
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                stand_in._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    @property
    def posts(self) -> list[dict]:
        return list(self.requests)

    # -- the partner's side of the status events

    def event(self, order_ref: str, status: str, *, event_id: str, carrier: str | None = None, tracking_number: str | None = None, timestamp: int | None = None,
              extra: dict | None = None, secret: str | None = None) -> tuple[bytes, str]:
        body: dict = {"event_id": event_id, "order_ref": order_ref, "status": status, "occurred_at": int(timestamp or time.time())}
        if carrier is not None:
            body["carrier"] = carrier
        if tracking_number is not None:
            body["tracking_number"] = tracking_number
        body.update(extra or {})
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ts = int(timestamp or time.time())
        mac = hmac.new((secret or self.signing_secret).encode("utf-8"), str(ts).encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
        return raw, "t=%d,v1=%s" % (ts, mac)

    # -- request handling

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        record = {"path": h.path, "headers": dict(h.headers.items()), "body": raw}
        with self._lock:
            self.requests.append(record)
            fault = self.fail_posts.pop(0) if self.fail_posts else None
        if h.path != "/v1/orders":
            return self._send(h, 404, {"code": "not_found"})
        if fault == "dropped":
            h.close_connection = True
            return
        if h.headers.get("Authorization") != "Bearer " + self.api_key or fault == "401":
            return self._send(h, 401, {"code": "unauthorized"})
        if fault in ("429", "409"):
            extra = {"Retry-After": self.retry_after} if self.retry_after is not None else None
            return self._send(h, int(fault), {"code": "too_many_requests" if fault == "429" else "request_in_progress"}, headers=extra)
        if fault in ("503", "500"):
            return self._send(h, int(fault), {"code": "unavailable"})
        if fault in ("400", "422"):
            return self._send(h, int(fault), {"code": "invalid_order" if fault == "400" else "duplicate_order"})
        if "application/json" not in h.headers.get("Content-Type", ""):
            return self._send(h, 415, {"code": "json_required"})
        key = h.headers.get("Idempotency-Key")
        if not key or not 8 <= len(key) <= 120:
            return self._send(h, 400, {"code": "idempotency_key_required"})
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or set(body) != {"order_ref", "clinic_ref"} or not isinstance(body["order_ref"], str) or not _REF.match(body["order_ref"]) \
                or not isinstance(body["clinic_ref"], str):
            return self._send(h, 400, {"code": "invalid_order"})
        fingerprint = json.dumps(body, sort_keys=True)
        with self._lock:
            saved = self._by_key.get(key)
            if saved is not None:
                if saved[0] != fingerprint:
                    return self._send(h, 422, {"code": "idempotency_key_reused"})
                first = saved[1]
                if fault == "lost":
                    h.close_connection = True
                    return
                return self._send(h, 200, {"partner_order_id": first["partner_order_id"]}, headers={"Idempotent-Replayed": "true"})
            if body["order_ref"] in self.orders:
                return self._send(h, 422, {"code": "duplicate_order"})
            self._seq += 1
            made = {"partner_order_id": "po_%06d" % self._seq, "order_ref": body["order_ref"], "clinic_ref": body["clinic_ref"], "key": key}
            self.orders[body["order_ref"]] = made
            self._by_key[key] = (fingerprint, made)
        if fault == "lost":
            h.close_connection = True       # the order exists from here on; only the answer is lost
            return
        if fault == "bad_body":
            return self._send(h, 201, {"ok": True})
        self._send(h, 201, {"partner_order_id": made["partner_order_id"]})

    @staticmethod
    def _send(h, status: int, body, *, headers: dict | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)
