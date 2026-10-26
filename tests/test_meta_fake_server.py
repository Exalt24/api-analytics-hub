"""The stand-in Meta server refuses what the saved pages say Meta refuses, and answers what they describe.

A stand-in that accepted anything would let a connector pass without having obeyed a documented rule. So each
refusal here is paired with the same request made valid, to prove the refusal is the rule and not a broken
request (docs/META_SOURCES.md says which page each rule came from).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.meta_fake import FakeMeta, error_body  # noqa: E402

TOKEN = "fake-token"
DAYS = {date(2026, 10, 1): {"spend": "10.00", "impressions": 100, "clicks": 5, "purchases": 1, "purchase_value": "20.00"},
        date(2026, 10, 2): {"spend": "11.00", "impressions": 120, "clicks": 6, "purchases": None, "purchase_value": None}}


@pytest.fixture()
def server():
    with FakeMeta(token=TOKEN, today=date(2026, 11, 2)) as fake:
        fake.set_account("1", DAYS, currency="USD")
        yield fake


def get(server, path="/v26.0/act_1/insights", **params):
    base = {"access_token": TOKEN, "time_range": json.dumps({"since": "2026-10-01", "until": "2026-10-02"}),
            "fields": "spend,impressions", "time_increment": "1", "level": "account"}
    base.update({k: v for k, v in params.items() if v is not None})
    for k, v in params.items():
        if v is None:
            base.pop(k, None)
    return httpx.get(server.url + path, params=base, timeout=5, trust_env=False)


def test_a_valid_request_gets_one_row_a_day_with_numeric_strings(server):
    body = get(server).json()
    assert [r["date_start"] for r in body["data"]] == ["2026-10-01", "2026-10-02"]
    assert body["data"][0] == {"spend": "10.00", "impressions": "100", "date_start": "2026-10-01", "date_stop": "2026-10-01"}
    assert "next" not in body["paging"]


def test_only_the_requested_fields_come_back(server):
    row = get(server, fields="clicks").json()["data"][0]
    assert set(row) == {"clicks", "date_start", "date_stop"}


def test_actions_carry_a_number_for_each_window_asked_for_and_value_for_the_default(server):
    body = get(server, fields="actions,action_values", action_attribution_windows='["7d_click","1d_view"]').json()
    entry = next(a for a in body["data"][0]["actions"] if a["action_type"] == "omni_purchase")
    assert entry["value"] == "1" and entry["7d_click"] == "1" and entry["1d_view"] == "1"
    assert "actions" not in body["data"][1], "a day the stand-in has no purchase figure for has no actions list"


@pytest.mark.parametrize("params,code", [
    ({"access_token": "wrong"}, 190),
    ({"fields": "spend,not_a_field"}, 100),
    ({"time_range": "not json"}, 100),
    ({"time_range": json.dumps({"since": "2026-10-03", "until": "2026-10-01"})}, 100),
    ({"time_range": json.dumps({"since": "2023-01-01", "until": "2026-10-01"})}, 3018),   # more than 37 months back
    ({"level": "galaxy"}, 100),
    ({"time_increment": "91"}, 100),
    ({"action_attribution_windows": '["9d_click"]'}, 100),
    ({"limit": "0"}, 100),
    ({"after": "garbage"}, 2642),
])
def test_what_the_pages_say_is_refused_is_refused_with_the_documented_error_object(server, params, code):
    assert get(server).status_code == 200                              # positive control
    response = get(server, **params)
    err = response.json()["error"]
    assert response.status_code == 400 and err["code"] == code and {"message", "type", "fbtrace_id"} <= set(err)


def test_an_account_the_token_cannot_read_is_a_permissions_error_and_a_bad_path_is_not_found(server):
    assert get(server, path="/v26.0/act_2/insights").json()["error"]["code"] == 200
    assert get(server, path="/v25.0/act_1/insights").status_code == 404


def test_the_37_month_limit_is_measured_in_months_from_today(server):
    since = json.dumps({"since": "2023-09-01", "until": "2023-09-02"})
    assert get(server, time_range=since).json()["error"]["code"] == 3018
    ok = json.dumps({"since": "2023-10-01", "until": "2023-10-02"})
    assert "error" not in get(server, time_range=ok).json()


def test_paging_hands_out_cursors_and_a_next_link_that_carries_the_token_until_the_last_page(server):
    first = get(server, limit="1").json()
    assert len(first["data"]) == 1 and first["paging"]["next"].endswith("access_token=" + TOKEN)
    second = get(server, limit="1", after=first["paging"]["cursors"]["after"]).json()
    assert [r["date_start"] for r in second["data"]] == ["2026-10-02"] and "next" not in second["paging"]


def test_the_usage_headers_are_present_and_json(server):
    server.utilisation = 42
    response = get(server)
    assert json.loads(response.headers["x-fb-ads-insights-throttle"])["acc_id_util_pct"] == 42
    assert json.loads(response.headers["x-business-use-case-usage"])["123"][0]["call_count"] == 42


def test_an_account_reads_its_status_currency_and_time_zone(server):
    body = get(server, path="/v26.0/act_1", fields="account_status,currency,timezone_name").json()
    assert body == {"id": "act_1", "account_status": 1, "currency": "USD", "timezone_name": "America/Los_Angeles"}
    assert get(server, path="/v26.0/act_1", fields="nope").json()["error"]["code"] == 100


def test_a_scripted_fault_answers_once_and_then_the_normal_answer_resumes(server):
    from app.connectors.meta_fake import Fault
    server.faults.append(Fault(500, error_body(2, "OAuthException", "down")))
    assert get(server).status_code == 500
    assert get(server).status_code == 200
