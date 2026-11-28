"""The Autotask connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the requests the connector builds (the zone lookup first and with no credential, the three credential headers, the query with its
window, `MaxRecords` and `IncludeFields`), how it reads the documented answers (`items`, `pageDetails`, the picklists), the arithmetic (hours to whole seconds, the UTC day, the resolution
goals missed), the pacing from `ThresholdInformation`, what it refuses (a zone or a next page that is not Autotask's API, a count that does not match, a malformed ticket), the retry and
error handling, and that no credential reaches a message. They do NOT prove Autotask accepts any of it: the server is written from the saved pages (docs/MSP_SOURCES.md) and no live
database was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import json
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.autotask import ID_CHUNK, MAX_RECORDS, METRICS, MIN_CALLS_LEFT, AutotaskConnector  # noqa: E402
from app.connectors.autotask_fake import FakeAutotask  # noqa: E402
from app.connectors.base import AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

USER, SECRET, CODE = "api@example.com", "SECRET-password-1", "CODE-ABCDEFGHIJKLMNOPQR"
START, END = date(2026, 10, 1), date(2026, 10, 4)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def dt(day: int, hour: int = 9, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture()
def server():
    with FakeAutotask(username=USER, secret=SECRET, code=CODE) as fake:
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("zone_lookup_url", server.url)
    return AutotaskConnector(username=kw.pop("username", USER), secret=kw.pop("secret", SECRET), integration_code=kw.pop("integration_code", CODE), client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


async def _call(conn, name, **kw):
    try:
        return await getattr(conn, name)(**kw)
    finally:
        await conn.aclose()


def tickets_of(server, start=START, end=END, **kw):
    return run(_call(make(server, **kw), "tickets", window_start=start, window_end=end))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn.aclose()


def seed(server):
    server.add_ticket(1, dt(1), completed=dt(2, 15), resolved=dt(2, 14), resolved_due=dt(2, 18))          # on time
    server.add_ticket(2, dt(1, 10), completed=dt(3, 9), resolved=dt(3, 8), resolved_due=dt(2, 12))        # a day late: the goal fell on the 2nd
    server.add_ticket(3, dt(2, 8), resolved_due=dt(2, 20))                                                 # open, goal passed
    server.add_ticket(4, dt(3, 8), resolved_due=dt(20, 9))                                                 # open, goal ahead
    server.add_ticket(5, dt(3, 9))                                                                         # open, no goal
    server.add_time(1, dt(1, 9), "1.5", ticket=1)
    server.add_time(2, dt(1, 13), "2.11", ticket=2)
    server.add_time(3, dt(3, 10), "0.25")                                                                  # not on a ticket


# ---------------------------------------------------------------- the happy path

def test_four_counts_per_day_are_written_with_no_currency(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("at_tickets_opened", d)] for d in (1, 2, 3)] == [2, 1, 2]
    assert [got[("at_tickets_closed", d)] for d in (1, 2, 3)] == [0, 1, 1]
    assert [got[("at_resolution_goals_missed", d)] for d in (1, 2, 3)] == [0, 2, 0]
    assert [got[("at_time_seconds", d)] for d in (1, 2, 3)] == [5400 + 7596, 0, 900]
    pts = points_of(pages)
    assert len(pts) == 4 * 3 and {p.metric_key for p in pts} == set(METRICS) and all(type(p.value_numeric) is int and p.currency is None for p in pts)


def test_the_zone_is_asked_first_with_no_credential_and_every_other_request_carries_the_three_headers(server):
    seed(server)
    fetch(server)
    first = server.requests[0]
    assert first.path == "/atservicesrest/v1.0/zoneInformation" and first.query == {"user": USER}
    assert not {"username", "secret", "apiintegrationcode"} & set(first.headers), "the zone lookup needs no authentication, so none is sent"
    assert [r.path.replace("/atservicesrest/v1.0", "") for r in server.requests[1:]] == [
        "/ThresholdInformation", "/Tickets/entityInformation/fields", "/Tickets/query", "/TimeEntries/query"]
    for r in server.requests[1:]:
        assert (r.headers["username"], r.headers["secret"], r.headers["apiintegrationcode"], r.headers["content-type"]) == (USER, SECRET, CODE, "application/json")


def test_the_ticket_query_names_the_window_the_page_size_and_the_fields_it_needs(server):
    seed(server)
    fetch(server)
    search = json.loads(next(r for r in server.requests if r.path.endswith("/Tickets/query")).query["search"])
    window = lambda f: {"op": "and", "items": [{"op": "gte", "field": f, "value": "2026-10-01T00:00:00.000"}, {"op": "lt", "field": f, "value": "2026-10-04T00:00:00.000"}]}  # noqa: E731
    assert search["MaxRecords"] == MAX_RECORDS == 500
    assert search["filter"] == [{"op": "or", "items": [window("createDate"), window("completedDate"), window("resolvedDueDateTime"), {"op": "notExist", "field": "completedDate"}]}]
    assert "id" in [f.lower() for f in search["IncludeFields"]], "results over a page must name Id as a field"
    assert "description" not in search["IncludeFields"] and "resolution" not in search["IncludeFields"], "the rich text fields are never read"
    time_search = json.loads(next(r for r in server.requests if r.path.endswith("/TimeEntries/query")).query["search"])
    assert time_search["filter"] == [window("dateWorked")] and time_search["MaxRecords"] == 500 and "id" in time_search["IncludeFields"]


def test_the_resolution_field_option_changes_the_goal_the_query_and_the_record_use(server):
    server.add_ticket(1, dt(1), resolved_due=dt(2, 12), due=dt(2, 20))
    default = tickets_of(server)[0]
    other = tickets_of(server, resolution_due_field="dueDateTime")[0]
    assert (default.resolution_due_at, other.resolution_due_at) == (dt(2, 12), dt(2, 20))
    assert "dueDateTime" in next(r for r in server.requests[::-1] if r.path.endswith("/Tickets/query")).query["search"]
    with pytest.raises(ValueError, match="resolution_due_field"):
        make(server, resolution_due_field="title")


def test_api_calls_counts_every_request_including_the_zone_and_threshold_calls(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 5, "zone, threshold, picklists, tickets, time"


def test_with_an_api_url_given_no_zone_lookup_is_made(server):
    seed(server)
    fetch(server, api_url=server.api_url)
    assert all(not r.path.endswith("zoneInformation") for r in server.requests)


def test_pages_are_followed_by_next_page_url_as_given_and_every_record_is_counted_once(server):
    for i in range(1, 8):
        server.add_ticket(i, dt(1 + i % 3))
        server.add_time(i, dt(1 + i % 3), "1.0", ticket=i)
    server.page_cap = 3
    got = by_key(fetch(server))
    assert sum(got[("at_tickets_opened", d)] for d in (1, 2, 3)) == 7 and sum(got[("at_time_seconds", d)] for d in (1, 2, 3)) == 7 * 3600
    ticket_calls = [r for r in server.requests if "/Tickets/query" in r.path]
    assert [r.path.rsplit("/", 1)[1] for r in ticket_calls] == ["query", "next", "next"]
    assert len({r.query["search"] for r in ticket_calls}) == 1, "the search is never changed while paging"
    assert all("paging" in r.query for r in ticket_calls[1:])


def test_the_records_the_run_read_are_kept_for_the_ledger(server):
    seed(server)
    conn = make(server)
    run(collect(conn, START, END))
    assert [t.ticket_id for t in conn.tickets_read] == ["1", "2", "3", "4", "5"] and [e.entry_id for e in conn.time_read] == ["1", "2", "3"]


# ---------------------------------------------------------------- the zone

def test_a_zone_answer_that_is_not_autotask_is_refused_and_a_good_one_passes(server):
    seed(server)
    assert fetch(server), "positive control: this server names itself, and it is a loopback stand-in asked through a loopback lookup"
    for bad in ["http://evil.example.test/atservicesrest/", "https://evil.example.com/atservicesrest/", "https://webservices3.autotask.net.evil.com/atservicesrest/",
                "https://autotask.net.evil.com/atservicesrest/", "https://webservices3.evilautotask.net/atservicesrest/", "https://user:pw@webservices3.autotask.net/atservicesrest/", "https://webservices3.autotask.net/other/",
                "https://webservices3.autotask.net/atservicesrest/?x=1", "http://webservices3.autotask.net/atservicesrest/", "ftp://webservices3.autotask.net/atservicesrest/"]:
        server.zone_url = bad.rstrip("/")
        server.requests.clear()
        with pytest.raises(UpstreamBroken, match="zone answer"):
            fetch(server)
        assert len(server.requests) == 1, "nothing with a credential was sent after a refused zone: %r" % bad


def test_an_answer_to_verify_that_is_not_an_entity_description_is_refused(server):
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, {"info": {"canQuery": True}}))
    with pytest.raises(UpstreamBroken, match="not an entity description"):
        run(_verify(make(server)))
    assert run(_verify(make(server))), "positive control"


def test_a_zone_with_no_url_is_refused(server):
    server.faults.append(Fault(200, {"zoneName": "x"}))
    with pytest.raises(UpstreamBroken, match="names no url"):
        fetch(server)


def test_the_default_lookup_is_the_webservices_host_and_the_answer_is_used_for_every_call():
    """No loopback here: a mock transport answers for the real names, so the connector's own defaults are what is checked."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers)))
        if request.url.host == "webservices.autotask.net":
            return httpx.Response(200, json={"zoneName": "America East", "url": "https://webservices3.autotask.net/atservicesrest/", "webUrl": "https://ww3.autotask.net/", "ci": 1})
        return httpx.Response(200, json={"info": {"name": "Ticket"}})

    conn = AutotaskConnector(username=USER, secret=SECRET, integration_code=CODE, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), retrier=retrier())
    assert run(_verify(conn)) == "Autotask API user %s" % USER
    assert seen[0][0] == "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation?user=api%40example.com"
    assert seen[1][0] == "https://webservices3.autotask.net/atservicesrest/v1.0/Tickets/entityInformation" and seen[1][1]["secret"] == SECRET


