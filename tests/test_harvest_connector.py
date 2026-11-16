"""The Harvest connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the requests the connector builds (the path, the inclusive `to`, the three headers Harvest asks for), how it reads the
documented answers (time entries, projects, the pagination block), the arithmetic (hours to whole seconds, rounded hours times a rate to integer minor units, half up), the
currency rule, the pacing against the documented 100 requests in 15 seconds, the retry and error handling, the budget status of a project, and that the token never goes to a
host the answer names. They do not prove Harvest accepts any of it: the server is written from the saved pages (docs/HARVEST_GRAPH_SOURCES.md) and no live account was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import random
import re
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import MONEY_METRICS, AuthExpired, ProtectedDataDenied, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.harvest import METRICS, HarvestConnector, HarvestProject, budget_alerts  # noqa: E402
from app.connectors.harvest_fake import FakeHarvest  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

TOKEN, ACCOUNT, CONTACT = "pat-SECRET-token", "1234567", "ops@example.com"
START, END = date(2026, 10, 1), date(2026, 10, 4)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server():
    with FakeHarvest(token=TOKEN, account_id=ACCOUNT) as fake:
        fake.add_project(1, "Marketing Website", budget=100)
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    return HarvestConnector(account_id=kw.pop("account_id", ACCOUNT), access_token=kw.pop("access_token", TOKEN), currency=kw.pop("currency", "USD"),
                            contact=kw.pop("contact", CONTACT), base_url=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


def paths(server):
    return [(r.path, r.query) for r in server.requests]


# ---------------------------------------------------------------- the happy path

def seed(server):
    server.add_entry(1, date(2026, 10, 1), "1.5", rate=100)                             # 5,400 s, 150.00
    server.add_entry(2, date(2026, 10, 1), "2.11", rounded="2.25", rate="100.10")      # 7,596 s, 2.25 x 100.10 = 225.225 -> 225.23
    server.add_entry(3, date(2026, 10, 1), "1.0", billable=False, rate=None)           # not billable
    server.add_entry(4, date(2026, 10, 3), "0.5", rate=None)                           # billable, no rate: counted in hours, adds no amount


def test_four_counts_per_day_are_written_and_money_carries_the_currency(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("harvest_hours", d)] for d in (1, 2, 3)] == [5400 + 7596 + 3600, 0, 1800]
    assert [got[("harvest_billable_hours", d)] for d in (1, 2, 3)] == [5400 + 7596, 0, 1800], "a non billable entry is in the hours and not in the billable hours"
    assert [got[("harvest_time_entries", d)] for d in (1, 2, 3)] == [3, 0, 1]
    assert [got[("harvest_billable_amount", d)] for d in (1, 2, 3)] == [15000 + 22523, 0, 0], "225.225 rounds half up to 225.23, a null rate adds nothing"
    pts = points_of(pages)
    assert len(pts) == 4 * 3 and {p.metric_key for p in pts} == set(METRICS)
    assert all(type(p.value_numeric) is int for p in pts)
    assert {(p.metric_key, p.currency) for p in pts if p.currency} == {("harvest_billable_amount", "USD")}


def test_the_requests_carry_the_three_headers_and_the_inclusive_to_date(server):
    seed(server)
    fetch(server)
    entries = [r for r in server.requests if r.path == "/v2/time_entries"]
    assert len(entries) == 1
    assert entries[0].query == {"from": "2026-10-01", "to": "2026-10-03", "per_page": "2000"}, "the window is half open and `to` is inclusive, so the 4th is not asked for"
    for r in server.requests:
        assert r.headers["authorization"] == "Bearer " + TOKEN
        assert r.headers["harvest-account-id"] == ACCOUNT
        assert r.headers["user-agent"] == "api-analytics-hub (%s)" % CONTACT
    assert [r.path for r in server.requests] == ["/v2/projects", "/v2/time_entries"]


def test_api_calls_counts_every_request_including_the_project_read(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 2


def test_pages_are_followed_by_links_next_and_every_entry_is_counted_once(server):
    for i in range(1, 8):
        server.add_entry(i, date(2026, 10, 1 + i % 3), "1.0", rate=10)
    server.page_cap = 3
    pages = fetch(server)
    got = by_key(pages)
    assert sum(got[("harvest_time_entries", d)] for d in (1, 2, 3)) == 7
    entry_calls = [r for r in server.requests if r.path == "/v2/time_entries"]
    assert len(entry_calls) == 3 and "cursor" in entry_calls[1].query and "cursor" in entry_calls[2].query, "the next page url is used as given"


def test_a_running_timer_counts_its_hours_so_far(server):
    server.add_entry(1, date(2026, 10, 1), "0.25", rate=100)
    server.entries[0]["is_running"] = True
    got = by_key(fetch(server))
    assert got[("harvest_hours", 1)] == 900


# ---------------------------------------------------------------- what it refuses

def test_a_next_page_on_another_host_is_never_followed_and_the_token_never_goes_there(server):
    for i in range(1, 5):
        server.add_entry(i, date(2026, 10, 1), "1.0")
    server.page_cap = 2
    assert by_key(fetch(server))[("harvest_time_entries", 1)] == 4, "positive control: the same data pages through"
    server.requests.clear()
    server.next_override = "http://evil.example.test/v2/time_entries?cursor=x"
    with pytest.raises(UpstreamBroken, match="outside the Harvest API"):
        fetch(server)
    assert all(r.headers.get("host", "").startswith("127.0.0.1") for r in server.requests)


@pytest.mark.parametrize("bad", ["http://127.0.0.1:1/v2/time_entries?cursor=x", "/v2/time_entries?cursor=x", "javascript:alert(1)", "SAME-HOST-OTHER-PATH", "SAME-HOST-DOTDOT"])
def test_a_next_page_that_is_not_this_host_under_v2_is_refused(server, bad):
    for i in range(1, 5):
        server.add_entry(i, date(2026, 10, 1), "1.0")
    server.page_cap = 2
    assert by_key(fetch(server))[("harvest_time_entries", 1)] == 4, "positive control"
    server.next_override = {"SAME-HOST-OTHER-PATH": server.url + "/v1/time_entries?cursor=x",
                            "SAME-HOST-DOTDOT": server.url + "/v2/../v1/time_entries?cursor=x"}.get(bad, bad)
    with pytest.raises(UpstreamBroken, match="outside the Harvest API"):
        fetch(server)


def test_a_next_page_that_names_itself_is_a_loop_not_a_page(server):
    for i in range(1, 6):
        server.add_entry(i, date(2026, 10, 1), "1.0")
    server.page_cap = 2
    server.next_override = server.url + "/v2/time_entries?cursor=Mg&per_page=2000&ref=next_cursor"
    with pytest.raises(UpstreamBroken, match="names itself"):
        fetch(server)


def test_a_total_that_does_not_match_what_arrived_is_a_broken_answer(server):
    server.add_entry(1, date(2026, 10, 1), "1.0")
    assert by_key(fetch(server))[("harvest_time_entries", 1)] == 1, "positive control"
    server.broken_total = 5
    with pytest.raises(UpstreamBroken, match="total_entries"):
        fetch(server)


def _page(entries, **extra):
    body = {"time_entries": entries, "per_page": 2000, "total_pages": 1, "total_entries": len(entries), "next_page": None, "previous_page": None, "page": 1,
            "links": {"first": None, "next": None, "previous": None, "last": None}}
    body.update(extra)
    return body


def good_entry(**kw):
    e = {"id": 1, "spent_date": "2026-10-02", "hours": 1.5, "rounded_hours": 1.5, "billable": True, "billable_rate": 10.0, "project": {"id": 1, "name": "P"}}
    e.update(kw)
    return e


@pytest.mark.parametrize("entries, match", [
    ([good_entry(), good_entry()], "appears twice"),
    ([good_entry(spent_date="2026-09-30")], "outside the window"),
    ([good_entry(spent_date="2026-10-04")], "outside the window"),
    ([good_entry(spent_date="10/02/2026")], "not a YYYY-MM-DD"),
    ([good_entry(spent_date="2026-02-30")], "not a calendar date"),
    ([good_entry(hours=24.5)], "outside 0 to 24"),
    ([good_entry(hours=-1)], "outside 0 to 24"),
    ([good_entry(hours="lots")], "not a number"),
    ([good_entry(hours=True)], "not a number"),
    ([good_entry(billable="yes")], "not true or false"),
    ([good_entry(project=None)], "names no project"),
    ([good_entry(id=True)], "no usable id"),
    (["not an object"], "not an object"),
])
def test_a_malformed_entry_is_refused_for_its_own_reason_and_a_good_one_passes(server, entries, match):
    """The project list holds project 1 here, so each refusal is the entry's own defect and not an unknown project."""
    def with_projects(page):
        server.faults.clear()
        server.requests.clear()
        server.faults.append(Fault(200, {"projects": [{"id": 1, "name": "P", "currency": "USD", "is_active": True, "budget": None, "budget_is_monthly": False}],
                                         "total_entries": 1, "links": {"next": None}}))
        server.faults.append(Fault(200, page))

    with_projects(_page([good_entry()]))
    assert by_key(fetch(server))[("harvest_time_entries", 2)] == 1, "positive control: the good entry is read"
    with_projects(_page(entries))
    with pytest.raises(UpstreamBroken, match=match):
        fetch(server)


