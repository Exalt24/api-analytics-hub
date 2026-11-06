"""The TikTok connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the `Access-Token` header, the JSON list parameters, the
inclusive end date, 30 day runs), how it reads the documented response (the envelope, string metrics, `page_info`, a day with no data left out), the money and
currency rules, the error taxonomy by return code, that retries are bounded, and that the stand-in refuses what the saved pages say TikTok refuses. They do
not prove TikTok accepts the request: the server is written from the saved pages (docs/AD_AND_BUSINESS_SOURCES.md) and no live advertiser was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from app.connectors.tiktok import TikTokConnector  # noqa: E402
from app.connectors.tiktok_fake import FakeTikTok, envelope  # noqa: E402
from vendor_helpers import Sleeps, as_set, client, collect, points_of, retrier, run  # noqa: E402

ADVERTISER = "7001234567890"
TOKEN = "tt-SECRET-token-123"
START, END = date(2026, 10, 1), date(2026, 10, 4)


def day(n: int, **over):
    base = {"spend": "%d.50" % (10 + n), "impressions": 1000 * n, "clicks": 20 * n, "conversion": n}
    base.update(over)
    return base


THREE_DAYS = {date(2026, 10, 1): day(1), date(2026, 10, 2): day(2), date(2026, 10, 3): day(3)}


@pytest.fixture()
def server():
    with FakeTikTok(token=TOKEN) as fake:
        fake.set_advertiser(ADVERTISER, THREE_DAYS, currency="EUR")
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("retrier", retrier(sleeps))
    return TikTokConnector(advertiser_id=kw.pop("advertiser_id", ADVERTISER), base_url=server.url, client=client(), **kw)


def expected(days=THREE_DAYS, currency="EUR", start=START, end=END):
    from app.connectors.common import minor_units
    out = set()
    for d, v in days.items():
        if start <= d < end:
            out |= {("ad_spend", d, minor_units(v["spend"], currency, "x"), currency), ("ad_impressions", d, v["impressions"], None),
                    ("ad_clicks", d, v["clicks"], None), ("ad_conversions", d, int(v["conversion"]) * 1000, None)}
    return out


# ---------------------------------------------------------------- the happy path

def test_every_day_and_metric_arrives_as_an_integer_with_the_advertisers_currency_on_money_only(server):
    pages = run(collect(make(server), START, END))
    assert as_set(points_of(pages)) == expected()
    assert all(type(p.value_numeric) is int for p in points_of(pages))
    assert {p.metric_key for p in points_of(pages) if p.currency} == {"ad_spend"}
    assert len(pages) == 1 and pages[0].cursor is None and pages[0].api_calls == 1


def test_the_request_is_the_documented_one(server):
    run(collect(make(server), START, END))
    seen = server.requests[-1]
    assert seen.method == "GET" and seen.path == "/open_api/v1.3/report/integrated/get/" and seen.headers["access-token"] == TOKEN
    q = seen.query
    assert (q["advertiser_id"], q["service_type"], q["report_type"], q["data_level"]) == (ADVERTISER, "AUCTION", "BASIC", "AUCTION_ADVERTISER")
    assert json.loads(q["dimensions"]) == ["advertiser_id", "stat_time_day"]
    assert json.loads(q["metrics"]) == ["spend", "impressions", "clicks", "conversion", "currency"]
    # half open [Oct 1, Oct 4) becomes the inclusive range Oct 1 to Oct 3
    assert (q["start_date"], q["end_date"], q["page"], q["page_size"]) == ("2026-10-01", "2026-10-03", "1", "200")
    assert "authorization" not in seen.headers


def test_a_fractional_or_zero_conversion_is_kept_in_thousandths(server):
    server.set_advertiser(ADVERTISER, {date(2026, 10, 1): day(1, conversion="2.5"), date(2026, 10, 2): day(2, conversion="0")}, currency="EUR")
    got = as_set(points_of(run(collect(make(server), START, END))))
    assert ("ad_conversions", date(2026, 10, 1), 2500, None) in got and ("ad_conversions", date(2026, 10, 2), 0, None) in got


def test_a_day_the_list_leaves_out_is_a_quiet_day_and_an_empty_window_is_an_empty_success(server):
    server.set_advertiser(ADVERTISER, {}, currency="EUR")
    pages = run(collect(make(server), START, END))
    assert points_of(pages) == [] and len(pages) == 1 and pages[0].cursor is None


def test_a_stat_time_day_with_or_without_a_time_is_read(server):
    server.day_format = "%s"
    assert as_set(points_of(run(collect(make(server), START, END)))) == expected()


# ---------------------------------------------------------------- paging and the 30 day runs

def test_pages_follow_total_page_and_the_cursor_names_the_run_and_the_next_page(server):
    pages = run(collect(make(server, page_size=2), START, END))
    assert [len(p.points) for p in pages] == [8, 4] and pages[0].cursor == "2026-10-01:2" and pages[1].cursor is None
    assert as_set(points_of(pages)) == expected()
    assert [r.query["page"] for r in server.requests] == ["1", "2"]


def test_a_window_longer_than_30_days_is_cut_into_runs_of_at_most_30_days(server):
    days = {date(2026, 8, 1) + timedelta(days=i): day(1) for i in range(75)}
    server.set_advertiser(ADVERTISER, days, currency="EUR")
    start, end = date(2026, 8, 1), date(2026, 8, 1) + timedelta(days=75)
    pages = run(collect(make(server), start, end))
    spans = [((date.fromisoformat(r.query["end_date"]) - date.fromisoformat(r.query["start_date"])).days + 1) for r in server.requests]
    assert spans == [30, 30, 15] and max(spans) <= 30
    assert as_set(points_of(pages)) == expected(days, start=start, end=end)
    assert [p.cursor for p in pages] == ["2026-08-31:1", "2026-09-30:1", None]


def test_a_cursor_resumes_at_its_run_and_page_and_a_foreign_or_outside_one_is_refused(server):
    first = run(collect(make(server, page_size=2), START, END))
    resumed = run(collect(make(server, page_size=2), START, END, cursor=first[0].cursor))
    assert as_set(points_of(resumed)) == as_set(first[1].points)
    for bad in ("garbage", "2026-10-01", "2026-09-01:1", "2026-10-09:1", "2026-10-01:0"):
        with pytest.raises(ValueError):
            run(collect(make(server), START, END, cursor=bad))


def test_a_report_that_never_ends_is_stopped_at_the_page_bound(server, monkeypatch):
    import app.connectors.tiktok as t
    monkeypatch.setattr(t, "MAX_PAGES", 3)
    for _ in range(5):
        server.faults.append(Fault(200, envelope(0, "OK", {"page_info": {"total_page": 99}, "list": []})))
    with pytest.raises(UpstreamBroken, match="more than 3 pages"):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("total", [None, "2", True, -1, 1.5])
def test_a_total_page_that_is_not_a_whole_number_is_broken(server, total):
    server.faults.append(Fault(200, envelope(0, "OK", {"page_info": {"total_page": total}, "list": []})))
    with pytest.raises(UpstreamBroken, match="total_page"):
        run(collect(make(server), START, END))


def test_the_truncation_header_means_the_figures_are_incomplete_and_is_refused(server):
    assert run(collect(make(server), START, END))                                    # positive control
    server.throttle_header = "Number of ad ids is equal or more than 20k, only return the top 20k id's results"
    with pytest.raises(UpstreamBroken, match="truncated"):
        run(collect(make(server), START, END))


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("code,exc", [(40105, AuthExpired), (40001, AuthRevoked), (40106, AuthRevoked), (40002, UpstreamBroken), (40000, UpstreamBroken),
                                      (40007, UpstreamBroken), (41000, UpstreamBroken)])
def test_a_return_code_that_retrying_cannot_fix_is_a_typed_error_at_once(server, code, exc):
    assert run(collect(make(server), START, END))                                    # positive control
    server.faults.append(Fault(200, envelope(code, "scripted")))
    sleeps = Sleeps()
    with pytest.raises(exc):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


@pytest.mark.parametrize("code", [40016, 40100, 40133, 50000, 50002, 51305, 60001])
def test_a_rate_limit_or_system_return_code_is_retried_with_a_bound_and_then_succeeds(server, code):
    server.faults.append(Fault(200, envelope(code, "scripted")))
    sleeps = Sleeps()
    pages = run(collect(make(server, sleeps), START, END))
    assert as_set(points_of(pages)) == expected() and sleeps.calls == [1.0] and pages[0].api_calls == 2 and pages[0].throttle_waits == 1


def test_retries_stop_at_the_budget_with_a_rate_limit_for_the_runner(server):
    for _ in range(3):
        server.faults.append(Fault(200, envelope(40133, "Advertiser's QPS reaches QPS limit")))
    sleeps = Sleeps()
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0, 2.0] and len(server.requests) == 3


@pytest.mark.parametrize("http,exc", [(401, AuthExpired), (403, AuthRevoked), (400, UpstreamBroken)])
def test_an_http_status_with_no_return_code_is_read_by_its_status(server, http, exc):
    server.faults.append(Fault(http, {"message": "no code here"}))
    with pytest.raises(exc):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("http", [429, 500, 502, 503])
def test_a_429_or_5xx_is_retried_and_a_retry_after_header_sets_the_wait(server, http):
    server.faults.append(Fault(http, {"message": "slow down"}, {"Retry-After": "7"}))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [7.0]


def test_a_wait_longer_than_the_cap_is_handed_back_and_never_slept(server):
    server.faults.append(Fault(429, {"message": "slow down"}, {"Retry-After": "900"}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as caught:
        run(collect(make(server, sleeps), START, END))
    assert caught.value.retry_after_seconds == 900.0 and sleeps.calls == []


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


def test_an_advertiser_the_token_cannot_read_is_a_revoked_credential(server):
    with pytest.raises(AuthRevoked):
        run(collect(make(server, advertiser_id="999"), START, END))


# ---------------------------------------------------------------- shapes that must not become figures

def row(**over):
    out = {"dimensions": {"advertiser_id": ADVERTISER, "stat_time_day": "2026-10-01 00:00:00"},
           "metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "EUR"}}
    for key, value in over.items():
        out[key] = value
    return out


def serve(server, *rows):
    server.faults.append(Fault(200, envelope(0, "OK", {"page_info": {"total_page": 1}, "list": list(rows)})))


@pytest.mark.parametrize("mutate", [
    {"dimensions": {"advertiser_id": "OTHER", "stat_time_day": "2026-10-01 00:00:00"}},
    {"dimensions": {"advertiser_id": ADVERTISER}}, {"dimensions": {"advertiser_id": ADVERTISER, "stat_time_day": "10/01/2026"}},
    {"dimensions": {"advertiser_id": ADVERTISER, "stat_time_day": "2026-02-30 00:00:00"}},
    {"dimensions": {"advertiser_id": ADVERTISER, "stat_time_day": "2026-09-30 00:00:00"}},
    {"metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "1"}},
    {"metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "eur"}},
    {"metrics": {"spend": "-1", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "EUR"}},
    {"metrics": {"spend": "abc", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "EUR"}},
    {"metrics": {"spend": 1.5, "impressions": "10", "clicks": "1", "conversion": "1", "currency": "EUR"}},
    {"metrics": {"spend": "1.00", "impressions": "1.5", "clicks": "1", "conversion": "1", "currency": "EUR"}},
    {"metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "currency": "EUR"}},
    {"metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "-2", "currency": "EUR"}},
    {"metrics": "nope"}, {"dimensions": "nope"},
])
def test_a_row_that_is_not_what_the_pages_describe_is_refused_after_a_positive_control(server, mutate):
    serve(server, row())
    assert run(collect(make(server), START, END))                                    # positive control
    serve(server, row(**mutate))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_two_rows_for_one_day_and_two_currencies_are_refused(server):
    serve(server, row(), row())
    with pytest.raises(UpstreamBroken, match="two rows"):
        run(collect(make(server), START, END))
    other = row(dimensions={"advertiser_id": ADVERTISER, "stat_time_day": "2026-10-02 00:00:00"})
    other["metrics"] = dict(other["metrics"], currency="USD")
    serve(server, row(), other)
    with pytest.raises(UpstreamBroken, match="two currencies"):
        run(collect(make(server), START, END))


def test_a_row_for_another_advertiser_is_refused_so_it_cannot_be_stored_under_this_connection(server):
    server.rows_for_advertiser = "555"
    with pytest.raises(UpstreamBroken, match="advertiser 555"):
        run(collect(make(server), START, END))


def test_the_currency_must_be_the_same_across_30_day_runs(server):
    days = {date(2026, 8, 1): day(1), date(2026, 9, 15): day(1)}
    server.set_advertiser(ADVERTISER, days, currency="EUR")
    pages = run(collect(make(server), date(2026, 8, 1), date(2026, 9, 20)))
    assert {p.currency for p in points_of(pages) if p.currency} == {"EUR"}
    server.faults.append(Fault(200, envelope(0, "OK", {"page_info": {"total_page": 1}, "list": [row(dimensions={
        "advertiser_id": ADVERTISER, "stat_time_day": "2026-08-02 00:00:00"})]})))
    other = row(dimensions={"advertiser_id": ADVERTISER, "stat_time_day": "2026-09-15 00:00:00"})
    other["metrics"] = dict(other["metrics"], currency="USD")
    server.faults.append(Fault(200, envelope(0, "OK", {"page_info": {"total_page": 1}, "list": [other]})))
    with pytest.raises(UpstreamBroken, match="two currencies"):
        run(collect(make(server), date(2026, 8, 1), date(2026, 9, 20)))


# ---------------------------------------------------------------- the account, the registry, construction

def test_verify_reads_status_currency_and_time_zone_and_refuses_a_disabled_advertiser(server):
    conn = make(server)
    assert run(_verify(conn)) == "TikTok advertiser %s (EUR, America/New_York)" % ADVERTISER and conn.currency == "EUR"
    server.set_advertiser(ADVERTISER, THREE_DAYS, currency="EUR", status="STATUS_DISABLE")
    with pytest.raises(UpstreamBroken, match="not enabled"):
        run(_verify(make(server)))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_the_advertiser_id_is_validated_before_any_request():
    for bad in ("", "abc", "12 34", "1" * 30):
        with pytest.raises(ValueError):
            TikTokConnector(advertiser_id=bad, access_token="t")
    assert TikTokConnector(advertiser_id=" 123 ", access_token="t").external_account == "123"
    with pytest.raises(ValueError):
        TikTokConnector(advertiser_id="1")
    with pytest.raises(ValueError):
        TikTokConnector(advertiser_id="1", access_token="t", page_size=0)


def test_a_window_with_no_days_is_refused_and_the_token_never_shows_in_a_repr():
    conn = TikTokConnector(advertiser_id="1", access_token=TOKEN)
    assert TOKEN not in repr(conn)
    with pytest.raises(ValueError):
        run(collect(conn, START, START))


def test_from_env_reads_the_documented_variables_and_names_what_is_missing():
    conn = TikTokConnector.from_env({"TIKTOK_ADVERTISER_ID": "42", "TIKTOK_ACCESS_TOKEN": "t", "TIKTOK_API_BASE_URL": "http://x"})
    assert conn.advertiser_id == "42" and conn._base == "http://x"
    with pytest.raises(ValueError, match="TIKTOK_ADVERTISER_ID and TIKTOK_ACCESS_TOKEN"):
        TikTokConnector.from_env({})


def test_the_registry_describes_the_platform_and_redacts_the_token():
    spec = registry.get("tiktok")
    assert spec.supported_metrics == TikTokConnector.supported_metrics and registry.secret_keys("tiktok") == frozenset({"access_token"})
    assert registry.redact("tiktok", {"advertiser_id": "1", "access_token": "x"}) == {"advertiser_id": "1", "access_token": "***"}
    assert registry.platforms_supporting("ad_conversions") == ["google_ads", "tiktok"]
    with pytest.raises(ValueError, match="missing credentials"):
        registry.build("tiktok", advertiser_id="1")


# ---------------------------------------------------------------- the stand-in refuses what the pages say TikTok refuses

def get(server, **params):
    base = {"advertiser_id": ADVERTISER, "service_type": "AUCTION", "report_type": "BASIC", "data_level": "AUCTION_ADVERTISER",
            "dimensions": '["advertiser_id","stat_time_day"]', "metrics": '["spend","currency"]', "start_date": "2026-10-01", "end_date": "2026-10-03"}
    base.update({k: v for k, v in params.items() if v is not None})
    for k, v in params.items():
        if v is None:
            base.pop(k, None)
    return httpx.get(server.url + "/open_api/v1.3/report/integrated/get/", params=base, headers={"Access-Token": TOKEN}, timeout=5, trust_env=False).json()


@pytest.mark.parametrize("params", [
    {"report_type": "NOPE"}, {"service_type": None}, {"data_level": "AUCTION_GALAXY"}, {"dimensions": '["stat_time_day"]', "metrics": '["spend","currency"]'},
    {"dimensions": '["nope"]'}, {"metrics": '["nope"]'}, {"metrics": "not json"}, {"start_date": "10/01/2026"}, {"start_date": "2026-10-04"},
    {"start_date": "2026-08-01", "end_date": "2026-10-03"}, {"page": "0"}, {"page_size": "x"}, {"advertiser_id": "999"},
])
def test_what_the_pages_say_is_refused_is_refused_with_a_non_zero_code(server, params):
    assert get(server)["code"] == 0                                                  # positive control
    assert get(server, **params)["code"] != 0


def test_the_30_day_rule_is_about_the_daily_breakdown_only(server):
    long = {"start_date": "2026-08-01", "end_date": "2026-10-03"}
    assert get(server, **long)["code"] == 40002
    assert get(server, dimensions='["advertiser_id"]', **long)["code"] == 0


def test_the_stand_in_rejects_a_missing_or_wrong_access_token_and_an_unknown_path(server):
    url = server.url + "/open_api/v1.3/report/integrated/get/"
    assert httpx.get(url, params={"advertiser_id": ADVERTISER}, timeout=5, trust_env=False).json()["code"] == 40105
    assert httpx.get(server.url + "/open_api/v1.2/report/integrated/get/", headers={"Access-Token": TOKEN}, timeout=5, trust_env=False).status_code == 404