def test_a_loopback_zone_is_refused_when_the_lookup_was_not_a_loopback_stand_in():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"zoneName": "x", "url": "http://127.0.0.1:9/atservicesrest/", "webUrl": "", "ci": 1})

    conn = AutotaskConnector(username=USER, secret=SECRET, integration_code=CODE, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), retrier=retrier())
    with pytest.raises(UpstreamBroken, match="zone answer"):
        run(_verify(conn))


# ---------------------------------------------------------------- the pacing

def test_the_sync_stops_before_a_call_when_the_hours_requests_are_nearly_spent(server):
    seed(server)
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 10000 - MIN_CALLS_LEFT}))
    assert fetch(server), "positive control: exactly MIN_CALLS_LEFT left is enough"
    for left in (MIN_CALLS_LEFT - 1, 0, -5):
        server.requests.clear()
        server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
        server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 10000 - left}))
        with pytest.raises(RateLimited, match="external requests this hour"):
            fetch(server)
        assert [r.path.rsplit("/", 1)[1] for r in server.requests] == ["zoneInformation", "ThresholdInformation"], "nothing was queried"


def test_the_threshold_is_read_from_the_platform_and_not_assumed(server):
    seed(server)
    server.threshold = 500                                                              # a smaller database limit than the page's 10,000
    server.used = 400
    with pytest.raises(RateLimited):
        fetch(server)
    server.used = 100
    assert fetch(server)


