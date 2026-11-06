"""The Google Business Profile connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the Bearer header, one `dailyMetrics` parameter per metric,
the range with BOTH ends inclusive), how it reads the documented answer (`multiDailyMetricTimeSeries`, an int64 as a string, a `value` that is absent
when it is zero), the sums and zeros it writes, what it refuses, the error taxonomy by HTTP status and Google status, and that retries are bounded. They do
not prove Google accepts the request: the server is written from the saved pages (docs/AD_AND_BUSINESS_SOURCES.md) and no live listing was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.gbp import METRICS, REQUESTED, GBPConnector  # noqa: E402
from app.connectors.gbp_fake import FakeGBP, error_body  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

LOCATION = "12345678901234567890"
TOKEN = "ya29.SECRET-gbp-token"
START, END = date(2026, 10, 1), date(2026, 10, 4)


@pytest.fixture()
def server():
    with FakeGBP(token=TOKEN) as fake:
        fake.set_location(LOCATION)
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("retrier", retrier(sleeps))
    return GBPConnector(location_id=kw.pop("location_id", LOCATION), base_url=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def ok_body(series=None):
    return {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": series or []}]}


def dated(day, value=None):
    point = {"date": {"year": day.year, "month": day.month, "day": day.day}}
    if value is not None:
        point["value"] = value
    return point


def series(metric, *points, **extra):
    return {"dailyMetric": metric, "timeSeries": {"datedValues": list(points)}, **extra}


# ---------------------------------------------------------------- the happy path

def test_six_counts_per_day_are_written_with_impressions_added_over_the_four_surfaces(server):
    server.set_series(LOCATION, "BUSINESS_IMPRESSIONS_DESKTOP_MAPS", {date(2026, 10, 1): 10, date(2026, 10, 3): 1})
    server.set_series(LOCATION, "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", {date(2026, 10, 1): 20})
    server.set_series(LOCATION, "BUSINESS_IMPRESSIONS_MOBILE_MAPS", {date(2026, 10, 1): 30})
    server.set_series(LOCATION, "BUSINESS_IMPRESSIONS_MOBILE_SEARCH", {date(2026, 10, 1): 40, date(2026, 10, 2): 5})
    server.set_series(LOCATION, "CALL_CLICKS", {date(2026, 10, 2): 7})
    server.set_series(LOCATION, "WEBSITE_CLICKS", {date(2026, 10, 3): 3})
    server.set_series(LOCATION, "BUSINESS_DIRECTION_REQUESTS", {date(2026, 10, 1): 2})
    server.set_series(LOCATION, "MESSAGING_CLICKS", {date(2026, 10, 2): 1})
    server.set_series(LOCATION, "BUSINESS_BOOKINGS", {date(2026, 10, 3): 4})
    pages = run(collect(make(server), START, END))
    got = by_key(pages)
    assert got[("gbp_impressions", 1)] == 100 and got[("gbp_impressions", 2)] == 5 and got[("gbp_impressions", 3)] == 1
    assert got[("gbp_calls", 2)] == 7 and got[("gbp_website_clicks", 3)] == 3 and got[("gbp_direction_requests", 1)] == 2
    assert got[("gbp_messages", 2)] == 1 and got[("gbp_bookings", 3)] == 4
    assert len(points_of(pages)) == 6 * 3
    assert all(type(p.value_numeric) is int and p.currency is None for p in points_of(pages))
    assert {p.metric_key for p in points_of(pages)} == set(METRICS)


def test_the_request_asks_every_metric_once_with_both_range_ends_inclusive(server):
    run(collect(make(server), START, END))
    seen = [r for r in server.requests if r.path.endswith(":fetchMultiDailyMetricsTimeSeries")]
    assert len(seen) == 1, "one request holds the whole window"
    req = seen[0]
    assert req.path == "/v1/locations/%s:fetchMultiDailyMetricsTimeSeries" % LOCATION
    assert req.query_all["dailyMetrics"] == list(REQUESTED) and len(set(REQUESTED)) == 9
    assert [req.query["dailyRange.start_date.%s" % p] for p in ("year", "month", "day")] == ["2026", "10", "1"]
    assert [req.query["dailyRange.end_date.%s" % p] for p in ("year", "month", "day")] == ["2026", "10", "3"], "the window [1st, 4th) ends on the 3rd, inclusive"
    assert req.headers["authorization"] == "Bearer " + TOKEN


def test_a_one_day_window_asks_that_day_as_both_ends(server):
    run(collect(make(server), date(2026, 10, 7), date(2026, 10, 8)))
    q = server.requests[-1].query
    assert (q["dailyRange.start_date.day"], q["dailyRange.end_date.day"]) == ("7", "7")


@pytest.mark.parametrize("empty_days,empty_metrics", [(True, True), (False, True), (True, False), (False, False)])
def test_a_zero_is_absent_and_an_unlisted_day_or_metric_is_also_zero(empty_days, empty_metrics):
    with FakeGBP(token=TOKEN, list_empty_days=empty_days, list_empty_metrics=empty_metrics) as fake:
        fake.set_location(LOCATION)
        fake.set_series(LOCATION, "CALL_CLICKS", {date(2026, 10, 2): 5})
        got = by_key(run(collect(make(fake), START, END)))
    assert got[("gbp_calls", 2)] == 5 and got[("gbp_calls", 1)] == 0 and got[("gbp_calls", 3)] == 0
    assert got[("gbp_impressions", 1)] == 0 and got[("gbp_bookings", 2)] == 0 and len(got) == 18


def test_a_window_with_no_activity_is_all_zeros(server):
    assert set(by_key(run(collect(make(server), START, END))).values()) == {0}


def test_another_locations_numbers_are_never_counted(server):
    server.set_location("999")
    server.set_series("999", "CALL_CLICKS", {date(2026, 10, 1): 500})
    server.set_series(LOCATION, "CALL_CLICKS", {date(2026, 10, 1): 1})
    assert by_key(run(collect(make(server), START, END)))[("gbp_calls", 1)] == 1


def test_a_value_is_an_int64_string_and_a_plain_int_is_also_read(server):
    server.faults.append(Fault(200, ok_body([series("CALL_CLICKS", dated(date(2026, 10, 1), "9223372036854775807"), dated(date(2026, 10, 2), 4))])))
    got = by_key(run(collect(make(server), START, END)))
    assert got[("gbp_calls", 1)] == 9223372036854775807 and got[("gbp_calls", 2)] == 4


# ---------------------------------------------------------------- what is refused (each with a positive control)

def refused(server, body, match):
    run(collect(make(server), START, END))                                   # positive control: the same call without the fault works
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match=match):
        run(collect(make(server), START, END))


def test_a_metric_that_was_not_asked_for_is_refused(server):
    refused(server, ok_body([series("BUSINESS_FOOD_ORDERS", dated(START, "1"))]), "not asked for")


def test_a_breakdown_that_was_not_asked_for_is_refused_because_it_would_be_added_twice(server):
    refused(server, ok_body([series("CALL_CLICKS", dated(START, "1"), dailySubEntityType={"dayOfWeek": "MONDAY"})]), "breakdown")


def test_the_same_metric_twice_is_refused(server):
    refused(server, ok_body([series("CALL_CLICKS", dated(START, "1")), series("CALL_CLICKS", dated(START, "2"))]), "twice")


def test_a_month_value_is_refused_because_it_is_not_a_day(server):
    month = {"date": {"year": 2026, "month": 10, "day": 0}, "value": "5"}
    refused(server, ok_body([series("CALL_CLICKS", month)]), "month value")


def test_a_date_outside_the_requested_range_is_refused(server):
    refused(server, ok_body([series("CALL_CLICKS", dated(date(2026, 10, 4), "1"))]), "outside the requested range")
    refused(server, ok_body([series("CALL_CLICKS", dated(date(2026, 9, 30), "1"))]), "outside the requested range")


def test_the_same_date_twice_for_one_metric_is_refused(server):
    refused(server, ok_body([series("CALL_CLICKS", dated(START, "1"), dated(START, "2"))]), "same date")


@pytest.mark.parametrize("bad", ["-1", "1.5", "abc", "", "12345678901234567890", "9223372036854775808", 1.5, True, [], {}])
def test_a_value_that_is_not_a_whole_count_of_at_least_zero_is_refused(server, bad):
    refused(server, ok_body([series("CALL_CLICKS", dated(START, bad))]), "count|whole number|larger")


@pytest.mark.parametrize("bad_date", [None, "2026-10-01", {"year": "2026", "month": 10, "day": 1}, {"year": 2026, "month": 2, "day": 30}, {"year": 2026, "month": 10}])
def test_a_date_that_is_not_a_calendar_day_is_refused(server, bad_date):
    refused(server, ok_body([series("CALL_CLICKS", {"date": bad_date, "value": "1"})]), "date|month value")


def test_an_error_object_inside_a_200_is_refused(server):
    refused(server, {"error": {"code": 500, "status": "INTERNAL"}}, "error in the body")


def test_a_body_that_is_not_an_object_or_not_json_is_refused(server):
    refused(server, ["not", "an", "object"], "not an object")
    run(collect(make(server), START, END))
    server.faults.append(Fault(200, b"<html>not json</html>"))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        run(collect(make(server), START, END))


def test_a_malformed_group_is_refused(server):
    refused(server, {"multiDailyMetricTimeSeries": "nope"}, "not a list")
    refused(server, {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": "nope"}]}, "dailyMetricTimeSeries")
    refused(server, ok_body(["nope"]), "not an object")


# ---------------------------------------------------------------- inputs

def test_a_cursor_an_empty_window_and_a_window_over_a_year_are_refused(server):
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="x"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))
    with pytest.raises(ValueError, match="366"):
        run(collect(make(server), date(2025, 1, 1), date(2026, 1, 3)))
    run(collect(make(server), date(2025, 1, 1), date(2026, 1, 2)))            # 366 days exactly is allowed


@pytest.mark.parametrize("bad", ["", "abc", "locations/123", "12 34", "1" * 25])
def test_a_location_id_that_is_not_numeric_is_refused(bad):
    with pytest.raises(ValueError, match="location_id"):
        GBPConnector(location_id=bad, access_token="t")


def test_exactly_one_of_token_and_provider_is_required():
    with pytest.raises(ValueError, match="exactly one"):
        GBPConnector(location_id="1")
    with pytest.raises(ValueError, match="exactly one"):
        GBPConnector(location_id="1", access_token="t", token_provider=lambda: "t")


def test_a_token_provider_is_called_on_every_request_and_an_empty_token_is_expired(server):
    calls = []
    conn = make(server, access_token=None, token_provider=lambda: calls.append(1) or TOKEN)
    run(collect(conn, START, END))
    assert len(calls) == 1
    empty = make(server, access_token=None, token_provider=lambda: "")
    with pytest.raises(AuthExpired):
        run(collect(empty, START, END))


def test_from_env_reads_the_location_and_token_and_names_what_is_missing(server):
    env = {"GBP_LOCATION_ID": LOCATION, "GBP_ACCESS_TOKEN": TOKEN, "GBP_API_BASE_URL": server.url}
    conn = GBPConnector.from_env(env, client=client(), retrier=retrier())
    assert conn.external_account == LOCATION and run(collect(conn, START, END))
    with pytest.raises(ValueError, match="GBP_LOCATION_ID and GBP_ACCESS_TOKEN"):
        GBPConnector.from_env({})


def test_verify_reaches_the_location_with_one_day_and_one_metric(server):
    conn = make(server)
    label = run(conn.verify())
    run(conn.aclose())
    assert LOCATION in label
    q = server.requests[-1]
    assert q.query_all["dailyMetrics"] == ["CALL_CLICKS"] and q.query["dailyRange.start_date.day"] == q.query["dailyRange.end_date.day"]


def test_the_connector_is_registered_with_its_six_metrics_and_two_credential_fields():
    spec = registry.get("gbp")
    assert spec.label == "Google Business Profile" and spec.supported_metrics == frozenset(METRICS) and len(METRICS) == 6
    assert [f.key for f in spec.credential_fields] == ["location_id", "access_token"]
    assert [f.secret for f in spec.credential_fields] == [False, True]


# ---------------------------------------------------------------- errors and retries

def failing(server, status, body, expected, headers=None):
    run(collect(make(server), START, END))                                   # positive control
    server.faults.append(Fault(status, body, headers or {}))
    with pytest.raises(expected) as info:
        run(collect(make(server), START, END))
    assert TOKEN not in str(info.value) and LOCATION not in repr(info.value.__cause__ or "")
    return info.value


def test_401_is_an_expired_token_and_403_and_404_are_no_access(server):
    failing(server, 401, error_body(401, "UNAUTHENTICATED"), AuthExpired)
    failing(server, 403, error_body(403, "PERMISSION_DENIED"), AuthRevoked)
    failing(server, 404, error_body(404, "NOT_FOUND"), AuthRevoked)


def test_the_http_status_decides_when_the_body_carries_no_google_status(server):
    failing(server, 401, {}, AuthExpired)
    failing(server, 403, {}, AuthRevoked)
    failing(server, 404, b"<html>gone</html>", AuthRevoked)


def test_429_and_5xx_with_no_google_status_in_the_body_are_still_retried_and_bounded(server):
    for status in (429, 500, 502, 504):
        sleeps = Sleeps()
        server.faults[:] = [Fault(status, {})] * 3
        with pytest.raises(RateLimited, match="gave up after 3"):
            run(collect(make(server, sleeps), START, END))
        assert len(sleeps.calls) == 2, status
        server.faults[:] = [Fault(status, {})]
        assert run(collect(make(server, Sleeps()), START, END)), status


def test_the_google_status_decides_when_the_http_status_does_not(server):
    failing(server, 400, error_body(400, "UNAUTHENTICATED"), AuthExpired)
    failing(server, 400, error_body(400, "PERMISSION_DENIED"), AuthRevoked)


def test_a_bad_request_is_a_broken_upstream_and_never_retried(server):
    sleeps = Sleeps()
    run(collect(make(server, sleeps), START, END))
    server.faults.append(Fault(400, error_body(400, "INVALID_ARGUMENT")))
    with pytest.raises(UpstreamBroken, match="refused"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


def test_429_then_success_is_retried_with_the_vendors_own_wait(server):
    sleeps = Sleeps()
    server.faults.append(Fault(429, error_body(429, "RESOURCE_EXHAUSTED"), {"Retry-After": "7"}))
    pages = run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [7.0] and pages[0].api_calls == 2 and pages[0].throttle_waits == 1


def test_a_wait_longer_than_the_cap_is_handed_back_to_the_runner_and_never_slept(server):
    sleeps = Sleeps()
    server.faults.append(Fault(429, error_body(429, "RESOURCE_EXHAUSTED"), {"Retry-After": "600"}))
    with pytest.raises(RateLimited) as info:
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [] and info.value.retry_after_seconds == 600


def test_retries_are_bounded_for_5xx_and_a_dropped_connection(server):
    sleeps = Sleeps()
    server.faults[:] = [Fault(503, error_body(503, "UNAVAILABLE"))] * 3
    with pytest.raises(RateLimited, match="gave up after 3"):
        run(collect(make(server, sleeps), START, END))
    assert len(sleeps.calls) == 2
    server.faults[:] = [Fault(0, drop=True)] * 3
    with pytest.raises(RateLimited, match="transport failure"):
        run(collect(make(server, Sleeps()), START, END))
    server.faults[:] = [Fault(0, drop=True)]
    assert run(collect(make(server, Sleeps()), START, END))


def test_the_token_and_location_never_appear_in_the_repr_or_an_error(server):
    conn = make(server)
    assert TOKEN not in repr(conn) and TOKEN not in str(conn.__dict__.get("_token"))
    server.faults.append(Fault(500, {"error": {"code": 500, "status": "INTERNAL", "message": "echo " + TOKEN}}))
    server.faults.append(Fault(500, {"error": {"code": 500, "status": "INTERNAL", "message": "echo " + TOKEN}}))
    server.faults.append(Fault(500, {"error": {"code": 500, "status": "INTERNAL", "message": "echo " + TOKEN}}))
    with pytest.raises(RateLimited) as info:
        run(collect(make(server), START, END))
    assert TOKEN not in str(info.value)


# ---------------------------------------------------------------- the stand-in refuses what Google refuses

def test_the_stand_in_refuses_a_wrong_token_an_unknown_location_a_missing_range_and_an_unknown_metric(server):
    base = {"dailyMetrics": "CALL_CLICKS", "dailyRange.start_date.year": "2026", "dailyRange.start_date.month": "10", "dailyRange.start_date.day": "1",
            "dailyRange.end_date.year": "2026", "dailyRange.end_date.month": "10", "dailyRange.end_date.day": "2"}
    path = "/v1/locations/%s:fetchMultiDailyMetricsTimeSeries" % LOCATION

    def call(path=path, token=TOKEN, params=base):
        return httpx.get(server.url + path, params=params, headers={"Authorization": "Bearer " + token}, trust_env=False)

    assert call().status_code == 200
    assert call(token="wrong").status_code == 401
    assert call(path="/v1/locations/55:fetchMultiDailyMetricsTimeSeries").status_code == 404
    assert call(params={k: v for k, v in base.items() if k != "dailyRange.end_date.day"}).status_code == 400
    assert call(params={**base, "dailyMetrics": "NOT_A_METRIC"}).status_code == 400
    assert call(params={**base, "dailyRange.end_date.day": "0"}).status_code == 400


# ---------------------------------------------------------------- seeded worlds: the stored sums equal an independent recount

WORLD_SEEDS = range(60)


def world(seed):
    rng = random.Random(seed)
    first = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 300))
    days = rng.randrange(1, 40)
    data: dict[str, dict[date, int]] = {}
    for metric in REQUESTED:
        if rng.random() < 0.25:
            continue                                                         # a metric with no datapoints at all
        data[metric] = {first + timedelta(days=d): rng.choice([0, 0, 1, 2, 9, 1000, 10 ** 12]) for d in range(days) if rng.random() < 0.7}
    return first, days, data, rng.random() < 0.5, rng.random() < 0.5


@pytest.mark.parametrize("seed", WORLD_SEEDS)
def test_in_every_seeded_world_the_stored_numbers_equal_an_independent_recount(seed):
    first, days, data, empty_days, empty_metrics = world(seed)
    with FakeGBP(token=TOKEN, list_empty_days=empty_days, list_empty_metrics=empty_metrics) as fake:
        fake.set_location(LOCATION)
        fake.set_location("77")
        fake.set_series("77", "CALL_CLICKS", {first: 123456})                # another listing's numbers must never leak in
        for metric, values in data.items():
            fake.set_series(LOCATION, metric, values)
        pages = run(collect(make(fake), first, first + timedelta(days=days)))
    points = points_of(pages)
    assert len(points) == 6 * days and len({(p.metric_key, p.observed_on) for p in points}) == len(points)
    got = {(p.metric_key, p.observed_on): p.value_numeric for p in points}
    for key, names in METRICS.items():
        for d in range(days):
            day = first + timedelta(days=d)
            assert got[(key, day)] == sum(data.get(name, {}).get(day, 0) for name in names), (seed, key, day)