def test_an_entry_on_a_project_in_another_currency_is_refused_not_added_in(server):
    server.add_entry(1, date(2026, 10, 1), "1.0", project=1, rate=100)
    assert by_key(fetch(server))[("harvest_billable_amount", 1)] == 10000, "positive control: same currency"
    server.projects[0]["currency"] = "EUR"
    with pytest.raises(UpstreamBroken, match="refusing to add across currencies"):
        fetch(server)


def test_an_entry_on_a_project_the_list_does_not_hold_is_refused(server):
    server.add_entry(1, date(2026, 10, 1), "1.0", project=1)
    assert fetch(server)
    server.add_entry(2, date(2026, 10, 1), "1.0", project=99)
    with pytest.raises(UpstreamBroken, match="does not hold"):
        fetch(server)


def test_a_billable_amount_is_left_out_when_the_key_may_not_read_projects(server):
    seed(server)
    pages = fetch(server)
    assert {p.metric_key for p in points_of(pages)} == set(METRICS), "positive control: with the permission all four are written"
    server.projects_forbidden = True
    conn = make(server)
    pages = run(collect(conn, START, END))
    assert {p.metric_key for p in points_of(pages)} == {"harvest_hours", "harvest_billable_hours", "harvest_time_entries"}
    assert conn.degraded_metrics == {"harvest_billable_amount"}


