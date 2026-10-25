"""The GA4 connector against the stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the
body, the half open window turned into an inclusive end date, the string limit and offset), how it
reads the documented response (string values, YYYYMMDD days, rowCount paging, rows left out for an
all zero day), the error taxonomy for 400, 401, 403, 429 and 5xx, and that retries are bounded. They do
not prove Google accepts the request: the server is written from the saved pages (docs/GA4_SOURCES.md),
no live property was used, so nothing here is a live integration.

Every test that expects a refusal runs a positive control first (the same call succeeds), so a broken
request cannot pass for a refused one.
"""
from __future__ import annotations

import asyncio
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.ga4 import GA4Connector, METRICS  # noqa: E402
from app.connectors.ga4_fake import QUOTA_BODY, Fault, FakeGA4, error_body  # noqa: E402
from app.connectors.registry import registry  # noqa: E402

PROPERTY = "123456"
TOKEN = "tok-SECRET-123"
START, END = date(2026, 10, 1), date(2026, 10, 4)          # half open: the 1st, 2nd and 3rd
NAMES = ("sessions", "totalUsers", "screenPageViews", "keyEvents")


def day(n: int, sessions: int = 10) -> dict[str, int]:
    return {"sessions": sessions + n, "totalUsers": 8 + n, "screenPageViews": 30 + n, "keyEvents": n}


THREE_DAYS = {date(2026, 10, 1): day(1), date(2026, 10, 2): day(2), date(2026, 10, 3): day(3)}


@pytest.fixture()
def server():
    with FakeGA4(token=TOKEN) as fake:
        fake.set_days(PROPERTY, THREE_DAYS)
        yield fake