@pytest.mark.parametrize("body", [{}, {"externalRequestThreshold": 10000}, {"externalRequestThreshold": "10000", "currentTimeframeRequestCount": 1},
                                  {"externalRequestThreshold": 0, "currentTimeframeRequestCount": 0}, {"externalRequestThreshold": 10000, "currentTimeframeRequestCount": -1},
                                  {"externalRequestThreshold": True, "currentTimeframeRequestCount": 1}, [1, 2]])
def test_a_threshold_answer_that_is_not_whole_numbers_is_refused(server, body):
    seed(server)
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match="ThresholdInformation"):
        fetch(server)


# ---------------------------------------------------------------- picklists and the ticket record

def test_a_ticket_becomes_a_record_with_labels_from_the_picklists(server):
    server.add_ticket(7, dt(1, 8), title="  Disk space low on SRV-042 ", category=2, status=8, priority=1, issue=11, company=42, resolved=dt(1, 12), first_response=dt(1, 9),
                      first_response_due=dt(1, 10), resolved_due=dt(1, 16))
    [t] = tickets_of(server)
    assert (t.source, t.ticket_id, t.title, t.category, t.subcategory, t.priority, t.status, t.company) == (
        "autotask", "7", "Disk space low on SRV-042", "RMM Alerts", "Network", "High", "In Progress", "company 42")
    assert (t.opened_at, t.closed, t.resolved_at, t.first_response_at, t.first_response_due_at, t.resolution_due_at) == (dt(1, 8), False, dt(1, 12), dt(1, 9), dt(1, 10), dt(1, 16))
    assert not t.is_open


def test_a_completed_ticket_is_closed_on_its_completed_date_and_no_status_number_is_assumed_to_mean_it(server):
    row = server.add_ticket(8, dt(1), completed=dt(2, 9))
    row["status"] = 77                                                                   # a status the picklist does not know: completion is the date
    [t] = tickets_of(server)
    assert t.closed and t.closed_at == dt(2, 9) and t.status == "status 77"


