"""The stand-in creative server refuses what the saved pages say Meta refuses, and answers what they describe.

A stand-in that accepted anything would let a client pass without having obeyed a documented rule. So each refusal here
is paired with the same request made valid, to prove the refusal is the rule and not a broken request
(docs/META_SOURCES.md says which page each rule came from).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.meta_creatives_fake import (  # noqa: E402
    BATCH_LIMIT,
    FakeMetaCreatives,
    FieldError,
    load_sample,
    parse_fields,
    project,
)

TOKEN = "fake-token"


@pytest.fixture()
def server():
    with FakeMetaCreatives(token=TOKEN) as fake:
        load_sample(fake, "1")
        yield fake


def get(server, path="/v26.0/act_1/ads", **params):
    base = {"access_token": TOKEN, "fields": "id,name"}
    base.update({k: v for k, v in params.items() if v is not None})
    for k, v in params.items():
        if v is None:
            base.pop(k, None)
    return httpx.get(server.url + path, params=base, timeout=5, trust_env=False)


def batch(server, items, **form):
    data = {"access_token": TOKEN, "batch": json.dumps(items)}
    data.update(form)
    return httpx.post(server.url + "/v26.0", data=data, timeout=5, trust_env=False)


def test_a_valid_read_gets_the_ads_with_only_the_requested_fields(server):
    body = get(server, effective_status=json.dumps(["ACTIVE", "PAUSED"])).json()
    assert [a["id"] for a in body["data"]] == ["1001", "1002", "1003", "1004", "1005", "1006", "1008"]
    assert body["data"][0] == {"id": "1001", "name": "Spring link ad"}
    assert "next" not in body["paging"]


def test_an_unknown_field_is_refused_at_any_depth_and_the_valid_request_is_not(server):
    assert get(server, fields="id,creative{id,body}").status_code == 200
    for fields in ("id,nonsense", "id,creative{id,nonsense}", "id,creative{object_story_spec{link_data{x}}}x"):
        r = get(server, fields=fields)
        if fields.endswith("}}}x"):
            assert r.status_code == 400
            continue
        assert r.status_code == 400 and r.json()["error"]["code"] == 100 and "nonexisting field" in r.json()["error"]["message"]


def test_a_query_that_does_not_parse_is_error_2500(server):
    for bad in ("id,creative{id", "id,}", "{id}"):
        r = get(server, fields=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == 2500, bad


def test_effective_status_must_be_a_list_of_the_status_values_the_ad_page_lists(server):
    assert get(server, effective_status=json.dumps(["ACTIVE"])).status_code == 200
    for bad in ("ACTIVE", json.dumps(["LIVE"]), json.dumps([]), json.dumps({"a": 1}), "[not json"):
        r = get(server, effective_status=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == 100, bad


def test_a_deleted_ad_comes_back_only_when_the_filter_names_it(server):
    ids = lambda **kw: [a["id"] for a in get(server, **kw).json()["data"]]  # noqa: E731
    assert "1007" not in ids(effective_status=json.dumps(["ACTIVE", "PAUSED"]))
    assert ids(effective_status=json.dumps(["DELETED"])) == ["1007"]
    assert "1007" not in ids()


def test_limit_and_cursor_rules(server):
    assert get(server, limit="2").status_code == 200
    for bad in ("0", "-1", "x"):
        assert get(server, limit=bad).status_code == 400
    r = get(server, after="not-a-cursor")
    assert r.status_code == 400 and r.json()["error"]["code"] == 2642
    first = get(server, limit="2").json()
    assert "next" in first["paging"] and TOKEN in first["paging"]["next"], "a next link carries the token"
    second = get(server, limit="2", after=first["paging"]["cursors"]["after"]).json()
    assert [a["id"] for a in second["data"]] != [a["id"] for a in first["data"]]


def test_token_account_and_campaign_checks(server):
    assert get(server).status_code == 200
    r = httpx.get(server.url + "/v26.0/act_1/ads", params={"access_token": "wrong", "fields": "id"}, timeout=5, trust_env=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == 190
    r = get(server, "/v26.0/act_2/ads")
    assert r.json()["error"]["code"] == 200
    assert [a["id"] for a in get(server, "/v26.0/102/ads", fields="id").json()["data"]] == ["1004", "1005", "1006"]
    assert get(server, "/v26.0/999/ads").json()["error"]["code"] == 100
    assert get(server, "/v26.0/act_1/other").status_code == 404


def test_nodes_are_read_by_id_with_the_same_field_rules(server):
    assert get(server, "/v26.0/9001", fields="source,permalink_url,length").json() == {
        "source": "https://video.example.test/v/9001.mp4", "permalink_url": "https://www.facebook.com/page1/videos/9001/", "length": 31.5}
    r = get(server, "/v26.0/9001", fields="source,nonsense")
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
    assert get(server, "/v26.0/101", fields="id,name").json() == {"id": "101", "name": "Spring sale"}
    assert get(server, "/v26.0/5001", fields="id,body,call_to_action_type").json() == {"id": "5001"}
    assert get(server, "/v26.0/424242", fields="id").json()["error"]["code"] == 100
    assert get(server, "/v26.0/9004", fields="source").json()["error"]["code"] == 200


def test_a_batch_of_fifty_is_answered_and_fifty_one_is_refused(server):
    ok = batch(server, [{"method": "GET", "relative_url": "9001?fields=source"}] * BATCH_LIMIT)
    assert ok.status_code == 200 and len(ok.json()) == 50
    refused = batch(server, [{"method": "GET", "relative_url": "9001?fields=source"}] * (BATCH_LIMIT + 1))
    assert refused.status_code == 400 and refused.json()["error"]["code"] == 100


def test_each_batch_answer_has_a_code_and_a_body_that_is_a_json_string(server):
    r = batch(server, [{"method": "GET", "relative_url": "9001?fields=source"},
                       {"method": "GET", "relative_url": "9004?fields=source"},
                       {"method": "GET", "relative_url": "424242?fields=source"},
                       {"method": "POST", "relative_url": "9001"},
                       {"method": "GET", "relative_url": "abc?fields=source"}]).json()
    assert [x["code"] for x in r] == [200, 400, 400, 400, 400]
    assert isinstance(r[0]["body"], str) and json.loads(r[0]["body"]) == {"source": "https://video.example.test/v/9001.mp4"}
    assert json.loads(r[1]["body"])["error"]["code"] == 200 and json.loads(r[2]["body"])["error"]["code"] == 100
    assert "headers" in batch(server, [{"method": "GET", "relative_url": "9001"}]).json()[0]
    assert "headers" not in batch(server, [{"method": "GET", "relative_url": "9001"}], include_headers="false").json()[0]


def test_a_batch_needs_the_token_and_a_list(server):
    r = httpx.post(server.url + "/v26.0", data={"batch": "[]", "access_token": "wrong"}, timeout=5, trust_env=False)
    assert r.json()["error"]["code"] == 190
    assert batch(server, []).status_code == 200
    r = httpx.post(server.url + "/v26.0", data={"access_token": TOKEN, "batch": "{}"}, timeout=5, trust_env=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
    assert httpx.post(server.url + "/v26.0/act_1/ads", data={"access_token": TOKEN}, timeout=5, trust_env=False).status_code == 400


def test_the_usage_headers_are_sent_with_the_ads_management_type(server):
    server.utilisation, server.regain_minutes = 42, 3
    r = get(server)
    assert json.loads(r.headers["x-app-usage"])["call_count"] == 42
    entry = json.loads(r.headers["x-business-use-case-usage"])["123"][0]
    assert entry["type"] == "ads_management" and entry["estimated_time_to_regain_access"] == 3


def test_scripted_faults_by_order_and_by_request_number(server):
    from app.connectors.meta_creatives_fake import Fault, error_body
    server.faults.append(Fault(400, error_body(4, "OAuthException", "x")))
    server.fault_at[2] = Fault(503, {})
    assert get(server).json()["error"]["code"] == 4
    assert get(server).status_code == 200
    assert get(server).status_code == 503
    assert get(server).status_code == 200


# ---------------------------------------------------------------- the fields parser

def _render(tree):
    return ",".join(name if sub is None else "%s{%s}" % (name, _render(sub)) for name, sub in tree.items())


def _random_tree(rng, depth=0):
    names = rng.sample(["id", "name", "creative", "campaign", "object_story_spec", "link_data", "video_data", "x1", "y_2"], rng.randint(1, 4))
    return {n: (_random_tree(rng, depth + 1) if depth < 3 and rng.random() < 0.4 else None) for n in names}


@pytest.mark.parametrize("seed", range(40))
def test_parse_fields_round_trips_random_nested_field_lists(seed):
    tree = _random_tree(random.Random(seed))
    assert parse_fields(_render(tree)) == tree
    assert parse_fields(_render(tree).replace(",", " , ")) == tree


def test_project_leaves_out_a_field_with_no_value_and_refuses_one_the_node_lacks():
    obj = {"id": "1", "creative": {"id": "2", "body": "b", "object_story_spec": {"link_data": {"link": "l", "message": "m"}}}}
    assert project(obj, parse_fields("id,creative{body,object_story_spec{link_data{link}}}"), "Ad") == {
        "id": "1", "creative": {"body": "b", "object_story_spec": {"link_data": {"link": "l"}}}}
    assert project(obj, parse_fields("id,name"), "Ad") == {"id": "1"}
    with pytest.raises(FieldError):
        project(obj, parse_fields("zzz"), "Ad")
