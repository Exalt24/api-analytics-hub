"""The stand-in GA4 server, checked against what the saved runReport pages say.

The connector tests are only as good as this server, so it is tested on its own, over real HTTP:
each rule below is one the pages state (docs/GA4_SOURCES.md has the page for each), and each has a
case the server must accept next to the case it must refuse, so a server that refuses everything
or accepts everything fails here.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.ga4_fake import Fault, FakeGA4, QUOTA_BODY  # noqa: E402

#: One client for the module, with no TLS and no proxy lookup: building a client loads the CA bundle, which costs
#: about a tenth of a second each time and the server here is plain HTTP on the loopback address.
CLIENT = httpx.Client(trust_env=False, verify=False, timeout=10)
PROPERTY = "123456"
TOKEN = "fake-token"
DAYS = {
    date(2026, 10, 1): {"sessions": 10, "totalUsers": 8, "screenPageViews": 30, "keyEvents": 1},
    date(2026, 10, 2): {"sessions": 0, "totalUsers": 0, "screenPageViews": 0, "keyEvents": 0},
    date(2026, 10, 3): {"sessions": 5, "totalUsers": 4, "screenPageViews": 12, "keyEvents": 0},
}


@pytest.fixture()
def server():
    with FakeGA4(token=TOKEN) as fake:
        fake.set_days(PROPERTY, DAYS)
        yield fake


def post(server, body, *, token=TOKEN, prop=PROPERTY):
    headers = {"Authorization": "Bearer " + token} if token else {}
    return CLIENT.post("%s/v1beta/properties/%s:runReport" % (server.url, prop), json=body, headers=headers)


def good_body(**over):
    body = {"dateRanges": [{"startDate": "2026-10-01", "endDate": "2026-10-03"}],
            "dimensions": [{"name": "date"}], "metrics": [{"name": "sessions"}, {"name": "keyEvents"}]}
    body.update(over)
    return body


def test_a_good_request_answers_in_the_documented_shape(server):
    r = post(server, good_body())
    assert r.status_code == 200
    out = r.json()
    assert out["kind"] == "analyticsData#runReport"
    assert out["dimensionHeaders"] == [{"name": "date"}]
    assert [h["name"] for h in out["metricHeaders"]] == ["sessions", "keyEvents"]
    assert all(h["type"] == "TYPE_INTEGER" for h in out["metricHeaders"])
    # every value is a string, and the date is YYYYMMDD
    first = out["rows"][0]
    assert first == {"dimensionValues": [{"value": "20261001"}], "metricValues": [{"value": "10"}, {"value": "1"}]}


def test_a_day_with_every_metric_at_zero_is_left_out_unless_keep_empty_rows_is_true(server):
    default = post(server, good_body()).json()
    assert [r["dimensionValues"][0]["value"] for r in default["rows"]] == ["20261001", "20261003"]
    assert default["rowCount"] == 2
    kept = post(server, good_body(keepEmptyRows=True)).json()
    assert [r["dimensionValues"][0]["value"] for r in kept["rows"]] == ["20261001", "20261002", "20261003"]


def test_row_count_is_the_whole_result_whatever_limit_and_offset_ask(server):
    page = post(server, good_body(keepEmptyRows=True, limit="1", offset="1")).json()
    assert len(page["rows"]) == 1 and page["rows"][0]["dimensionValues"][0]["value"] == "20261002"
    assert page["rowCount"] == 3
    beyond = post(server, good_body(keepEmptyRows=True, offset="3")).json()
    assert beyond["rows"] == [] and beyond["rowCount"] == 3


def test_order_by_a_dimension_descending_is_honoured(server):
    out = post(server, good_body(orderBys=[{"dimension": {"dimensionName": "date"}, "desc": True}])).json()
    assert [r["dimensionValues"][0]["value"] for r in out["rows"]] == ["20261003", "20261001"]


def test_relative_dates_resolve_against_the_servers_today(server):
    server.set_days(PROPERTY, {date(2026, 11, 1): {"sessions": 7, "totalUsers": 5, "screenPageViews": 9, "keyEvents": 0}})
    out = post(server, {"dateRanges": [{"startDate": "yesterday", "endDate": "yesterday"}],
                        "metrics": [{"name": "sessions"}]}).json()
    # no dimension: one aggregate row over the range
    assert out["rows"] == [{"metricValues": [{"value": "7"}]}] and out["rowCount"] == 1


def test_property_quota_is_returned_only_when_asked_and_counts_down(server):
    assert "propertyQuota" not in post(server, good_body()).json()
    a = post(server, good_body(returnPropertyQuota=True)).json()["propertyQuota"]["tokensPerHour"]["remaining"]
    b = post(server, good_body(returnPropertyQuota=True)).json()["propertyQuota"]["tokensPerHour"]["remaining"]
    assert b == a - 1


@pytest.mark.parametrize("body,why", [
    (good_body(metrics=[{"name": "conversions"}]), "conversions is not a metric in the schema listing"),
    (good_body(metrics=[{"name": "nope"}]), "unknown metric"),
    (good_body(metrics=[]), "no metric"),
    (good_body(metrics=[{"name": "sessions"}] * 11), "more than 10 metrics"),
    (good_body(dimensions=[{"name": "date"}] * 10), "more than 9 dimensions"),
    (good_body(dimensions=[{"name": "nope"}]), "unknown dimension"),
    (good_body(dateRanges=[]), "no date range"),
    (good_body(dateRanges=[{"startDate": "2026-10-05", "endDate": "2026-10-01"}]), "start after end"),
    (good_body(dateRanges=[{"startDate": "10/01/2026", "endDate": "2026-10-03"}]), "bad date format"),
    (good_body(limit=10), "limit must be a string"),
    (good_body(limit="0"), "limit must be positive"),
    (good_body(offset="-1"), "offset is not a non-negative int64 string"),
])
def test_what_the_pages_say_is_refused_is_refused_with_googles_error_object(server, body, why):
    r = post(server, body)
    assert r.status_code == 400, why
    err = r.json()["error"]
    assert err["code"] == 400 and err["status"] == "INVALID_ARGUMENT" and err["message"]


def test_a_wrong_or_missing_token_is_401_and_an_unknown_property_is_403(server):
    for token in ("wrong", None):
        r = post(server, good_body(), token=token)
        assert r.status_code == 401 and r.json()["error"]["status"] == "UNAUTHENTICATED"
    r = post(server, good_body(), prop="999")
    assert r.status_code == 403 and r.json()["error"]["status"] == "PERMISSION_DENIED"
    assert post(server, good_body()).status_code == 200      # the positive control: the same request, right token


def test_scripted_faults_are_served_once_each_in_order_then_normal_service_resumes(server):
    server.faults.extend([Fault(429, QUOTA_BODY, {"Retry-After": "3"}), Fault(503), Fault(500)])
    first, second, third, fourth = (post(server, good_body()) for _ in range(4))
    assert (first.status_code, first.headers["Retry-After"], first.json()["error"]["status"]) == (429, "3", "RESOURCE_EXHAUSTED")
    assert (second.status_code, third.status_code, fourth.status_code) == (503, 500, 200)


def test_an_exhausted_hourly_quota_answers_429_resource_exhausted(server):
    server.tokens_per_hour = 0
    r = post(server, good_body())
    assert r.status_code == 429 and r.json()["error"]["status"] == "RESOURCE_EXHAUSTED"


def test_a_dropped_connection_is_a_transport_error_not_an_answer(server):
    server.faults.append(Fault(0, drop=True))
    with pytest.raises(httpx.TransportError):
        post(server, good_body())
    assert post(server, good_body()).status_code == 200


def test_requests_are_recorded_with_the_authorization_header(server):
    post(server, good_body())
    path, body, auth = server.requests[-1]
    assert path == "/v1beta/properties/%s:runReport" % PROPERTY and auth == "Bearer " + TOKEN
    assert body["metrics"][0]["name"] == "sessions"