def test_projects_for_a_key_that_may_not_read_them_is_protected_data_denied(server):
    server.projects_forbidden = True
    conn = make(server)
    with pytest.raises(ProtectedDataDenied):
        run(conn.projects())
    run(conn.aclose())


def test_a_cursor_and_an_empty_window_are_refused(server):
    seed(server)
    assert fetch(server)
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="abc"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))


@pytest.mark.parametrize("kw, match", [
    ({"account_id": "abc"}, "numeric"),
    ({"currency": "usd"}, "ISO 4217"),
    ({"access_token": " "}, "access_token"),
    ({"contact": "nobody"}, "contact"),
    ({"contact": "a b@c.d"}, "contact"),
])
def test_bad_options_are_refused_and_good_ones_pass(server, kw, match):
    run(make(server).aclose())
    with pytest.raises(ValueError, match=match):
        make(server, **kw)


@pytest.mark.parametrize("url", ["ftp://api.harvestapp.com", "https://user:pw@api.harvestapp.com", "https://api.harvestapp.com/v2", "http://api.harvestapp.com"])
def test_a_base_address_that_is_not_a_bare_https_origin_is_refused(url):
    with pytest.raises(ValueError):
        HarvestConnector(account_id=ACCOUNT, access_token=TOKEN, currency="USD", contact=CONTACT, base_url=url)


