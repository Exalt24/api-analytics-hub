"""Observability: one JSON log line per request, and a /metrics page in the Prometheus text format.

Two rules shape this file.

NOTHING SECRET GOES IN A LOG LINE. A request line carries the method, the route TEMPLATE (`/api/kpis`, never a path with an id in it), the status, the duration and a request id.
It never carries a header, a query string, a body or a token, so a credential cannot reach a log by accident. The tenant id is logged because an operator needs it to follow one
customer's trail, and it is an opaque uuid.

A METRIC THAT CANNOT BE READ IS NOT ZERO. The freshness gauges are read from the database at scrape time. If the read fails the page says so (`hub_scrape_ok 0`) and omits them,
so a dashboard shows "no data" and an alert fires, instead of showing a healthy-looking empty chart.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from bisect import bisect_left
from collections import defaultdict

#: Latency buckets in seconds. Chosen so a normal read (a few milliseconds) and a stuck one (several seconds) land in different buckets.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

#: The only extra fields a log call may attach. Anything else is dropped, which is how a token passed by mistake stays out.
LOG_FIELDS = ("request_id", "method", "route", "status", "duration_ms", "tenant_id", "event", "connection_id", "run_id", "findings")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z", "level": record.levelname, "msg": record.getMessage()}
        for name in LOG_FIELDS:
            if hasattr(record, name):
                out[name] = getattr(record, name)
        if record.exc_info:
            out["error"] = record.exc_info[0].__name__            # the class only: an exception message can carry a URL or a key
        return json.dumps(out, separators=(",", ":"), default=str)


def configure_logging(stream=None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("hub")
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class Metrics:
    """In-process counters and a latency histogram. One instance per process; /metrics reports this process only, which is stated on the page."""

    def __init__(self) -> None:
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._hist_counts: dict[str, list[int]] = defaultdict(lambda: [0] * (len(BUCKETS) + 1))
        self._hist_sum: dict[str, float] = defaultdict(float)

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        self.requests[(method, route, str(status))] += 1
        self._hist_counts[route][bisect_left(BUCKETS, seconds)] += 1
        self._hist_sum[route] += seconds

    def render(self, gauges: list[tuple[str, dict[str, str], float]] | None, scrape_ok: bool) -> str:
        lines = ["# HELP hub_http_requests_total Requests handled by this process.", "# TYPE hub_http_requests_total counter"]
        for (method, route, status), n in sorted(self.requests.items()):
            lines.append('hub_http_requests_total{method="%s",route="%s",status="%s"} %d' % (method, route, status, n))
        lines += ["# HELP hub_http_request_seconds Request latency by route.", "# TYPE hub_http_request_seconds histogram"]
        for route in sorted(self._hist_counts):
            running = 0
            for upper, n in zip(BUCKETS, self._hist_counts[route]):
                running += n
                lines.append('hub_http_request_seconds_bucket{route="%s",le="%s"} %d' % (route, upper, running))
            running += self._hist_counts[route][-1]
            lines.append('hub_http_request_seconds_bucket{route="%s",le="+Inf"} %d' % (route, running))
            lines.append('hub_http_request_seconds_sum{route="%s"} %.6f' % (route, self._hist_sum[route]))
            lines.append('hub_http_request_seconds_count{route="%s"} %d' % (route, running))
        lines += ["# HELP hub_scrape_ok 1 when the database gauges below were read, 0 when they could not be.", "# TYPE hub_scrape_ok gauge", "hub_scrape_ok %d" % (1 if scrape_ok else 0)]
        for name, labels, value in (gauges or []):
            label_text = ",".join('%s="%s"' % (k, _escape(v)) for k, v in sorted(labels.items()))
            lines.append("%s%s %s" % (name, "{%s}" % label_text if label_text else "", ("%d" % value) if float(value).is_integer() else ("%.3f" % value)))
        return "\n".join(lines) + "\n"


def _escape(value) -> str:
    """Prometheus label escaping: backslash, double quote and newline. A raw newline would end the sample line and let a value start a new one."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def new_request_id(incoming: str | None) -> str:
    """Reuse a caller's X-Request-ID only when it is short and made of safe characters; otherwise mint one. A header copied into a log line unchecked is a log-injection hole."""
    if incoming and 8 <= len(incoming) <= 64 and all(c.isascii() and (c.isalnum() or c in "-_") for c in incoming):
        return incoming
    return uuid.uuid4().hex
