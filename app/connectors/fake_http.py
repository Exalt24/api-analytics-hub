"""The shared body of the stand-in vendor servers for Google Ads, TikTok, Square, QuickBooks and Shopmonkey.

Each stand-in speaks real HTTP on 127.0.0.1 to the connector and answers what the vendor's public documentation
describes (the page each rule came from is named in docs/AD_AND_BUSINESS_SOURCES.md). This base owns only the
plumbing the five have in common: a threaded server, a log of every request, and scripted faults.

Faults are scripted: `server.faults.append(Fault(429, body, headers))` makes the next request answer that way,
once per entry, before the normal answer resumes. `Fault(drop=True)` drops the connection without answering, a
transport failure.

It is a stand-in written from documentation, not a recording of a vendor. Whatever a subclass invents (an HTTP
status on an error the pages do not give, a cursor format, a rate figure) is listed in its own docstring as its own
choice and in the sources page as UNVERIFIED.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class Fault:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    #: True makes the server drop the connection without answering, a transport failure.
    drop: bool = False


@dataclass
class Seen:
    """One request the stand-in received."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    #: Every value of every query parameter, in order: `query` keeps only the first, which loses a repeated parameter.
    query_all: dict[str, list[str]] = field(default_factory=dict)


Answer = tuple[int, dict[str, str], Any]


class FakeHTTP:
    """Start with `with FakeX(...) as server:`, then point the connector at `server.url`."""

    #: The content type the stand-in answers with.
    content_type = "application/json; charset=UTF-8"

    def __init__(self) -> None:
        self.faults: list[Fault] = []
        self.requests: list[Seen] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ to override

    def answer(self, seen: Seen) -> Answer:
        raise NotImplementedError

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parsed = urlparse(self.path)
                query_all = parse_qs(parsed.query, keep_blank_values=True)
                query = {k: v[0] for k, v in query_all.items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, out_headers, payload, drop = fake._handle(Seen(self.command, parsed.path, query, headers, body, query_all))
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", fake.content_type)
                self.send_header("Content-Length", str(len(data)))
                for name, value in out_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve  # noqa: N815 - the names http.server looks for

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def _handle(self, seen: Seen) -> tuple[int, dict[str, str], Any, bool]:
        with self._lock:
            self.requests.append(seen)
            if self.faults:
                fault = self.faults.pop(0)
                if fault.drop:
                    return 0, {}, b"", True
                return fault.status, dict(fault.headers), fault.body if fault.body is not None else {}, False
            status, headers, payload = self.answer(seen)
            return status, headers, payload, False