# ---------------------------------------------------------------- errors and retries

def test_a_refused_token_is_auth_expired_and_a_good_one_verifies(server):
    assert run(_verify(make(server))) == "Harvest account %s" % ACCOUNT
    with pytest.raises(AuthExpired):
        run(_verify(make(server, access_token="wrong")))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn.aclose()


def test_the_wrong_account_id_is_a_protected_data_error(server):
    with pytest.raises(ProtectedDataDenied):
        run(_verify(make(server, account_id="999")))


def test_a_429_with_a_short_retry_after_is_waited_for_once_and_then_succeeds(server):
    seed(server)
    sleeps = Sleeps()
    server.faults.append(Fault(429, {"message": "slow down"}, {"Retry-After": "7"}))
    pages = fetch(server, sleeps=sleeps)
    assert sleeps.calls == [7.0], "the vendor's own wait is used"
    assert by_key(pages)[("harvest_time_entries", 1)] == 3
    assert sum(p.throttle_waits for p in pages) == 1


def test_a_429_that_asks_for_longer_than_the_cap_is_handed_to_the_runner(server):
    seed(server)
    sleeps = Sleeps()
    server.faults.append(Fault(429, {}, {"Retry-After": "120"}))
    with pytest.raises(RateLimited) as caught:
        fetch(server, sleeps=sleeps)
    assert caught.value.retry_after_seconds == 120.0 and sleeps.calls == [], "never slept for two minutes"


def test_a_server_error_is_retried_a_bounded_number_of_times(server):
    seed(server)
    server.faults += [Fault(503, {}), Fault(503, {})]
    assert fetch(server)                                                     # two failures then the third attempt answers
    server.faults += [Fault(503, {}) for _ in range(3)]
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        fetch(server)


def test_a_dropped_connection_is_retried(server):
    seed(server)
    server.faults.append(Fault(0, drop=True))
    assert by_key(fetch(server))[("harvest_time_entries", 1)] == 3


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_statuses_are_upstream_broken_and_quote_only_the_status(server, status):
    server.faults.append(Fault(status, {"error": "x", "error_description": "secret detail " + TOKEN}))
    with pytest.raises(UpstreamBroken) as caught:
        run(_verify(make(server)))
    assert "HTTP %d" % status in str(caught.value) and TOKEN not in str(caught.value) and "secret detail" not in str(caught.value)


def test_the_token_is_in_neither_the_repr_nor_the_error_text(server):
    conn = make(server)
    assert TOKEN not in repr(conn) and TOKEN not in repr(conn._token)
    with pytest.raises(Exception):
        "%s" % format(conn._token)                                           # a Secret refuses to be formatted into a log line
    run(conn.aclose())


# ---------------------------------------------------------------- pacing against the documented limit

def test_the_connector_stops_before_the_101st_request_in_15_seconds(server):
    now = [1000.0]
    conn = make(server, clock=lambda: now[0])

    async def many(n):
        for _ in range(n):
            await conn._get("/v2/users/me")

    run(many(100))
    assert len(server.requests) == 100
    with pytest.raises(RateLimited) as caught:
        run(many(1))
    assert len(server.requests) == 100, "nothing was sent for the 101st"
    assert caught.value.retry_after_seconds == pytest.approx(15.0)
    now[0] += 15.0
    run(many(1))                                                              # positive control: after the window the next one goes
    assert len(server.requests) == 101
    run(conn.aclose())


def test_a_retry_counts_against_the_limit_too(server):
    now = [1000.0]
    conn = make(server, clock=lambda: now[0])
    server.faults += [Fault(503, {}), Fault(503, {})]
    run(conn._get("/v2/users/me"))
    assert len(conn._sent) == 3
    run(conn.aclose())


