"""The Square connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the version and Bearer headers, the begin and end times cut at the
location's local midnight, ascending order, the page size and cursor), how it reads the documented Payment objects (integer Money, `status`, `refunded_money`, the
cursor that is absent on the last page), the local-day, currency and refund rules, the error taxonomy by category and code, that retries are bounded, and that the
stand-in refuses what the saved pages say Square refuses. They do not prove Square accepts the request: the server is written from the saved pages
(docs/AD_AND_BUSINESS_SOURCES.md) and no live account was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from app.connectors.square import API_VERSION, SquareConnector  # noqa: E402
from app.connectors.square_fake import FakeSquare, errors_body  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

LOCATION = "LNYC0001"
TOKEN = "EAAA-SECRET-square-token"
START, END = date(2026, 10, 1), date(2026, 10, 4)


@pytest.fixture()
def server():
    with FakeSquare(token=TOKEN) as fake:
        fake.set_location(LOCATION, timezone_name="America/New_York", currency="USD")
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("retrier", retrier(sleeps))
    return SquareConnector(location_id=kw.pop("location_id", LOCATION), base_url=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


# ---------------------------------------------------------------- the happy path

def test_completed_payments_are_counted_per_local_day_with_total_and_refunded_money(server):
    server.add_payment("a", "2026-10-01T15:00:00Z", location_id=LOCATION, total=5000, refunded=1200)
    server.add_payment("b", "2026-10-01T19:00:00Z", location_id=LOCATION, total=2500, tip=300)
    server.add_payment("c", "2026-10-03T15:00:00Z", location_id=LOCATION, total=999)
    pages = run(collect(make(server), START, END))
    got = by_key(pages)
    assert got[("payments", 1)] == 2 and got[("payments_total", 1)] == 7500 and got[("payments_refunded", 1)] == 1200
    assert got[("payments", 2)] == 0 and got[("payments_total", 2)] == 0 and got[("payments_refunded", 2)] == 0
    assert got[("payments", 3)] == 1 and got[("payments_total", 3)] == 999
    assert all(type(p.value_numeric) is int for p in points_of(pages))
    assert {(p.metric_key, p.currency) for p in points_of(pages)} == {
        ("payments", None), ("payments_total", "USD"), ("payments_refunded", "USD")}
    assert len(points_of(pages)) == 9


def test_a_day_is_cut_at_the_locations_midnight_and_not_at_utc(server):
    server.add_payment("late", "2026-10-02T03:30:00Z", location_id=LOCATION, total=1000)      # 23:30 on Oct 1 in New York
    server.add_payment("early", "2026-10-02T04:00:00Z", location_id=LOCATION, total=2000)     # 00:00 on Oct 2 in New York
    got = by_key(run(collect(make(server), START, END)))
    assert got[("payments_total", 1)] == 1000 and got[("payments_total", 2)] == 2000


def test_the_request_asks_for_the_local_midnight_range_in_utc_ascending_with_the_version_pinned(server):
    run(collect(make(server), START, END))
    payments = [r for r in server.requests if r.path == "/v2/payments"][-1]
    assert payments.query["begin_time"] == "2026-10-01T04:00:00Z" and payments.query["end_time"] == "2026-10-04T04:00:00Z"
    assert (payments.query["location_id"], payments.query["sort_order"], payments.query["limit"]) == (LOCATION, "ASC", "100")
    assert payments.headers["square-version"] == API_VERSION and payments.headers["authorization"] == "Bearer " + TOKEN


def test_the_day_the_clocks_change_is_still_cut_at_local_midnight(server):
    # US daylight time ended on 2026-11-01: that local day is 25 hours long, from 04:00Z to 05:00Z the next day.
    server.add_payment("a", "2026-11-01T04:30:00Z", location_id=LOCATION, total=100)          # 00:30 EDT on Nov 1
    server.add_payment("b", "2026-11-02T04:30:00Z", location_id=LOCATION, total=200)          # 23:30 EST on Nov 1
    server.add_payment("c", "2026-11-02T05:30:00Z", location_id=LOCATION, total=400)          # 00:30 EST on Nov 2
    pages = run(collect(make(server), date(2026, 11, 1), date(2026, 11, 3)))
    got = by_key(pages)
    assert got[("payments_total", 1)] == 300 and got[("payments_total", 2)] == 400
    payments = [r for r in server.requests if r.path == "/v2/payments"][-1]
    assert payments.query["begin_time"] == "2026-11-01T04:00:00Z" and payments.query["end_time"] == "2026-11-03T05:00:00Z"


def test_only_completed_payments_count(server):
    for i, status in enumerate(("COMPLETED", "APPROVED", "PENDING", "CANCELED", "FAILED")):
        server.add_payment("p%d" % i, "2026-10-01T15:00:00Z", location_id=LOCATION, total=100, status=status)
    got = by_key(run(collect(make(server), START, END)))
    assert got[("payments", 1)] == 1 and got[("payments_total", 1)] == 100


def test_a_payment_at_another_location_is_not_counted(server):
    server.set_location("LOTHER")
    server.add_payment("x", "2026-10-01T15:00:00Z", location_id="LOTHER", total=9999)
    server.add_payment("y", "2026-10-01T15:00:00Z", location_id=LOCATION, total=1)
    assert by_key(run(collect(make(server), START, END)))[("payments_total", 1)] == 1


def test_a_window_with_no_payment_is_all_zeros_in_the_locations_currency(server):
    pages = run(collect(make(server), START, END))
    assert set(by_key(pages).values()) == {0} and len(points_of(pages)) == 9
    assert {p.currency for p in points_of(pages) if p.currency} == {"USD"}


def test_a_payment_at_the_exact_end_instant_belongs_to_the_next_day_whatever_end_time_inclusivity(server):
    for inclusive in (True, False):
        server.end_inclusive = inclusive
        server.payments.clear()
        server.add_payment("edge", "2026-10-04T04:00:00Z", location_id=LOCATION, total=777)   # 00:00 on Oct 4 in New York
        server.add_payment("in", "2026-10-04T03:59:59Z", location_id=LOCATION, total=5)
        assert by_key(run(collect(make(server), START, END)))[("payments_total", 3)] == 5


# ---------------------------------------------------------------- paging

def test_pages_follow_the_cursor_and_a_day_spanning_two_pages_is_one_total(server):
    server.page_limit = 2
    for i in range(5):
        server.add_payment("p%d" % i, "2026-10-01T%02d:00:00Z" % (12 + i), location_id=LOCATION, total=100 * (i + 1))
    pages = run(collect(make(server), START, END))
    assert by_key(pages)[("payments", 1)] == 5 and by_key(pages)[("payments_total", 1)] == 1500
    assert len([r for r in server.requests if r.path == "/v2/payments"]) == 3
    assert [len(p.points) for p in pages] == [0, 0, 9] and pages[-1].cursor is None
    assert sum(p.api_calls for p in pages) == 4                                          # locations once, then three pages


def test_a_payment_repeated_across_pages_is_one_payment(server):
    server.add_payment("dup", "2026-10-01T15:00:00Z", location_id=LOCATION, total=100)
    first = {"payments": [server.payments[0]], "cursor": "NEXT"}
    server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]}), Fault(200, first), Fault(200, {"payments": [server.payments[0]]})]
    got = by_key(run(collect(make(server), START, END)))
    assert got[("payments", 1)] == 1 and got[("payments_total", 1)] == 100


def test_a_cursor_that_comes_back_is_refused_instead_of_looping(server):
    page = {"payments": [], "cursor": "SAME"}
    server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]}), Fault(200, page), Fault(200, page)]
    with pytest.raises(UpstreamBroken, match="does not advance"):
        run(collect(make(server), START, END))


def test_a_runaway_response_is_stopped_at_the_page_bound(server, monkeypatch):
    import app.connectors.square as sq
    monkeypatch.setattr(sq, "MAX_PAGES", 3)
    server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]})] + [
        Fault(200, {"payments": [], "cursor": "C%d" % i}) for i in range(6)]
    with pytest.raises(UpstreamBroken, match="more than 3 pages"):
        run(collect(make(server), START, END))


def test_there_is_no_resume_cursor_so_one_passed_in_is_refused_not_ignored(server):
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="anything"))


# ---------------------------------------------------------------- the location

def test_the_locations_time_zone_and_currency_are_read_once_and_a_given_zone_wins(server):
    conn = make(server)
    run(collect(conn, START, END))
    assert (conn.time_zone, conn.currency) == ("America/New_York", "USD")
    assert len([r for r in server.requests if r.path == "/v2/locations"]) == 1
    server.requests.clear()
    other = make(server, time_zone="America/Los_Angeles")
    run(collect(other, START, END))
    assert other.time_zone == "America/Los_Angeles"
    assert [r for r in server.requests if r.path == "/v2/payments"][-1].query["begin_time"] == "2026-10-01T07:00:00Z"


def test_a_location_the_token_cannot_see_is_a_revoked_credential_and_an_inactive_or_currency_less_one_is_broken(server):
    with pytest.raises(AuthRevoked):
        run(collect(make(server, location_id="LNOPE"), START, END))
    server.set_location(LOCATION, status="INACTIVE")
    with pytest.raises(UpstreamBroken, match="not active"):
        run(collect(make(server), START, END))
    server.set_location(LOCATION)
    server.locations[LOCATION]["currency"] = "usd"
    with pytest.raises(UpstreamBroken, match="no currency"):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("zone", ["", "Not/AZone", None])
def test_a_location_with_no_usable_time_zone_is_broken_not_cut_at_utc(server, zone):
    server.locations[LOCATION]["timezone"] = zone
    with pytest.raises(UpstreamBroken, match="time zone"):
        run(collect(make(server), START, END))


def test_verify_reads_the_location(server):
    conn = make(server)
    assert run(_verify(conn)) == "Square location LNYC0001 (USD, America/New_York)"


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("http,category,code,exc", [
    (401, "AUTHENTICATION_ERROR", "UNAUTHORIZED", AuthExpired), (401, "AUTHENTICATION_ERROR", "ACCESS_TOKEN_EXPIRED", AuthExpired),
    (401, "AUTHENTICATION_ERROR", "ACCESS_TOKEN_REVOKED", AuthRevoked), (401, "AUTHENTICATION_ERROR", "CLIENT_DISABLED", AuthRevoked),
    (403, "AUTHENTICATION_ERROR", "INSUFFICIENT_SCOPES", AuthRevoked), (403, "INVALID_REQUEST_ERROR", "FORBIDDEN", AuthRevoked),
    (400, "INVALID_REQUEST_ERROR", "INVALID_CURSOR", UpstreamBroken), (404, "INVALID_REQUEST_ERROR", "NOT_FOUND", UpstreamBroken)])
def test_an_error_that_retrying_cannot_fix_is_a_typed_error_at_once(server, http, category, code, exc):
    assert run(collect(make(server), START, END))                                    # positive control
    server.faults.append(Fault(http, errors_body(category, code)))
    sleeps = Sleeps()
    with pytest.raises(exc):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


@pytest.mark.parametrize("http,category,code", [(429, "RATE_LIMITED_ERROR", "RATE_LIMITED"), (500, "API_ERROR", "INTERNAL_SERVER_ERROR"),
                                                (503, "API_ERROR", "SERVICE_UNAVAILABLE"), (502, "API_ERROR", "BAD_GATEWAY")])
def test_a_rate_limit_or_server_error_is_retried_with_a_bound_and_then_succeeds(server, http, category, code):
    server.faults.append(Fault(http, errors_body(category, code), {"Retry-After": "3"}))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [3.0]


def test_retries_stop_at_the_budget_with_a_rate_limit_for_the_runner(server):
    for _ in range(3):
        server.faults.append(Fault(429, errors_body("RATE_LIMITED_ERROR", "RATE_LIMITED")))
    sleeps = Sleeps()
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0, 2.0]


def test_a_long_retry_after_is_handed_back_and_never_slept(server):
    server.faults.append(Fault(429, errors_body("RATE_LIMITED_ERROR", "RATE_LIMITED"), {"Retry-After": "600"}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as caught:
        run(collect(make(server, sleeps), START, END))
    assert caught.value.retry_after_seconds == 600.0 and sleeps.calls == []


def test_an_errors_array_inside_a_200_is_a_failure_and_not_an_empty_success(server):
    server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]}), Fault(200, errors_body("API_ERROR", "INTERNAL_SERVER_ERROR"))]
    with pytest.raises(UpstreamBroken, match="errors in the body"):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("body", [b"<html>502</html>", b"[]", b'"text"', b"null", b""])
def test_a_200_that_is_not_a_json_object_is_broken_not_empty(server, body):
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_a_dropped_connection_is_a_transport_failure_that_is_retried(server):
    server.faults.append(Fault(0, drop=True))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0]


def test_a_bad_token_is_an_expired_credential_and_never_appears_in_the_message(server):
    with pytest.raises(AuthExpired) as caught:
        run(collect(make(server, access_token="wrong-SECRET"), START, END))
    assert "wrong-SECRET" not in str(caught.value) and TOKEN not in str(caught.value)


# ---------------------------------------------------------------- shapes that must not become figures

def payment(**over):
    out = {"id": "p1", "created_at": "2026-10-01T15:00:00Z", "status": "COMPLETED", "location_id": LOCATION,
           "total_money": {"amount": 100, "currency": "USD"}}
    out.update(over)
    return out


def serve(server, *payments):
    server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]}), Fault(200, {"payments": list(payments)})]


@pytest.mark.parametrize("mutate", [
    {"id": None}, {"id": ""}, {"created_at": None}, {"created_at": "2026-10-01T15:00:00"}, {"created_at": "yesterday"},
    {"created_at": "2026-09-30T15:00:00Z"}, {"created_at": "2026-10-09T15:00:00Z"},
    {"total_money": {"amount": 1.5, "currency": "USD"}}, {"total_money": {"amount": "100", "currency": "USD"}}, {"total_money": {"amount": True, "currency": "USD"}},
    {"total_money": {"amount": -5, "currency": "USD"}}, {"total_money": {"amount": 10 ** 19, "currency": "USD"}}, {"total_money": {"amount": 100}},
    {"total_money": {"amount": 100, "currency": "usd"}}, {"total_money": None}, {"total_money": "100"},
    {"total_money": {"amount": 100, "currency": "EUR"}},
    {"refunded_money": {"amount": 5, "currency": "EUR"}}, {"refunded_money": {"amount": -5, "currency": "USD"}}, {"refunded_money": {"amount": 1.5, "currency": "USD"}},
])
def test_a_payment_that_is_not_what_the_pages_describe_is_refused_after_a_positive_control(server, mutate):
    serve(server, payment())
    assert run(collect(make(server), START, END))                                    # positive control
    serve(server, payment(**mutate))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_a_payment_list_that_is_not_a_list_or_holds_a_non_object_is_refused(server):
    for bad in ("nope", {"a": 1}, [5], [None]):
        server.faults[:] = [Fault(200, {"locations": [dict(server.locations[LOCATION])]}), Fault(200, {"payments": bad})]
        with pytest.raises(UpstreamBroken):
            run(collect(make(server), START, END))


def test_a_non_completed_payment_is_not_validated_for_money_it_may_not_have(server):
    serve(server, payment(status="FAILED", total_money=None, id="f"), payment())
    assert by_key(run(collect(make(server), START, END)))[("payments", 1)] == 1


# ---------------------------------------------------------------- construction and the registry

def test_the_location_id_is_validated_before_any_request():
    for bad in ("", "has space", "x" * 80, "a/b"):
        with pytest.raises(ValueError):
            SquareConnector(location_id=bad, access_token="t")
    with pytest.raises(ValueError):
        SquareConnector(location_id="L1")
    assert SquareConnector(location_id=" L1 ", access_token="t").external_account == "L1"


def test_a_window_with_no_days_is_refused_and_the_token_never_shows_in_a_repr():
    conn = SquareConnector(location_id="L1", access_token=TOKEN)
    assert TOKEN not in repr(conn)
    with pytest.raises(ValueError):
        run(collect(conn, START, START))


def test_from_env_reads_the_documented_variables_and_names_what_is_missing():
    conn = SquareConnector.from_env({"SQUARE_LOCATION_ID": "L9", "SQUARE_ACCESS_TOKEN": "t", "SQUARE_API_BASE_URL": "http://x"})
    assert conn.location_id == "L9" and conn._base == "http://x"
    with pytest.raises(ValueError, match="SQUARE_LOCATION_ID and SQUARE_ACCESS_TOKEN"):
        SquareConnector.from_env({})


def test_the_registry_describes_the_platform_and_the_money_metrics_carry_a_currency():
    spec = registry.get("square")
    assert spec.supported_metrics == frozenset({"payments", "payments_total", "payments_refunded"})
    assert registry.secret_keys("square") == frozenset({"access_token"}) and registry.platforms_supporting("payments_total") == ["square"]
    from app.connectors.base import CanonicalPoint
    with pytest.raises(UpstreamBroken):
        CanonicalPoint("payments_total", date(2026, 10, 1), 1)
    with pytest.raises(UpstreamBroken):
        CanonicalPoint("payments", date(2026, 10, 1), 1, "USD")


# ---------------------------------------------------------------- the stand-in refuses what the pages say Square refuses

def get(server, path="/v2/payments", **params):
    base = {"location_id": LOCATION, "begin_time": "2026-10-01T00:00:00Z", "end_time": "2026-10-05T00:00:00Z"}
    base.update(params)
    return httpx.get(server.url + path, params=base, headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False)


@pytest.mark.parametrize("params,status", [
    ({"begin_time": "yesterday"}, 400), ({"begin_time": "2026-10-01T00:00:00"}, 400), ({"sort_order": "SIDEWAYS"}, 400), ({"limit": "0"}, 400),
    ({"limit": "x"}, 400), ({"cursor": "garbage"}, 400), ({"location_id": "LNOPE"}, 404)])
def test_what_the_pages_say_is_refused_is_refused_with_the_documented_error_array(server, params, status):
    assert get(server).status_code == 200                                            # positive control
    response = get(server, **params)
    assert response.status_code == status and response.json()["errors"][0]["category"] == "INVALID_REQUEST_ERROR"


def test_a_limit_above_100_is_ignored_and_the_default_is_used(server):
    for i in range(150):
        server.add_payment("p%d" % i, "2026-10-01T15:00:%02dZ" % (i % 60), location_id=LOCATION, total=1)
    body = get(server, limit="500", sort_order="ASC").json()
    assert len(body["payments"]) == 100 and "cursor" in body


def test_the_stand_in_rejects_a_bad_token_and_an_unknown_path(server):
    assert httpx.get(server.url + "/v2/payments", timeout=5, trust_env=False).json()["errors"][0]["code"] == "UNAUTHORIZED"
    assert httpx.get(server.url + "/v1/payments", headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False).status_code == 404
