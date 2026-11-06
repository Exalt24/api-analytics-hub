"""The Google Ads connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path with the API version and the customer id, the Bearer header,
no developer token, the GAQL text with its inclusive date range, the Local Services query), how it reads the documented response (lowerCamelCase, int64 as
strings, doubles as numbers, `nextPageToken`), the money, currency and conversion rules, the error taxonomy by Status, that retries are bounded, and that the
stand-in refuses what the saved pages say Google refuses. They do not prove Google accepts the request: the server is written from the saved pages
(docs/AD_AND_BUSINESS_SOURCES.md) and no live account was used, so nothing here is a live integration.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.google_ads import GoogleAdsConnector, normalise_customer_id  # noqa: E402
from app.connectors.google_ads_fake import FakeGoogleAds, status_error  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, as_set, client, collect, points_of, retrier, run  # noqa: E402

CUSTOMER = "1234567890"
TOKEN = "ya29.SECRET-google-token"
START, END = date(2026, 10, 1), date(2026, 10, 4)


def day(n: int, **over):
    base = {"cost_micros": 10_500_000 + n * 1_000_000, "impressions": 1000 * n, "clicks": 20 * n, "conversions": Decimal(n) + Decimal("0.5"),
            "conversions_value": Decimal(50 * n) + Decimal("0.25")}
    base.update(over)
    return base


THREE_DAYS = {date(2026, 10, 1): day(1), date(2026, 10, 2): day(2), date(2026, 10, 3): day(3)}


@pytest.fixture()
def server():
    with FakeGoogleAds(token=TOKEN) as fake:
        fake.set_customer(CUSTOMER, THREE_DAYS, currency="EUR", leads=["2026-10-01 08:00:00", "2026-10-01 17:30:00", "2026-10-03 09:15:00"])
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("retrier", retrier(sleeps))
    return GoogleAdsConnector(customer_id=kw.pop("customer_id", CUSTOMER), base_url=server.url, client=client(), **kw)


def expected(days=THREE_DAYS, currency="EUR", start=START, end=END):
    out = set()
    exp = 0 if currency == "JPY" else 2
    for d, v in days.items():
        if not start <= d < end:
            continue
        spend = (Decimal(v["cost_micros"]) / Decimal(10) ** (6 - exp)).quantize(Decimal(1), rounding="ROUND_HALF_UP")
        out |= {("ad_spend", d, int(spend), currency), ("ad_impressions", d, v["impressions"], None), ("ad_clicks", d, v["clicks"], None),
                ("ad_conversions", d, int(Decimal(v["conversions"]) * 1000), None),
                ("ad_conversion_value", d, int((Decimal(v["conversions_value"]) * 10 ** exp).quantize(Decimal(1), rounding="ROUND_HALF_UP")), currency)}
    return out


# ---------------------------------------------------------------- the happy path

def test_every_day_and_metric_arrives_as_an_integer_with_the_accounts_currency_on_money_only(server):
    pages = run(collect(make(server), START, END))
    assert as_set(points_of(pages)) == expected()
    assert all(type(p.value_numeric) is int for p in points_of(pages))
    assert {p.metric_key for p in points_of(pages) if p.currency} == {"ad_spend", "ad_conversion_value"}
    assert len(pages) == 1 and pages[0].cursor is None and pages[0].api_calls == 1


def test_the_request_is_the_documented_one_and_sends_no_developer_token(server):
    run(collect(make(server, login_customer_id="987-654-3210"), START, END))
    seen = server.requests[-1]
    assert seen.method == "POST" and seen.path == "/v26/customers/%s/googleAds:search" % CUSTOMER
    assert seen.headers["authorization"] == "Bearer " + TOKEN and seen.headers["login-customer-id"] == "9876543210"
    assert "developer-token" not in seen.headers
    import json
    query = json.loads(seen.body)["query"]
    # half open [Oct 1, Oct 4) becomes the inclusive range Oct 1 to Oct 3
    assert "segments.date >= '2026-10-01' AND segments.date <= '2026-10-03'" in query
    assert query.startswith("SELECT segments.date, customer.currency_code, metrics.cost_micros") and "FROM customer" in query
    assert "pageToken" not in json.loads(seen.body)


def test_no_login_customer_header_is_sent_for_a_direct_account(server):
    run(collect(make(server), START, END))
    assert "login-customer-id" not in server.requests[-1].headers


def test_micros_become_minor_units_by_dividing_by_ten_thousand_for_a_two_decimal_currency_and_a_million_for_yen():
    with FakeGoogleAds(token=TOKEN) as fake:
        fake.set_customer(CUSTOMER, {date(2026, 10, 1): day(1, cost_micros=12_345_000), date(2026, 10, 2): day(2, cost_micros=4_999)}, currency="USD")
        usd = as_set(points_of(run(collect(make(fake), START, END))))
        fake.set_customer(CUSTOMER, {date(2026, 10, 1): day(1, cost_micros=12_345_000)}, currency="JPY")
        jpy = as_set(points_of(run(collect(make(fake), START, END))))
    assert ("ad_spend", date(2026, 10, 1), 1235, "USD") in usd, "12.345 is 1234.5 cents and half rounds up"
    assert ("ad_spend", date(2026, 10, 2), 0, "USD") in usd, "0.004999 is under half a cent"
    assert ("ad_spend", date(2026, 10, 1), 12, "JPY") in jpy, "a yen has no minor unit"


def test_a_fractional_conversion_is_kept_in_thousandths(server):
    server.set_customer(CUSTOMER, {date(2026, 10, 1): day(1, conversions=Decimal("2.3333333"))}, currency="EUR")
    got = as_set(points_of(run(collect(make(server), START, END))))
    assert ("ad_conversions", date(2026, 10, 1), 2333, None) in got


def test_a_zero_double_the_response_leaves_out_reads_as_zero_and_never_as_a_missing_row(server):
    server.set_customer(CUSTOMER, {date(2026, 10, 1): day(1, conversions=0, conversions_value=0)}, currency="EUR")
    server.omit_zero_doubles = True
    got = as_set(points_of(run(collect(make(server), START, END))))
    assert ("ad_conversions", date(2026, 10, 1), 0, None) in got and ("ad_conversion_value", date(2026, 10, 1), 0, "EUR") in got


def test_a_day_with_no_row_is_a_quiet_day_and_an_empty_window_is_an_empty_success(server):
    server.set_customer(CUSTOMER, {}, currency="EUR")
    pages = run(collect(make(server), START, END))
    assert points_of(pages) == [] and len(pages) == 1


def test_the_window_is_cut_to_what_was_asked(server):
    got = as_set(points_of(run(collect(make(server), date(2026, 10, 2), date(2026, 10, 3)))))
    assert got == expected(start=date(2026, 10, 2), end=date(2026, 10, 3))


# ---------------------------------------------------------------- paging

def test_pages_follow_the_next_page_token_to_the_end_and_a_cursor_names_where_it_stopped(server):
    server.page_size = 2
    pages = run(collect(make(server), START, END))
    assert [len(p.points) for p in pages] == [10, 5] and pages[0].cursor.startswith("m:") and pages[1].cursor is None
    assert as_set(points_of(pages)) == expected()
    import json
    assert [("pageToken" in json.loads(r.body)) for r in server.requests] == [False, True]


def test_a_page_token_that_comes_back_is_refused_instead_of_looping(server):
    server.faults.append(Fault(200, {"results": [], "nextPageToken": "SAME"}))
    server.faults.append(Fault(200, {"results": [], "nextPageToken": "SAME"}))
    with pytest.raises(UpstreamBroken, match="does not advance"):
        run(collect(make(server), START, END))


def test_a_runaway_response_is_stopped_at_the_page_bound(server, monkeypatch):
    import app.connectors.google_ads as g
    monkeypatch.setattr(g, "MAX_PAGES", 3)
    for i in range(5):
        server.faults.append(Fault(200, {"results": [], "nextPageToken": "T%d" % i}))
    with pytest.raises(UpstreamBroken, match="more than 3 pages"):
        run(collect(make(server), START, END))


def test_a_cursor_this_connector_did_not_issue_is_refused_and_a_valid_one_resumes(server):
    with pytest.raises(ValueError, match="not issued"):
        run(collect(make(server), START, END, cursor="garbage"))
    server.page_size = 2
    first = run(collect(make(server), START, END))
    resumed = run(collect(make(server), START, END, cursor=first[0].cursor))
    assert as_set(points_of(resumed)) == as_set(first[1].points)


# ---------------------------------------------------------------- Local Services Ads

def test_local_services_leads_are_counted_per_day_and_a_quiet_day_is_a_zero(server):
    pages = run(collect(make(server, local_services=True), START, END))
    leads = {p.observed_on.day: p.value_numeric for p in points_of(pages) if p.metric_key == "ad_leads"}
    assert leads == {1: 2, 2: 0, 3: 1}
    assert pages[0].cursor == "l:" and pages[-1].cursor is None
    query = [__import__("json").loads(r.body)["query"] for r in server.requests][-1]
    assert "FROM local_services_lead" in query and "creation_date_time >= '2026-10-01 00:00:00'" in query and "< '2026-10-04 00:00:00'" in query


def test_no_leads_are_asked_for_unless_the_option_is_set(server):
    pages = run(collect(make(server), START, END))
    assert "ad_leads" not in {p.metric_key for p in points_of(pages)}
    assert all("local_services_lead" not in __import__("json").loads(r.body)["query"] for r in server.requests)


def test_a_cursor_after_the_account_query_goes_straight_to_the_leads(server):
    pages = run(collect(make(server, local_services=True), START, END, cursor="l:"))
    assert {p.metric_key for p in points_of(pages)} == {"ad_leads"}


def test_a_lead_outside_the_window_or_with_a_bad_timestamp_is_refused(server):
    def lead(stamp):
        return {"results": [{"localServicesLead": {"creationDateTime": stamp, "leadType": "PHONE_CALL"}}]}

    for stamp in ("2026-09-30 23:59:59", "not a time", "2026-10-02", None, 5):
        server.faults[:] = [Fault(200, {"results": []}), Fault(200, lead(stamp))]
        with pytest.raises(UpstreamBroken):
            run(collect(make(server, local_services=True), START, END))
    server.faults[:] = [Fault(200, {"results": []}), Fault(200, lead("2026-10-03 23:59:59"))]      # positive control
    leads = {p.observed_on.day: p.value_numeric for p in points_of(run(collect(make(server, local_services=True), START, END))) if p.metric_key == "ad_leads"}
    assert leads == {1: 0, 2: 0, 3: 1}


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("status,http,exc", [("UNAUTHENTICATED", 401, AuthExpired), ("PERMISSION_DENIED", 403, AuthRevoked),
                                             ("INVALID_ARGUMENT", 400, UpstreamBroken), ("NOT_FOUND", 404, UpstreamBroken)])
def test_a_status_that_retrying_cannot_fix_is_a_typed_error_at_once(server, status, http, exc):
    assert run(collect(make(server), START, END))                                # positive control
    server.faults.append(Fault(http, status_error(http, status, "scripted")))
    sleeps = Sleeps()
    with pytest.raises(exc):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


@pytest.mark.parametrize("status,http", [("RESOURCE_EXHAUSTED", 429), ("UNAVAILABLE", 503), ("INTERNAL", 500), ("DEADLINE_EXCEEDED", 504),
                                         ("UNKNOWN", 500), ("ABORTED", 409)])
def test_a_transient_status_is_retried_with_a_bound_and_then_succeeds(server, status, http):
    server.faults.append(Fault(http, status_error(http, status, "scripted")))
    sleeps = Sleeps()
    pages = run(collect(make(server, sleeps), START, END))
    assert as_set(points_of(pages)) == expected() and sleeps.calls == [1.0] and pages[0].throttle_waits == 1 and pages[0].api_calls == 2


def test_retries_stop_at_the_budget_with_a_rate_limit_for_the_runner(server):
    for _ in range(3):
        server.faults.append(Fault(429, status_error(429, "RESOURCE_EXHAUSTED", "scripted", [{"errorCode": {"quotaError": "RESOURCE_TEMPORARILY_EXHAUSTED"}}])))
    sleeps = Sleeps()
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0, 2.0] and len(server.requests) == 3


def test_a_retry_delay_in_the_error_details_is_used_and_a_long_one_is_handed_back(server):
    detail = lambda s: [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": s}]
    server.faults.append(Fault(429, status_error(429, "RESOURCE_EXHAUSTED", "x", detail("7s"))))
    sleeps = Sleeps()
    run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [7.0]
    server.faults.append(Fault(429, status_error(429, "RESOURCE_EXHAUSTED", "x", detail("500s"))))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as caught:
        run(collect(make(server, sleeps), START, END))
    assert caught.value.retry_after_seconds == 500.0 and sleeps.calls == []


def test_a_status_error_inside_a_200_is_read_as_an_error_and_not_as_an_empty_success(server):
    server.faults.append(Fault(200, status_error(401, "UNAUTHENTICATED", "expired")))
    with pytest.raises(AuthExpired):
        run(collect(make(server), START, END))


@pytest.mark.parametrize("body", [b"<html>502</html>", b"[]", b'"text"', b"5", b"null", b""])
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
    assert "wrong-SECRET" not in str(caught.value) and TOKEN not in str(caught.value) and "Bearer" not in str(caught.value)


def test_a_customer_the_token_cannot_read_is_a_revoked_credential(server):
    with pytest.raises(AuthRevoked):
        run(collect(make(server, customer_id="1111111111"), START, END))
    server.require_login = "9999999999"
    with pytest.raises(AuthRevoked):
        run(collect(make(server), START, END))                                    # a manager account's id was needed
    assert run(collect(make(server, login_customer_id="9999999999"), START, END))


def test_a_window_past_the_lookback_is_the_documented_date_range_error_and_is_not_retried(server):
    sleeps = Sleeps()
    with pytest.raises(UpstreamBroken):
        run(collect(make(server, sleeps), date(2020, 1, 1), date(2020, 1, 3)))
    assert sleeps.calls == []


# ---------------------------------------------------------------- shapes that must not become figures

def one_row(**row):
    base = {"segments": {"date": "2026-10-01"}, "customer": {"currencyCode": "EUR"},
            "metrics": {"costMicros": "1000000", "impressions": "10", "clicks": "1", "conversions": 1, "conversionsValue": 2}}
    for key, value in row.items():
        base[key] = value
    return {"results": [base]}


@pytest.mark.parametrize("mutate", [
    {"segments": {"date": "10/01/2026"}}, {"segments": {"date": "2026-02-30"}}, {"segments": {"date": "2026-09-30"}}, {"segments": {}},
    {"customer": {}}, {"customer": {"currencyCode": "eur"}}, {"customer": {"currencyCode": 5}},
    {"metrics": {"impressions": "10", "clicks": "1"}}, {"metrics": {"costMicros": "-5", "impressions": "10", "clicks": "1"}},
    {"metrics": {"costMicros": "1.5", "impressions": "10", "clicks": "1"}}, {"metrics": {"costMicros": "1000000", "impressions": "1.5", "clicks": "1"}},
    {"metrics": {"costMicros": "1000000", "impressions": "10", "clicks": "x"}}, {"metrics": {"costMicros": "1000000", "impressions": "10", "clicks": "1", "conversions": -1}},
    {"metrics": {"costMicros": True, "impressions": "10", "clicks": "1"}}, {"metrics": "nope"},
])
def test_a_row_that_is_not_what_the_pages_describe_is_refused_after_a_positive_control(server, mutate):
    import json
    server.faults.append(Fault(200, json.dumps(one_row()).encode()))
    assert run(collect(make(server), START, END))                                    # positive control
    server.faults.append(Fault(200, json.dumps(one_row(**mutate)).encode()))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_two_rows_for_one_day_and_two_currencies_are_refused(server):
    import json
    a, b = one_row()["results"][0], one_row()["results"][0]
    server.faults.append(Fault(200, json.dumps({"results": [a, b]}).encode()))
    with pytest.raises(UpstreamBroken, match="two rows"):
        run(collect(make(server), START, END))
    c = one_row()["results"][0]
    c["segments"] = {"date": "2026-10-02"}
    c["customer"] = {"currencyCode": "USD"}
    server.faults.append(Fault(200, json.dumps({"results": [a, c]}).encode()))
    with pytest.raises(UpstreamBroken, match="two currencies"):
        run(collect(make(server), START, END))


def test_a_money_json_number_is_read_exactly_and_not_through_a_float(server):
    import json
    row = one_row()["results"][0]
    row["metrics"]["conversionsValue"] = Decimal("0.1") + Decimal("0.2")
    text = json.dumps({"results": [row]}, default=lambda d: float(d))
    server.faults.append(Fault(200, text.encode()))
    got = as_set(points_of(run(collect(make(server), START, END))))
    assert ("ad_conversion_value", date(2026, 10, 1), 30, "EUR") in got


# ---------------------------------------------------------------- the account, the registry, construction

def test_verify_reads_status_currency_and_time_zone_and_refuses_a_disabled_account(server):
    conn = make(server)
    label = run(_verify(conn))
    assert label == "Google Ads customer 1234567890 (EUR, America/New_York)" and conn.currency == "EUR" and conn.time_zone == "America/New_York"
    server.set_customer(CUSTOMER, THREE_DAYS, currency="EUR", status="SUSPENDED")
    with pytest.raises(UpstreamBroken, match="not enabled"):
        run(_verify(make(server)))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_the_customer_id_is_validated_before_any_request():
    for bad in ("123", "12345678901", "abc", "", "123-456-789"):
        with pytest.raises(ValueError):
            GoogleAdsConnector(customer_id=bad, access_token="t")
    assert normalise_customer_id("123-456-7890") == "1234567890" == GoogleAdsConnector(customer_id=" 123-456-7890 ", access_token="t").external_account
    with pytest.raises(ValueError):
        GoogleAdsConnector(customer_id=CUSTOMER)
    with pytest.raises(ValueError):
        GoogleAdsConnector(customer_id=CUSTOMER, access_token="t", token_provider=lambda: "t")


def test_a_window_with_no_days_is_refused_and_the_token_never_shows_in_a_repr():
    conn = GoogleAdsConnector(customer_id=CUSTOMER, access_token=TOKEN)
    assert TOKEN not in repr(conn) and "Secret" not in repr(conn)
    with pytest.raises(ValueError):
        run(collect(conn, START, START))


def test_from_env_reads_the_documented_variables_and_names_what_is_missing():
    conn = GoogleAdsConnector.from_env({"GOOGLE_ADS_CUSTOMER_ID": CUSTOMER, "GOOGLE_ADS_ACCESS_TOKEN": "t", "GOOGLE_ADS_API_BASE_URL": "http://x",
                                        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "9876543210", "GOOGLE_ADS_LOCAL_SERVICES": "1"})
    assert conn.local_services and conn.login_customer_id == "9876543210" and conn._base == "http://x"
    with pytest.raises(ValueError, match="GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_ACCESS_TOKEN"):
        GoogleAdsConnector.from_env({})


def test_a_token_provider_is_asked_for_a_fresh_token_each_call(server):
    tokens = iter(["old", TOKEN])
    conn = make(server, access_token=None, token_provider=lambda: next(tokens))
    server.faults.append(Fault(503, status_error(503, "UNAVAILABLE", "x")))
    assert run(collect(conn, START, END)) and server.requests[0].headers["authorization"] == "Bearer old" \
        and server.requests[1].headers["authorization"] == "Bearer " + TOKEN


def test_the_registry_describes_the_platform_and_redacts_the_token():
    spec = registry.get("google_ads")
    assert spec.supported_metrics == GoogleAdsConnector.supported_metrics and registry.secret_keys("google_ads") == frozenset({"access_token"})
    assert registry.redact("google_ads", {"customer_id": "1", "access_token": "x"}) == {"customer_id": "1", "access_token": "***"}
    assert {"google_ads", "meta", "tiktok"} == set(registry.platforms_supporting("ad_spend"))
    assert registry.platforms_supporting("ad_leads") == ["google_ads"] and spec.max_history_days == 37 * 30
    with pytest.raises(ValueError, match="missing credentials"):
        registry.build("google_ads", customer_id="1")
    built = registry.build("google_ads", customer_id=CUSTOMER, access_token="t")
    assert isinstance(built, GoogleAdsConnector)


# ---------------------------------------------------------------- the stand-in refuses what the pages say Google refuses

def post(server, query, **extra):
    return httpx.post("%s/v26/customers/%s/googleAds:search" % (server.url, CUSTOMER), json={"query": query, **extra},
                      headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False)


GOOD = "SELECT segments.date, metrics.clicks FROM customer WHERE segments.date >= '2026-10-01' AND segments.date <= '2026-10-03'"


@pytest.mark.parametrize("query,reason", [
    ("SELECT segments.date, metrics.clicks FROM customer", "CANNOT_SELECT_WITHOUT_DATE_RANGE"),
    ("SELECT segments.date, metrics.clicks FROM customer WHERE segments.date >= '2026-10-01'", "CANNOT_SELECT_WITHOUT_DATE_RANGE"),
    ("SELECT segments.date, metrics.bogus FROM customer WHERE segments.date >= '2026-10-01' AND segments.date <= '2026-10-03'", "UNRECOGNIZED_FIELD"),
    ("SELECT segments.date, metrics.clicks FROM galaxy WHERE segments.date >= '2026-10-01' AND segments.date <= '2026-10-03'", "INVALID_FROM_CLAUSE"),
    ("SELECT segments.date, metrics.clicks FROM customer WHERE segments.date >= '2020-01-01' AND segments.date <= '2020-01-03'", "REQUESTED_DATE_GRANULARITY_NOT_SUPPORTED"),
    ("nonsense", "UNEXPECTED_END_OF_QUERY"),
])
def test_the_stand_in_refuses_the_queries_the_pages_say_are_invalid(server, query, reason):
    assert post(server, GOOD).status_code == 200                                  # positive control
    response = post(server, query)
    assert response.status_code == 400 and reason in response.text and response.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_the_stand_in_answers_in_camel_case_with_int64_as_strings_and_doubles_as_numbers(server):
    body = post(server, "SELECT segments.date, metrics.clicks, metrics.conversions, customer.currency_code FROM customer "
                        "WHERE segments.date >= '2026-10-01' AND segments.date <= '2026-10-01'").json()
    row = body["results"][0]
    assert row == {"customer": {"currencyCode": "EUR"}, "metrics": {"clicks": "20", "conversions": 1.5}, "segments": {"date": "2026-10-01"}}
    assert body["fieldMask"] == "segments.date,metrics.clicks,metrics.conversions,customer.currencyCode"


def test_the_stand_in_rejects_a_page_token_it_did_not_issue_a_bad_token_and_a_bad_path(server):
    assert post(server, GOOD, pageToken="garbage").status_code == 400
    assert httpx.post(server.url + "/v26/customers/%s/googleAds:search" % CUSTOMER, json={"query": GOOD}, timeout=5, trust_env=False).status_code == 401
    assert httpx.post(server.url + "/v25/customers/%s/googleAds:search" % CUSTOMER, json={"query": GOOD},
                      headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False).status_code == 404
    assert httpx.post(server.url + "/v26/customers/%s/googleAds:search" % CUSTOMER, content=b"{",
                      headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False).status_code == 400