# ---------------------------------------------------------------- budgets

def project(pid=1, budget="100", monthly=False, name=None):
    return HarvestProject(pid, name or "P%d" % pid, "USD", True, None if budget is None else Decimal(budget), monthly)


@pytest.mark.parametrize("spent, status, percent", [
    ("0", "under", 0), ("79.99", "under", 79), ("80", "warn", 80), ("99.99", "warn", 99), ("100", "warn", 100), ("100.01", "over", 100), ("250", "over", 250),
])
def test_budget_status_has_exact_edges(spent, status, percent):
    [alert] = budget_alerts([project()], {1: Decimal(spent)}, warn_percent=80)
    assert (alert.status, alert.percent_used) == (status, percent)


def test_a_project_with_no_hours_budget_or_a_monthly_one_is_never_called_under():
    alerts = budget_alerts([project(1, None), project(2, "0"), project(3, "100", monthly=True)], {1: Decimal(5), 2: Decimal(5), 3: Decimal(500)})
    assert [a.status for a in alerts] == ["no_hours_budget", "no_hours_budget", "monthly_not_checked"]
    assert all(a.percent_used is None for a in alerts)


def test_a_project_with_no_entries_has_zero_hours_spent():
    [alert] = budget_alerts([project()], {})
    assert (alert.hours_spent, alert.status) == (Decimal(0), "under")


@pytest.mark.parametrize("warn", [0, 101, -5, True, 80.5])
def test_a_warn_percent_that_is_not_a_whole_number_from_1_to_100_is_refused(warn):
    budget_alerts([project()], {}, warn_percent=80)
    with pytest.raises(ValueError):
        budget_alerts([project()], {}, warn_percent=warn)


def test_projects_and_hours_feed_budget_alerts_end_to_end(server):
    server.add_project(2, "Online Store", budget="10", monthly=False)
    server.add_project(3, "Retainer", budget="50", monthly=True)
    server.add_project(4, "Internal")
    server.add_entry(1, date(2026, 10, 1), "7.5", project=1)
    server.add_entry(2, date(2026, 10, 2), "9.0", project=2)
    server.add_entry(3, date(2026, 10, 2), "2.5", project=2)
    conn = make(server)
    projects = run(conn.projects())
    hours = run(conn.hours_by_project(window_start=START, window_end=END))
    run(conn.aclose())
    alerts = {a.name: a.status for a in budget_alerts(projects, hours)}
    assert alerts == {"Marketing Website": "under", "Online Store": "over", "Retainer": "monthly_not_checked", "Internal": "no_hours_budget"}
    assert hours == {1: Decimal("7.5"), 2: Decimal("11.5")}


def test_a_project_with_a_bad_field_is_a_broken_answer(server):
    assert len(run(_projects(make(server)))) == 1, "positive control"
    for bad in ({"id": True}, {"currency": "usd"}, {"name": ""}, {"budget": -1}, {"is_active": "yes"}, {"budget": "lots"}):
        row = {"id": 1, "name": "P", "currency": "USD", "is_active": True, "budget": None, "budget_is_monthly": False}
        row.update(bad)
        server.faults.append(Fault(200, {"projects": [row], "total_entries": 1, "links": {"next": None}}))
        with pytest.raises(UpstreamBroken):
            run(_projects(make(server)))


async def _projects(conn):
    try:
        return await conn.projects()
    finally:
        await conn.aclose()


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

