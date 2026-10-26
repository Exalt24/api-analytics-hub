"""The Search Console connector against the stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the property URL
percent-encoded into the path, a date query paged with `startRow` until an empty page, the inclusive end date,
`dataState` all, then one query a day for the top queries and the top pages), how it reads the documented
response (doubles, `keys`, days left out, `first_incomplete_date`), the error taxonomy by reason as well as by
status, and that retries are bounded. They do not prove Google accepts the request: the server is written from
the saved pages (docs/GSC_SOURCES.md), no live property was used, so nothing here is a live integration.

The classes of fault the GA4 connector's adversary and blind passes found are hunted here in this connector's
own terms: an error body of any shape (an error object inside a 200, an HTML page, a list, an OAuth string),
an expired token, an empty report, a day that is not final yet, a count that arrives as a string or a float,
paging that repeats a day, a duplicate day, and a 403 that means "slow down". Every refusal runs a positive
control first.
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import AuthExpired, AuthRevoked, DimensionRow, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.gsc import GSCConnector, normalise_site  # noqa: E402
from app.connectors.gsc_fake import FINAL_LAG_DAYS, Fault, FakeGSC, error_body  # noqa: E402
from app.connectors.registry import registry  # noqa: E402

SITE = "sc-domain:example.com"
URL_SITE = "https://www.example.com/"
TOKEN = "tok-SECRET-gsc-123"
START, END = date(2026, 10, 1), date(2026, 10, 4)          # half open: the 1st, 2nd and 3rd
TODAY = date(2026, 11, 2)

DAYS = {date(2026, 10, 1): (10, 100, 7.5), date(2026, 10, 2): (20, 200, 6.25), date(2026, 10, 3): (30, 400, 4.125)}
QUERIES = {
    date(2026, 10, 1): [("shoes", 6, 60, 5.0), ("boots", 4, 40, 10.0)],
    date(2026, 10, 2): [("shoes", 12, 120, 5.0), ("boots", 8, 80, 8.0), ("socks", 1, 10, 3.0)],
    date(2026, 10, 3): [("shoes", 30, 400, 4.0)],
}
PAGES = {
    date(2026, 10, 1): [("/a", 10, 100, 7.5)],
    date(2026, 10, 2): [("/a", 15, 150, 6.0), ("/b", 5, 50, 7.0)],
    date(2026, 10, 3): [("/a", 30, 400, 4.0)],
}


@pytest.fixture()
def server():
    with FakeGSC(token=TOKEN, today=TODAY) as fake:
        fake.set_site(SITE, DAYS, queries=QUERIES, pages=PAGES)
        fake.set_site(URL_SITE, DAYS, queries=QUERIES, pages=PAGES)
        yield fake


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make(server, sleeps=None, **kw):
    kw.setdefault("sleep", sleeps or Sleeps())
    kw.setdefault("rng", lambda: 1.0)                         # full delay, no jitter, so waits are exact
    kw.setdefault("clock", lambda: datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    client = httpx.AsyncClient(trust_env=False, verify=False, timeout=10)
    if "token_provider" not in kw:
        kw.setdefault("access_token", TOKEN)
    return GSCConnector(site_url=kw.pop("site_url", SITE), base_url=server.url, client=client, **kw)


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


def rows_of(pages):
    return [r for page in pages for r in page.rows]


def milli(position: float) -> int:
    return int((Decimal(str(position)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def expected_points(days, start=START, end=END):
    out = set()
    for d, (clicks, impressions, position) in days.items():
        if start <= d < end:
            out |= {("search_clicks", d, clicks), ("search_impressions", d, impressions), ("search_position", d, milli(position))}
    return out


def as_set(points):
    return {(p.metric_key, p.observed_on, p.value_numeric) for p in points}


def bodies(server):
    return [r[1] for r in server.requests]


# ---------------------------------------------------------------- the happy path

def test_every_day_arrives_as_three_integer_metrics_and_the_top_rows_as_dimension_rows(server):
    pages = run(collect(make(server)))
    assert as_set(points_of(pages)) == expected_points(DAYS)
    assert all(type(p.value_numeric) is int and p.currency is None for p in points_of(pages))
    got = {(r.observed_on.day, r.dimension, r.key): (r.clicks, r.impressions, r.position_milli) for r in rows_of(pages)}
    assert got[(2, "query", "shoes")] == (12, 120, 5000) and got[(2, "page", "/b")] == (5, 50, 7000)
    assert len(got) == 6 + 4, "6 query rows and 4 page rows across three days"
    assert all(isinstance(r, DimensionRow) for r in rows_of(pages))
    assert pages[-1].cursor is None


def test_the_date_request_is_the_documented_one(server):
    run(collect(make(server)))
    first = bodies(server)[0]
    assert first == {"startDate": "2026-10-01", "endDate": "2026-10-03", "dimensions": ["date"], "type": "web",
                     "dataState": "all", "rowLimit": 25000, "startRow": 0}
    assert server.requests[0][2] == "Bearer " + TOKEN
    assert isinstance(first["rowLimit"], int) and isinstance(first["startRow"], int), "the pages type both as integers"


def test_the_request_flow_is_one_date_query_one_empty_page_then_two_queries_for_each_day_with_data(server):
    run(collect(make(server, top_n=5)))
    seq = [(b["dimensions"], b["startDate"], b["endDate"], b["rowLimit"], b["startRow"]) for b in bodies(server)]
    assert seq[0] == (["date"], "2026-10-01", "2026-10-03", 25000, 0)
    assert seq[1] == (["date"], "2026-10-01", "2026-10-03", 25000, 3), "a page of three rows is followed by one more, which comes back empty"
    assert seq[2:] == [(dims, d, d, 5, 0) for d in ("2026-10-01", "2026-10-02", "2026-10-03") for dims in (["query"], ["page"])]


@pytest.mark.parametrize("site,encoded", [
    ("sc-domain:example.com", "sc-domain%3Aexample.com"),
    ("https://www.example.com/", "https%3A%2F%2Fwww.example.com%2F"),
    ("http://example.com/blog/", "http%3A%2F%2Fexample.com%2Fblog%2F"),
])
def test_the_property_url_is_percent_encoded_into_the_path_in_full(site, encoded):
    with FakeGSC(token=TOKEN, today=TODAY) as server:
        server.set_site(site, DAYS)
        assert as_set(points_of(run(collect(make(server, site_url=site, top_n=0))))) == expected_points(DAYS)
        assert server.requests[0][3] == "/webmasters/v3/sites/%s/searchAnalytics/query" % encoded
        assert server.requests[0][0] == site, "decoded once, the stand-in sees the property as it was named"


@pytest.mark.parametrize("given,expected", [
    ("sc-domain:example.com", "sc-domain:example.com"),
    ("https://www.example.com", "https://www.example.com/"),
    ("https://www.example.com/", "https://www.example.com/"),
    ("http://example.com/blog/", "http://example.com/blog/"),
    ("  https://example.com  ", "https://example.com/"),
])
def test_a_property_is_a_domain_or_a_url_prefix_with_a_bare_origin_given_its_slash(given, expected):
    assert normalise_site(given) == expected


@pytest.mark.parametrize("bad", ["", "example.com", "sc-domain:", "sc-domain:exa mple.com", "ftp://example.com/", "https://", "javascript:1",
                                 "https://a b/", "sc-domain:../x", "https://example.com/?q=1", "https://example.com/#x", "sc-domain:-", "\n"])
def test_anything_else_is_refused_before_a_request(bad):
    with pytest.raises(ValueError):
        GSCConnector(site_url=bad, access_token="t")


def test_an_empty_report_is_an_empty_run_not_a_failure_and_asks_for_no_top_rows(server):
    server.set_site(SITE, {})
    pages = run(collect(make(server)))
    assert len(pages) == 1 and pages[0].points == [] and pages[0].rows == [] and pages[0].cursor is None
    assert len(server.requests) == 1


def test_a_window_with_no_data_but_a_property_that_has_some_elsewhere_is_empty_too(server):
    pages = run(collect(make(server), start=date(2026, 9, 1), end=date(2026, 9, 5)))
    assert points_of(pages) == [] and len(server.requests) == 1


# ---------------------------------------------------------------- paging

def test_paging_asks_for_each_start_row_in_turn_until_a_page_comes_back_empty(server):
    days = {date(2026, 10, d): (d, 10 * d, float(d)) for d in range(1, 6)}
    server.set_site(SITE, days)
    pages = run(collect(make(server, page_size=2, top_n=0), end=date(2026, 10, 6)))
    assert [(b["startRow"], b["rowLimit"]) for b in bodies(server)] == [(0, 2), (2, 2), (4, 2), (5, 2)]
    assert [p.cursor for p in pages] == ["2", "4", "5", None]
    assert as_set(points_of(pages)) == expected_points(days, end=date(2026, 10, 6))


def test_the_cursor_after_the_date_pages_is_top_when_top_rows_follow(server):
    pages = run(collect(make(server, page_size=2)))
    assert [p.cursor for p in pages] == ["2", "3", "top", "top", "top", None]


def test_a_run_can_resume_from_a_start_row_without_repeating_the_first_page(server):
    days = {date(2026, 10, d): (d, 10 * d, float(d)) for d in range(1, 6)}
    server.set_site(SITE, days)
    pages = run(collect(make(server, page_size=2, top_n=0), end=date(2026, 10, 6), cursor="2"))
    assert [b["startRow"] for b in bodies(server)] == [2, 4, 5]
    assert {p.observed_on.day for p in points_of(pages)} == {3, 4, 5}


def test_the_top_cursor_restarts_the_fetch_from_the_first_row(server):
    pages = run(collect(make(server), cursor="top"))
    assert bodies(server)[0]["startRow"] == 0 and as_set(points_of(pages)) == expected_points(DAYS)


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5", "2e3", "top ", "0x2"])
def test_a_cursor_that_is_not_a_row_offset_is_refused_before_any_request(server, cursor):
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), cursor=cursor))
    assert server.requests == []


def test_a_page_that_repeats_a_day_is_refused_not_written_as_one(server):
    row = {"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}
    server.faults.extend([Fault(200, {"rows": [row]}), Fault(200, {"rows": [row]})])
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server, page_size=1)))
    assert "two rows for 2026-10-01" in str(exc.value)


def test_a_server_that_never_stops_sending_rows_is_stopped_by_the_duplicate_rule_not_looped_on(server):
    row = {"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}
    server.faults.extend([Fault(200, {"rows": [row]})] * 50)
    with pytest.raises(UpstreamBroken):
        run(collect(make(server, page_size=1)))
    assert len(server.requests) == 2


@pytest.mark.parametrize("keys", [[], [1], ["a", "b"], "2026-10-01", None, [""], ["2026-10-01", "x"]])
def test_a_row_whose_keys_are_not_one_text_value_is_a_broken_response(server, keys):
    server.faults.append(Fault(200, {"rows": [{"keys": keys, "clicks": 1, "impressions": 2, "position": 3.0}]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("text", ["20261001", "2026-02-30", "Oct 1", "2026-9-1", "2026-09-30", "2026-10-04", "2027-10-01"])
def test_a_row_whose_day_is_not_a_calendar_day_inside_the_window_is_a_broken_response(server, text):
    server.faults.append(Fault(200, {"rows": [{"keys": [text], "clicks": 1, "impressions": 2, "position": 3.0}]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("rows", ["no", {}, 5, [1], ["x"], [None]])
def test_rows_that_are_not_a_list_of_objects_are_a_broken_response(server, rows):
    server.faults.append(Fault(200, {"rows": rows}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


# ---------------------------------------------------------------- what is final, and what is not

def test_days_from_first_incomplete_date_are_stored_marked_provisional(server):
    server.today = date(2026, 10, 5)                      # data is final through Oct 2, so Oct 3 is the first incomplete day
    pages = run(collect(make(server, top_n=0)))
    marks = {p.observed_on.day: bool(p.raw.get("provisional")) for p in points_of(pages)}
    assert marks == {1: False, 2: False, 3: True}
    assert all(p.raw["source"] == "searchAnalytics" for p in points_of(pages))


def test_a_settled_window_marks_nothing_and_no_metadata_marks_nothing(server):
    pages = run(collect(make(server, top_n=0)))
    assert all("provisional" not in p.raw for p in points_of(pages))
    server.faults.append(Fault(200, {"rows": [{"keys": ["2026-10-03"], "clicks": 1, "impressions": 2, "position": 3.0}]}))
    server.faults.append(Fault(200, {}))
    assert all("provisional" not in p.raw for p in points_of(run(collect(make(server, top_n=0)))))


@pytest.mark.parametrize("meta", [{"first_incomplete_date": "soon"}, {"first_incomplete_date": 5}, {"first_incomplete_date": "2026-02-30"},
                                  {"first_incomplete_date": None}, "no", [], {}])
def test_a_first_incomplete_date_that_is_not_a_date_is_ignored_not_a_crash(server, meta):
    row = {"keys": ["2026-10-03"], "clicks": 1, "impressions": 2, "position": 3.0}
    server.faults.extend([Fault(200, {"rows": [row], "metadata": meta}), Fault(200, {})])
    pages = run(collect(make(server, top_n=0)))
    assert len(points_of(pages)) == 3 and all("provisional" not in p.raw for p in points_of(pages))


def test_the_request_always_asks_for_fresh_data_so_a_late_day_is_not_silently_a_quiet_one(server):
    run(collect(make(server)))
    assert {b["dataState"] for b in bodies(server)} == {"all"}


# ---------------------------------------------------------------- numbers of every type

@pytest.mark.parametrize("style", ["int", "float", "string"])
def test_clicks_and_impressions_may_arrive_as_integers_whole_floats_or_numeric_strings(server, style):
    server.number_style = style
    pages = run(collect(make(server)))
    assert as_set(points_of(pages)) == expected_points(DAYS)
    assert len(rows_of(pages)) == 10


@pytest.mark.parametrize("field,value", [
    ("clicks", 1.5), ("clicks", "1.5"), ("clicks", -1), ("clicks", "-1"), ("clicks", "abc"), ("clicks", None), ("clicks", True),
    ("clicks", [1]), ("clicks", {}), ("clicks", "NaN"), ("clicks", "Infinity"), ("clicks", 10 ** 30), ("clicks", "1e999999"),
    ("impressions", 2.5), ("impressions", -2), ("impressions", None), ("impressions", ""),
    ("position", -1), ("position", "abc"), ("position", None), ("position", True), ("position", "NaN"), ("position", [3]),
    ("position", 10 ** 9),
])
def test_a_figure_that_is_not_a_sane_number_is_a_broken_response(server, field, value):
    row = {"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}
    row[field] = value
    server.faults.append(Fault(200, {"rows": [row]}))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_nan_in_the_body_is_refused_because_python_json_would_accept_it(server):
    server.faults.append(Fault(200, b'{"rows": [{"keys": ["2026-10-01"], "clicks": NaN, "impressions": 2, "position": 3.0}]}'))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("missing", ["clicks", "impressions", "position"])
def test_a_row_missing_a_figure_is_a_broken_response(server, missing):
    row = {"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}
    del row[missing]
    server.faults.append(Fault(200, {"rows": [row]}))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert missing in str(exc.value)


def test_more_clicks_than_impressions_is_a_broken_response_not_a_ctr_above_a_hundred_percent(server):
    server.faults.append(Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 5, "impressions": 4, "position": 3.0}]}))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "5 clicks and 4 impressions" in str(exc.value)


@pytest.mark.parametrize("position,expected", [(7.5, 7500), (7.1234, 7123), (7.1235, 7124), (7.9996, 8000), (1, 1000), (0, 0),
                                               ("12.0005", 12001), (100.0, 100000), (3.14159265, 3142)])
def test_position_is_kept_in_thousandths_rounded_half_up(server, position, expected):
    server.faults.append(Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": position}]}))
    server.faults.append(Fault(200, {}))
    got = {p.metric_key: p.value_numeric for p in points_of(run(collect(make(server, top_n=0))))}
    assert got["search_position"] == expected and type(got["search_position"]) is int


# ---------------------------------------------------------------- the top rows

def test_top_rows_are_asked_only_for_days_that_have_a_row_and_one_query_per_dimension(server):
    server.set_site(SITE, {date(2026, 10, 1): DAYS[date(2026, 10, 1)], date(2026, 10, 3): DAYS[date(2026, 10, 3)]},
                    queries=QUERIES, pages=PAGES)
    run(collect(make(server)))
    asked = [(b["dimensions"][0], b["startDate"]) for b in bodies(server) if b["dimensions"] != ["date"]]
    assert asked == [("query", "2026-10-01"), ("page", "2026-10-01"), ("query", "2026-10-03"), ("page", "2026-10-03")]


def test_top_n_zero_asks_for_no_top_rows_and_ends_the_last_page_with_no_cursor(server):
    pages = run(collect(make(server, top_n=0)))
    assert all(b["dimensions"] == ["date"] for b in bodies(server)) and rows_of(pages) == [] and pages[-1].cursor is None


def test_a_response_with_more_top_rows_than_were_asked_for_is_a_broken_response(server):
    server.set_site(SITE, {date(2026, 10, 1): DAYS[date(2026, 10, 1)]})
    rows = [{"keys": ["q%d" % i], "clicks": 1, "impressions": 2, "position": 3.0} for i in range(4)]
    server.faults.extend([Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}]}),
                          Fault(200, {}), Fault(200, {"rows": rows})])
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server, top_n=3)))
    assert "asked for 3 query rows and got 4" in str(exc.value)


def test_two_top_rows_with_one_key_are_refused_not_summed_or_overwritten(server):
    server.set_site(SITE, {date(2026, 10, 1): DAYS[date(2026, 10, 1)]})
    one = {"keys": ["same"], "clicks": 1, "impressions": 2, "position": 3.0}
    server.faults.extend([Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}]}),
                          Fault(200, {}), Fault(200, {"rows": [one, dict(one)]})])
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "two query rows" in str(exc.value)


def test_a_top_row_with_more_clicks_than_impressions_is_refused(server):
    server.set_site(SITE, {date(2026, 10, 1): DAYS[date(2026, 10, 1)]})
    bad = {"keys": ["q"], "clicks": 9, "impressions": 2, "position": 3.0}
    server.faults.extend([Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}]}),
                          Fault(200, {}), Fault(200, {"rows": [bad]})])
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


def test_a_query_with_every_odd_character_survives_as_a_key(server):
    keys = ["café ☕", "a/b?c=d&e", "  spaced  ", "x" * 200, "日本語", "quote\"s", "<script>"]
    server.set_site(SITE, {date(2026, 10, 1): (20, 200, 3.0)},
                    queries={date(2026, 10, 1): [(k, 1, 10, 3.0) for k in keys]})
    got = {r.key for r in rows_of(run(collect(make(server), start=date(2026, 10, 1), end=date(2026, 10, 2)))) if r.dimension == "query"}
    assert got == set(keys)


@pytest.mark.parametrize("seed", range(12))
def test_top_rows_are_a_valid_top_n_whichever_way_the_ties_are_ordered(seed):
    """Ties on clicks are sorted 'in an arbitrary way'. Whatever order a request gets, the rows returned must be a top n."""
    rng = random.Random(seed)
    keys = ["q%02d" % i for i in range(rng.randint(3, 25))]
    clicks = {k: rng.choice([0, 1, 1, 2, 2, 3, 5]) for k in keys}
    queries = {date(2026, 10, 1): [(k, clicks[k], clicks[k] + rng.randint(0, 3), 4.0) for k in keys]}
    top_n = rng.randint(1, 8)
    with FakeGSC(token=TOKEN, today=TODAY) as server:
        server.shuffle_ties = True
        server.set_site(SITE, {date(2026, 10, 1): (sum(clicks.values()) + 5, 1000, 4.0)}, queries=queries)
        got = [r for r in rows_of(run(collect(make(server, top_n=top_n), start=date(2026, 10, 1), end=date(2026, 10, 2))))
               if r.dimension == "query"]
    assert len(got) == min(top_n, len(keys)) and len({r.key for r in got}) == len(got)
    floor = min(r.clicks for r in got)
    assert all(clicks[k] <= floor for k in keys if k not in {r.key for r in got}), "a row left out beat a row kept"


# ---------------------------------------------------------------- the error taxonomy

def throttled(reason, code=403):
    return error_body(code, reason, "slow down")


@pytest.mark.parametrize("status", [200, 400, 401, 403, 500])
@pytest.mark.parametrize("body,expected", [
    (error_body(401, "authError", "Invalid Credentials"), AuthExpired),
    (error_body(401, "expired", "Session Expired"), AuthExpired),
    (error_body(403, "forbidden", "no permission"), AuthRevoked),
    (error_body(403, "insufficientPermissions", "no permission"), AuthRevoked),
    (error_body(403, "accessNotConfigured", "not enabled"), AuthRevoked),
    (error_body(400, "invalidParameter", "bad"), UpstreamBroken),
    (error_body(400, "badRequest", "bad"), UpstreamBroken),
    (error_body(404, "notFound", "gone"), UpstreamBroken),
    (error_body(402, "dailyLimitExceeded402", "pay"), UpstreamBroken),
])
def test_the_error_code_decides_the_typed_error_whatever_the_http_status_or_if_the_body_is_in_a_200(server, status, body, expected):
    assert run(collect(make(server)))                                  # positive control
    server.requests.clear()
    server.faults.append(Fault(status, body))
    sleeps = Sleeps()
    with pytest.raises(expected) as exc:
        run(collect(make(server, sleeps)))
    assert type(exc.value) is expected and len(server.requests) == 1 and sleeps.calls == []


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded", "limitExceeded",
                                    "servingLimitExceeded", "concurrentLimitExceeded", "variableTermLimitExceeded"])
def test_a_403_whose_reason_is_a_quota_means_slow_down_not_you_may_not_read_this(server, reason):
    """The same status as a permission refusal. Reading it as AuthRevoked would mark a healthy connection as disconnected."""
    server.faults.extend([Fault(403, throttled(reason))] * 10)
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps, max_attempts=3)))
    assert exc.value.retryable and len(server.requests) == 3 and len(sleeps.calls) == 2
    server.faults.clear()
    server.requests.clear()
    server.faults.append(Fault(403, throttled(reason)))
    assert as_set(points_of(run(collect(make(server, Sleeps()))))) == expected_points(DAYS)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.parametrize("body", [b"", b"<html><body>Bad gateway</body></html>", [1, 2], "plain text",
                                  error_body(500, "backendError", "x"), {"error": {"status": "RESOURCE_EXHAUSTED"}}])
def test_a_5xx_or_429_with_a_page_a_list_or_an_error_object_is_retried_then_rate_limited_never_a_crash(server, status, body):
    server.faults.extend([Fault(status, body)] * 5)
    with pytest.raises(RateLimited):
        run(collect(make(server, Sleeps())))


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": "invalid_token", "error_description": "expired"}, AuthExpired),     # an OAuth style refusal: `error` is a string
    (401, [{"error": {"code": 401, "errors": [{"reason": "authError"}]}}], AuthExpired),    # the error object wrapped in a list
    (403, {"error": "access_denied"}, AuthRevoked),
    (401, b"", AuthExpired), (401, "<html>unauthorized</html>", AuthExpired), (403, b"", AuthRevoked), (403, [1], AuthRevoked),
    (400, [1, 2], UpstreamBroken), (418, b"", UpstreamBroken), (404, "no", UpstreamBroken),
])
def test_an_error_body_of_any_shape_still_becomes_the_typed_error(server, status, body, expected):
    server.faults.append(Fault(status, body))
    with pytest.raises(expected):
        run(collect(make(server)))


@pytest.mark.parametrize("body", [b"<html>ok</html>", b"", b"not json", [], [1], "text", 5, b"null"])
def test_a_200_that_is_not_a_json_object_is_a_broken_response(server, body):
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server)))


@pytest.mark.parametrize("err", ["boom", None, 7, ["x"], {"message": "no code"}])
def test_an_error_member_of_any_shape_inside_a_200_is_a_refusal_not_a_crash(server, err):
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
    assert as_set(points_of(run(collect(make(server, token_provider=lambda: TOKEN))))) == expected_points(DAYS)
    with pytest.raises(AuthExpired):
        run(collect(make(server, token_provider=lambda: "")))


def test_a_property_the_token_cannot_read_is_auth_revoked_and_names_the_property_not_the_token(server):
    with pytest.raises(AuthRevoked) as exc:
        run(collect(make(server, site_url="sc-domain:other.org")))
    assert "sc-domain:other.org" in str(exc.value) and TOKEN not in str(exc.value) and not exc.value.retryable


def test_a_numeric_retry_after_is_honoured_a_garbage_one_ignored_and_a_long_one_handed_back(server):
    server.faults.append(Fault(429, throttled("rateLimitExceeded", 429), {"Retry-After": "3"}))
    sleeps = Sleeps()
    run(collect(make(server, sleeps)))
    assert sleeps.calls == [3.0]
    server.faults.append(Fault(429, throttled("rateLimitExceeded", 429), {"Retry-After": "soon"}))
    sleeps2 = Sleeps()
    run(collect(make(server, sleeps2)))
    assert sleeps2.calls == [1.0]
    server.faults.append(Fault(429, throttled("rateLimitExceeded", 429), {"Retry-After": "600"}))
    sleeps3 = Sleeps()
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, sleeps3)))
    assert exc.value.retry_after_seconds == 600 and sleeps3.calls == []


def test_the_backoff_is_capped_and_jittered_between_half_and_all_of_the_delay(server):
    server.faults.extend([Fault(503, error_body(503, "backendError", "x"))] * 4)
    sleeps = Sleeps()
    run(collect(make(server, sleeps, max_attempts=5, backoff_base=10.0, backoff_cap=30.0, rng=lambda: 0.0)))
    assert sleeps.calls == [5.0, 10.0, 15.0, 15.0]


def test_a_dropped_connection_is_retried_and_the_message_carries_the_class_only(server):
    server.faults.extend([Fault(0, drop=True)] * 3)
    with pytest.raises(RateLimited) as exc:
        run(collect(make(server, Sleeps(), max_attempts=3)))
    text = str(exc.value)
    assert "transport failure" in text and TOKEN not in text and "127.0.0.1" not in text


def test_the_token_never_appears_in_a_message_or_a_repr(server):
    conn = make(server)
    assert TOKEN not in repr(conn) and TOKEN not in str(conn) and TOKEN not in repr(vars(conn).get("_token"))
    for faults, expected in (([Fault(401, error_body(401, "authError", "x"))], AuthExpired),
                             ([Fault(400, error_body(400, "invalid", "x"))], UpstreamBroken),
                             ([Fault(500, error_body(500, "internalError", "x"))] * 3, RateLimited)):
        server.faults.extend(faults)
        with pytest.raises(expected) as exc:
            run(collect(make(server, Sleeps())))
        assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)


def test_the_error_text_is_cut_short_and_keeps_the_reason(server):
    server.faults.append(Fault(400, error_body(400, "invalidParameter", "x" * 800)))
    with pytest.raises(UpstreamBroken) as exc:
        run(collect(make(server)))
    assert "invalidParameter" in str(exc.value) and len(str(exc.value)) < 330


# ---------------------------------------------------------------- verify and construction

async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_verify_names_the_property_and_its_newest_day_of_data(server):
    server.set_site(SITE, {date(2026, 10, 28): (1, 2, 3.0), date(2026, 10, 30): (1, 2, 3.0)})
    conn = make(server)
    assert run(_verify(conn)) == "Search Console property sc-domain:example.com (newest data 2026-10-30)"
    assert conn.latest_data == date(2026, 10, 30)
    body = bodies(server)[-1]
    assert body["dimensions"] == ["date"] and body["startDate"] == "2026-10-23" and body["endDate"] == "2026-11-01"


def test_verify_with_no_recent_data_still_succeeds_and_a_wrong_token_fails_in_front_of_the_person(server):
    server.set_site(SITE, {})
    conn = make(server)
    assert run(_verify(conn)) == "Search Console property sc-domain:example.com" and conn.latest_data is None
    with pytest.raises(AuthExpired):
        run(_verify(make(server, access_token="not-the-token")))


@pytest.mark.parametrize("kw", [{"access_token": None}, {"access_token": "a", "token_provider": lambda: "b"}, {"page_size": 0},
                                {"page_size": 25001}, {"top_n": -1}, {"top_n": 25001}, {"max_attempts": 0}])
def test_constructor_arguments_that_cannot_work_are_refused(kw):
    base = {"site_url": SITE}
    if "access_token" not in kw and "token_provider" not in kw:
        base["access_token"] = TOKEN
    with pytest.raises(ValueError):
        GSCConnector(**base, **kw)


def test_an_empty_or_backwards_window_is_a_caller_error(server):
    for start, end in ((START, START), (END, START)):
        with pytest.raises(ValueError):
            run(collect(make(server), start=start, end=end))
    assert server.requests == []


def test_from_env_builds_from_the_environment_and_names_what_is_missing():
    conn = GSCConnector.from_env({"GSC_SITE_URL": "https://example.com", "GSC_ACCESS_TOKEN": "t", "GSC_API_BASE_URL": "http://x/"}, top_n=3)
    assert conn.site_url == "https://example.com/" and conn._base == "http://x" and conn._top_n == 3
    with pytest.raises(ValueError) as exc:
        GSCConnector.from_env({"GSC_SITE_URL": SITE})
    assert "GSC_ACCESS_TOKEN" in str(exc.value) and "GSC_SITE_URL" not in str(exc.value)


def test_gsc_is_registered_with_its_credentials_and_its_metrics():
    spec = registry.get("gsc")
    assert spec.supported_metrics == frozenset({"search_clicks", "search_impressions", "search_position"})
    assert registry.secret_keys("gsc") == frozenset({"access_token"})
    assert registry.platforms_supporting("search_clicks") == ["gsc"] and registry.platforms_supporting("ad_spend") == ["meta"]
    assert registry.redact("gsc", {"site_url": SITE, "access_token": "x"}) == {"site_url": SITE, "access_token": "***"}
    conn = registry.build("gsc", site_url=SITE, access_token="t")
    assert isinstance(conn, GSCConnector)


# ---------------------------------------------------------------- the class, over many seeded worlds

def _world(rng: random.Random):
    days = {}
    for _ in range(rng.randint(0, 9)):
        d = date(2026, 10, 1) + timedelta(days=rng.randint(0, 11))
        impressions = rng.randint(1, 3000)
        days[d] = (rng.randint(0, impressions), impressions, round(rng.uniform(1, 60), rng.randint(0, 5)))
    queries = {d: [("q%d" % i, rng.randint(0, 3), 10 + rng.randint(0, 5), rng.uniform(1, 30)) for i in range(rng.randint(0, 6))]
               for d in days}
    pages = {d: [("/p%d" % i, rng.randint(0, 3), 10 + rng.randint(0, 5), rng.uniform(1, 30)) for i in range(rng.randint(0, 4))]
             for d in days}
    return days, queries, pages


@pytest.mark.parametrize("seed", range(40))
def test_over_random_worlds_every_day_arrives_once_with_the_right_figures_and_the_right_marks(seed):
    rng = random.Random(seed)
    days, queries, pages_ = _world(rng)
    start = date(2026, 10, 1) + timedelta(days=rng.randint(0, 3))
    end = start + timedelta(days=rng.randint(1, 10))
    today = date(2026, 10, 1) + timedelta(days=rng.randint(4, 20))
    top_n = rng.choice([0, 1, 3, 10])
    with FakeGSC(token=TOKEN, today=today) as server:
        server.number_style = rng.choice(["int", "float", "string"])
        server.shuffle_ties = rng.random() < 0.5
        server.set_site(SITE, days, queries=queries, pages=pages_)
        server.faults.extend(Fault(rng.choice([500, 503, 429, 403]), error_body(500, rng.choice(
            ["backendError", "rateLimitExceeded", "userRateLimitExceeded"]), "x")) for _ in range(rng.randint(0, 2)))
        pages = run(collect(make(server, Sleeps(), page_size=rng.randint(1, 4), top_n=top_n, max_attempts=3), start=start, end=end))
        got = points_of(pages)
        assert as_set(got) == expected_points(days, start, end), "seed %d" % seed
        assert len({(p.metric_key, p.observed_on) for p in got}) == len(got)
        final_through = today - timedelta(days=FINAL_LAG_DAYS)
        for p in got:
            assert bool(p.raw.get("provisional")) == (p.observed_on > final_through), "seed %d day %s" % (seed, p.observed_on)
        rows = rows_of(pages)
        assert all(r.observed_on in {p.observed_on for p in got} for r in rows), "a top row for a day with no daily row"
        assert len({(r.observed_on, r.dimension, r.key) for r in rows}) == len(rows)
        for d in {r.observed_on for r in rows}:
            for dim in ("query", "page"):
                assert len([r for r in rows if r.observed_on == d and r.dimension == dim]) <= top_n
        assert all(r[2] == "Bearer " + TOKEN and r[0] == SITE for r in server.requests)
        assert pages[-1].cursor is None and all(p.cursor for p in pages[:-1])


# ---------------------------------------------------------------- a day that is still running is never pulled

def test_the_default_window_of_the_sync_command_ends_twelve_hours_behind_utc_as_for_every_source():
    from app.connectors import ga4, gsc
    assert gsc.settled_window_end is ga4.settled_window_end
    source = (Path(__file__).resolve().parents[1] / "scripts" / "sync_gsc.py").read_text(encoding="utf-8")
    assert "settled_window_end(datetime.now(timezone.utc))" in source and "date.today()" not in source
    assert gsc.settled_window_end(datetime.fromisoformat("2026-11-01T00:30:00+00:00")) == date(2026, 10, 31), \
        "a Pacific day that is still running on Oct 31 is not in the window"


# ---------------------------------------------------------------- the error body reader, field by field

def test_an_oauth_style_string_error_keeps_its_name_and_a_description_cut_to_150_and_a_name_cut_to_60():
    from app.connectors.gsc import _read_error
    err = _read_error({"error": "x" * 80, "error_description": "d" * 200}, 400)
    assert err.reason == "x" * 60 and err.message == "d" * 150 and err.code is None and err.status is None and err.http == 400
    short = _read_error({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, 400)
    assert (short.reason, short.message) == ("invalid_grant", "Token has been expired or revoked.")
    assert _read_error({"error": "invalid_grant"}, 400).message == ""


def test_an_error_that_is_neither_a_string_nor_an_object_is_kept_as_text_cut_to_120():
    from app.connectors.gsc import _read_error
    err = _read_error({"error": ["y" * 200]}, 500)
    assert err.message.startswith("['yyy") and len(err.message) == 120 and err.reason is None and err.code is None and err.http == 500


def test_an_error_object_gives_code_reason_status_and_a_message_cut_to_150_and_a_bad_field_is_dropped_not_guessed():
    from app.connectors.gsc import _read_error
    err = _read_error({"error": {"code": 403, "message": "m" * 200, "status": "PERMISSION_DENIED",
                                 "errors": [{"reason": "r" * 80, "message": "x"}]}}, 403)
    assert (err.code, err.status, err.http) == (403, "PERMISSION_DENIED", 403)
    assert err.reason == "r" * 60 and err.message == "m" * 150
    odd = _read_error({"error": {"code": True, "message": None, "status": 5, "errors": [{"reason": 7}]}}, 500)
    assert (odd.code, odd.reason, odd.status, odd.message, odd.effective) == (None, None, None, "", 500)
    assert _read_error({"error": {"code": "403"}}, 500).code is None
    assert _read_error({"error": {"errors": []}}, 500).reason is None
    assert _read_error({"error": {"status": "RESOURCE_EXHAUSTED"}}, 500).status == "RESOURCE_EXHAUSTED"
    assert _read_error([{"error": {"code": 401}}], 200).code == 401        # a list holding the error object
    assert _read_error([], 200) is None and _read_error({"rows": []}, 200) is None and _read_error("text", 200) is None


def test_a_retry_after_of_zero_is_honoured_as_zero_and_a_negative_or_missing_one_is_not():
    from app.connectors.gsc import GSCConnector as C

    def of(value):
        headers = {} if value is None else {"Retry-After": value}
        return C._retry_after(httpx.Response(429, headers=headers))
    assert of("0") == 0.0 and of("2.5") == 2.5 and of("-1") is None and of(None) is None and of("soon") is None


def test_the_throttle_waits_of_a_page_count_each_sleep_once(server):
    server.faults.extend([Fault(429, throttled("rateLimitExceeded", 429), {"Retry-After": "1"})] * 2)
    pages = run(collect(make(server, Sleeps(), max_attempts=4)))
    assert pages[0].throttle_waits == 2 and pages[0].api_calls == 3


def test_the_connector_closes_a_client_it_made_and_leaves_one_it_was_given_open():
    async def go():
        given = httpx.AsyncClient(trust_env=False)
        conn = GSCConnector(site_url=SITE, base_url="http://127.0.0.1:1", client=given, access_token=TOKEN)
        await conn.aclose()
        assert not given.is_closed
        await given.aclose()
        own = GSCConnector(site_url=SITE, base_url="http://127.0.0.1:1", access_token=TOKEN)
        await own.aclose()
        assert own._client.is_closed
    run(go())


# ---------------------------------------------------------------- constants, messages and bookkeeping, pinned with a scripted transport

def scripted(handler, **kw):
    """A connector whose HTTP answers come from `handler(body, call number) -> (status, json or text)`, recording each body."""
    import json as _json
    seen: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        seen.append(body)
        status, content = handler(body, len(seen))
        if isinstance(content, str):
            return httpx.Response(status, content=content.encode(), headers={"content-type": "text/html"})
        return httpx.Response(status, json=content)
    kw.setdefault("sleep", Sleeps())
    kw.setdefault("clock", lambda: datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return GSCConnector(site_url=SITE, base_url="http://stand-in.invalid", client=client, access_token=TOKEN, **kw), seen


def day_row(day="2026-10-01", clicks=1, impressions=10, position=2.0):
    return {"keys": [day], "clicks": clicks, "impressions": impressions, "position": position}


def key_row(key, clicks=1, impressions=10, position=2.0):
    return {"keys": [key], "clicks": clicks, "impressions": impressions, "position": position}


def broken(coro_factory):
    with pytest.raises(UpstreamBroken) as exc:
        run(coro_factory())
    return str(exc.value)


def one_day(conn):
    return collect(conn, start=date(2026, 10, 1), end=date(2026, 10, 2))


def test_the_defaults_are_the_documented_ones():
    from app.connectors import gsc
    assert gsc.BASE_URL == "https://www.googleapis.com"
    assert gsc.SCOPE == "https://www.googleapis.com/auth/webmasters.readonly"
    assert gsc.DEFAULT_TOP_N == 10
    conn = GSCConnector(site_url=SITE, access_token=TOKEN)
    assert conn._base == "https://www.googleapis.com" and conn._top_n == 10
    run(conn.aclose())


def test_a_count_of_the_largest_stored_integer_is_taken_and_one_more_is_refused():
    from app.connectors.gsc import _count
    assert _count(2 ** 63 - 1, "x") == 2 ** 63 - 1 and _count(str(2 ** 63 - 1), "x") == 2 ** 63 - 1
    with pytest.raises(UpstreamBroken, match="larger than any count"):
        _count(2 ** 63, "x")


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
                                    "rateLimitExceededUnreg", "userRateLimitExceededUnreg", "dailyLimitExceededUnreg"])
def test_every_quota_reason_the_page_lists_is_a_throttle_not_a_revoked_login(reason):
    from app.connectors.gsc import _GoogleError
    assert _GoogleError(403, reason, None, "", 403).throttled()
    conn, _ = scripted(lambda body, n: (403, error_body(403, reason, "quota")), max_attempts=1)
    with pytest.raises(RateLimited):
        run(one_day(conn))


def test_constructor_refusals_say_the_range_they_enforce_and_one_attempt_is_allowed():
    from app.connectors.gsc import MAX_ROW_LIMIT
    with pytest.raises(ValueError) as exc:
        GSCConnector(site_url=SITE, access_token=TOKEN, page_size=0)
    assert str(exc.value) == "page_size must be 1 to %d" % MAX_ROW_LIMIT
    with pytest.raises(ValueError) as exc:
        GSCConnector(site_url=SITE, access_token=TOKEN, top_n=MAX_ROW_LIMIT + 1)
    assert str(exc.value) == "top_n must be 0 to %d" % MAX_ROW_LIMIT
    with pytest.raises(ValueError) as exc:
        GSCConnector(site_url=SITE, access_token=TOKEN, max_attempts=0)
    assert str(exc.value) == "max_attempts must be at least 1"
    GSCConnector(site_url=SITE, access_token=TOKEN, page_size=MAX_ROW_LIMIT, top_n=MAX_ROW_LIMIT, max_attempts=1)
    GSCConnector(site_url=SITE, access_token=TOKEN, page_size=1, top_n=0)


def test_from_env_builds_a_connector_of_the_same_type_and_the_repr_names_the_class():
    conn = GSCConnector.from_env({"GSC_SITE_URL": SITE, "GSC_ACCESS_TOKEN": TOKEN})
    assert type(conn) is GSCConnector and repr(conn).startswith("GSCConnector(") and TOKEN not in repr(conn)
    run(conn.aclose())


def test_a_200_that_is_not_an_object_says_what_it_was():
    conn, _ = scripted(lambda body, n: (200, "<html>maintenance</html>"))
    assert "a body that is not JSON" in broken(lambda: one_day(conn))
    conn, _ = scripted(lambda body, n: (200, [1, 2]))
    assert "with list, not an object" in broken(lambda: one_day(conn))
    conn, _ = scripted(lambda body, n: (200, 7))
    assert "with int, not an object" in broken(lambda: one_day(conn))


def test_rows_that_are_not_a_list_and_rows_with_missing_or_bad_numbers_are_named():
    conn, _ = scripted(lambda body, n: (200, {"rows": "none"}))
    assert broken(lambda: one_day(conn)) == "rows is not a list"
    for field_name, bad in (("clicks", "many"), ("impressions", None), ("position", "top")):
        row = day_row()
        row[field_name] = bad
        conn, _ = scripted(lambda body, n, row=row: (200, {"rows": [row]}))
        assert broken(lambda: one_day(conn)).startswith(field_name + " "), field_name
    row = day_row()
    del row["position"]
    conn, _ = scripted(lambda body, n: (200, {"rows": [row]}))
    assert "has no position" in broken(lambda: one_day(conn))
    conn, _ = scripted(lambda body, n: (200, {"rows": [{"keys": [], "clicks": 1, "impressions": 1, "position": 1}]}))
    assert "(date)" in broken(lambda: one_day(conn))


def test_a_long_key_is_cut_to_forty_characters_in_the_message_that_names_it():
    long = "q" * 40 + "OVERFLOW"

    def make_handler(rows):
        def handler(body, n):
            if body["dimensions"] == ["date"]:
                return 200, ({"rows": [day_row()]} if body["startRow"] == 0 else {})
            if body["dimensions"] == ["query"]:
                return 200, {"rows": rows}
            return 200, {}
        return handler
    conn, _ = scripted(make_handler([key_row(long), key_row(long)]))
    message = broken(lambda: one_day(conn))
    assert repr("q" * 40) in message and "OVERFLOW" not in message and "2026-10-01" in message
    conn, _ = scripted(make_handler([key_row(long, clicks="x")]))
    message = broken(lambda: one_day(conn))
    assert repr("q" * 40) in message and "OVERFLOW" not in message and message.startswith("clicks the query row")


def test_verify_asks_for_the_ten_days_before_today_from_row_zero_with_a_hundred_rows():
    conn, seen = scripted(lambda body, n: (200, {"rows": [day_row("2026-10-30")]}))
    text = run(conn.verify())
    assert "newest data 2026-10-30" in text
    assert seen[0]["startDate"] == "2026-10-23" and seen[0]["endDate"] == "2026-11-01"
    assert seen[0]["startRow"] == 0 and seen[0]["rowLimit"] == 100 and seen[0]["dimensions"] == ["date"]
    run(conn.aclose())


def test_the_top_row_pages_count_both_of_their_calls_and_every_wait():
    def handler(body, n):
        if body["dimensions"] == ["date"]:
            return 200, ({"rows": [day_row()]} if body["startRow"] == 0 else {})
        if n == 3:                                             # the first top query of the day is throttled once
            return 429, error_body(429, "rateLimitExceeded", "slow down")
        return 200, {"rows": [key_row("shoes")]}
    conn, seen = scripted(handler)
    top = [p for p in run(one_day(conn)) if p.rows]
    assert len(top) == 1
    assert top[0].api_calls == 3 and top[0].throttle_waits == 1, "two dimensions and one retry make three calls and one wait"

    def clean_handler(body, n):
        if body["dimensions"] == ["date"]:
            return 200, ({"rows": [day_row()]} if body["startRow"] == 0 else {})
        return 200, {"rows": [key_row("shoes")]}
    clean, _ = scripted(clean_handler)
    top = [p for p in run(one_day(clean)) if p.rows]
    assert top[0].api_calls == 2 and top[0].throttle_waits == 0


def test_a_dimension_row_refuses_anything_but_whole_counts_of_at_least_zero_and_needs_a_dimension_and_a_key():
    day = date(2026, 10, 1)
    DimensionRow(day, "query", "k", 0, 0, 0)                                     # zero is a count
    for name in ("clicks", "impressions", "position_milli"):
        for bad in (True, 1.5, -1, "5", None):
            figures = {"clicks": 1, "impressions": 2, "position_milli": 3, name: bad}
            with pytest.raises(UpstreamBroken, match="^%s must be a whole number of at least 0" % name):
                DimensionRow(day, "query", "k", **figures)
    for dimension, key in (("", "k"), ("query", ""), ("", "")):
        with pytest.raises(UpstreamBroken, match="a dimension row needs a dimension and a key"):
            DimensionRow(day, dimension, key, 1, 2, 3)
