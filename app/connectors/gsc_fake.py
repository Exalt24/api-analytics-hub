"""A stand-in Search Console API server on 127.0.0.1, for tests and the sample deck.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/GSC_SOURCES.md): the
path `/webmasters/v3/sites/{siteUrl}/searchAnalytics/query` with the property URL percent-encoded in full, a
POST body with `startDate`, `endDate`, `dimensions`, `type`, `dataState`, `rowLimit` (1 to 25,000) and
`startRow`, rows of `keys`, `clicks`, `impressions`, `ctr` (0 to 1) and `position`, days without data left
out of a date query, a date query sorted by date ascending and any other sorted by clicks descending with
ties in an arbitrary order, `startRow` beyond the end answering a successful empty page, `dataState` `final`
(the default) holding back the newest days and `all` including them with `metadata.first_incomplete_date`,
and Google's error object for 400, 401, 403, 429 and 5xx.

It is a stand-in written from documentation, not a recording of Google. What it cannot tell you is listed in
docs/GSC_SOURCES.md. Where it refuses something (a rowLimit above 25,000, a dimension named twice, a start
after the end) it does so because a page says the real API would. The choices that are its own: the number of
days data takes to become final (3, from "typically available after 2-3 days"), and the 403 reason it gives an
unknown property.

Faults are scripted: `server.faults.extend([Fault(429, error_body(429, "rateLimitExceeded", "..."))])` makes the
next request answer that way, once per entry, before the normal answer resumes.
"""
from __future__ import annotations

import json
import random
import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import unquote