class Sleeps:
    """Stands in for asyncio.sleep: records the waits and returns at once."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make(server, sleeps=None, **kw):
    kw.setdefault("sleep", sleeps or Sleeps())
    kw.setdefault("rng", lambda: 1.0)                         # full delay, no jitter, so waits are exact
    # no TLS and no proxy lookup: building a default client loads the CA bundle, which is slow and useless on loopback
    client = httpx.AsyncClient(trust_env=False, verify=False, timeout=10)
    if "token_provider" not in kw:
        kw.setdefault("access_token", TOKEN)
    return GA4Connector(property_id=kw.pop("property_id", PROPERTY), base_url=server.url, client=client, **kw)


async def collect(conn, start=START, end=END, cursor=None):
    pages = []
    try:
        async for page in conn.fetch(window_start=start, window_end=end, cursor=cursor):
            pages.append(page)
    finally:
        await conn._client.aclose()
    return pages


def run(coro):
    return asyncio.run(coro)


def points_of(pages):
    return [p for page in pages for p in page.points]


def as_set(points):
    return {(p.metric_key, p.observed_on, p.value_numeric) for p in points}


def expected_points(days, start=START, end=END):
    out = set()
    for d, values in days.items():
        if start <= d < end and any(values.values()):
            for name, canonical in METRICS:
                out.add((canonical, d, values[name]))
    return out


# ---------------------------------------------------------------- the happy path

def test_every_day_and_metric_arrives_as_an_integer_in_the_canonical_vocabulary(server):
    pages = run(collect(make(server)))
    assert as_set(points_of(pages)) == expected_points(THREE_DAYS)
    assert all(type(p.value_numeric) is int and p.currency is None for p in points_of(pages))
    assert {p.metric_key for p in points_of(pages)} == {"sessions", "users", "page_views", "key_events"}
    assert len(pages) == 1 and pages[0].cursor is None and pages[0].api_calls == 1 and pages[0].throttle_waits == 0


def test_the_request_is_the_documented_one(server):
    run(collect(make(server)))
    path, body, auth = server.requests[-1]
    assert path == "/v1beta/properties/%s:runReport" % PROPERTY
    assert auth == "Bearer " + TOKEN
    # half open [Oct 1, Oct 4) becomes the inclusive range Oct 1 to Oct 3
    assert body["dateRanges"] == [{"startDate": "2026-10-01", "endDate": "2026-10-03"}]
    assert body["dimensions"] == [{"name": "date"}]
    assert [m["name"] for m in body["metrics"]] == list(NAMES), "keyEvents is the current name, conversions is not requested"
    assert body["orderBys"] == [{"dimension": {"dimensionName": "date", "orderType": "ALPHANUMERIC"}}]
    assert body["limit"] == "10000" and body["offset"] == "0", "int64 fields travel as strings"
    assert "keepEmptyRows" not in body, "the documented default leaves all zero days out, and the report reads that as quiet"


def test_a_day_with_every_metric_at_zero_has_no_row_and_so_no_points(server):
    server.set_days(PROPERTY, {date(2026, 10, 2): dict.fromkeys(NAMES, 0)})
    pages = run(collect(make(server)))
    assert points_of(pages) == []
    assert len(pages) == 1 and pages[0].cursor is None


def test_paging_asks_for_each_offset_in_turn_and_yields_a_resumable_cursor(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_days(PROPERTY, days)
    pages = run(collect(make(server, page_size=2), end=date(2026, 10, 6)))
    assert [r[1]["offset"] for r in server.requests] == ["0", "2", "4"]
    assert [r[1]["limit"] for r in server.requests] == ["2", "2", "2"]
    assert [p.cursor for p in pages] == ["2", "4", None]
    assert as_set(points_of(pages)) == expected_points(days, end=date(2026, 10, 6))
    assert sum(p.api_calls for p in pages) == 3


def test_a_run_can_resume_from_a_cursor_without_repeating_the_first_page(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_days(PROPERTY, days)
    pages = run(collect(make(server, page_size=2), end=date(2026, 10, 6), cursor="2"))
    assert [r[1]["offset"] for r in server.requests] == ["2", "4"]
    got = {p.observed_on.day for p in points_of(pages)}
    assert got == {3, 4, 5}


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5"])
def test_a_cursor_that_is_not_a_row_offset_is_refused_before_any_request(server, cursor):
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), cursor=cursor))
    assert server.requests == []


def test_an_empty_or_backwards_window_is_a_caller_error(server):
    for start, end in ((START, START), (END, START)):
        with pytest.raises(ValueError):
            run(collect(make(server), start=start, end=end))
    assert server.requests == []


def test_a_sampled_report_is_marked_on_every_row_it_produced(server):
    server.sampled = True
    pages = run(collect(make(server)))
    assert pages[0].points and all(p.raw and p.raw.get("sampled") is True for p in pages[0].points)
    server.sampled = False
    assert all("sampled" not in (p.raw or {}) for p in points_of(run(collect(make(server)))))


def test_verify_makes_one_cheap_call_and_names_the_property_and_its_time_zone(server):
    conn = make(server)
    label = run(_verify(conn))
    assert label == "GA4 property 123456 (time zone America/Los_Angeles)"
    body = server.requests[-1][1]
    assert body["metrics"] == [{"name": "sessions"}] and body["limit"] == "1"
    assert body["dateRanges"] == [{"startDate": "yesterday", "endDate": "yesterday"}]


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_verify_with_a_wrong_token_fails_in_front_of_the_person_who_pasted_it(server):
    assert run(_verify(make(server)))                                  # positive control
    with pytest.raises(AuthExpired):
        run(_verify(make(server, access_token="not-the-token")))


# ---------------------------------------------------------------- the error taxonomy

def test_a_rejected_token_is_auth_expired_and_an_unknown_property_is_auth_revoked(server):
    assert run(collect(make(server)))                                  # positive control
    with pytest.raises(AuthExpired) as expired:
        run(collect(make(server, access_token="wrong")))
    assert expired.value.retryable and expired.value.code == "auth_expired"
    with pytest.raises(AuthRevoked) as revoked:
        run(collect(make(server, property_id="999")))
    assert not revoked.value.retryable and revoked.value.code == "auth_revoked"
    assert "999" in str(revoked.value)


def test_a_token_provider_is_asked_for_the_current_token_on_every_call(server):
    tokens = iter(["stale", TOKEN])
    conn = make(server, token_provider=lambda: next(tokens))
    with pytest.raises(AuthExpired):
        run(collect(conn))
    assert as_set(points_of(run(collect(make(server, token_provider=lambda: TOKEN))))) == expected_points(THREE_DAYS)


@pytest.mark.parametrize("status,body", [(400, None), (404, None), (418, None)])
def test_other_refusals_are_upstream_broken_and_never_retried(server, status, body):
    assert run(collect(make(server)))                                  # positive control
    server.requests.clear()
    server.faults.append(Fault(status, body))
    sleeps = Sleeps()
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server, sleeps)))
    assert not exc.value.retryable and sleeps.calls == [] and len(server.requests) == 1


def test_the_400_text_from_the_error_object_reaches_the_message_cut_short(server):
    server.faults.append(Fault(400, error_body(400, "INVALID_ARGUMENT", "x" * 500)))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "INVALID_ARGUMENT" in str(exc.value) and len(str(exc.value)) < 330


# ---------------------------------------------------------------- bounded retries

def test_a_429_with_retry_after_waits_that_long_once_and_then_succeeds(server):
    server.faults.append(Fault(429, QUOTA_BODY, {"Retry-After": "2"}))
    sleeps = Sleeps()
    pages = run(collect(make(server, sleeps)))
    assert sleeps.calls == [2.0]
    assert as_set(points_of(pages)) == expected_points(THREE_DAYS)
    assert pages[0].api_calls == 2 and pages[0].throttle_waits == 1


def test_a_quota_error_with_no_retry_after_backs_off_exponentially(server):
    server.faults.extend([Fault(429, QUOTA_BODY), Fault(429, QUOTA_BODY)])
    sleeps = Sleeps()
    run(collect(make(server, sleeps, backoff_base=1.0)))
    assert sleeps.calls == [1.0, 2.0]


def test_the_backoff_is_capped_and_jittered_between_half_and_all_of_the_delay(server):
    server.faults.extend([Fault(503)] * 4)
    sleeps = Sleeps()
    run(collect(make(server, sleeps, max_attempts=5, backoff_base=10.0, backoff_cap=30.0, rng=lambda: 0.0)))
    assert sleeps.calls == [5.0, 10.0, 15.0, 15.0], "half of 10, 20, then 30 and 30 held at the cap"


def test_it_gives_up_after_max_attempts_and_says_so_as_a_retryable_rate_limit(server):
    server.faults.extend([Fault(429, QUOTA_BODY)] * 10)
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps, max_attempts=3)))
    assert exc.value.retryable and exc.value.code == "rate_limited"
    assert len(server.requests) == 3 and len(sleeps.calls) == 2
    assert "gave up after 3 attempts" in str(exc.value) and "RESOURCE_EXHAUSTED" in str(exc.value)


@pytest.mark.parametrize("status", [500, 503, 502])
def test_server_errors_are_retried_and_then_raised_as_rate_limited(server, status):
    server.faults.append(Fault(status))
    assert as_set(points_of(run(collect(make(server))))) == expected_points(THREE_DAYS)
    server.faults.extend([Fault(status)] * 3)
    server.requests.clear()
    with pytest.raises(RateLimited):
        run(collect(make(server, max_attempts=3)))
    assert len(server.requests) == 3


def test_a_wait_longer_than_the_cap_is_handed_to_the_runner_not_slept(server):
    server.faults.append(Fault(429, QUOTA_BODY, {"Retry-After": "3600"}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps, backoff_cap=30.0)))
    assert exc.value.retry_after_seconds == 3600.0 and sleeps.calls == [] and len(server.requests) == 1


def test_a_dropped_connection_is_retried_like_a_server_error(server):
    server.faults.append(Fault(0, drop=True))
    sleeps = Sleeps()
    pages = run(collect(make(server, sleeps)))
    assert len(sleeps.calls) == 1 and as_set(points_of(pages)) == expected_points(THREE_DAYS)


def test_max_attempts_of_one_means_no_retry_at_all(server):
    server.faults.append(Fault(503))
    with pytest.raises(RateLimited):
        run(collect(make(server, max_attempts=1)))
    assert len(server.requests) == 1


def test_it_stops_before_a_further_page_when_the_hourly_tokens_are_nearly_spent(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_days(PROPERTY, days)
    server.tokens_per_hour = 101            # the first answer reports 100 left, which is the floor
    conn = make(server, page_size=2, quota_floor=100)
    got = []

    async def go():
        try:
            async for page in conn.fetch(window_start=START, window_end=date(2026, 10, 6)):
                got.append(page)
        finally:
            await conn._client.aclose()

    with pytest.raises(RateLimited) as exc:
        run(go())
    assert [p.cursor for p in got] == ["2"], "the first page was kept and carries the cursor to resume from"
    assert len(server.requests) == 1 and "100 tokens" in str(exc.value)


# ---------------------------------------------------------------- a response it cannot trust

def good_payload(**over):
    payload = {
        "dimensionHeaders": [{"name": "date"}],
        "metricHeaders": [{"name": n, "type": "TYPE_INTEGER"} for n in NAMES],
        "rows": [{"dimensionValues": [{"value": "20261001"}], "metricValues": [{"value": v} for v in ("5", "4", "9", "1")]}],
        "rowCount": 1,
    }
    payload.update(over)
    return payload


def row(date_text, values=("5", "4", "9", "1")):
    return {"dimensionValues": [{"value": date_text}], "metricValues": [{"value": v} for v in values]}


BAD = {
    "non-date value": good_payload(rows=[row("Oct 1")]),
    "impossible day": good_payload(rows=[row("20261340")]),
    "seven digit date": good_payload(rows=[row("2026101")]),
    "reserved total row": good_payload(rows=[row("RESERVED_TOTAL")]),
    "outside the window": good_payload(rows=[row("20260930")]),
    "two rows for one day": good_payload(rows=[row("20261001"), row("20261001")], rowCount=2),
    "a float count": good_payload(rows=[row("20261001", ("5.5", "4", "9", "1"))]),
    "a negative count": good_payload(rows=[row("20261001", ("-5", "4", "9", "1"))]),
    "not a number": good_payload(rows=[row("20261001", ("many", "4", "9", "1"))]),
    "a null value": good_payload(rows=[{"dimensionValues": [{"value": "20261001"}],
                                        "metricValues": [{"value": None}] * 4}]),
    "too few values": good_payload(rows=[row("20261001", ("5", "4", "9"))]),
    "no dimensionValues": good_payload(rows=[{"metricValues": [{"value": "1"}] * 4}]),
    "wrong dimension column": good_payload(dimensionHeaders=[{"name": "country"}]),
    "a metric column missing": good_payload(metricHeaders=[{"name": n} for n in NAMES[:3]]),
    "rowCount missing": {k: v for k, v in good_payload().items() if k != "rowCount"},
    "rowCount a string": good_payload(rowCount="1"),
    "rows not a list": good_payload(rows={"a": 1}),
    "rows promised and an empty page": good_payload(rows=[], rowCount=5),
    "a body that is a list": [1, 2],
}


def test_a_good_scripted_payload_is_read(server):
    """The control for the table below: the same shape, nothing wrong, is accepted."""
    server.faults.append(Fault(200, good_payload()))
    pages = run(collect(make(server)))
    assert as_set(points_of(pages)) == {("sessions", date(2026, 10, 1), 5), ("users", date(2026, 10, 1), 4),
                                       ("page_views", date(2026, 10, 1), 9), ("key_events", date(2026, 10, 1), 1)}


@pytest.mark.parametrize("why", sorted(BAD))
def test_a_response_that_breaks_a_documented_rule_is_upstream_broken_not_a_chart_of_nulls(server, why):
    server.faults.append(Fault(200, BAD[why]))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert not exc.value.retryable


def test_a_200_that_is_not_json_is_upstream_broken(server):
    server.faults.append(Fault(200, b"<html>not json</html>"))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        run(collect(make(server)))


# ---------------------------------------------------------------- credentials

@pytest.mark.parametrize("fault", [Fault(401), Fault(403), Fault(400), Fault(500)])
def test_the_token_is_never_in_an_error_a_repr_or_a_log_line(server, fault, caplog):
    conn = make(server, max_attempts=1)
    server.faults.append(fault)
    with caplog.at_level("DEBUG"):
        with pytest.raises(Exception) as exc:
            run(collect(conn))
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)
    assert TOKEN not in repr(conn) and TOKEN not in str(conn.__dict__.get("_token"))
    assert TOKEN not in caplog.text
    # the positive control: the token WAS sent, in the header and nowhere else
    assert server.requests[0][2] == "Bearer " + TOKEN
    assert TOKEN not in str(server.requests[0][1])


def test_from_env_reads_the_environment_and_only_the_environment(server):
    env = {"GA4_PROPERTY_ID": "properties/" + PROPERTY, "GA4_ACCESS_TOKEN": TOKEN, "GA4_API_BASE_URL": server.url}
    conn = GA4Connector.from_env(env, client=httpx.AsyncClient(trust_env=False, verify=False))
    assert conn.property_id == PROPERTY
    assert as_set(points_of(run(collect(conn)))) == expected_points(THREE_DAYS)
    for missing in ("GA4_PROPERTY_ID", "GA4_ACCESS_TOKEN"):
        incomplete = {k: v for k, v in env.items() if k != missing}
        with pytest.raises(ValueError) as exc:
            GA4Connector.from_env(incomplete)
        assert missing in str(exc.value) and TOKEN not in str(exc.value)


@pytest.mark.parametrize("bad", ["abc", "12ab", "", "properties/", "-5", "1.5", "properties/abc"])
def test_a_property_id_that_is_not_numeric_is_refused_before_any_call(server, bad):
    with pytest.raises(ValueError, match="numeric"):
        GA4Connector(property_id=bad, access_token=TOKEN, base_url=server.url)
    assert server.requests == []
    assert GA4Connector(property_id="properties/77", access_token=TOKEN, base_url=server.url).property_id == "77"


def test_exactly_one_of_a_token_and_a_token_provider(server):
    with pytest.raises(ValueError):
        GA4Connector(property_id=PROPERTY, base_url=server.url)
    with pytest.raises(ValueError):
        GA4Connector(property_id=PROPERTY, access_token=TOKEN, token_provider=lambda: TOKEN, base_url=server.url)


@pytest.mark.parametrize("kw", [{"page_size": 0}, {"page_size": 250_001}, {"max_attempts": 0}])
def test_nonsense_settings_are_refused(server, kw):
    with pytest.raises(ValueError):
        GA4Connector(property_id=PROPERTY, access_token=TOKEN, base_url=server.url, **kw)


# ---------------------------------------------------------------- the registry

def test_ga4_is_registered_next_to_shopify_with_nothing_else_changed():
    spec = registry.get("ga4")
    assert spec.supported_metrics == frozenset({"sessions", "users", "page_views", "key_events"})
    assert registry.platforms() == sorted(["ga4", "shopify"])
    assert registry.platforms_supporting("sessions") == ["ga4"] and registry.platforms_supporting("orders") == ["shopify"]
    from app.connectors.registry import MetricNotSupported
    with pytest.raises(MetricNotSupported):
        registry.assert_supports("ga4", "orders")
    registry.assert_supports("ga4", "key_events")


def test_the_registry_builds_a_connector_and_redacts_only_the_token():
    assert registry.secret_keys("ga4") == frozenset({"access_token"})
    out = registry.redact("ga4", {"property_id": PROPERTY, "access_token": TOKEN})
    assert out == {"property_id": PROPERTY, "access_token": "***"}
    conn = registry.build("ga4", property_id=PROPERTY, access_token=TOKEN)
    assert isinstance(conn, GA4Connector) and conn.platform == "ga4"
    with pytest.raises(ValueError, match="access_token"):
        registry.build("ga4", property_id=PROPERTY)
    run(conn.aclose())


# ---------------------------------------------------------------- a seeded property test

def _model(n_faults: int, pages: int, max_attempts: int) -> tuple[str, int, int]:
    """What the connector must do, worked out apart from it: (outcome, requests made, waits slept)."""
    remaining, requests, waits = n_faults, 0, 0
    for _ in range(pages):
        for attempt in range(max_attempts):
            requests += 1
            if remaining > 0:
                remaining -= 1
                if attempt == max_attempts - 1:
                    return "fail", requests, waits
                waits += 1
                continue
            break
    return "ok", requests, waits


def _world(seed: int):
    rng = random.Random(seed)
    start = date(2026, 9, 25) + timedelta(days=rng.randint(0, 20))
    length = rng.randint(1, 40)
    end = start + timedelta(days=length)
    days = {}
    for i in range(length):
        if rng.random() < 0.75:                                   # a quarter of the days have no row
            days[start + timedelta(days=i)] = {
                n: rng.choice([0, 0, rng.randint(0, 9), rng.randint(0, 5000)]) for n in NAMES}
    # one day of the property's data sits outside the window and must never come back
    days[start - timedelta(days=1)] = day(1)
    days[end] = day(2)
    faults = []
    for _ in range(rng.randint(0, 9)):
        kind = rng.choice(["ra0", "ra2", "quota", "500", "503", "drop"])
        faults.append({
            "ra0": Fault(429, QUOTA_BODY, {"Retry-After": "0"}), "ra2": Fault(429, QUOTA_BODY, {"Retry-After": "2"}),
            "quota": Fault(429, QUOTA_BODY), "500": Fault(500), "503": Fault(503), "drop": Fault(0, drop=True),
        }[kind])
    return rng, start, end, days, faults


@pytest.mark.parametrize("seed", range(40))
def test_over_random_windows_gaps_page_sizes_and_faults_the_rows_are_exact_and_the_retries_bounded(server, seed):
    rng, start, end, days, faults = _world(seed)
    page_size, max_attempts = rng.randint(1, 7), rng.randint(1, 4)
    server.set_days(PROPERTY, days)
    server.requests.clear()
    server.faults.extend(faults)
    sleeps = Sleeps()
    rows = sum(1 for d, v in days.items() if start <= d < end and any(v.values()))
    pages_needed = max(1, math.ceil(rows / page_size))
    outcome, requests, waits = _model(len(faults), pages_needed, max_attempts)

    conn = make(server, sleeps, page_size=page_size, max_attempts=max_attempts, backoff_base=1.0, backoff_cap=30.0)
    got = []

    async def go():
        try:
            async for page in conn.fetch(window_start=start, window_end=end):
                got.append(page)
        finally:
            await conn._client.aclose()

    if outcome == "fail":
        with pytest.raises(RateLimited):
            run(go())
    else:
        run(go())
        points = [p for page in got for p in page.points]
        # exact: every stored day in the window once per metric, nothing from outside it, nothing twice
        assert as_set(points) == expected_points(days, start, end)
        assert len(points) == len(as_set(points)) == 4 * rows
        # the cursor of every page but the last is the offset to resume from, the last has none
        assert [p.cursor for p in got][-1] is None
        assert [p.cursor for p in got][:-1] == [str(page_size * (i + 1)) for i in range(len(got) - 1)]
        assert sum(p.api_calls for p in got) == requests and sum(p.throttle_waits for p in got) == waits
    assert len(server.requests) == requests, "the number of calls is the model's, so retries stay inside the bound"
    assert len(sleeps.calls) == waits
    assert all(0 <= w <= 30.0 for w in sleeps.calls)
    assert all(r[2] == "Bearer " + TOKEN for r in server.requests)
    assert not server.faults or outcome == "fail", "every scripted fault was met unless the run gave up first"
