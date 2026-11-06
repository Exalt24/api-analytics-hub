"""The Shopmonkey connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the Bearer header, `limit` and `skip`), how it reads the documented
envelope (`success`, `data`, `meta.hasMore`) and Order fields, the rules that matter for a repair shop (a job is booked on its created date and earns on its invoiced
date, a deleted order is not counted, a day is cut in the shop's time zone, an order repeated across pages is one order, a scan that never ends is refused), the error
taxonomy by status, that retries are bounded, and that the stand-in refuses what the saved pages say Shopmonkey refuses. They do not prove Shopmonkey accepts the
request: the server is written from the saved pages (docs/AD_AND_BUSINESS_SOURCES.md) and no live shop was used.

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
from app.connectors.fake_http import Fault as _Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from app.connectors.shopmonkey import ShopmonkeyConnector  # noqa: E402
from app.connectors.shopmonkey_fake import FakeShopmonkey, error_body  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402


def Fault(status, body=None, headers=None, drop=False):  # noqa: N802 - reads as the class it wraps
    return _Fault(status, body.encode() if isinstance(body, str) else body, headers or {}, drop)


TOKEN = "sm-SECRET-api-key"
START, END = date(2026, 10, 1), date(2026, 10, 4)


@pytest.fixture()
def server():
    with FakeShopmonkey(token=TOKEN) as fake:
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("currency", "USD")
    kw.setdefault("retrier", retrier(sleeps))
    return ShopmonkeyConnector(base_url=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


# ---------------------------------------------------------------- the happy path

def test_a_job_is_booked_on_its_created_day_and_earns_on_its_invoiced_day(server):
    server.add_order("a", "2026-10-01T14:00:00Z", invoiced="2026-10-03T16:00:00Z", total_cents=123456)
    server.add_order("b", "2026-10-01T15:00:00Z")
    server.add_order("c", "2026-10-02T15:00:00Z", invoiced="2026-10-02T17:00:00Z", total_cents=5000)
    pages = run(collect(make(server), START, END))
    got = by_key(pages)
    assert got[("work_orders", 1)] == 2 and got[("work_orders", 2)] == 1 and got[("work_orders", 3)] == 0
    assert got[("work_order_revenue", 1)] == 0 and got[("work_order_revenue", 2)] == 5000 and got[("work_order_revenue", 3)] == 123456
    assert all(type(p.value_numeric) is int for p in points_of(pages)) and len(points_of(pages)) == 6
    assert {(p.metric_key, p.currency) for p in points_of(pages)} == {("work_orders", None), ("work_order_revenue", "USD")}


def test_the_request_is_the_documented_one(server):
    run(collect(make(server), START, END))
    seen = server.requests[-1]
    assert seen.method == "GET" and seen.path == "/v3/order" and seen.headers["authorization"] == "Bearer " + TOKEN
    assert (seen.query["limit"], seen.query["skip"]) == ("100", "0")


def test_an_order_outside_the_window_is_ignored_and_a_window_with_no_order_is_all_zeros(server):
    server.add_order("old", "2026-09-30T14:00:00Z", invoiced="2026-09-30T15:00:00Z", total_cents=777)
    server.add_order("new", "2026-10-04T00:00:00Z")
    pages = run(collect(make(server), START, END))
    assert set(by_key(pages).values()) == {0} and len(points_of(pages)) == 6


def test_a_deleted_order_and_an_order_at_another_location_are_not_counted(server):
    server.add_order("gone", "2026-10-01T14:00:00Z", deleted=True)
    server.add_order("other", "2026-10-01T14:00:00Z", location_id="loc2")
    server.add_order("mine", "2026-10-01T14:00:00Z", location_id="loc1")
    assert by_key(run(collect(make(server, location_id="loc1"), START, END)))[("work_orders", 1)] == 1
    assert by_key(run(collect(make(server), START, END)))[("work_orders", 1)] == 2, "no location given counts the whole shop, the deleted one still left out"


def test_a_day_is_cut_in_the_shops_time_zone(server):
    server.add_order("late", "2026-10-02T03:30:00Z", invoiced="2026-10-02T03:45:00Z", total_cents=900)      # 23:30 on Oct 1 in New York
    utc = by_key(run(collect(make(server), START, END)))
    ny = by_key(run(collect(make(server, time_zone="America/New_York"), START, END)))
    assert utc[("work_orders", 2)] == 1 and ny[("work_orders", 1)] == 1 and ny[("work_orders", 2)] == 0
    assert ny[("work_order_revenue", 1)] == 900


def test_a_timestamp_without_an_offset_is_read_as_utc(server):
    server.add_order("naive", "2026-10-01T10:00:00")
    assert by_key(run(collect(make(server), START, END)))[("work_orders", 1)] == 1


def test_an_invoiced_order_with_no_invoiced_date_earns_nothing_until_it_has_one(server):
    order = server.add_order("a", "2026-10-01T10:00:00Z", invoiced="2026-10-01T11:00:00Z", total_cents=100)
    order["invoicedDate"] = None
    assert by_key(run(collect(make(server), START, END)))[("work_order_revenue", 1)] == 0


# ---------------------------------------------------------------- paging

def test_pages_follow_has_more_by_skip_and_every_order_is_read_once(server):
    server.max_limit = 2
    for i in range(5):
        server.add_order("o%d" % i, "2026-10-01T1%d:00:00Z" % i)
    pages = run(collect(make(server, page_size=2), START, END))
    assert by_key(pages)[("work_orders", 1)] == 5
    assert [(r.query["limit"], r.query["skip"]) for r in server.requests] == [("2", "0"), ("2", "2"), ("2", "4")]
    assert [len(p.points) for p in pages] == [0, 0, 6] and sum(p.api_calls for p in pages) == 3


def test_an_order_repeated_across_pages_is_one_order(server):
    order = {"id": "dup", "createdDate": "2026-10-01T10:00:00Z", "invoiced": False, "invoicedDate": None, "totalCostCents": 0, "deleted": False}
    server.faults[:] = [Fault(200, {"success": True, "data": [order], "meta": {"hasMore": True, "total": 2}}),
                        Fault(200, {"success": True, "data": [order], "meta": {"hasMore": False, "total": 2}})]
    assert by_key(run(collect(make(server), START, END)))[("work_orders", 1)] == 1


def test_has_more_with_an_empty_page_is_refused_instead_of_looping(server):
    server.faults[:] = [Fault(200, {"success": True, "data": [], "meta": {"hasMore": True, "total": 9}})]
    with pytest.raises(UpstreamBroken, match="does not advance"):
        run(collect(make(server), START, END))


def test_a_scan_that_never_ends_is_stopped_at_the_page_bound(server, monkeypatch):
    import app.connectors.shopmonkey as sm
    monkeypatch.setattr(sm, "MAX_PAGES", 3)
    for i in range(6):
        one = {"id": "o%d" % i, "createdDate": "2026-10-01T10:00:00Z", "invoiced": False}
        server.faults.append(Fault(200, {"success": True, "data": [one], "meta": {"hasMore": True, "total": 99}}))
    with pytest.raises(UpstreamBroken, match="more than 3 pages"):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("more", [None, "yes", 1])
def test_a_has_more_that_is_not_a_boolean_is_broken(server, more):
    server.faults[:] = [Fault(200, {"success": True, "data": [], "meta": {"hasMore": more}})]
    with pytest.raises(UpstreamBroken, match="hasMore"):
        run(collect(make(server), START, END))


def test_there_is_no_resume_cursor_so_one_passed_in_is_refused_not_ignored(server):
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="anything"))


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("http,exc", [(401, AuthExpired), (403, AuthRevoked), (400, UpstreamBroken), (404, UpstreamBroken)])
def test_a_status_that_retrying_cannot_fix_is_a_typed_error_at_once(server, http, exc):
    assert run(collect(make(server), START, END))                                    # positive control
    server.faults.append(Fault(http, error_body("API-0efe28", "scripted")))
    sleeps = Sleeps()
    with pytest.raises(exc):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


@pytest.mark.parametrize("http", [429, 500, 502, 503])
def test_a_429_or_5xx_is_retried_and_retry_after_sets_the_wait(server, http):
    server.faults.append(Fault(http, error_body("API-1", "slow"), {"Retry-After": "5"}))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [5.0]


def test_retries_stop_at_the_budget_with_a_rate_limit_for_the_runner(server):
    for _ in range(3):
        server.faults.append(Fault(429, error_body("API-1", "slow")))
    sleeps = Sleeps()
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0, 2.0]


def test_a_long_retry_after_is_handed_back_and_never_slept(server):
    server.faults.append(Fault(429, error_body("API-1", "slow"), {"Retry-After": "300"}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as caught:
        run(collect(make(server, sleeps), START, END))
    assert caught.value.retry_after_seconds == 300.0 and sleeps.calls == []


def test_success_false_inside_a_200_is_a_failure_and_not_an_empty_success(server):
    server.faults.append(Fault(200, error_body("API-9", "no way")))
    with pytest.raises(UpstreamBroken, match="success false"):
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


def test_a_bad_key_is_an_expired_credential_and_a_key_without_permission_is_revoked_and_neither_is_echoed(server):
    with pytest.raises(AuthExpired) as caught:
        run(collect(make(server, access_token="wrong-SECRET"), START, END))
    assert "wrong-SECRET" not in str(caught.value) and TOKEN not in str(caught.value)
    server.forbidden_tokens.add("limited-key")
    with pytest.raises(AuthRevoked):
        run(collect(make(server, access_token="limited-key"), START, END))


# ---------------------------------------------------------------- shapes that must not become figures

def order(**over):
    out = {"id": "o1", "createdDate": "2026-10-01T10:00:00Z", "invoiced": True, "invoicedDate": "2026-10-02T10:00:00Z", "totalCostCents": 100, "deleted": False}
    out.update(over)
    return out


def serve(server, *orders):
    server.faults[:] = [Fault(200, {"success": True, "data": list(orders), "meta": {"hasMore": False, "total": len(orders)}})]


@pytest.mark.parametrize("mutate", [
    {"id": None}, {"id": ""}, {"id": 5}, {"createdDate": None}, {"createdDate": "yesterday"}, {"createdDate": 5},
    {"invoicedDate": "soon"}, {"invoicedDate": 5},
    {"totalCostCents": None}, {"totalCostCents": 1.5}, {"totalCostCents": "100"}, {"totalCostCents": -1}, {"totalCostCents": True}, {"totalCostCents": 10 ** 19},
])
def test_an_order_that_is_not_what_the_pages_describe_is_refused_after_a_positive_control(server, mutate):
    serve(server, order())
    assert run(collect(make(server), START, END))                                    # positive control
    serve(server, order(**mutate))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_a_total_that_is_not_a_number_is_ignored_for_an_order_that_earns_outside_the_window(server):
    serve(server, order(invoicedDate="2026-11-20T10:00:00Z", totalCostCents=None))
    assert by_key(run(collect(make(server), START, END)))[("work_orders", 1)] == 1


def test_data_that_is_not_a_list_or_holds_a_non_object_is_refused(server):
    for bad in ("nope", {"a": 1}, [5], [None]):
        server.faults[:] = [Fault(200, {"success": True, "data": bad, "meta": {"hasMore": False}})]
        with pytest.raises(UpstreamBroken):
            run(collect(make(server), START, END))


# ---------------------------------------------------------------- construction, the key, the registry

def test_verify_checks_the_api_key_status(server):
    assert run(_verify(make(server, location_id="loc1"))) == "Shopmonkey API key (USD, location loc1)"
    assert server.requests[-1].path == "/v3/auth/api_key/status"
    with pytest.raises(AuthExpired):
        run(_verify(make(server, access_token="nope")))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_the_currency_is_required_and_a_zero_decimal_one_is_refused_because_the_field_is_cents():
    for bad in (None, "", "usd", "US", "DOLLARS", 5):
        with pytest.raises((ValueError, TypeError)):
            ShopmonkeyConnector(currency=bad, access_token="t")
    for yen in ("JPY", "KRW"):
        with pytest.raises(ValueError, match="two decimal"):
            ShopmonkeyConnector(currency=yen, access_token="t")
    with pytest.raises(TypeError):
        ShopmonkeyConnector(access_token="t")


def test_other_arguments_are_validated_before_any_request():
    for kw in ({"time_zone": "Not/AZone"}, {"time_zone": ""}, {"location_id": "has space"}, {"page_size": 0}):
        with pytest.raises(ValueError):
            ShopmonkeyConnector(currency="USD", access_token="t", **kw)
    with pytest.raises(ValueError):
        ShopmonkeyConnector(currency="USD")
    assert ShopmonkeyConnector(currency="USD", access_token="t").external_account == "all-locations"
    assert ShopmonkeyConnector(currency="USD", access_token="t", location_id="loc9").external_account == "loc9"


def test_a_window_with_no_days_is_refused_and_the_token_never_shows_in_a_repr():
    conn = ShopmonkeyConnector(currency="USD", access_token=TOKEN)
    assert TOKEN not in repr(conn)
    with pytest.raises(ValueError):
        run(collect(conn, START, START))


def test_from_env_reads_the_documented_variables_and_names_what_is_missing():
    conn = ShopmonkeyConnector.from_env({"SHOPMONKEY_API_KEY": "k", "SHOPMONKEY_CURRENCY": "USD", "SHOPMONKEY_LOCATION_ID": "L1",
                                         "SHOPMONKEY_TIME_ZONE": "America/New_York", "SHOPMONKEY_API_BASE_URL": "http://x"})
    assert conn.location_id == "L1" and conn.time_zone == "America/New_York" and conn._base == "http://x"
    with pytest.raises(ValueError, match="SHOPMONKEY_API_KEY and SHOPMONKEY_CURRENCY"):
        ShopmonkeyConnector.from_env({})


def test_the_registry_describes_the_platform_and_redacts_the_key():
    spec = registry.get("shopmonkey")
    assert spec.supported_metrics == frozenset({"work_orders", "work_order_revenue"})
    assert registry.redact("shopmonkey", {"currency": "USD", "access_token": "x"}) == {"currency": "USD", "access_token": "***"}
    built = registry.build("shopmonkey", currency="USD", access_token="t")
    assert isinstance(built, ShopmonkeyConnector)
    with pytest.raises(ValueError, match="missing credentials"):
        registry.build("shopmonkey", currency="USD")


# ---------------------------------------------------------------- the stand-in refuses what the pages say Shopmonkey refuses

def get(server, path="/v3/order", token=TOKEN, **params):
    return httpx.get(server.url + path, params=params, headers={"Authorization": "Bearer " + token}, timeout=5, trust_env=False)


@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "x"}, {"limit": "101"}, {"skip": "-1"}, {"skip": "x"}])
def test_what_the_pages_say_is_refused_is_refused_with_the_documented_error_body(server, params):
    assert get(server).status_code == 200                                            # positive control
    response = get(server, **params)
    body = response.json()
    assert response.status_code == 400 and body["success"] is False and body["code"].startswith("API-") and body["documentation_url"].endswith(body["code"])


def test_the_stand_in_carries_a_request_id_and_rejects_a_missing_key_and_an_unknown_path(server):
    assert get(server).headers["x-request-id"]
    assert httpx.get(server.url + "/v3/order", timeout=5, trust_env=False).status_code == 401
    assert get(server, path="/v2/order").status_code == 404
