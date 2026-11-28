"""The ConnectWise Manage connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the requests the connector builds (the Basic string, the `clientId` header, forward-only paging at 1,000 a page, no `conditions`
unless the operator gives one), how it reads the answer (tickets, time entries, the `Link` header), the arithmetic (hours to whole seconds, the UTC day, the resolution goals
missed), what it refuses (a next page on another host, a repeated page, a cut-short body, a malformed ticket), the retry and error handling, and that no key reaches a message. They
do NOT prove ConnectWise accepts any of it: the server is written from an OpenAPI file kept in an open source client and from a blog post, because ConnectWise's own developer
documentation needs a login that refuses a free mailbox address (docs/MSP_SOURCES.md), and no live tenant was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import base64
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.connectwise import METRICS, ConnectWiseConnector  # noqa: E402
from app.connectors.connectwise_fake import FakeConnectWise  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

COMPANY, PUBLIC, PRIVATE, CLIENT = "acme", "PUB-SECRET-key", "PRIV-SECRET-key", "client-guid-1234"
START, END = date(2026, 10, 1), date(2026, 10, 4)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def dt(day: int, hour: int = 9, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture()
def server():
    with FakeConnectWise(company=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT) as fake:
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    kw.setdefault("clock", lambda: NOW)
    return ConnectWiseConnector(api_url=kw.pop("api_url", server.api_url), company_id=kw.pop("company_id", COMPANY), public_key=kw.pop("public_key", PUBLIC),
                                private_key=kw.pop("private_key", PRIVATE), client_id=kw.pop("client_id", CLIENT), client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


async def _tickets(conn):
    try:
        return await conn.tickets()
    finally:
        await conn.aclose()


async def _times(conn):
    try:
        return await conn.time_entries()
    finally:
        await conn.aclose()


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn.aclose()


def seed(server):
    server.add_ticket(1, dt(1), closed_at=dt(2, 15), resolved_at=dt(2, 14), resolution_goal=dt(2, 18))          # on time
    server.add_ticket(2, dt(1, 10), closed_at=dt(3, 9), resolved_at=dt(3, 8), resolution_goal=dt(2, 12))        # finished a day late: goal fell on the 2nd
    server.add_ticket(3, dt(2, 8), resolution_goal=dt(2, 20))                                                    # open, goal passed
    server.add_ticket(4, dt(3, 8), resolution_goal=dt(20, 9))                                                    # open, goal in the future
    server.add_ticket(5, dt(3, 9))                                                                               # open, no goal
    server.add_time(1, dt(1, 9), "1.5", ticket=1)
    server.add_time(2, dt(1, 13), "2.11", ticket=2)
    server.add_time(3, dt(3, 10), "0.25")                                                                        # not on a ticket


# ---------------------------------------------------------------- the happy path

def test_four_counts_per_day_are_written_with_no_currency(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("cw_tickets_opened", d)] for d in (1, 2, 3)] == [2, 1, 2]
    assert [got[("cw_tickets_closed", d)] for d in (1, 2, 3)] == [0, 1, 1]
    assert [got[("cw_resolution_goals_missed", d)] for d in (1, 2, 3)] == [0, 2, 0], "ticket 2 finished late and ticket 3 is open past its goal, both on the 2nd; ticket 4's goal is ahead"
    assert [got[("cw_time_seconds", d)] for d in (1, 2, 3)] == [5400 + 7596, 0, 900]
    pts = points_of(pages)
    assert len(pts) == 4 * 3 and {p.metric_key for p in pts} == set(METRICS) and all(type(p.value_numeric) is int and p.currency is None for p in pts)


def test_the_requests_carry_the_basic_string_the_client_id_and_forward_only_paging(server):
    seed(server)
    fetch(server)
    assert [r.path.rsplit("/", 2)[-2:] for r in server.requests] == [["service", "tickets"], ["time", "entries"]]
    expect = base64.b64encode(("%s+%s:%s" % (COMPANY, PUBLIC, PRIVATE)).encode()).decode()
    for r in server.requests:
        assert r.headers["authorization"] == "Basic " + expect
        assert r.headers["clientid"] == CLIENT
        assert r.headers["pagination-type"] == "forward-only"
        assert r.query == {"pageSize": "1000"}, "no conditions and no fields are sent: the grammar is on no saved page"


def test_conditions_the_operator_gives_are_passed_through_untouched(server):
    seed(server)
    fetch(server, ticket_conditions='closedFlag=false', time_conditions='timeStart > [2026-10-01T00:00:00Z]')
    queries = {r.path.rsplit("/", 2)[-2]: r.query for r in server.requests}
    assert queries["service"]["conditions"] == "closedFlag=false" and queries["time"]["conditions"] == "timeStart > [2026-10-01T00:00:00Z]"


def test_api_calls_counts_every_request(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 2


def test_pages_are_followed_by_the_link_header_as_given_and_every_record_is_counted_once(server):
    for i in range(1, 8):
        server.add_ticket(i, dt(1 + i % 3))
        server.add_time(i, dt(1 + i % 3), "1.0", ticket=i)
    server.page_cap = 3
    got = by_key(fetch(server))
    assert sum(got[("cw_tickets_opened", d)] for d in (1, 2, 3)) == 7 and sum(got[("cw_time_seconds", d)] for d in (1, 2, 3)) == 7 * 3600
    ticket_calls = [r for r in server.requests if r.path.endswith("/service/tickets")]
    assert len(ticket_calls) == 3 and [r.query.get("pageId") for r in ticket_calls] == [None, "3", "6"], "the next page url is used as given"
    assert all(r.query["pageSize"] == "3" for r in ticket_calls[1:]), "the size in the Link is not replaced by the connector's own"
    assert all(r.headers["pagination-type"] == "forward-only" and r.headers["clientid"] == CLIENT for r in server.requests), "every page asks the same way, not only the first"


def test_the_records_the_run_read_are_kept_for_the_ledger(server):
    seed(server)
    conn = make(server)
    run(collect(conn, START, END))
    assert [t.ticket_id for t in conn.tickets_read] == ["1", "2", "3", "4", "5"] and [e.entry_id for e in conn.time_read] == ["1", "2", "3"]


# ---------------------------------------------------------------- the ticket and time records

def test_a_ticket_becomes_a_record_with_the_fields_the_openapi_file_names(server):
    server.add_ticket(7, dt(1, 8), summary="  Disk space low on SRV-042 ", board="Service Board", ttype="Incident", priority="Priority 1 - Critical", status="In Progress",
                      company="Acme Co", resolved_at=dt(1, 12), responded_at=dt(1, 9), respond_goal=dt(1, 10), resolution_goal=dt(1, 16))
    [t] = run(_tickets(make(server)))
    assert (t.source, t.ticket_id, t.title, t.category, t.subcategory, t.priority, t.status, t.company) == (
        "connectwise", "7", "Disk space low on SRV-042", "Service Board", "Incident", "Priority 1 - Critical", "In Progress", "Acme Co")
    assert (t.opened_at, t.closed, t.closed_at, t.resolved_at, t.first_response_at, t.first_response_due_at, t.resolution_due_at) == (
        dt(1, 8), False, None, dt(1, 12), dt(1, 9), dt(1, 10), dt(1, 16))
    assert not t.is_open, "a ticket resolved but not closed is no longer backlog"


def test_a_ticket_with_no_type_priority_company_or_goal_has_none_for_each(server):
    server.add_ticket(8, dt(1), ttype=None, priority=None, company=None)
    [t] = run(_tickets(make(server)))
    assert (t.subcategory, t.priority, t.company, t.resolution_due_at, t.first_response_due_at) == (None, None, None, None, None) and t.is_open


def test_an_empty_summary_is_shown_as_no_summary_and_not_refused(server):
    server.add_ticket(9, dt(1), summary="   ")
    assert run(_tickets(make(server)))[0].title == "(no summary)"


def test_dates_with_an_offset_are_read_as_the_utc_instant_they_are(server):
    row = server.add_ticket(10, dt(1, 8))
    row["_info"]["dateEntered"] = "2026-10-01T13:00:00+05:00"                     # 08:00 UTC
    assert run(_tickets(make(server)))[0].opened_at == dt(1, 8)


def test_a_fraction_of_a_second_is_kept_to_the_microsecond_and_the_rest_is_dropped(server):
    row = server.add_time(1, dt(1, 8), "1.0")
    row["timeStart"] = "2026-10-01T08:00:00.123456789Z"
    assert run(_times(make(server)))[0].worked_on == dt(1, 8).replace(microsecond=123456)


def test_a_goal_met_to_the_second_is_not_missed_and_one_missed_by_a_second_is(server):
    server.add_ticket(1, dt(1), resolved_at=dt(2, 12), closed_at=dt(2, 13), resolution_goal=dt(2, 12))                      # resolved exactly at the goal
    server.add_ticket(2, dt(1), resolved_at=dt(2, 12).replace(second=1), closed_at=dt(2, 13), resolution_goal=dt(2, 12))   # one second late
    server.add_ticket(3, dt(1), resolution_goal=NOW)                                                                         # open, as_of exactly at the goal
    server.add_ticket(4, dt(1), resolution_goal=NOW - timedelta(seconds=1))                                                  # open, one second past
    got = by_key(fetch(server, start=date(2026, 10, 1), end=date(2026, 10, 12)))
    assert got[("cw_resolution_goals_missed", 2)] == 1 and got[("cw_resolution_goals_missed", 10)] == 1


def test_a_ticket_resolved_on_time_but_closed_late_met_its_goal(server):
    server.add_ticket(1, dt(1), resolved_at=dt(2, 11), closed_at=dt(5, 9), resolution_goal=dt(2, 12))
    assert by_key(fetch(server))[("cw_resolution_goals_missed", 2)] == 0


def test_time_entries_become_records_with_whole_seconds_and_a_ticket_or_none(server):
    server.add_time(1, dt(1, 9), "2.111", ticket=4, member="Kim Allen")
    server.add_time(2, dt(1, 9), 0.0005)
    a, b = run(_times(make(server)))
    assert (a.entry_id, a.ticket_id, a.technician, a.seconds, a.worked_on) == ("1", "4", "Kim Allen", 7600, dt(1, 9)), "2.111 h = 7599.6 s, rounded half up"
    assert (b.ticket_id, b.seconds) == (None, 2), "0.0005 h = 1.8 s"


def test_a_member_with_only_an_identifier_is_named_by_it_and_one_with_neither_is_refused(server):
    row = server.add_time(1, dt(1), "1.0")
    row["member"] = {"id": 9, "identifier": "KAllen"}
    assert run(_times(make(server)))[0].technician == "KAllen"
    row["member"] = {"id": 9}
    with pytest.raises(UpstreamBroken, match="names no member"):
        run(_times(make(server)))


# ---------------------------------------------------------------- what it refuses

def test_a_next_page_on_another_host_is_never_followed_and_the_keys_never_go_there(server):
    for i in range(1, 6):
        server.add_ticket(i, dt(1))
    server.page_cap = 2
    assert len(run(_tickets(make(server)))) == 5, "positive control: the same data pages through"
    server.requests.clear()
    server.next_override = "http://evil.example.test/v4_6_release/apis/3.0/service/tickets?pageId=2"
    with pytest.raises(UpstreamBroken, match="outside the ConnectWise API"):
        run(_tickets(make(server)))
    assert all(r.headers.get("host", "").startswith("127.0.0.1") for r in server.requests)


@pytest.mark.parametrize("bad", ["OTHER-PORT", "OTHER-PATH", "DOTDOT", "RELATIVE", "javascript:alert(1)", "PREFIX-ONLY"])
def test_a_next_page_that_is_not_this_host_under_the_api_path_is_refused(server, bad):
    for i in range(1, 6):
        server.add_ticket(i, dt(1))
    server.page_cap = 2
    assert len(run(_tickets(make(server)))) == 5, "positive control"
    server.next_override = {"OTHER-PORT": "http://127.0.0.1:1/v4_6_release/apis/3.0/service/tickets?pageId=2",
                            "OTHER-PATH": server.url + "/v4_6_release/apis/3.0x/service/tickets?pageId=2",
                            "DOTDOT": server.api_url + "/service/../../other?pageId=2",
                            "RELATIVE": "/v4_6_release/apis/3.0/service/tickets?pageId=2",
                            "PREFIX-ONLY": server.api_url}.get(bad, bad)
    with pytest.raises(UpstreamBroken, match="outside the ConnectWise API"):
        run(_tickets(make(server)))


def test_a_next_page_that_names_itself_is_a_loop_not_a_page(server):
    for i in range(1, 6):
        server.add_ticket(i, dt(1))
    server.page_cap = 2
    server.next_override = server.api_url + "/service/tickets?pageSize=2&page=1&pageId=2"
    with pytest.raises(UpstreamBroken, match="names itself|appears twice"):
        run(_tickets(make(server)))


def test_an_empty_page_that_links_to_itself_is_a_loop_and_is_named_one(server):
    assert run(_tickets(make(server))) == [], "positive control: no tickets is an empty list"
    server.loop_back = True
    with pytest.raises(UpstreamBroken, match="names itself"):
        run(_tickets(make(server)))


def test_a_last_page_that_links_back_to_the_first_is_a_changed_cursor_and_is_refused(server):
    for i in range(1, 4):
        server.add_ticket(i, dt(1))
    assert len(run(_tickets(make(server)))) == 3, "positive control"
    server.loop_back = True
    with pytest.raises(UpstreamBroken, match="appears twice|names itself"):
        run(_tickets(make(server)))


def test_a_body_cut_short_is_a_broken_answer_not_fewer_tickets(server):
    server.add_ticket(1, dt(1))
    assert len(run(_tickets(make(server)))) == 1, "positive control"
    server.faults.append(Fault(200, b'[{"id": 1, "summary": "x", "board": {"id": 1, "na'))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        run(_tickets(make(server)))


def test_a_list_that_is_an_object_is_refused(server):
    server.faults.append(Fault(200, {"items": []}))
    with pytest.raises(UpstreamBroken, match="not a list"):
        run(_tickets(make(server)))


def good(**kw):
    row = {"id": 1, "summary": "s", "board": {"id": 1, "name": "B"}, "status": {"id": 1, "name": "New"}, "closedFlag": False,
           "_info": {"dateEntered": "2026-10-02T09:00:00Z"}}
    row.update(kw)
    return row


@pytest.mark.parametrize("rows, match", [
    ([good(), good()], "appears twice"),
    ([good(id=True)], "no usable id"),
    ([good(id="7")], "no usable id"),
    (["x"], "no usable id"),
    ([good(_info=None)], "no _info.dateEntered"),
    ([good(_info={})], "no _info.dateEntered"),
    ([good(_info={"dateEntered": "2026-10-02T09:00:00"})], "no time zone"),
    ([good(_info={"dateEntered": "2026-10-02"})], "not an ISO 8601"),
    ([good(_info={"dateEntered": "2026-13-02T09:00:00Z"})], "not a calendar date"),
    ([good(_info={"dateEntered": "2026-10-02T09:00:00+25:00"})], "impossible zone offset"),
    ([good(_info={"dateEntered": "2026-10-02T09:00:00+05:75"})], "impossible zone offset"),
    ([good(_info={"dateEntered": 20261002})], "not a date and time"),
    ([good(closedFlag="yes")], "not true or false"),
    ([good(closedFlag=True)], "no closedDate"),
    ([good(closedFlag=True, closedDate="2026-10-01T09:00:00Z")], "closed before it opened"),
    ([good(board=None)], "board of ticket 1 is missing"),
    ([good(board={"id": 1})], "no name"),
    ([good(status=None)], "status of ticket 1 is missing"),
    ([good(summary=None)], "summary of ticket 1 is not text"),
    ([good(resolutionGoalUTC="soon")], "not an ISO 8601"),
    ([good(dateResolved="2026-10-01T09:00:00Z")], "resolved before it opened"),
])
def test_a_malformed_ticket_is_refused_for_its_own_reason_and_a_good_one_passes(server, rows, match):
    server.faults.append(Fault(200, [good()]))
    assert run(_tickets(make(server)))[0].ticket_id == "1", "positive control: the good ticket is read"
    server.faults.append(Fault(200, rows))
    with pytest.raises(UpstreamBroken, match=match):
        run(_tickets(make(server)))


@pytest.mark.parametrize("row, match", [
    ({"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00Z", "actualHours": 25}, "outside 0 to 24"),
    ({"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00Z", "actualHours": -1}, "outside 0 to 24"),
    ({"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00Z", "actualHours": "lots"}, "not a number"),
    ({"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00Z", "actualHours": None}, "not a number"),
    ({"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00", "actualHours": 1}, "no time zone"),
    ({"id": 1, "member": {"name": "K"}, "actualHours": 1}, "not a date and time"),
])
def test_a_malformed_time_entry_is_refused_for_its_own_reason_and_a_good_one_passes(server, row, match):
    ok = {"id": 1, "member": {"name": "K"}, "timeStart": "2026-10-02T09:00:00Z", "actualHours": 1.5}
    server.faults.append(Fault(200, [ok]))
    assert run(_times(make(server)))[0].seconds == 5400, "positive control"
    server.faults.append(Fault(200, [row]))
    with pytest.raises(UpstreamBroken, match=match):
        run(_times(make(server)))


def test_a_cursor_and_an_empty_window_are_refused(server):
    seed(server)
    assert fetch(server)
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="abc"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))


def test_a_clock_with_no_zone_is_refused(server):
    seed(server)
    with pytest.raises(ValueError, match="timezone aware"):
        run(collect(make(server, clock=lambda: datetime(2026, 10, 10)), START, END))


@pytest.mark.parametrize("kw, match", [
    ({"company_id": " "}, "company_id"),
    ({"public_key": ""}, "public_key"),
    ({"private_key": " "}, "private_key"),
    ({"client_id": ""}, "client_id"),
    ({"company_id": "a+b"}, "Basic string"),
    ({"company_id": "a:b"}, "Basic string"),
    ({"public_key": "a:b"}, "Basic string"),
])
def test_bad_options_are_refused_and_good_ones_pass(server, kw, match):
    run(make(server).aclose())
    with pytest.raises(ValueError, match=match):
        make(server, **kw)


@pytest.mark.parametrize("url", ["http://na.myconnectwise.net/v4_6_release/apis/3.0", "ftp://na.myconnectwise.net/v4_6_release/apis/3.0",
                                 "https://user:pw@na.myconnectwise.net/v4_6_release/apis/3.0", "https://na.myconnectwise.net",
                                 "https://na.myconnectwise.net/v4_6_release/../apis", "https://na.myconnectwise.net/a?x=1"])
def test_an_api_address_that_is_not_https_with_the_api_path_is_refused(url):
    with pytest.raises(ValueError):
        ConnectWiseConnector(api_url=url, company_id=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT)


def test_a_real_looking_https_address_is_accepted():
    conn = ConnectWiseConnector(api_url="https://na.myconnectwise.net/v4_6_release/apis/3.0/", company_id=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT)
    assert conn._base == "https://na.myconnectwise.net/v4_6_release/apis/3.0"


# ---------------------------------------------------------------- the current SLA times of an active ticket

def test_refresh_open_sla_reads_each_open_ticket_by_id_and_takes_its_current_goal(server):
    seed(server)
    server.fresh[3] = {"resolutionGoalUTC": "2026-10-30T09:00:00Z"}                         # the list's goal for ticket 3 was stale
    stale = {t.ticket_id: t for t in run(_tickets(make(server)))}
    assert stale["3"].resolution_due_at == dt(2, 20)
    server.requests.clear()
    fresh = {t.ticket_id: t for t in run(_tickets(make(server, refresh_open_sla=True)))}
    assert fresh["3"].resolution_due_at == dt(30, 9)
    assert sorted(r.path.rsplit("/", 1)[1] for r in server.requests if r.path.rsplit("/", 1)[1].isdigit()) == ["3", "4", "5"], "only the open tickets are re-read"
    assert fresh["1"].resolution_due_at == stale["1"].resolution_due_at


def test_refresh_changes_the_goals_missed_figure(server):
    seed(server)
    server.fresh[3] = {"resolutionGoalUTC": "2026-10-30T09:00:00Z"}
    assert by_key(fetch(server))[("cw_resolution_goals_missed", 2)] == 2
    assert by_key(fetch(server, refresh_open_sla=True))[("cw_resolution_goals_missed", 2)] == 1


def test_a_refresh_answer_for_another_ticket_is_refused(server):
    server.add_ticket(3, dt(2))
    assert run(_tickets(make(server, refresh_open_sla=True)))
    server.fresh[3] = {"id": 99}
    with pytest.raises(UpstreamBroken, match="not that ticket"):
        run(_tickets(make(server, refresh_open_sla=True)))


# ---------------------------------------------------------------- errors and retries

def test_a_refused_key_or_client_id_is_auth_revoked_and_a_good_one_verifies(server):
    assert run(_verify(make(server))) == "ConnectWise Manage company %s" % COMPANY
    for kw in ({"private_key": "wrong"}, {"client_id": "not-registered"}, {"company_id": "other"}):
        with pytest.raises(AuthRevoked):
            run(_verify(make(server, **kw)))


def test_a_429_with_a_short_retry_after_is_waited_for_once_and_then_succeeds(server):
    seed(server)
    sleeps = Sleeps()
    server.faults.append(Fault(429, {"code": "TooManyRequests"}, {"Retry-After": "7"}))
    pages = fetch(server, sleeps=sleeps)
    assert sleeps.calls == [7.0] and by_key(pages)[("cw_tickets_opened", 1)] == 2 and sum(p.throttle_waits for p in pages) == 1


def test_a_429_with_no_retry_after_backs_off_instead_of_assuming_a_figure(server):
    seed(server)
    sleeps = Sleeps()
    server.faults.append(Fault(429, {"code": "TooManyRequests"}))
    assert fetch(server, sleeps=sleeps)
    assert sleeps.calls == [1.0], "no saved page says ConnectWise sends Retry-After, so the retrier's own backoff is used"


def test_a_429_that_asks_for_longer_than_the_cap_is_handed_to_the_runner(server):
    seed(server)
    sleeps = Sleeps()
    server.faults.append(Fault(429, {}, {"Retry-After": "120"}))
    with pytest.raises(RateLimited) as caught:
        fetch(server, sleeps=sleeps)
    assert caught.value.retry_after_seconds == 120.0 and sleeps.calls == []


def test_a_server_error_is_retried_a_bounded_number_of_times(server):
    seed(server)
    server.faults += [Fault(503, {}), Fault(503, {})]
    assert fetch(server)
    server.faults += [Fault(503, {}) for _ in range(3)]
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        fetch(server)


def test_a_dropped_connection_is_retried(server):
    seed(server)
    server.faults.append(Fault(0, drop=True))
    assert by_key(fetch(server))[("cw_tickets_opened", 1)] == 2


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_statuses_are_upstream_broken_and_quote_only_the_status(server, status):
    server.faults.append(Fault(status, {"code": "X", "message": "secret detail " + PRIVATE}))
    with pytest.raises(UpstreamBroken) as caught:
        run(_verify(make(server)))
    assert "HTTP %d" % status in str(caught.value) and PRIVATE not in str(caught.value) and "secret detail" not in str(caught.value)


def test_the_keys_are_in_neither_the_repr_nor_the_error_text(server):
    conn = make(server, private_key=PRIVATE)
    assert PRIVATE not in repr(conn) and PUBLIC not in repr(conn) and PRIVATE not in repr(conn._basic)
    with pytest.raises(Exception):
        "%s" % format(conn._basic)
    run(conn.aclose())
    with pytest.raises(AuthRevoked) as caught:
        run(_verify(make(server, private_key="wrong-private")))
    assert "wrong-private" not in str(caught.value) and PUBLIC not in str(caught.value)


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

def oracle(tickets, entries, start, end, as_of):
    """What the four keys must be, worked out with plain loops and no code shared with the connector."""
    out = {}
    day = start
    while day < end:
        opened = closed = missed = secs = 0
        for t in tickets:
            if t["opened"].date() == day:
                opened += 1
            if t["closed"] is not None and t["closed"].date() == day:
                closed += 1
            if t["goal"] is not None and t["goal"].date() == day:
                finished = t["resolved"] if t["resolved"] is not None else t["closed"]
                if finished is not None:
                    missed += 1 if finished > t["goal"] else 0
                else:
                    missed += 1 if as_of > t["goal"] else 0
        for e in entries:
            if e["start"].date() == day:
                secs += int((e["hours"] * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        out[("cw_tickets_opened", day)] = opened
        out[("cw_tickets_closed", day)] = closed
        out[("cw_resolution_goals_missed", day)] = missed
        out[("cw_time_seconds", day)] = secs
        day += timedelta(days=1)
    return out


@pytest.mark.parametrize("seed_value", range(40))
def test_seeded_worlds_match_the_brute_force_oracle_over_pages_faults_zones_and_edges(seed_value):
    rng = random.Random(seed_value)
    with FakeConnectWise(company=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT) as s:
        start = date(2026, 9, 28) + timedelta(days=rng.randint(0, 5))
        end = start + timedelta(days=rng.randint(1, 9))
        tickets, entries = [], []
        for i in range(rng.randint(0, 40)):
            opened = datetime.combine(start + timedelta(days=rng.randint(-3, (end - start).days + 2)), datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=rng.randint(0, 1439))
            closed = opened + timedelta(minutes=rng.randint(0, 60 * 72)) if rng.random() < 0.5 else None
            resolved = None
            if rng.random() < 0.6:
                resolved = opened + timedelta(minutes=rng.randint(0, 60 * 48))
                if closed is not None and resolved > closed:
                    resolved = closed
            goal = opened + timedelta(minutes=rng.randint(0, 60 * 72)) if rng.random() < 0.8 else None
            row = s.add_ticket(100 + i, opened, closed_at=closed, resolved_at=resolved, resolution_goal=goal)
            zone = timezone(timedelta(hours=rng.choice([0, 0, 5, -7, 13])))
            row["_info"]["dateEntered"] = opened.astimezone(zone).isoformat()          # the same instant written with an offset
            if goal is not None and rng.random() < 0.5:
                row["resolutionGoalUTC"] = goal.astimezone(zone).isoformat()
            tickets.append({"opened": opened, "closed": closed, "resolved": resolved, "goal": goal})
        for i in range(rng.randint(0, 30)):
            started = datetime.combine(start + timedelta(days=rng.randint(-2, (end - start).days + 1)), datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=rng.randint(0, 1439))
            hours = Decimal(rng.randint(0, 2400)) / 100
            s.add_time(500 + i, started, hours, ticket=rng.choice([None, 100 + rng.randint(0, 20)]))
            entries.append({"start": started, "hours": hours})
        s.page_cap = rng.choice([1, 2, 3, 7, 1000])
        picks = sorted(rng.sample(range(1, 40), rng.randint(0, 5)))
        s.fail_on = {n for i, n in enumerate(picks) if i == 0 or n - picks[i - 1] > 1}
        conn = ConnectWiseConnector(api_url=s.api_url, company_id=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT, client=client(), retrier=retrier(),
                                    clock=lambda: NOW)
        got = {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(run(collect(conn, start, end)))}
        assert got == oracle(tickets, entries, start, end, NOW), "seed %d page_cap %d" % (seed_value, s.page_cap)


# ---------------------------------------------------------------- the registry and the migration

def test_the_connector_is_registered_with_its_four_metrics_and_two_secrets():
    spec = registry.get("connectwise")
    assert spec.supported_metrics == frozenset(METRICS) and registry.secret_keys("connectwise") == frozenset({"public_key", "private_key"})
    assert registry.redact("connectwise", {"company_id": "acme", "public_key": PUBLIC, "private_key": PRIVATE})["private_key"] == "***"
    built = registry.build("connectwise", api_url="https://na.myconnectwise.net/v4_6_release/apis/3.0", company_id=COMPANY, public_key=PUBLIC, private_key=PRIVATE, client_id=CLIENT)
    assert isinstance(built, ConnectWiseConnector)


def test_migration_025_names_every_key_with_its_unit_and_no_money_key():
    sql = (ROOT / "migrations" / "025_connectwise_metrics.sql").read_text(encoding="utf-8")
    for key in METRICS:
        assert re.search(r"\('%s'," % key, sql), key
    assert "'seconds'" in sql.split("'cw_time_seconds'")[1].split(")")[0] and "currency" not in sql.split("insert into")[1]


def test_the_connector_makes_no_claim_it_cannot_back_in_its_own_text():
    text = (ROOT / "app" / "connectors" / "connectwise.py").read_text(encoding="utf-8")
    assert "NEVER run against a live" in text.upper() or "Nothing here ran against a" in text
    assert "third party" in text
