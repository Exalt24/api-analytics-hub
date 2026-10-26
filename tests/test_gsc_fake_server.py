"""The stand-in Search Console server refuses what the saved pages say Google refuses, and answers what they describe.

A stand-in that accepted anything would let a connector pass without having obeyed a documented rule. So each
refusal here is paired with the same request made valid (docs/GSC_SOURCES.md says which page each rule came from).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.gsc_fake import FINAL_LAG_DAYS, Fault, FakeGSC, error_body  # noqa: E402

TOKEN = "fake-token"
SITE = "sc-domain:example.com"
TODAY = date(2026, 11, 2)
DAYS = {date(2026, 10, d): (d, 10 * d, 5.0) for d in (1, 2, 4, 28, 30, 31)}
QUERIES = {date(2026, 10, 1): [("a", 1, 10, 4.0), ("b", 1, 10, 6.0), ("c", 5, 10, 5.0)],
           date(2026, 10, 2): [("a", 2, 20, 2.0)]}


@pytest.fixture()
def server():
    with FakeGSC(token=TOKEN, today=TODAY) as fake:
        fake.set_site(SITE, DAYS, queries=QUERIES)
        yield fake


def post(server, body=None, site=SITE, token=TOKEN, **over):
    payload = {"startDate": "2026-10-01", "endDate": "2026-10-02", "dimensions": ["date"]}
    payload.update(over)
    path = "/webmasters/v3/sites/%s/searchAnalytics/query" % quote(site, safe="")
    return httpx.post(server.url + path, json=payload if body is None else body, headers={"Authorization": "Bearer " + token},
                      timeout=5, trust_env=False)


def test_a_date_query_returns_one_row_a_day_ascending_and_leaves_out_days_with_no_data(server):
    rows = post(server, startDate="2026-10-01", endDate="2026-10-05").json()["rows"]
    assert [r["keys"] for r in rows] == [["2026-10-01"], ["2026-10-02"], ["2026-10-04"]]
    assert set(rows[0]) == {"keys", "clicks", "impressions", "ctr", "position"} and rows[0]["ctr"] == 0.1


def test_any_other_query_is_sorted_by_clicks_descending(server):
    rows = post(server, dimensions=["query"]).json()["rows"]
    assert [r["keys"][0] for r in rows][0] == "c" and [r["clicks"] for r in rows] == sorted((r["clicks"] for r in rows), reverse=True)
    merged = {r["keys"][0]: r for r in rows}
    assert merged["a"]["clicks"] == 3 and merged["a"]["impressions"] == 30
    assert merged["a"]["position"] == pytest.approx((4.0 * 10 + 2.0 * 20) / 30), "position is weighted by impressions"


def test_row_limit_and_start_row_page_and_a_start_row_past_the_end_is_a_successful_empty_page(server):
    first = post(server, dimensions=["query"], rowLimit=1).json()["rows"]
    second = post(server, dimensions=["query"], rowLimit=1, startRow=1).json()["rows"]
    assert first[0]["keys"] != second[0]["keys"]
    beyond = post(server, dimensions=["query"], rowLimit=1, startRow=99)
    assert beyond.status_code == 200 and "rows" not in beyond.json()


@pytest.mark.parametrize("over", [
    {"rowLimit": 25001}, {"rowLimit": 0}, {"rowLimit": "10"}, {"rowLimit": True}, {"startRow": -1}, {"startRow": 1.5},
    {"startDate": "2026-10-03"}, {"startDate": "2026-13-01"}, {"startDate": "10/01/2026"}, {"endDate": None},
    {"dimensions": ["date", "date"]}, {"dimensions": ["nope"]}, {"dimensions": "date"}, {"type": "carrier-pigeon"}, {"dataState": "soon"},
])
def test_what_the_pages_say_is_refused_is_refused_with_the_global_error_object(server, over):
    assert post(server).status_code == 200                              # positive control
    response = post(server, **over)
    err = response.json()["error"]
    assert response.status_code == 400 and err["code"] == 400 and err["errors"][0]["reason"] == "invalidParameter"


def test_the_largest_row_limit_and_the_three_data_states_are_accepted(server):
    assert post(server, rowLimit=25000).status_code == 200
    for state in ("all", "ALL", "final", "Final", "hourly_all"):
        assert post(server, dataState=state).status_code == 200


def test_final_data_is_the_default_and_holds_back_the_newest_days_while_all_includes_them(server):
    final_through = TODAY - __import__("datetime").timedelta(days=FINAL_LAG_DAYS)
    window = {"startDate": "2026-10-27", "endDate": "2026-11-01"}
    final = post(server, **window).json()
    assert [r["keys"][0] for r in final["rows"]] == ["2026-10-28", "2026-10-30"] and "metadata" not in final
    assert final_through == date(2026, 10, 30)
    fresh = post(server, dataState="all", **window).json()
    assert [r["keys"][0] for r in fresh["rows"]] == ["2026-10-28", "2026-10-30", "2026-10-31"]
    assert fresh["metadata"] == {"first_incomplete_date": "2026-10-31"}


def test_first_incomplete_date_is_only_given_for_all_grouped_by_date_over_a_range_with_fresh_days(server):
    assert "metadata" not in post(server, dataState="all").json(), "the range holds only settled days"
    assert "metadata" not in post(server, dataState="all", dimensions=["query"], startDate="2026-10-27", endDate="2026-11-01").json()
    assert "metadata" in post(server, dataState="all", startDate="2026-10-27", endDate="2026-11-01").json()


def test_an_unknown_property_is_a_403_not_a_404_and_a_wrong_token_is_a_401(server):
    forbidden = post(server, site="sc-domain:other.org")
    assert forbidden.status_code == 403 and forbidden.json()["error"]["errors"][0]["reason"] == "forbidden"
    assert post(server, token="wrong").status_code == 401
    assert httpx.post(server.url + "/webmasters/v3/nope", json={}, timeout=5, trust_env=False).status_code == 404


def test_the_property_in_the_path_is_decoded_once(server):
    server.set_site("https://www.example.com/", DAYS)
    assert post(server, site="https://www.example.com/").status_code == 200
    assert server.requests[-1][0] == "https://www.example.com/"


def test_a_scripted_fault_answers_once_and_then_the_normal_answer_resumes(server):
    server.faults.append(Fault(429, error_body(429, "rateLimitExceeded", "slow")))
    assert post(server).status_code == 429
    assert post(server).status_code == 200


def test_the_number_style_switch_changes_how_counts_are_written_and_nothing_else(server):
    ints = post(server).json()["rows"][0]
    server.number_style = "float"
    assert post(server).json()["rows"][0]["clicks"] == float(ints["clicks"])
    server.number_style = "string"
    assert post(server).json()["rows"][0]["clicks"] == str(ints["clicks"])