def test_a_value_the_picklist_does_not_hold_is_named_by_its_number_not_invented(server):
    server.add_ticket(1, dt(1), category=99, priority=9, issue=98)
    [t] = tickets_of(server)
    assert (t.category, t.priority, t.subcategory) == ("ticketCategory 99", "priority 9", "issueType 98")


def test_a_ticket_with_no_category_priority_issue_or_company_has_none_or_a_label_for_each(server):
    server.add_ticket(1, dt(1), category=None, priority=None, issue=None, company=None)
    [t] = tickets_of(server)
    assert (t.category, t.priority, t.subcategory, t.company) == ("(no category)", None, None, None)


def test_a_goal_met_to_the_second_is_not_missed_and_one_missed_by_a_second_is(server):
    server.add_ticket(1, dt(1), resolved=dt(2, 12), completed=dt(2, 13), resolved_due=dt(2, 12))                                   # resolved exactly at the goal
    server.add_ticket(2, dt(1), resolved=dt(2, 12).replace(second=1), completed=dt(2, 13), resolved_due=dt(2, 12))                 # one second late
    server.add_ticket(3, dt(1), resolved_due=NOW)                                                                                    # open, as_of exactly at the goal
    server.add_ticket(4, dt(1), resolved_due=NOW - timedelta(seconds=1))                                                             # open, one second past
    got = by_key(fetch(server, start=date(2026, 10, 1), end=date(2026, 10, 12)))
    assert got[("at_resolution_goals_missed", 2)] == 1 and got[("at_resolution_goals_missed", 10)] == 1


def test_a_ticket_resolved_on_time_but_completed_late_met_its_goal(server):
    server.add_ticket(1, dt(1), resolved=dt(2, 11), completed=dt(5, 9), resolved_due=dt(2, 12))
    assert by_key(fetch(server))[("at_resolution_goals_missed", 2)] == 0


def test_a_ticket_completed_after_the_window_whose_goal_fell_inside_it_is_a_missed_goal_on_the_goals_day(server):
    server.add_ticket(1, dt(1, month=9), completed=dt(20), resolved_due=dt(2, 12))
    got = by_key(fetch(server))
    assert got[("at_resolution_goals_missed", 2)] == 1 and got[("at_tickets_opened", 2)] == 0 and sum(v for (k, d), v in got.items() if k == "at_tickets_closed") == 0


def test_a_blank_title_is_shown_as_no_title_and_not_refused(server):
    server.add_ticket(1, dt(1), title="  ")
    assert tickets_of(server)[0].title == "(no title)"


def test_a_picklist_label_is_trimmed_and_an_empty_one_falls_back_to_the_number(server):
    server.picklists["priority"][0]["label"] = "  High  "
    server.picklists["priority"][1]["label"] = ""
    server.add_ticket(1, dt(1), priority=1)
    server.add_ticket(2, dt(1), priority=2)
    a, b = tickets_of(server)
    assert (a.priority, b.priority) == ("High", "priority 2")


def test_dates_with_no_zone_are_read_as_utc_because_the_page_says_so_and_a_zone_is_honoured(server):
    row = server.add_ticket(1, dt(1, 8))
    assert tickets_of(server)[0].opened_at == dt(1, 8)
    row["createDate"] = "2026-10-01T13:00:00+05:00"
    assert tickets_of(server)[0].opened_at == dt(1, 8)
    row["createDate"] = "2026-10-01T08:00:00Z"
    assert tickets_of(server)[0].opened_at == dt(1, 8)


def test_a_picklist_answer_that_is_malformed_is_refused(server):
    seed(server)
    assert fetch(server), "positive control"
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 1}))
    server.faults.append(Fault(200, {"fields": "none"}))
    with pytest.raises(UpstreamBroken, match="no fields list"):
        fetch(server)
    for bad in ({"name": "status", "isPickList": True, "picklistValues": None}, {"name": "status", "isPickList": True, "picklistValues": [{"value": 1, "label": "x"}]},
                {"name": "status", "isPickList": True, "picklistValues": [{"value": "1"}]}):
        server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
        server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 1}))
        server.faults.append(Fault(200, {"fields": [bad]}))
        with pytest.raises(UpstreamBroken, match="picklist"):
            fetch(server)


# ---------------------------------------------------------------- what it refuses

def _wrap(items, **extra):
    page = {"items": items, "pageDetails": {"count": len(items), "requestCount": 500, "prevPageUrl": None, "nextPageUrl": None}}
    page["pageDetails"].update(extra)
    return page


def good(**kw):
    row = {"id": 1, "title": "t", "createDate": "2026-10-02T09:00:00.000", "completedDate": None, "status": 1}
    row.update(kw)
    return row


