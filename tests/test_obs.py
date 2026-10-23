"""The log formatter, request ids and the metrics page. No database, no network.

THE CLASS THESE TESTS GUARD: a credential or an attacker-controlled string reaching a log line or a metric label. Every case below crosses a hostile value with every place
the code could put it, and each is paired with a positive control so a formatter that dropped everything would not pass.
"""
from __future__ import annotations

import io
import json
import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.obs import BUCKETS, LOG_FIELDS, Metrics, configure_logging, new_request_id  # noqa: E402

SECRET = "shp" + "at_" + "0123456789abcdef" * 2        # assembled at runtime so no secret scanner reads the source as a real token


def _log_one(**extra):
    buf = io.StringIO()
    logger = configure_logging(stream=buf)
    logger.info("request", extra=extra)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_a_log_line_is_one_json_object_with_the_allowed_fields():
    line = _log_one(event="request", method="GET", route="/api/kpis", status=200, duration_ms=3.2, request_id="abc12345", tenant_id="t-1")
    assert line["msg"] == "request" and line["level"] == "INFO" and line["route"] == "/api/kpis" and line["status"] == 200
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line["ts"])


@pytest.mark.parametrize("name", ["authorization", "token", "access_token", "password", "body", "query", "headers", "credentials_enc"])
def test_a_field_outside_the_allow_list_never_reaches_the_line(name):
    line = _log_one(event="request", **{name: SECRET})
    assert SECRET not in json.dumps(line) and name not in line
    assert name not in LOG_FIELDS


def test_an_exception_logs_its_class_and_never_its_message():
    buf = io.StringIO()
    logger = configure_logging(stream=buf)
    try:
        raise ValueError("connect failed for postgresql://u:%s@host/db" % SECRET)
    except ValueError:
        logger.error("boom", exc_info=True)
    out = buf.getvalue()
    assert SECRET not in out and json.loads(out)["error"] == "ValueError"


@pytest.mark.parametrize("incoming, kept", [
    ("abcd1234efgh", True), ("req_12345678", True), ("A" * 64, True),
    (None, False), ("", False), ("short", False), ("A" * 65, False),
    ("abcd1234\nINJECTED", False), ("abcd 1234 5678", False), ('abcd1234"}', False), ("abcd1234;rm", False), ("\u00e9" * 10, False),
])
def test_a_request_id_is_reused_only_when_it_is_short_and_plain(incoming, kept):
    got = new_request_id(incoming)
    assert (got == incoming) is kept
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", got)


def test_the_page_counts_every_request_and_the_buckets_are_cumulative():
    m = Metrics()
    for seconds in (0.001, 0.02, 0.02, 0.3, 20.0):
        m.observe("GET", "/api/kpis", 200, seconds)
    m.observe("GET", "/api/kpis", 500, 0.004)
    page = m.render(None, True)
    assert 'hub_http_requests_total{method="GET",route="/api/kpis",status="200"} 5' in page
    assert 'hub_http_requests_total{method="GET",route="/api/kpis",status="500"} 1' in page
    buckets = [int(x) for x in re.findall(r'hub_http_request_seconds_bucket\{route="/api/kpis",le="[^"]+"\} (\d+)', page)]
    assert len(buckets) == len(BUCKETS) + 1 and buckets == sorted(buckets)
    assert buckets[-1] == 6 and 'hub_http_request_seconds_count{route="/api/kpis"} 6' in page
    assert buckets[0] == 2          # 0.001 and 0.004 fall in the 0.005 bucket: the first bucket is not empty and not everything


def test_a_failed_database_read_says_so_and_adds_no_gauge():
    m = Metrics()
    down = m.render(None, False)
    assert "hub_scrape_ok 0" in down and "hub_sync_problems" not in down
    up = m.render([("hub_sync_problems_total", {}, 0.0)], True)
    assert "hub_scrape_ok 1" in up and "hub_sync_problems_total 0" in up       # a real zero is shown as a zero


def test_a_label_value_is_escaped_so_it_cannot_close_the_label():
    page = Metrics().render([("hub_sync_problems", {"platform": 'x"}\nhub_evil 1'}, 1.0)], True)
    assert "\nhub_evil" not in page and '\\"' in page


def test_the_route_label_set_is_bounded_by_the_route_templates():
    m = Metrics()
    for i in range(500):
        m.observe("GET", "unmatched", 404, 0.001)
    assert len(m.requests) == 1 and len(m._hist_counts) == 1