def oracle(entries, start, end, currency="USD"):
    """What the four keys must be, worked out with no code shared with the connector: plain dictionaries and Decimal."""
    out = {}
    day = start
    while day < end:
        todays = [e for e in entries if e["day"] == day]
        out[("harvest_hours", day)] = sum(int((e["hours"] * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for e in todays)
        out[("harvest_billable_hours", day)] = sum(int((e["hours"] * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for e in todays if e["billable"])
        out[("harvest_time_entries", day)] = len(todays)
        out[("harvest_billable_amount", day)] = sum(int((e["rounded"] * e["rate"] * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                                                    for e in todays if e["billable"] and e["rate"] is not None)
        day += timedelta(days=1)
    return out


@pytest.mark.parametrize("seed_value", range(40))
def test_seeded_worlds_match_the_brute_force_oracle_over_pages_faults_and_edges(seed_value):
    rng = random.Random(seed_value)
    with FakeHarvest(token=TOKEN, account_id=ACCOUNT) as s:
        for pid in (1, 2, 3):
            s.add_project(pid, "P%d" % pid)
        start = date(2026, 9, 28) + timedelta(days=rng.randint(0, 5))
        end = start + timedelta(days=rng.randint(1, 9))
        entries = []
        for i in range(rng.randint(0, 40)):
            day = start + timedelta(days=rng.randint(-2, (end - start).days + 1))      # some land outside the window, so the server's from and to must cut them
            hours = (Decimal(rng.randint(0, 2400)) / 100)                              # 0.00 to 24.00
            rounded = hours + Decimal(rng.choice([0, 0, "0.25", "0.01"]))
            rate = rng.choice([None, Decimal("0"), Decimal("75"), Decimal("100.10"), Decimal("33.33")])
            e = {"day": day, "hours": hours, "rounded": rounded, "rate": rate, "billable": rng.random() < 0.7}
            s.add_entry(100 + i, day, hours, project=rng.choice((1, 2, 3)), billable=e["billable"], rate=rate, rounded=rounded)
            entries.append(e)
        s.page_cap = rng.choice([1, 2, 3, 7, 2000])
        picks = sorted(rng.sample(range(1, 14), rng.randint(0, 5)))                    # transient failures at random points, which the retry must hide
        s.fail_on = {n for i, n in enumerate(picks) if i == 0 or n - picks[i - 1] > 1}  # never two in a row: three in a row is a real outage, and the next test covers it
        got = by_key_full(run(collect(make_world(s), start, end)))
        inside = [e for e in entries if start <= e["day"] < end]
        assert got == oracle(inside, start, end), "seed %d page_cap %d" % (seed_value, s.page_cap)


def make_world(server):
    return HarvestConnector(account_id=ACCOUNT, access_token=TOKEN, currency="USD", contact=CONTACT, base_url=server.url, client=client(), retrier=retrier())


def by_key_full(pages):
    return {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(pages)}


# ---------------------------------------------------------------- the registry and the migration

def test_the_connector_is_registered_with_its_four_metrics_and_one_secret():
    spec = registry.get("harvest")
    assert spec.supported_metrics == frozenset(METRICS) and registry.secret_keys("harvest") == frozenset({"access_token"})
    assert "harvest_billable_amount" in MONEY_METRICS and not (set(METRICS) - {"harvest_billable_amount"}) & MONEY_METRICS
    assert registry.redact("harvest", {"account_id": "1", "access_token": TOKEN})["access_token"] == "***"
    built = registry.build("harvest", account_id=ACCOUNT, currency="USD", contact=CONTACT, access_token=TOKEN)
    assert isinstance(built, HarvestConnector)


def test_migration_018_names_every_key_and_puts_the_money_key_in_the_currency_check():
    sql = (ROOT / "migrations" / "018_harvest_and_graph_metrics.sql").read_text(encoding="utf-8")
    for key in METRICS:
        assert re.search(r"\('%s'," % key, sql), key
    check = sql.split("add constraint currency_iff_money")[1].split(");")[0]
    assert "'harvest_billable_amount'" in check and "'sf_won_amount'" in check and "'harvest_hours'" not in check
    earlier = (ROOT / "migrations" / "017_salesforce_metrics.sql").read_text(encoding="utf-8").split("add constraint currency_iff_money")[1].split(");")[0]
    for money in re.findall(r"'([a-z_]+)'", earlier):
        assert "'%s'" % money in check, "the new check dropped %s from the money list" % money