def stage(server, ticket_page):
    """Answer the zone, threshold and picklist calls from the stand-in and the first ticket query with `ticket_page`."""
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 1}))
    server.faults.append(Fault(200, {"fields": [{"name": "status", "isPickList": True, "picklistValues": server.picklists["status"]}]}))
    server.faults.append(Fault(200, ticket_page))
    server.faults.append(Fault(200, _wrap([])))                                           # no time entries


@pytest.mark.parametrize("rows, match", [
    ([good(), good()], "appears twice"),
    ([good(id=True)], "no usable id"),
    ([good(id="7")], "no usable id"),
    (["x"], "no usable id"),
    ([good(createDate=None)], "has no createDate"),
    ([good(createDate="2026-10-02")], "not an ISO 8601"),
    ([good(createDate="2026-13-02T09:00:00")], "not a calendar date"),
    ([good(createDate=20261002)], "not a date and time"),
    ([good(completedDate="yesterday")], "not an ISO 8601"),
    ([good(completedDate="2026-10-01T09:00:00")], "closed before it opened"),
    ([good(resolvedDateTime="2026-10-01T09:00:00")], "resolved before it opened"),
    ([good(title=None)], "title of ticket 1 is not text"),
    ([good(status=None)], "ticket 1 has no status"),
    ([good(status="open")], "not a picklist number"),
    ([good(status=True)], "not a picklist number"),
    ([good(companyID="acme")], "companyID of ticket 1 is not a number"),
    ([good(resolvedDueDateTime="soon")], "not an ISO 8601"),
])
def test_a_malformed_ticket_is_refused_for_its_own_reason_and_a_good_one_passes(server, rows, match):
    stage(server, _wrap([good()]))
    assert fetch(server)
    stage(server, _wrap(rows))
    with pytest.raises(UpstreamBroken, match=match):
        fetch(server)


