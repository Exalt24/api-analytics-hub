# LIFESPAN: permanent (stand-in for the Twilio Messages API, used by tests/test_sms.py and tests/mutation_check_sms.py)
"""A stand-in for the Twilio Programmable Messaging REST API, on localhost, random port, no network.

Serves exactly the two documented routes:

    POST /2010-04-01/Accounts/{AccountSid}/Messages.json     create a message (form encoded, basic auth)
    GET  /2010-04-01/Accounts/{AccountSid}/Messages/{Sid}.json   fetch its current status

and the documented shapes: a Message resource with sid, status, to, from, body, error_code and
error_message; errors as JSON with status, message, code and more_info. It was written from the
documentation, not from observing the real service.

One thing is NOT from Twilio's documentation: idempotency. The Messages page documents no key, so
the stand-in invents one (header `Idempotency-Key`; a repeat is refused with 409 and the first
message's sid in `existing_sid`). That is the contract app/sms.py is written against.

Switches, set as attributes before or between calls (each is documented where it is read):

    script          statuses a message walks through on successive GETs: "delivered", "undelivered", "failed"
    fail_posts      list of faults, one consumed per POST: "429", "503", "lost", "slow", "malformed", "401"
    enforce_idempotency   True: a repeated key is refused with 409. False: the repeat creates a second message.
    fail_gets       faults for fetches, one consumed per GET: "lost", "503"
    retry_after     value of the Retry-After header on a 429, or None for no header
    leak_secret_in_errors  the 401 text repeats the auth token, to prove the client scrubs vendor text
    reject_numbers  numbers answered 400 with code 21211 and the number echoed in the text
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

API = "/2010-04-01"
SCRIPTS = {
    "delivered": [("queued", None), ("sending", None), ("sent", None), ("delivered", None)],
    # 30007 is Twilio's "Message filtered" error: the carrier blocked it after it was accepted.
    "undelivered": [("queued", None), ("sent", None), ("undelivered", 30007)],
    "failed": [("queued", None), ("failed", 30008)],
    "stuck": [("queued", None)],     # never reaches a final status, to show polling is bounded
}


class StandIn:
    def __init__(self, account_sid: str, auth_token: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.script = "delivered"
        self.fail_posts: list[str] = []
        self.retry_after: str | None = "7"   # None: a 429 without the header
        self.fail_gets: list[str] = []          # faults for fetches: "lost" or "503"
        self.leak_secret_in_errors = False      # True: the 401 text repeats the token, to test our scrubbing
        self.slow_seconds = 1.0
        self.enforce_idempotency = True
        self.reject_numbers: set[str] = set()   # numbers answered 400/21211, with the number echoed back as Twilio's text does
        self.messages: list[dict] = []          # every message actually created
        self.requests: list[dict] = []          # every request seen, whatever the answer
        self._by_key: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def reset(self) -> None:
        """Forget every message, request and queued fault, keeping the server and its port."""
        with self._lock:
            self.messages.clear()
            self.requests.clear()
            self._by_key.clear()
            self.fail_posts.clear()
            self.fail_gets.clear()

    # -- lifecycle

    def start(self) -> "StandIn":
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def do_POST(self):
                stand_in._handle(self, "POST")

            def do_GET(self):
                stand_in._handle(self, "GET")

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
        return [r for r in self.requests if r["method"] == "POST"]

    # -- request handling

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
        record = {"method": method, "path": h.path, "headers": dict(h.headers.items()), "form": form}
        with self._lock:
            self.requests.append(record)

        if not self._authorised(h):
            return self._unauthorised(h)
        base = f"{API}/Accounts/{self.account_sid}"
        if method == "POST" and h.path == f"{base}/Messages.json":
            return self._create(h, form, record)
        if method == "GET" and h.path.startswith(f"{base}/Messages/") and h.path.endswith(".json"):
            return self._fetch(h, h.path[len(f"{base}/Messages/"):-len(".json")])
        return self._error(h, 404, 20404, "The requested resource %s was not found" % h.path)

    def _authorised(self, h) -> bool:
        want = "Basic " + base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        return h.headers.get("Authorization") == want

    def _create(self, h, form, record) -> None:
        with self._lock:
            fault = self.fail_posts.pop(0) if self.fail_posts else None
        if fault == "429":
            extra = {"Retry-After": self.retry_after} if self.retry_after is not None else None
            return self._error(h, 429, 20429, "Too many requests", extra=extra)
        if fault == "503":
            return self._error(h, 503, 20503, "Service temporarily unavailable")
        if fault == "401":
            return self._unauthorised(h)
        if form.get("To") in self.reject_numbers:
            return self._error(h, 400, 21211, "The 'To' number %s is not a valid phone number." % form.get("To", ""))
        if not form.get("Body"):
            return self._error(h, 400, 21602, "Message body is required.")
        if not (form.get("From") or form.get("MessagingServiceSid")):
            return self._error(h, 400, 21603, "A 'From' or 'MessagingServiceSid' parameter is required.")
        if "application/x-www-form-urlencoded" not in h.headers.get("Content-Type", ""):
            return self._error(h, 400, 20001, "Content-Type must be application/x-www-form-urlencoded")

        key = h.headers.get("Idempotency-Key")
        with self._lock:
            first = self._by_key.get(key) if key else None
            if first is not None and self.enforce_idempotency:
                body = {"status": 409, "code": 20409, "message": "Idempotency-Key already used",
                        "more_info": "https://www.twilio.com/docs/errors/20409",
                        "existing_sid": first["sid"], "existing_status": first["status"]}
                return self._send(h, 409, body)
            sid = "SM" + "%032x" % (len(self.messages) + 1)
            message = {
                "sid": sid, "account_sid": self.account_sid, "api_version": "2010-04-01",
                "to": form["To"], "from": form.get("From"), "messaging_service_sid": form.get("MessagingServiceSid"),
                "body": form["Body"], "status": "queued", "error_code": None, "error_message": None,
                "direction": "outbound-api", "num_segments": "1",
                "uri": f"{API}/Accounts/{self.account_sid}/Messages/{sid}.json",
                "_gets": 0, "_script": SCRIPTS[self.script],
            }
            self.messages.append(message)
            if key:
                self._by_key[key] = message
        # The message exists from here on. The faults below only change what the caller sees.
        if fault == "lost":
            h.close_connection = True   # no status line: the client sees the connection drop
            return
        if fault == "slow":
            time.sleep(self.slow_seconds)
            return self._send(h, 201, self._public(message))
        if fault == "malformed":
            return self._send(h, 201, None, raw=b"{not json")
        return self._send(h, 201, self._public(message))

    def _unauthorised(self, h) -> None:
        text = "Authenticate"
        if self.leak_secret_in_errors:
            text += " (token %s rejected for account %s)" % (self.auth_token, self.account_sid)
        self._error(h, 401, 20003, text)

    def _fetch(self, h, sid: str) -> None:
        with self._lock:
            fault = self.fail_gets.pop(0) if self.fail_gets else None
        if fault == "lost":
            h.close_connection = True
            return
        if fault == "503":
            return self._error(h, 503, 20503, "Service temporarily unavailable")
        with self._lock:
            message = next((m for m in self.messages if m["sid"] == sid), None)
            if message is None:
                return self._error(h, 404, 20404, "The requested resource was not found")
            step = message["_script"][min(message["_gets"], len(message["_script"]) - 1)]
            message["_gets"] += 1
            message["status"], message["error_code"] = step
            if step[1]:
                message["error_message"] = "Message filtered" if step[1] == 30007 else "Unknown error"
        self._send(h, 200, self._public(message))

    # -- answers

    @staticmethod
    def _public(message: dict) -> dict:
        return {k: v for k, v in message.items() if not k.startswith("_")}

    def _error(self, h, status: int, code: int, text: str, extra: dict | None = None) -> None:
        body = {"code": code, "message": text, "more_info": "https://www.twilio.com/docs/errors/%d" % code, "status": status}
        self._send(h, status, body, headers=extra)

    @staticmethod
    def _send(h, status: int, body, *, raw: bytes | None = None, headers: dict | None = None) -> None:
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)
