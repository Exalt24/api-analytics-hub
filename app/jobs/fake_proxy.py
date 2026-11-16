"""A stand-in forward HTTP proxy on 127.0.0.1, for the tests of proxy rotation: it relays a plain HTTP request (the absolute-URI form a browser sends to a proxy) and
records what went through it, so a test can show which proxy carried which session. It speaks no CONNECT and keeps no state beyond the log. It is not a rotating proxy
service; the rotation is browser.ProxyRotator's."""
from __future__ import annotations

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeProxy:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeProxy":
        proxy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                if not self.path.startswith("http://"):
                    self.send_error(400, "a proxy request carries an absolute address")
                    return
                proxy.hits.append(self.path)
                try:
                    with urllib.request.urlopen(urllib.request.Request(self.path, headers={"User-Agent": self.headers.get("User-Agent", "")}), timeout=15) as r:
                        body = r.read()
                        self.send_response(r.status)
                        self.send_header("Content-Type", r.headers.get("Content-Type", "text/html"))
                except Exception as exc:  # the target said no, or was not there
                    body = str(exc).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
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