@pytest.mark.parametrize("row, match", [
    ({"id": 1, "resourceID": 7, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": 25}, "outside 0 to 24"),
    ({"id": 1, "resourceID": 7, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": -1}, "outside 0 to 24"),
    ({"id": 1, "resourceID": 7, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": "lots"}, "not a number"),
    ({"id": 1, "resourceID": 7, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": None}, "not a number"),
    ({"id": 1, "resourceID": 7, "hoursWorked": 1}, "not a date and time"),
    ({"id": 1, "resourceID": None, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": 1}, "names no resource"),
    ({"id": 1, "resourceID": "7", "dateWorked": "2026-10-02T00:00:00", "hoursWorked": 1}, "names no resource"),
    ({"id": 1, "resourceID": 7, "ticketID": "x", "dateWorked": "2026-10-02T00:00:00", "hoursWorked": 1}, "ticketID of time entry 1 is not a number"),
])
def test_a_malformed_time_entry_is_refused_for_its_own_reason_and_a_good_one_passes(server, row, match):
    ok = {"id": 1, "resourceID": 7, "ticketID": None, "dateWorked": "2026-10-02T00:00:00", "hoursWorked": 1.5}

    def staged(entry):
        server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
        server.faults.append(Fault(200, {"externalRequestThreshold": 10000, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": 1}))
        server.faults.append(Fault(200, {"fields": []}))
        server.faults.append(Fault(200, _wrap([])))
        server.faults.append(Fault(200, _wrap([entry])))

    staged(ok)
    assert by_key(fetch(server))[("at_time_seconds", 2)] == 5400, "positive control"
    staged(row)
    with pytest.raises(UpstreamBroken, match=match):
        fetch(server)


def test_a_next_page_that_is_not_this_host_under_the_api_path_is_refused_and_the_credentials_never_go_there(server):
    for i in range(1, 6):
        server.add_ticket(i, dt(1))
    server.page_cap = 2
    assert len(tickets_of(server)) == 5, "positive control: the same data pages through"
    base = server.url
    bad = ["http://evil.example.test/atservicesrest/v1.0/Tickets/query/next?x=1", "http://127.0.0.1:1/atservicesrest/v1.0/Tickets/query/next?x=1",
           base + "/atservicesrest/v1.0x/Tickets/query/next?x=1", base + "/atservicesrest/v1.0/../v2/Tickets", "/atservicesrest/v1.0/Tickets/query/next?x=1",
           base + "/other", "javascript:alert(1)"]
    for url in bad:
        server.next_override = url
        server.requests.clear()
        with pytest.raises(UpstreamBroken, match="outside the Autotask API"):
            tickets_of(server)
        assert all(r.headers.get("host", "").startswith("127.0.0.1") for r in server.requests), url
        assert not any("evil" in r.headers.get("host", "") for r in server.requests)


def test_a_next_page_that_names_itself_is_a_loop_and_is_named_one(server):
    server.add_ticket(1, dt(1))
    assert len(tickets_of(server)) == 1, "positive control"
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, {"fields": []}))
    again = server.api_url + "/v1.0/Tickets/query/next?paging=%7B%7D&search=%7B%7D"
    server.faults.append(Fault(200, _wrap([], nextPageUrl=again)))
    server.faults.append(Fault(200, _wrap([], nextPageUrl=again)))
    server.faults.append(Fault(200, _wrap([], nextPageUrl=again)))
    with pytest.raises(UpstreamBroken, match="names itself"):
        tickets_of(server)


def test_a_count_that_does_not_match_what_arrived_is_a_broken_answer(server):
    stage(server, _wrap([good()]))
    assert fetch(server), "positive control"
    stage(server, _wrap([good()], count=2))
    with pytest.raises(UpstreamBroken, match="pageDetails says 2 records and 1 arrived"):
        fetch(server)


def test_a_page_larger_than_the_page_size_asked_for_is_refused(server):
    rows = [good(id=i) for i in range(1, MAX_RECORDS + 2)]
    stage(server, _wrap(rows[:MAX_RECORDS]))
    assert fetch(server), "positive control: a full page of 500 is fine"
    stage(server, _wrap(rows))
    with pytest.raises(UpstreamBroken, match="more than the 500"):
        fetch(server)


@pytest.mark.parametrize("body", [[], {"items": []}, {"pageDetails": {}}, {"items": "x", "pageDetails": {}}, {"items": [], "pageDetails": "x"}])
def test_an_answer_without_items_and_page_details_is_refused(server, body):
    stage(server, _wrap([good()]))
    assert fetch(server)
    stage(server, body)
    with pytest.raises(UpstreamBroken, match="no items list and pageDetails"):
        fetch(server)


def test_a_body_that_is_not_json_is_a_broken_answer(server):
    seed(server)
    assert fetch(server), "positive control"
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(200, b'{"externalRequestThreshold": 100'))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        fetch(server)


def test_a_query_over_one_page_that_leaves_id_out_would_be_refused_by_the_server_and_the_connector_never_does(server):
    for i in range(1, 8):
        server.add_ticket(i, dt(2))
    server.page_cap = 3
    assert len(tickets_of(server)) == 7, "the stand-in answers 400 to a multi page query without Id; reaching seven tickets shows the connector names it"


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
    ({"username": " "}, "username"),
    ({"username": "nobody"}, "email address"),
    ({"secret": ""}, "secret"),
    ({"integration_code": " "}, "integration_code"),
])
def test_bad_options_are_refused_and_good_ones_pass(server, kw, match):
    run(make(server).aclose())
    with pytest.raises(ValueError, match=match):
        make(server, **kw)


@pytest.mark.parametrize("kw", [{"api_url": "http://webservices3.autotask.net/atservicesrest"}, {"api_url": "https://user:pw@webservices3.autotask.net/atservicesrest"},
                                {"api_url": "https://webservices3.autotask.net"}, {"api_url": "https://webservices3.autotask.net/a/../b"},
                                {"zone_lookup_url": "http://webservices.autotask.net"}, {"zone_lookup_url": "https://webservices.autotask.net/atservicesrest"},
                                {"zone_lookup_url": "ftp://webservices.autotask.net"}])
def test_an_address_that_is_not_https_or_not_plain_is_refused(kw):
    with pytest.raises(ValueError):
        AutotaskConnector(username=USER, secret=SECRET, integration_code=CODE, **kw)


def test_a_real_looking_address_is_accepted():
    conn = AutotaskConnector(username=USER, secret=SECRET, integration_code=CODE, api_url="https://webservices3.autotask.net/atservicesrest/")
    assert conn._api_root == "https://webservices3.autotask.net/atservicesrest"


def test_from_env_reads_the_three_credentials_and_the_stand_in_addresses(server):
    conn = AutotaskConnector.from_env({"AUTOTASK_USERNAME": USER, "AUTOTASK_SECRET": SECRET, "AUTOTASK_INTEGRATION_CODE": CODE, "AUTOTASK_API_URL": server.api_url,
                                       "AUTOTASK_ZONE_LOOKUP_URL": server.url})
    assert conn.external_account == USER and conn._api_root == server.api_url
    with pytest.raises(ValueError, match="AUTOTASK_SECRET"):
        AutotaskConnector.from_env({"AUTOTASK_USERNAME": USER, "AUTOTASK_INTEGRATION_CODE": CODE})


# ---------------------------------------------------------------- time on a ticket the window query did not return

def test_time_on_an_older_completed_ticket_is_matched_to_it_by_an_id_query_in_chunks(server):
    for i in range(1, 251):
        server.add_ticket(i, dt(1, month=8), completed=dt(2, month=8))                  # old and complete: no window branch returns them
        server.add_time(i, dt(2), "1.0", ticket=i)
    server.add_ticket(900, dt(2), resolved_due=dt(2, 12))                               # in the window
    conn = make(server)
    pages = run(collect(conn, START, END))
    assert len(conn.tickets_read) == 251 and len([r for r in server.requests if r.path.endswith("/Tickets/query")]) == 1 + 3
    in_queries = [json.loads(r.query["search"])["filter"][0]["value"] for r in server.requests if r.path.endswith("/Tickets/query") and '"in"' in r.query["search"]]
    assert [len(v) for v in in_queries] == [ID_CHUNK, ID_CHUNK, 50] and sorted(sum(in_queries, [])) == list(range(1, 251))
    got = by_key(pages)
    assert got[("at_tickets_opened", 2)] == 1 and got[("at_tickets_opened", 1)] == 0, "the old tickets are records, not days of this window"
    assert got[("at_time_seconds", 2)] == 250 * 3600


def test_time_on_a_ticket_that_cannot_be_found_stays_unmatched_and_is_not_invented(server):
    server.add_time(1, dt(2), "1.0", ticket=424242)
    conn = make(server)
    run(collect(conn, START, END))
    assert conn.tickets_read == [] and [e.ticket_id for e in conn.time_read] == ["424242"]


# ---------------------------------------------------------------- errors and retries

def test_a_refused_credential_is_auth_revoked_and_a_good_one_verifies(server):
    assert run(_verify(make(server))) == "Autotask API user %s" % USER
    for kw in ({"secret": "wrong"}, {"integration_code": "not-this-one"}):
        with pytest.raises(AuthRevoked):
            run(_verify(make(server, **kw)))


def test_a_429_with_no_retry_after_backs_off_and_one_with_a_short_one_waits_for_it(server):
    seed(server)
    sleeps = Sleeps()
    server.faults += [Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}), Fault(429, {"errors": ["thread count exceeded"]})]
    assert fetch(server, sleeps=sleeps) and sleeps.calls == [1.0], "no saved page says Retry-After is sent, so the retrier's own backoff is used"
    sleeps = Sleeps()
    server.faults += [Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}), Fault(429, {}, {"Retry-After": "7"})]
    pages = fetch(server, sleeps=sleeps)
    assert sleeps.calls == [7.0] and sum(p.throttle_waits for p in pages) == 1


def test_a_429_that_asks_for_longer_than_the_cap_is_handed_to_the_runner(server):
    seed(server)
    sleeps = Sleeps()
    server.faults += [Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}), Fault(429, {}, {"Retry-After": "120"})]
    with pytest.raises(RateLimited) as caught:
        fetch(server, sleeps=sleeps)
    assert caught.value.retry_after_seconds == 120.0 and sleeps.calls == []


def test_a_server_error_is_retried_a_bounded_number_of_times(server):
    seed(server)
    server.fail_on = {1, 2}
    assert fetch(server)
    server.fail_on = {1, 2, 3}
    server.answered = 0
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        fetch(server)


def test_a_dropped_connection_is_retried(server):
    seed(server)
    server.faults.append(Fault(0, drop=True))
    assert by_key(fetch(server))[("at_tickets_opened", 1)] == 2


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_statuses_are_upstream_broken_and_quote_only_the_status(server, status):
    server.faults.append(Fault(200, {"zoneName": "x", "url": server.api_url + "/", "webUrl": "", "ci": 1}))
    server.faults.append(Fault(status, {"errors": ["secret detail " + SECRET]}))
    with pytest.raises(UpstreamBroken) as caught:
        run(_verify(make(server)))
    assert "HTTP %d" % status in str(caught.value) and SECRET not in str(caught.value) and "secret detail" not in str(caught.value)


def test_the_secret_and_the_code_are_in_neither_the_repr_nor_the_error_text(server):
    conn = make(server)
    assert SECRET not in repr(conn) and CODE not in repr(conn) and SECRET not in repr(conn._secret) and CODE not in repr(conn._code)
    with pytest.raises(Exception):
        "%s" % format(conn._secret)
    run(conn.aclose())
    with pytest.raises(AuthRevoked) as caught:
        run(_verify(make(server, secret="wrong-secret", integration_code="wrong-code-1")))
    assert "wrong-secret" not in str(caught.value) and "wrong-code-1" not in str(caught.value)


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

def oracle(tickets, entries, start, end, as_of):
    """What the four keys must be, worked out with plain loops and no code shared with the connector."""
    out = {}
    day = start
    while day < end:
        opened = closed = missed = secs = 0
        for t in tickets:
            if t["created"].date() == day:
                opened += 1
            if t["completed"] is not None and t["completed"].date() == day:
                closed += 1
            if t["goal"] is not None and t["goal"].date() == day:
                finished = t["resolved"] if t["resolved"] is not None else t["completed"]
                missed += (1 if finished > t["goal"] else 0) if finished is not None else (1 if as_of > t["goal"] else 0)
        for e in entries:
            if e["worked"].date() == day:
                secs += int((e["hours"] * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        out[("at_tickets_opened", day)] = opened
        out[("at_tickets_closed", day)] = closed
        out[("at_resolution_goals_missed", day)] = missed
        out[("at_time_seconds", day)] = secs
        day += timedelta(days=1)
    return out


@pytest.mark.parametrize("seed_value", range(40))
def test_seeded_worlds_match_the_brute_force_oracle_over_pages_faults_edges_and_old_tickets(seed_value):
    rng = random.Random(seed_value)
    with FakeAutotask(username=USER, secret=SECRET, code=CODE) as s:
        start = date(2026, 9, 28) + timedelta(days=rng.randint(0, 5))
        end = start + timedelta(days=rng.randint(1, 9))
        base = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        tickets, entries = [], []
        for i in range(rng.randint(0, 40)):
            created = base + timedelta(days=rng.randint(-40, (end - start).days + 3), minutes=rng.randint(0, 1439))
            completed = created + timedelta(minutes=rng.randint(0, 60 * 24 * 20)) if rng.random() < 0.5 else None
            resolved = None
            if rng.random() < 0.6:
                resolved = created + timedelta(minutes=rng.randint(0, 60 * 24 * 10))
                if completed is not None and resolved > completed:
                    resolved = completed
            goal = created + timedelta(minutes=rng.randint(0, 60 * 24 * 14)) if rng.random() < 0.8 else None
            s.add_ticket(100 + i, created, completed=completed, resolved=resolved, resolved_due=goal)
            tickets.append({"created": created, "completed": completed, "resolved": resolved, "goal": goal})
        for i in range(rng.randint(0, 30)):
            worked = base + timedelta(days=rng.randint(-2, (end - start).days + 1), minutes=rng.randint(0, 1439))
            hours = Decimal(rng.randint(0, 2400)) / 100
            s.add_time(500 + i, worked, hours, ticket=rng.choice([None, 100 + rng.randint(0, 45)]))
            entries.append({"worked": worked, "hours": hours})
        s.page_cap = rng.choice([1, 2, 3, 7, 500])
        picks = sorted(rng.sample(range(2, 60), rng.randint(0, 5)))
        s.fail_on = {n for i, n in enumerate(picks) if i == 0 or n - picks[i - 1] > 1}
        conn = AutotaskConnector(username=USER, secret=SECRET, integration_code=CODE, zone_lookup_url=s.url, client=client(), retrier=retrier(), clock=lambda: NOW)
        got = {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(run(collect(conn, start, end)))}
        assert got == oracle(tickets, entries, start, end, NOW), "seed %d page_cap %d" % (seed_value, s.page_cap)


# ---------------------------------------------------------------- the registry and the migration

def test_the_connector_is_registered_with_its_four_metrics_and_two_secrets():
    spec = registry.get("autotask")
    assert spec.supported_metrics == frozenset(METRICS) and registry.secret_keys("autotask") == frozenset({"secret", "integration_code"})
    assert registry.redact("autotask", {"username": USER, "secret": SECRET, "integration_code": CODE})["secret"] == "***"
    built = registry.build("autotask", username=USER, secret=SECRET, integration_code=CODE)
    assert isinstance(built, AutotaskConnector)


def test_migration_026_names_every_key_with_its_unit_and_no_money_key():
    sql = (ROOT / "migrations" / "026_autotask_metrics.sql").read_text(encoding="utf-8")
    for key in METRICS:
        assert re.search(r"\('%s'," % key, sql), key
    assert "'seconds'" in sql.split("'at_time_seconds'")[1].split(")")[0] and "currency" not in sql.split("insert into")[1]