KNOWN_DIMENSIONS = ("country", "device", "page", "query", "searchAppearance", "date", "hour")
KNOWN_TYPES = ("discover", "googleNews", "news", "image", "video", "web")
KNOWN_STATES = ("all", "final", "hourly_all")
MAX_ROW_LIMIT = 25_000
#: Days before today (Pacific) that data is still being collected.
FINAL_LAG_DAYS = 3
_PATH_RE = re.compile(r"^/webmasters/v3/sites/([^/]+)/searchAnalytics/query$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Fault:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    #: True makes the server drop the connection without answering, a transport failure.
    drop: bool = False


def error_body(code: int, reason: str, message: str) -> dict:
    """The global error object from the standard errors page."""
    return {"error": {"errors": [{"domain": "global", "reason": reason, "message": message}],
                      "code": code, "message": message}}


class FakeGSC:
    """Start with `with FakeGSC(token="t") as server:`, then point the connector at `server.url`."""

    def __init__(self, *, token: str = "test-token", today: date | None = None):
        self.token = token
        #: Today in Pacific time, which is what the page's dates are in.
        self.today = today or date(2026, 11, 2)
        self.faults: list[Fault] = []
        #: property -> {"days": {day: (clicks, impressions, position)}, "queries": {day: [(key, clicks, impressions, position)]}, "pages": ...}
        self.sites: dict[str, dict[str, dict]] = {}
        #: Every request seen: (the decoded property from the path, parsed body or None, Authorization header, the raw path).
        self.requests: list[tuple[str, dict | None, str | None, str]] = []
        #: How clicks and impressions are written in the answer: "int", "float" (12.0) or "string" ("12").
        self.number_style = "int"
        #: Order rows that tie on clicks differently on every request, as the page says they may be.
        self.shuffle_ties = False
        self._rng = random.Random(7)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "FakeGSC":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, headers, body, drop = fake.handle(self.path, raw, self.headers.get("Authorization"))
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

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

    def set_site(self, site_url: str, days: Mapping[date, tuple], *, queries: Mapping[date, list] | None = None,
                 pages: Mapping[date, list] | None = None) -> None:
        """`days` maps a day to (clicks, impressions, position); `queries` and `pages` map a day to a list of (key, clicks, impressions, position)."""
        with self._lock:
            self.sites[site_url] = {"days": dict(days), "queries": dict(queries or {}), "pages": dict(pages or {})}

    # ------------------------------------------------------------------ the request

    def handle(self, path: str, raw: bytes, auth: str | None) -> tuple[int, dict[str, str], Any, bool]:
        with self._lock:
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            match = _PATH_RE.match(path)
            site = unquote(match.group(1)) if match else ""
            self.requests.append((site, parsed, auth, path))
            if self.faults:
                fault = self.faults.pop(0)
                if fault.drop:
                    return 0, {}, b"", True
                body = fault.body if fault.body is not None else error_body(500, "internalError", "scripted fault")
                return fault.status, dict(fault.headers), body, False
            return self._answer(match, site, parsed, auth)

    def _answer(self, match: re.Match | None, site: str, body: dict | None, auth: str | None) -> tuple[int, dict[str, str], Any, bool]:
        if match is None:
            return 404, {}, error_body(404, "notFound", "The requested operation failed because a resource could not be found."), False
        if auth != "Bearer " + self.token:
            return 401, {}, error_body(401, "authError", "Invalid Credentials"), False
        if site not in self.sites:
            return 403, {}, error_body(403, "forbidden", "User does not have sufficient permission for site '%s'." % site), False
        if not isinstance(body, dict):
            return 400, {}, error_body(400, "parseError", "the body is not a JSON object"), False
        problem = self._validate(body)
        if problem:
            return 400, {}, error_body(400, "invalidParameter", problem), False

        start, end = date.fromisoformat(body["startDate"]), date.fromisoformat(body["endDate"])
        dims = body.get("dimensions") or []
        state = str(body.get("dataState", "final")).lower()
        data = self.sites[site]
        final_through = self.today - timedelta(days=FINAL_LAG_DAYS)
        last = end if state == "all" else min(end, final_through)
        rows = self._rows(data, dims, start, last)
        limit = body.get("rowLimit", 1000)
        offset = body.get("startRow", 0)
        page = rows[offset: offset + limit]
        out: dict[str, Any] = {"rows": [self._wire(r) for r in page], "responseAggregationType": "auto"} if page else {
            "responseAggregationType": "auto"}
        if state == "all" and dims == ["date"] and end > final_through and start <= end:
            out["metadata"] = {"first_incomplete_date": max(start, final_through + timedelta(days=1)).isoformat()}
        return 200, {}, out, False

    def _rows(self, data: Mapping[str, Any], dims: list[str], start: date, last: date) -> list[tuple]:
        """(keys, clicks, impressions, position) rows in the order the page promises."""
        days = [d for d in sorted(data["days"]) if start <= d <= last]
        if dims == ["date"]:
            return [([d.isoformat()], *data["days"][d]) for d in days]
        source = data["queries"] if dims == ["query"] else data["pages"] if dims == ["page"] else {}
        merged: dict[str, list[float]] = {}
        for d in days:
            for key, clicks, impressions, position in source.get(d, []):
                m = merged.setdefault(key, [0, 0, 0.0])
                m[0] += clicks
                m[1] += impressions
                m[2] += position * impressions
        rows = [([k], m[0], m[1], (m[2] / m[1]) if m[1] else 0.0) for k, m in merged.items()]
        rows.sort(key=lambda r: r[0])
        if self.shuffle_ties:
            self._rng.shuffle(rows)
        rows.sort(key=lambda r: -r[1])
        return rows

    def _wire(self, row: tuple) -> dict[str, Any]:
        keys, clicks, impressions, position = row
        def num(v: int) -> Any:
            return float(v) if self.number_style == "float" else str(v) if self.number_style == "string" else v
        return {"keys": keys, "clicks": num(clicks), "impressions": num(impressions),
                "ctr": (clicks / impressions) if impressions else 0.0, "position": position}

    def _validate(self, body: dict) -> str | None:
        """The refusals the saved pages describe; None when the request would be accepted."""
        for name in ("startDate", "endDate"):
            text = body.get(name)
            if not isinstance(text, str) or not _DATE_RE.match(text):
                return "%s must be YYYY-MM-DD" % name
            try:
                date.fromisoformat(text)
            except ValueError:
                return "%s is not a calendar day" % name
        if body["startDate"] > body["endDate"]:
            return "startDate must be less than or equal to endDate"
        dims = body.get("dimensions", [])
        if not isinstance(dims, list) or any(d not in KNOWN_DIMENSIONS for d in dims):
            return "dimensions must be a list of known dimensions"
        if len(set(dims)) != len(dims):
            return "you cannot group by the same dimension twice"
        if "type" in body and body["type"] not in KNOWN_TYPES:
            return "type is not a known search type"
        if "dataState" in body and str(body["dataState"]).lower() not in KNOWN_STATES:
            return "dataState must be all, final or hourly_all"
        for name, low, high in (("rowLimit", 1, MAX_ROW_LIMIT), ("startRow", 0, None)):
            if name in body:
                value = body[name]
                if isinstance(value, bool) or not isinstance(value, int):
                    return "%s must be an integer" % name
                if value < low or (high is not None and value > high):
                    return "%s is out of range" % name
        return None
