"""The Meta connector against the stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the path, the account
level, one row a day, the fields, the attribution window named in the request, the inclusive time range), how
it reads the documented response (numeric strings, `date_start` and `date_stop`, the action lists, cursor
paging), the money and currency rules, the error taxonomy by error code, and that retries are bounded. They do
not prove Meta accepts the request: the server is written from the saved pages (docs/META_SOURCES.md), no live
ad account was used, so nothing here is a live integration.

The classes of fault the GA4 connector's adversary and blind passes found are hunted here in this connector's
own terms: an error object inside a 200, an HTML page, a list for an object, an expired token, an empty report,
a day that is not finished, currency, a number that arrives as the wrong type, paging that repeats a cursor or
a day, a duplicate day. Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.meta import ATTRIBUTION_WINDOW, FIELDS, MetaConnector, read_usage  # noqa: E402
from app.connectors.meta_fake import Fault, FakeMeta, error_body  # noqa: E402
from app.connectors.registry import registry  # noqa: E402

ACCOUNT = "555001"
TOKEN = "tok-SECRET-meta-123"
START, END = date(2026, 10, 1), date(2026, 10, 4)          # half open: the 1st, 2nd and 3rd


def day(n: int, **over):
    base = {"spend": "%d.50" % (10 + n), "impressions": 1000 * n, "clicks": 20 * n, "purchases": n, "purchase_value": "%d.25" % (50 * n)}
    base.update(over)
    return base


THREE_DAYS = {date(2026, 10, 1): day(1), date(2026, 10, 2): day(2), date(2026, 10, 3): day(3)}


@pytest.fixture()
def server():
    with FakeMeta(token=TOKEN) as fake:
        fake.set_account(ACCOUNT, THREE_DAYS, currency="EUR")
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
    client = httpx.AsyncClient(trust_env=False, verify=False, timeout=10)
    if "token_provider" not in kw:
        kw.setdefault("access_token", TOKEN)
    return MetaConnector(ad_account_id=kw.pop("ad_account_id", ACCOUNT), base_url=server.url, client=client, **kw)


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
    return {(p.metric_key, p.observed_on, p.value_numeric, p.currency) for p in points}


def minor(text: str, exponent: int = 2) -> int:
    return int((Decimal(text) * 10 ** exponent).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def expected_points(days, currency="EUR", start=START, end=END):
    out = set()
    exp = 0 if currency == "JPY" else 2
    for d, v in days.items():
        if not start <= d < end or not (v["impressions"] or Decimal(v["spend"])):
            continue
        out |= {("ad_spend", d, minor(v["spend"], exp), currency), ("ad_impressions", d, v["impressions"], None),
                ("ad_clicks", d, v["clicks"], None)}
        if v.get("purchases") is not None:
            out.add(("ad_purchases", d, v["purchases"], None))
        if v.get("purchase_value") is not None:
            out.add(("ad_purchase_value", d, minor(v["purchase_value"], exp), currency))
    return out


# ---------------------------------------------------------------- the happy path

def test_every_day_and_metric_arrives_as_an_integer_with_the_ad_accounts_currency_on_money_only(server):
    pages = run(collect(make(server)))
    assert as_set(points_of(pages)) == expected_points(THREE_DAYS)
    assert all(type(p.value_numeric) is int for p in points_of(pages))
    money = {p.metric_key for p in points_of(pages) if p.currency}
    assert money == {"ad_spend", "ad_purchase_value"}
    assert {p.metric_key for p in points_of(pages)} == {"ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value"}
    assert len(pages) == 1 and pages[0].cursor is None and pages[0].api_calls == 1


def test_the_request_is_the_documented_one(server):
    run(collect(make(server)))
    path, query, raw = server.requests[-1]
    assert path == "/v26.0/act_%s/insights" % ACCOUNT
    assert query["access_token"] == TOKEN
    assert query["level"] == "account" and query["time_increment"] == "1"
    # half open [Oct 1, Oct 4) becomes the inclusive range Oct 1 to Oct 3
    assert json.loads(query["time_range"]) == {"since": "2026-10-01", "until": "2026-10-03"}
    assert query["fields"].split(",") == list(FIELDS)
    assert json.loads(query["action_attribution_windows"]) == [ATTRIBUTION_WINDOW] == ["7d_click"], \
        "the window is named in the request, never left to Meta's default"
    assert query["limit"] == "500" and "after" not in query


def test_an_account_id_with_or_without_the_act_prefix_is_the_same_account(server):
    assert as_set(points_of(run(collect(make(server, ad_account_id="act_" + ACCOUNT))))) == expected_points(THREE_DAYS)
    assert server.requests[-1][0] == "/v26.0/act_%s/insights" % ACCOUNT


@pytest.mark.parametrize("bad", ["abc", "12 3", "", "act_", "act_12a", "1" * 21, "../1", "123/insights"])
def test_a_malformed_account_id_is_refused_before_any_request(server, bad):
    with pytest.raises(ValueError):
        make(server, ad_account_id=bad)
    assert server.requests == []


@pytest.mark.parametrize("kw", [{"access_token": None}, {"access_token": "a", "token_provider": lambda: "b"},
                                {"page_size": 0}, {"max_attempts": 0}])
def test_constructor_arguments_that_cannot_work_are_refused(server, kw):
    base = {"ad_account_id": ACCOUNT, "base_url": server.url}
    if "access_token" not in kw and "token_provider" not in kw:
        base["access_token"] = TOKEN
    with pytest.raises(ValueError):
        MetaConnector(**base, **kw)


# ---------------------------------------------------------------- money and currency

@pytest.mark.parametrize("spend,currency,expected", [
    ("12.34", "USD", 1234), ("12.345", "USD", 1235), ("12.344", "USD", 1234), ("0.005", "USD", 1), ("0.004", "USD", 0),
    ("5339.5", "EUR", 533950), ("1000", "JPY", 1000), ("1000.5", "JPY", 1001), ("0", "USD", 0),
    ("99999999.99", "USD", 9999999999),
])
def test_money_is_parsed_with_decimal_into_minor_units_rounded_half_up(server, spend, currency, expected):
    server.set_account(ACCOUNT, {date(2026, 10, 1): day(1, spend=spend)}, currency=currency)
    points = points_of(run(collect(make(server))))
    got = next(p for p in points if p.metric_key == "ad_spend")
    assert got.value_numeric == expected and got.currency == currency and type(got.value_numeric) is int


def test_a_float_style_amount_is_never_how_the_sum_is_made(server):
    """19.99 times 100 as a float is 1998.9999999999998, which a naive int() makes 1998."""
    server.set_account(ACCOUNT, {date(2026, 10, 1): day(1, spend="19.99", purchase_value="1.15")}, currency="USD")
    got = {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got["ad_spend"] == 1999 and got["ad_purchase_value"] == 115


@pytest.mark.parametrize("value", [-1, "-0.01", "abc", "", "1,5", "NaN", "Infinity", None, [], {}, True, 12.5])
def test_an_amount_that_is_not_a_non_negative_numeric_string_is_a_broken_response(server, value):
    assert run(collect(make(server)))                                  # positive control
    row = {"account_currency": "EUR", "spend": value, "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.append(Fault(200, {"data": [row]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("currency", [None, "", "usd", "US", "USDX", 5, ["USD"]])
def test_a_row_without_a_real_currency_is_refused_never_given_a_default(server, currency):
    row = {"account_currency": currency, "spend": "1.00", "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    if currency is None:
        del row["account_currency"]
    server.faults.append(Fault(200, {"data": [row]}))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "currency" in str(exc.value)


def test_two_currencies_in_one_window_are_refused_even_across_pages(server):
    rows = [{"account_currency": c, "spend": "1.00", "impressions": "10", "clicks": "1",
             "date_start": "2026-10-0%d" % n, "date_stop": "2026-10-0%d" % n} for n, c in ((1, "USD"), (2, "EUR"))]
    server.faults.append(Fault(200, {"data": rows}))
    with pytest.raises(UpstreamBroken) as same_page:
        run(collect(make(server)))
    assert "two currencies" in str(same_page.value)
    page_one = {"data": [rows[0]], "paging": {"cursors": {"after": "AAA"}, "next": "x"}}
    server.faults.extend([Fault(200, page_one), Fault(200, {"data": [rows[1]]})])
    with pytest.raises(UpstreamBroken) as across:
        run(collect(make(server)))
    assert "two currencies" in str(across.value)


def test_a_zero_decimal_currency_is_not_scaled(server):
    server.set_account(ACCOUNT, {date(2026, 10, 1): day(1, spend="1500", purchase_value="9000")}, currency="JPY")
    got = {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got["ad_spend"] == 1500 and got["ad_purchase_value"] == 9000


# ---------------------------------------------------------------- what a purchase is

def test_purchases_are_the_omni_total_only_never_the_sum_of_its_parts(server):
    """The stand-in reports three purchase action types for each purchase, as Meta does; adding them triples the count."""
    got = {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got[("ad_purchases", 2)] == 2 and got[("ad_purchases", 3)] == 3
    assert got[("ad_purchase_value", 2)] == minor("100.25")


def test_the_number_read_is_the_window_the_request_named_not_the_default_windows_value(server):
    server.default_value_skew = 7
    got = {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got[("ad_purchases", 1)] == 1, "value is the default window, 7d_click is what was asked for"
    assert got[("ad_purchase_value", 1)] == minor("50.25")


def test_a_day_with_no_actions_list_has_no_purchase_figure_not_a_zero(server):
    server.set_account(ACCOUNT, {date(2026, 10, 1): day(1, purchases=None, purchase_value=None)}, currency="EUR")
    keys = {p.metric_key for p in points_of(run(collect(make(server))))}
    assert keys == {"ad_spend", "ad_impressions", "ad_clicks"}


def test_actions_without_a_purchase_entry_are_a_real_zero(server):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
           "actions": [{"action_type": "link_click", "value": "1", "7d_click": "1"}],
           "action_values": [{"action_type": "other", "value": "1", "7d_click": "1"}],
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.append(Fault(200, {"data": [row]}))
    got = {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got["ad_purchases"] == 0 and got["ad_purchase_value"] == 0


@pytest.mark.parametrize("actions", [
    [{"action_type": "omni_purchase", "value": "1", "7d_click": "1"}, {"action_type": "omni_purchase", "value": "2", "7d_click": "2"}],
    [{"action_type": "omni_purchase", "value": "1"}],                      # no number for the window asked for
    [{"action_type": "omni_purchase", "7d_click": "1.5"}],                  # half a purchase
    [{"action_type": "omni_purchase", "7d_click": "-1"}],
    [{"action_type": "omni_purchase", "7d_click": 3}],                      # accepted: a whole JSON number
    "not a list", {"action_type": "omni_purchase"},
])
def test_an_action_list_of_the_wrong_shape_is_a_broken_response(server, actions):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1", "actions": actions,
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.append(Fault(200, {"data": [row]}))
    if actions == [{"action_type": "omni_purchase", "7d_click": 3}]:
        assert {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server))))}["ad_purchases"] == 3
        return
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


# ---------------------------------------------------------------- the shape of a row

@pytest.mark.parametrize("field,value", [
    ("impressions", 12.5), ("impressions", "12.5"), ("impressions", "-3"), ("impressions", "abc"), ("impressions", None),
    ("impressions", [1]), ("clicks", True), ("clicks", "1e999999"), ("clicks", {}), ("clicks", "NaN"),
])
def test_a_count_that_is_not_a_whole_non_negative_number_is_a_broken_response(server, field, value):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    row[field] = value
    server.faults.append(Fault(200, {"data": [row]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_counts_may_arrive_as_strings_or_whole_json_numbers(server):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": 10, "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.append(Fault(200, {"data": [row]}))
    got = {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server))))}
    assert got["ad_impressions"] == 10 and got["ad_clicks"] == 1


@pytest.mark.parametrize("row_update", [
    {"date_start": "2026-10-01", "date_stop": "2026-10-02"},   # a row that spans two days
    {"date_start": "20261001", "date_stop": "20261001"},
    {"date_start": "2026-02-30", "date_stop": "2026-02-30"},
    {"date_start": "2026-09-30", "date_stop": "2026-09-30"},   # before the window
    {"date_start": "2026-10-04", "date_stop": "2026-10-04"},   # the first day after it: the end is exclusive
    {"date_start": None, "date_stop": None},
    {"date_start": 20261001, "date_stop": 20261001},
])
def test_a_row_whose_day_is_not_one_day_inside_the_window_is_a_broken_response(server, row_update):
    assert run(collect(make(server)))                                  # positive control
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1"} | row_update
    server.faults.append(Fault(200, {"data": [row]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_two_rows_for_one_day_are_refused_not_written_as_one(server):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.append(Fault(200, {"data": [row, dict(row, spend="2.00")]}))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "two rows for 2026-10-01" in str(exc.value)


def test_the_same_day_on_two_pages_is_refused_too(server):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    server.faults.extend([Fault(200, {"data": [row], "paging": {"cursors": {"after": "A"}, "next": "x"}}),
                          Fault(200, {"data": [row]})])
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "two rows" in str(exc.value)


@pytest.mark.parametrize("key", ["spend", "impressions", "clicks"])
def test_a_row_missing_a_required_field_is_a_broken_response(server, key):
    row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
           "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    del row[key]
    server.faults.append(Fault(200, {"data": [row]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("body", [{"data": "no"}, {"data": {}}, {"data": None}, {}, {"data": [1]}, {"data": ["x"]}, {"data": [[]]}])
def test_a_data_member_that_is_not_a_list_of_objects_is_a_broken_response(server, body):
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_an_empty_report_is_an_empty_run_not_a_failure(server):
    server.set_account(ACCOUNT, {}, currency="EUR")
    pages = run(collect(make(server)))
    assert len(pages) == 1 and pages[0].points == [] and pages[0].cursor is None


def test_a_day_with_no_delivery_has_no_row_and_so_no_points(server):
    server.set_account(ACCOUNT, {date(2026, 10, 2): day(2, spend="0", impressions=0, clicks=0)}, currency="EUR")
    assert points_of(run(collect(make(server)))) == []


# ---------------------------------------------------------------- paging

def test_paging_follows_the_after_cursor_until_next_is_gone(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_account(ACCOUNT, days, currency="EUR")
    pages = run(collect(make(server, page_size=2), end=date(2026, 10, 6)))
    assert [r[1]["limit"] for r in server.requests] == ["2", "2", "2"]
    assert "after" not in server.requests[0][1]
    assert server.requests[1][1]["after"] == pages[0].cursor and server.requests[2][1]["after"] == pages[1].cursor
    assert pages[-1].cursor is None and all(p.cursor for p in pages[:-1])
    assert as_set(points_of(pages)) == expected_points(days, end=date(2026, 10, 6))
    assert sum(p.api_calls for p in pages) == 3


def test_a_run_can_resume_from_a_cursor_without_repeating_the_first_page(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_account(ACCOUNT, days, currency="EUR")
    first = run(collect(make(server, page_size=2), end=date(2026, 10, 6)))
    server.requests.clear()
    rest = run(collect(make(server, page_size=2), end=date(2026, 10, 6), cursor=first[0].cursor))
    assert {p.observed_on.day for p in points_of(rest)} == {3, 4, 5}
    assert server.requests[0][1]["after"] == first[0].cursor


def test_a_cursor_the_server_did_not_issue_is_a_broken_response_with_the_documented_code(server):
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server), cursor="not-a-cursor"))
    assert "2642" in str(exc.value) and not exc.value.retryable


def test_a_next_link_is_never_requested_so_a_hostile_address_gets_no_token(server):
    """A Graph `next` link is a full URL that carries the access token. Only its presence is read."""
    days = {date(2026, 10, d): day(d) for d in range(1, 4)}
    server.set_account(ACCOUNT, days, currency="EUR")
    with FakeMeta(token=TOKEN) as hostile:
        server.next_base = hostile.url
        pages = run(collect(make(server, page_size=1)))
        assert as_set(points_of(pages)) == expected_points(days)
        assert hostile.requests == [], "the connector followed a next link to another host"
    assert len(server.requests) == 3
    assert all(r[0].startswith("/v26.0/act_%s/insights" % ACCOUNT) for r in server.requests)


def test_an_empty_page_that_still_has_a_next_link_is_read_on_to_the_end(server):
    server.empty_pages_with_next = 2
    pages = run(collect(make(server, page_size=10)))
    assert as_set(points_of(pages)) == expected_points(THREE_DAYS)
    assert len(pages) == 3 and [len(p.points) for p in pages][:2] == [0, 0]


def test_a_response_whose_cursor_never_advances_is_refused_not_looped_on(server):
    """Each page carries different days but the same `after` cursor again: following it would loop for ever."""
    def page(n):
        row = {"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
               "date_start": "2026-10-0%d" % n, "date_stop": "2026-10-0%d" % n}
        return {"data": [row], "paging": {"cursors": {"after": "SAME"}, "next": "x"}}
    server.faults.extend([Fault(200, page(1)), Fault(200, page(2)), Fault(200, page(3))])
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server, page_size=1)))
    assert "does not advance" in str(exc.value)
    assert len(server.requests) == 2, "paging that repeats must stop at once, it spends the rate limit"


@pytest.mark.parametrize("paging", [{"next": "http://x/y"}, {"next": "x", "cursors": {}}, {"next": "x", "cursors": {"after": ""}},
                                    {"next": "x", "cursors": {"after": 5}}, {"next": "x", "cursors": "no"}])
def test_a_next_page_without_a_usable_after_cursor_is_a_broken_response(server, paging):
    server.faults.append(Fault(200, {"data": [], "paging": paging}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("paging", [None, [], "x", {}, {"cursors": {"after": "A"}}])
def test_paging_without_a_next_link_means_the_last_page(server, paging):
    body = {"data": []}
    if paging is not None:
        body["paging"] = paging
    server.faults.append(Fault(200, body))
    pages = run(collect(make(server)))
    assert len(pages) == 1 and pages[0].cursor is None


# ---------------------------------------------------------------- the error taxonomy

def error_in(code, subcode=None, message="m"):
    return error_body(code, "OAuthException", message, subcode)


EXPIRED = [error_in(190), error_in(190, 460), error_in(102)]
REVOKED = [error_in(10), error_in(200), error_in(299), error_in(3)]
THROTTLED = [error_in(4), error_in(17), error_in(32), error_in(341), error_in(613), error_in(613, 1996), error_in(1), error_in(2)]
BROKEN = [error_in(100), error_in(100, 1487534), error_in(2642), error_in(3018), error_in(2500), error_in(105), error_in(3001),
          error_in(9999), error_in(300), error_in(0)]


@pytest.mark.parametrize("status", [200, 400, 401, 403, 500])
@pytest.mark.parametrize("body,expected", [(b, AuthExpired) for b in EXPIRED] + [(b, AuthRevoked) for b in REVOKED]
                         + [(b, UpstreamBroken) for b in BROKEN])
def test_the_error_code_decides_the_typed_error_whatever_the_http_status_or_if_the_body_is_in_a_200(server, status, body, expected):
    assert run(collect(make(server)))                                  # positive control
    server.requests.clear()
    server.faults.append(Fault(status, body))
    sleeps = Sleeps()
    with pytest.raises(expected) as exc:
        run(collect(make(server, sleeps)))
    assert type(exc.value) is expected
    assert len(server.requests) == 1 and sleeps.calls == [], "a failure retrying cannot fix is not retried"


@pytest.mark.parametrize("status", [200, 400, 403, 500])
@pytest.mark.parametrize("body", THROTTLED, ids=lambda b: "code%s" % b["error"]["code"])
def test_a_throttle_or_temporary_error_is_retried_a_bounded_number_of_times_then_rate_limited(server, status, body):
    server.faults.extend([Fault(status, body)] * 10)
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps, max_attempts=3)))
    assert exc.value.retryable and len(server.requests) == 3 and len(sleeps.calls) == 2
    # and one such failure followed by success is absorbed
    server.faults.clear()
    server.requests.clear()
    server.faults.append(Fault(status, body))
    assert as_set(points_of(run(collect(make(server, Sleeps()))))) == expected_points(THREE_DAYS)


@pytest.mark.parametrize("status,body", [
    (401, b""), (401, "<html>unauthorized</html>"), (401, []), (403, b""), (403, "<html>"), (403, [1]),
])
def test_a_refusal_with_no_error_object_is_still_typed_by_its_status(server, status, body):
    server.faults.append(Fault(status, body))
    with pytest.raises(AuthExpired if status == 401 else AuthRevoked):
        run(collect(make(server)))


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.parametrize("body", [b"", b"<html><body>Bad gateway</body></html>", [1, 2], "plain text"])
def test_a_5xx_or_429_with_a_page_a_list_or_nothing_is_retried_then_rate_limited_never_a_crash(server, status, body):
    server.faults.extend([Fault(status, body)] * 5)
    with pytest.raises(RateLimited):
        run(collect(make(server, Sleeps())))


@pytest.mark.parametrize("body", [b"<html>ok</html>", b"", b"not json", [], [1], "text", 5, None])
def test_a_200_that_is_not_a_json_object_is_a_broken_response(server, body):
    server.faults.append(Fault(200, body if body is not None else b"null"))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("err", ["boom", None, 7, ["x"]])
def test_an_error_member_that_is_not_an_object_is_a_refusal_not_a_crash(server, err):
    server.faults.append(Fault(200, {"error": err}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_a_wrong_token_is_auth_expired_and_a_token_provider_is_asked_each_call(server):
    with pytest.raises(AuthExpired) as exc:
        run(collect(make(server, access_token="wrong")))
    assert exc.value.retryable and exc.value.code == "auth_expired"
    tokens = iter(["stale", TOKEN])
    with pytest.raises(AuthExpired):
        run(collect(make(server, token_provider=lambda: next(tokens))))
    assert as_set(points_of(run(collect(make(server, token_provider=lambda: TOKEN))))) == expected_points(THREE_DAYS)
    with pytest.raises(AuthExpired):
        run(collect(make(server, token_provider=lambda: "")))


def test_an_unknown_account_is_a_permissions_error_and_names_the_account_not_the_token(server):
    with pytest.raises(AuthRevoked) as exc:
        run(collect(make(server, ad_account_id="999")))
    assert "999" in str(exc.value) and TOKEN not in str(exc.value)


def test_the_error_text_is_cut_short_and_keeps_the_code(server):
    server.faults.append(Fault(400, error_in(100, 33, "x" * 800)))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "code 100 subcode 33" in str(exc.value) and len(str(exc.value)) < 330


def test_a_dropped_connection_is_retried_and_the_message_carries_the_class_only(server):
    server.faults.extend([Fault(0, drop=True)] * 3)
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, Sleeps(), max_attempts=3)))
    text = str(exc.value)
    assert "transport failure" in text and TOKEN not in text and "127.0.0.1" not in text and "access_token" not in text


def test_the_token_never_appears_in_a_message_a_repr_or_a_connector_attribute_dump(server):
    conn = make(server)
    assert TOKEN not in repr(conn) and TOKEN not in str(conn)
    for faults, expected in (([Fault(400, error_in(190))], AuthExpired), ([Fault(400, error_in(100))], UpstreamBroken),
                             ([Fault(500)] * 3, RateLimited)):
        server.faults.extend(faults)
        with pytest.raises(expected) as exc:
            run(collect(make(server, Sleeps())))
        assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)
    assert TOKEN not in repr(vars(conn).get("_token"))


# ---------------------------------------------------------------- bounded retries and the rate limit headers

def test_a_throttle_with_the_business_use_case_wait_sleeps_that_many_minutes_when_it_is_short_enough(server):
    server.regain_minutes = 0.25                       # 15 seconds, under the 30 second cap
    server.faults.append(Fault(400, error_in(613), server._headers() | {"x-business-use-case-usage": json.dumps(
        {"1": [{"type": "ads_insights", "call_count": 100, "estimated_time_to_regain_access": 0.25}]})}))
    sleeps = Sleeps()
    pages = run(collect(make(server, sleeps)))
    assert sleeps.calls == [15.0] and as_set(points_of(pages)) == expected_points(THREE_DAYS)


def test_a_wait_longer_than_the_cap_is_handed_to_the_runner_not_slept(server):
    server.faults.append(Fault(400, error_in(4), {"x-business-use-case-usage": json.dumps(
        {"1": [{"type": "ads_insights", "call_count": 100, "estimated_time_to_regain_access": 19}]})}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps)))
    assert exc.value.retry_after_seconds == 19 * 60 and sleeps.calls == [] and len(server.requests) == 1


def test_a_numeric_retry_after_is_honoured_and_a_garbage_one_is_ignored(server):
    server.faults.append(Fault(500, None, {"Retry-After": "3"}))
    sleeps = Sleeps()
    run(collect(make(server, sleeps)))
    assert sleeps.calls == [3.0]
    server.faults.append(Fault(500, None, {"Retry-After": "soon"}))
    sleeps2 = Sleeps()
    run(collect(make(server, sleeps2)))
    assert sleeps2.calls == [1.0]


def test_the_backoff_is_capped_and_jittered_between_half_and_all_of_the_delay(server):
    server.faults.extend([Fault(500)] * 4)
    sleeps = Sleeps()
    run(collect(make(server, sleeps, max_attempts=5, backoff_base=10.0, backoff_cap=30.0, rng=lambda: 0.0)))
    assert sleeps.calls == [5.0, 10.0, 15.0, 15.0]


def test_a_page_is_not_requested_when_the_headers_say_usage_is_at_the_ceiling_and_the_first_pages_are_kept(server):
    days = {date(2026, 10, d): day(d) for d in range(1, 6)}
    server.set_account(ACCOUNT, days, currency="EUR")
    server.utilisation = 95
    conn = make(server, page_size=2)
    got = []

    async def go():
        try:
            async for page in conn.fetch(window_start=START, window_end=date(2026, 10, 6)):
                got.append(page)
        finally:
            await conn._client.aclose()

    with pytest.raises(RateLimited):
        run(go())
    assert len(got) == 1 and len(server.requests) == 1 and got[0].cursor, "the cursor of the kept page is what a resume starts from"


def test_usage_below_the_ceiling_does_not_stop_paging(server):
    server.set_account(ACCOUNT, {date(2026, 10, d): day(d) for d in range(1, 4)}, currency="EUR")
    server.utilisation = 89
    assert len(run(collect(make(server, page_size=1)))) == 3


class _Resp:
    def __init__(self, headers):
        self.headers = httpx.Headers(headers)


@pytest.mark.parametrize("headers,level,minutes", [
    ({"x-fb-ads-insights-throttle": '{"app_id_util_pct": 40, "acc_id_util_pct": 10.5}'}, 40.0, None),
    ({"X-Ad-Account-Usage": '{"acc_id_util_pct": 9.67}'}, 9.67, None),
    ({"x-app-usage": '{"call_count": 28, "total_time": 25, "total_cputime": 91}'}, 91.0, None),
    ({"x-business-use-case-usage": '{"1": [{"call_count": 30, "total_time": 70, "estimated_time_to_regain_access": 19}]}'}, 70.0, 19.0),
    ({"x-business-use-case-usage": '{"1": [{"call_count": 30}], "2": [{"total_cputime": 80, "estimated_time_to_regain_access": 3}]}'}, 80.0, 3.0),
    ({"x-fb-ads-insights-throttle": "not json"}, None, None),
    ({"x-fb-ads-insights-throttle": "[1,2]"}, None, None),
    ({"x-fb-ads-insights-throttle": '{"app_id_util_pct": "high", "acc_id_util_pct": null, "call_count": true}'}, None, None),
    ({"x-business-use-case-usage": '{"1": "no", "2": [1, {"call_count": -5}], "3": [{"call_count": 5000}]}'}, None, None),
    ({"x-business-use-case-usage": '[1]'}, None, None),
    ({}, None, None),
])
def test_usage_headers_of_any_shape_are_read_or_ignored_never_a_crash(headers, level, minutes):
    got_level, got_minutes = read_usage(_Resp(headers))
    assert got_level == level and got_minutes == minutes


# ---------------------------------------------------------------- verify

async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_verify_reads_the_account_and_names_its_currency_and_time_zone(server):
    conn = make(server)
    assert run(_verify(conn)) == "Meta ad account %s (EUR, America/Los_Angeles)" % ACCOUNT
    assert conn.currency == "EUR" and conn.time_zone == "America/Los_Angeles"
    path, query, _ = server.requests[-1]
    assert path == "/v26.0/act_%s" % ACCOUNT and query["fields"] == "account_status,currency,timezone_name"


def test_verify_refuses_an_account_that_is_not_active_and_a_wrong_token(server):
    assert run(_verify(make(server)))                                  # positive control
    server.set_account(ACCOUNT, THREE_DAYS, currency="EUR", status=2)
    with pytest.raises(UpstreamBroken) as exc:
        run(_verify(make(server)))
    assert "not active" in str(exc.value)
    with pytest.raises(AuthExpired):
        run(_verify(make(server, access_token="not-the-token")))
    for body in ({"account_status": "1"}, {"account_status": True}, {}):
        server.faults.append(Fault(200, body))
        with pytest.raises(UpstreamBroken):
            run(_verify(make(server)))


def test_verify_survives_a_response_with_no_currency_or_time_zone(server):
    server.faults.append(Fault(200, {"account_status": 1, "currency": 5, "timezone_name": None}))
    assert run(_verify(make(server))) == "Meta ad account %s" % ACCOUNT


def test_an_empty_or_backwards_window_is_a_caller_error(server):
    for start, end in ((START, START), (END, START)):
        with pytest.raises(ValueError):
            run(collect(make(server), start=start, end=end))
    assert server.requests == []


def test_from_env_builds_from_the_environment_and_names_what_is_missing():
    conn = MetaConnector.from_env({"META_AD_ACCOUNT_ID": "act_77", "META_ACCESS_TOKEN": "t", "META_API_BASE_URL": "http://x/"})
    assert conn.account_id == "77" and conn._base == "http://x"
    with pytest.raises(ValueError) as exc:
        MetaConnector.from_env({"META_AD_ACCOUNT_ID": "1"})
    assert "META_ACCESS_TOKEN" in str(exc.value) and "META_AD_ACCOUNT_ID" not in str(exc.value)


# ---------------------------------------------------------------- the registry

def test_meta_is_registered_with_its_credentials_and_its_metrics():
    spec = registry.get("meta")
    assert spec.supported_metrics == frozenset({"ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value"})
    assert registry.secret_keys("meta") == frozenset({"access_token"})
    assert registry.platforms_supporting("ad_spend") == ["meta"] and registry.platforms_supporting("shipping") == []
    assert registry.redact("meta", {"ad_account_id": "1", "access_token": "x"}) == {"ad_account_id": "1", "access_token": "***"}
    with pytest.raises(ValueError):
        registry.build("meta", ad_account_id="1")


# ---------------------------------------------------------------- the class, over many seeded worlds

def _world(rng: random.Random):
    """Random days with random holes, zero spend days, purchases present or absent, a random currency and page size."""
    days = {}
    for n in range(rng.randint(0, 9)):
        d = date(2026, 10, 1) + timedelta(days=rng.randint(0, 11))
        purchases = rng.choice([None, 0, rng.randint(1, 9)])
        days[d] = {
            "spend": rng.choice(["0", "0.00", "%d.%02d" % (rng.randint(0, 900), rng.randint(0, 99)), "%d.%03d" % (rng.randint(1, 50), rng.randint(0, 999))]),
            "impressions": rng.choice([0, rng.randint(1, 5000)]),
            "clicks": 0,
            "purchases": purchases,
            "purchase_value": None if purchases is None else "%d.%02d" % (rng.randint(0, 500), rng.randint(0, 99)),
        }
        days[d]["clicks"] = rng.randint(0, days[d]["impressions"])
    return days, rng.choice(["USD", "EUR", "JPY", "GBP"]), rng.randint(1, 4)


@pytest.mark.parametrize("seed", range(40))
def test_over_random_worlds_every_day_arrives_once_with_the_right_figures_and_no_link_is_followed(seed):
    rng = random.Random(seed)
    days, currency, page = _world(rng)
    start = date(2026, 10, 1) + timedelta(days=rng.randint(0, 3))
    end = start + timedelta(days=rng.randint(1, 10))
    with FakeMeta(token=TOKEN) as server, FakeMeta(token=TOKEN) as hostile:
        server.set_account(ACCOUNT, days, currency=currency)
        server.next_base = hostile.url
        server.default_value_skew = rng.choice([0, 3])
        faults = [Fault(rng.choice([500, 503, 400, 200]), error_in(rng.choice([1, 2, 4, 17, 613]))) for _ in range(rng.randint(0, 2))]
        server.faults.extend(faults)                          # fewer than max_attempts in a row, so each is absorbed
        pages = run(collect(make(server, Sleeps(), page_size=page, max_attempts=3), start=start, end=end))
        got = points_of(pages)
        assert as_set(got) == expected_points(days, currency, start, end), "seed %d" % seed
        assert len({(p.metric_key, p.observed_on) for p in got}) == len(got), "no day twice"
        assert hostile.requests == []
        assert all(type(p.value_numeric) is int and (p.currency == currency) == (p.metric_key in ("ad_spend", "ad_purchase_value")) for p in got)
        tokens = {r[1].get("access_token") for r in server.requests}
        assert tokens == {TOKEN}
        assert pages[-1].cursor is None and all(p.cursor for p in pages[:-1])


@pytest.mark.parametrize("seed", range(15))
def test_a_run_stopped_at_any_page_resumes_to_the_same_set_of_figures(seed):
    rng = random.Random(1000 + seed)
    days, currency, _ = _world(rng)
    with FakeMeta(token=TOKEN) as server:
        server.set_account(ACCOUNT, days, currency=currency)
        full = run(collect(make(server, page_size=2), end=date(2026, 10, 13)))
        want = as_set(points_of(full))
        union = set()
        cursor = None
        # walk the pages one resume at a time, as a runner that was stopped after each page would
        for _ in range(len(full) + 1):
            part = run(collect(make(server, page_size=2), end=date(2026, 10, 13), cursor=cursor))
            union |= as_set(points_of(part[:1]))
            cursor = part[0].cursor
            if cursor is None:
                break
        assert union == want, "seed %d" % seed
