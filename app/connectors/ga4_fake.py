"""A stand-in Google Analytics Data API server on 127.0.0.1, for tests and the sample deck.

It speaks real HTTP to the connector and implements what the saved runReport pages describe:
the path `/v1beta/properties/{id}:runReport`, a body with `dateRanges`, `dimensions`,
`metrics`, `orderBys`, `limit` and `offset` (both strings), rows with every value a string,
`rowCount` independent of the page, the date dimension as YYYYMMDD, rows with all metrics at
zero left out unless `keepEmptyRows` is true, and Google's error object
(`{"error": {"code", "message", "status"}}`) for 400, 401, 403, 429 and 5xx.

It is a stand-in written from documentation, not a recording of Google. What it cannot tell
you is listed in docs/GA4_SOURCES.md: that is the point of the word UNVERIFIED there. Where
it refuses something (a metric name it does not know, `conversions`, a start after the end,
more than 10 metrics) it does so because a page says the real API would, so a connector that
passes here has not been let off a documented rule.

Faults are scripted: `server.faults.extend([Fault(429, headers={"Retry-After": "2"})])` makes
the next request answer that way, once per entry, before the normal answer resumes.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

#: GA4 metric names the stand-in knows (a small subset; the real list is far longer).
KNOWN_METRICS = ("sessions", "totalUsers", "screenPageViews", "keyEvents", "activeUsers", "eventCount", "newUsers")
KNOWN_DIMENSIONS = ("date", "country", "city")

_PATH_RE = re.compile(r"^/v1beta/properties/(\d+):runReport$")
_RELATIVE_RE = re.compile(r"^(\d+)daysAgo$")
MAX_ROWS_PER_REQUEST = 250_000
MAX_METRICS, MAX_DIMENSIONS, MAX_DATE_RANGES = 10, 9, 4


@dataclass
class Fault:
    status: int
    body: dict | str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: True makes the server drop the connection without answering, a transport failure.
    drop: bool = False


def error_body(code: int, status: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "status": status}}


QUOTA_BODY = error_body(
    429, "RESOURCE_EXHAUSTED", "Exhausted property tokens for a project per hour. Retry later.")


class FakeGA4:
    """Start with `with FakeGA4(token="t") as server:`, then point the connector at `server.url`."""

    def __init__(self, *, token: str = "test-token", today: date | None = None):
        self.token = token
        self.today = today or date(2026, 11, 2)
        self.faults: list[Fault] = []
        #: property id -> day -> {GA4 metric name: int}
        self.data: dict[str, dict[date, dict[str, int]]] = {}
        #: Every request seen: (path, parsed body or None, Authorization header).
        self.requests: list[tuple[str, dict | None, str | None]] = []
        #: Tokens left in the hour as the stand-in reports them, spent one per answered request.
        self.tokens_per_hour = 40_000
        self.time_zone = "America/Los_Angeles"
        #: Set to answer with metadata.samplingMetadatas, as a sampled report does.
        self.sampled = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "FakeGA4":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep the test output quiet
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

    def set_days(self, property_id: str, days: Mapping[date, Mapping[str, int]]) -> None:
        with self._lock:
            self.data[str(property_id)] = {d: dict(v) for d, v in days.items()}

    # ------------------------------------------------------------------ the request

    def _resolve_date(self, text: Any) -> date | None:
        if not isinstance(text, str):
            return None
        if text == "today":
            return self.today
        if text == "yesterday":
            return self.today - timedelta(days=1)
        rel = _RELATIVE_RE.match(text)
        if rel:
            return self.today - timedelta(days=int(rel.group(1)))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        return None

    def handle(self, path: str, raw: bytes, auth: str | None) -> tuple[int, dict[str, str], Any, bool]:
        with self._lock:
            parsed: dict | None
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            self.requests.append((path, parsed, auth))
            if self.faults:
                fault = self.faults.pop(0)
                if fault.drop:
                    return 0, {}, b"", True
                body = fault.body if fault.body is not None else error_body(fault.status, "INTERNAL", "scripted fault")
                return fault.status, dict(fault.headers), body, False
            return self._answer(path, parsed, auth)

    def _answer(self, path: str, body: dict | None, auth: str | None) -> tuple[int, dict[str, str], Any, bool]:
        match = _PATH_RE.match(path)
        if not match:
            return 404, {}, error_body(404, "NOT_FOUND", "no such method"), False
        if auth != "Bearer " + self.token:
            return 401, {}, error_body(401, "UNAUTHENTICATED", "Request had invalid authentication credentials."), False
        property_id = match.group(1)
        if property_id not in self.data:
            return 403, {}, error_body(403, "PERMISSION_DENIED",
                                       "User does not have sufficient permissions for this property."), False
        if self.tokens_per_hour <= 0:
            return 429, {}, QUOTA_BODY, False
        if not isinstance(body, dict):
            return 400, {}, error_body(400, "INVALID_ARGUMENT", "the body is not a JSON object"), False
        problem = self._validate(body)
        if problem:
            return 400, {}, error_body(400, "INVALID_ARGUMENT", problem), False

        ranges = body["dateRanges"]
        start, end = self._resolve_date(ranges[0]["startDate"]), self._resolve_date(ranges[0]["endDate"])
        metrics = [m["name"] for m in body["metrics"]]
        dims = [d["name"] for d in body.get("dimensions") or []]
        keep_empty = bool(body.get("keepEmptyRows"))
        days = self.data[property_id]
        rows_all: list[tuple[str, list[int]]] = []
        day = start
        while day <= end:
            values = [days.get(day, {}).get(name, 0) for name in metrics]
            if keep_empty or any(values):
                rows_all.append((day.strftime("%Y%m%d"), values))
            day += timedelta(days=1)
        # Only the date dimension is ever requested by the connector, and the order it asks for
        # (ALPHANUMERIC on YYYYMMDD) is the order the rows are already in; a descending order is honoured.
        order = (body.get("orderBys") or [{}])[0]
        if order.get("desc"):
            rows_all.reverse()
        limit = int(body.get("limit", 10_000))
        offset = int(body.get("offset", 0))
        page = rows_all[offset: offset + min(limit, MAX_ROWS_PER_REQUEST)]
        self.tokens_per_hour -= 1
        out: dict[str, Any] = {
            "dimensionHeaders": [{"name": n} for n in dims],
            "metricHeaders": [{"name": n, "type": "TYPE_INTEGER"} for n in metrics],
            "rows": [
                {"dimensionValues": [{"value": text}] if dims else [],
                 "metricValues": [{"value": str(v)} for v in values]}
                for text, values in page
            ],
            "rowCount": len(rows_all),
            "metadata": {"currencyCode": "USD", "timeZone": self.time_zone},
            "kind": "analyticsData#runReport",
        }
        if not dims:  # no dimension means one aggregate row over the whole range
            total = [sum(v[i] for _, v in rows_all) for i in range(len(metrics))]
            out["rows"] = [{"metricValues": [{"value": str(t)} for t in total]}]
            out["rowCount"] = 1
        if not rows_all:
            out["metadata"]["emptyReason"] = "no rows matched"
        if self.sampled:
            out["metadata"]["samplingMetadatas"] = [{"samplesReadCount": "1000", "samplingSpaceSize": "5000"}]
        if body.get("returnPropertyQuota"):
            out["propertyQuota"] = {
                "tokensPerDay": {"consumed": 1, "remaining": 199_999},
                "tokensPerHour": {"consumed": 1, "remaining": self.tokens_per_hour},
                "concurrentRequests": {"consumed": 0, "remaining": 10},
                "serverErrorsPerProjectPerHour": {"consumed": 0, "remaining": 10},
                "potentiallyThresholdedRequestsPerHour": {"consumed": 0, "remaining": 120},
                "tokensPerProjectPerHour": {"consumed": 1, "remaining": min(self.tokens_per_hour, 14_000)},
            }
        return 200, {}, out, False

    def _validate(self, body: dict) -> str | None:
        """The refusals the saved pages describe; None when the request would be accepted."""
        ranges = body.get("dateRanges")
        if not isinstance(ranges, list) or not ranges:
            return "at least one dateRanges entry is required"
        if len(ranges) > MAX_DATE_RANGES:
            return "at most %d date ranges are allowed" % MAX_DATE_RANGES
        if len(ranges) > 1:
            return "this stand-in answers one date range only"
        start, end = self._resolve_date(ranges[0].get("startDate")), self._resolve_date(ranges[0].get("endDate"))
        if start is None or end is None:
            return "startDate and endDate must be YYYY-MM-DD, NdaysAgo, yesterday or today"
        if start > end:
            return "startDate cannot be after endDate"
        metrics = body.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            return "at least one metric is required"
        if len(metrics) > MAX_METRICS:
            return "at most %d metrics are allowed" % MAX_METRICS
        for metric in metrics:
            if not isinstance(metric, dict) or metric.get("name") not in KNOWN_METRICS:
                return "Field %s is not a valid metric" % (metric.get("name") if isinstance(metric, dict) else metric)
        dims = body.get("dimensions") or []
        if len(dims) > MAX_DIMENSIONS:
            return "at most %d dimensions are allowed" % MAX_DIMENSIONS
        for dim in dims:
            if not isinstance(dim, dict) or dim.get("name") not in KNOWN_DIMENSIONS:
                return "Field %s is not a valid dimension" % (dim.get("name") if isinstance(dim, dict) else dim)
        for key in ("limit", "offset"):
            if key in body:
                if not isinstance(body[key], str) or not re.fullmatch(r"\d+", body[key]):
                    return "%s must be an int64 string" % key
        if "limit" in body and int(body["limit"]) < 1:
            return "limit must be positive"
        return None
