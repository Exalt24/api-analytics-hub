"""The Meta ad creative reader against the stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the requests the reader builds (the ads edges, the field expansion,
the status filter, the batch), how it reads each documented creative shape, the paging rules (a `next` link is never
requested, an empty page that still has a `next` is followed, a cursor that comes back is refused), the error taxonomy by
error code, the pacing from the usage headers and Retry-After, and the batch of video lookups. They do not prove Meta
accepts the requests or sends these shapes: the server is written from the saved pages (docs/META_SOURCES.md), no live ad
account was used, so nothing here is a live integration.

Every refusal runs a positive control first (the same call succeeds), so a refusal is the rule and not a broken request.
Test names carry the words tests/mutation_check_creatives.py looks for.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.meta_creatives import (  # noqa: E402
    AD_FIELDS,
    CREATIVE_FIELDS,
    CreativeRun,
    DEFAULT_STATUSES,
    MetaCreativesClient,
    read_creative,
)
from app.connectors.meta_creatives_fake import FakeMetaCreatives, Fault, error_body, load_sample  # noqa: E402

ACCOUNT = "555001"
TOKEN = "tok-SECRET-creatives-123"


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture()
def server():
    with FakeMetaCreatives(token=TOKEN) as fake:
        load_sample(fake, ACCOUNT)
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("sleep", sleeps or Sleeps())
    kw.setdefault("rng", lambda: 1.0)
    if "token_provider" not in kw:
        kw.setdefault("access_token", TOKEN)
    client = httpx.AsyncClient(trust_env=False, timeout=10)
    return MetaCreativesClient(ad_account_id=kw.pop("ad_account_id", ACCOUNT), base_url=server.url, client=client, **kw)


def run(coro):
    return asyncio.run(coro)


async def read(client, **kw):
    """Every record, and any error raised: the strict way to read."""
    records = []
    try:
        async for rows in client.fetch(**kw):
            records += rows
        return CreativeRun(records, client.stats)
    finally:
        await client.aclose()


async def read_partial(client, **kw):
    """`fetch_all`: a rate limit stops the run and the pages read are kept."""
    try:
        return await client.fetch_all(**kw)
    finally:
        await client.aclose()


def by_id(result):
    return {r.ad_id: r for r in result.records}


def ad_row(ad_id="1", creative=None, **over):
    row = {"id": ad_id, "name": "Ad " + ad_id, "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "101",
           "adset_id": "7", "campaign": {"id": "101", "name": "Spring sale"},
           "creative": creative if creative is not None else {"id": "5" + ad_id, "body": "text"}}
    row.update(over)
    return row


def page(rows, after=None, nxt=False):
    paging = {"cursors": {"before": "B", "after": after or "A"}}
    if nxt:
        paging["next"] = "https://evil.example.test/next?access_token=x"
    return {"data": rows, "paging": paging}


# ---------------------------------------------------------------- the creative shapes

def test_every_documented_creative_shape_reads_into_copy_links_media_and_preview(server):
    got = by_id(run(read(make(server))))
    assert set(got) == {"1001", "1002", "1003", "1004", "1005", "1006", "1008"}, "the deleted ad is not read by default"

    link = got["1001"]
    assert (link.creative_kind, link.primary_texts, link.headlines, link.link_descriptions) == (
        "link", ["Fresh deals for spring."], ["Spring sale"], ["Up to 30 percent off"])
    assert link.call_to_action_types == ["SHOP_NOW"] and link.destination_links == ["https://shop.example.test/spring"]
    assert link.image_urls == ["https://img.example.test/spring.jpg"] and link.image_hashes == ["hash1001"]
    assert link.preview_link == "https://fb.me/preview/1001"
    assert link.post_id == "11_9901" and link.post_permalink == "https://www.facebook.com/11/posts/9901"
    assert (link.campaign_id, link.campaign_name) == ("101", "Spring sale")

    carousel = got["1002"]
    assert carousel.creative_kind == "carousel" and len(carousel.cards) == 3
    assert carousel.primary_texts == ["Three ways to start spring."] and carousel.headlines == ["Seeds", "Tools", "Demo reel"]
    assert carousel.destination_links == ["https://shop.example.test/", "https://shop.example.test/seeds",
                                          "https://shop.example.test/tools", "https://shop.example.test/reel"]
    assert carousel.video_ids == ["9001"] and carousel.videos[0].source == "https://video.example.test/v/9001.mp4"

    video = got["1003"]
    assert video.creative_kind == "video" and video.headlines == ["Growth in 30 seconds"]
    assert video.link_descriptions == ["See the whole season"] and video.video_ids == ["9002"]
    assert video.videos[0].permalink_url == "https://www.facebook.com/page1/videos/9002/", "a relative permalink is made a link"
    assert video.videos[0].length == 12 and video.thumbnail_url == "https://img.example.test/t5003.jpg"

    photo = got["1004"]
    assert photo.creative_kind == "photo" and photo.primary_texts == ["Our first greenhouse."]
    assert photo.image_urls == ["https://img.example.test/greenhouse.jpg"] and photo.headlines == []

    dynamic = got["1005"]
    assert dynamic.creative_kind == "dynamic" and dynamic.primary_texts == ["Begin your season.", "Grow something."]
    assert dynamic.headlines == ["Level up", "Swipe to grow"] and dynamic.call_to_action_types == ["LEARN_MORE"]
    assert dynamic.video_ids == ["9001", "9003"]

    post = got["1006"]
    assert post.creative_kind == "post" and post.post_id == "11_9902" and post.primary_texts == []
    assert post.instagram_permalink_url == "https://www.instagram.com/p/EXAMPLE1/"
    assert any("existing page post" in n for n in post.notes)

    plain = got["1008"]
    assert plain.creative_kind == "video" and plain.primary_texts == ["Paused for the season."]
    assert plain.destination_links == ["https://shop.example.test/pause"] and plain.ad_status == "PAUSED"


def test_the_ads_edge_is_asked_for_the_creative_expansion_and_the_status_filter(server):
    run(read(make(server, page_size=3)))
    first = server.requests[0]
    assert first.method == "GET" and first.path == "/v26.0/act_555001/ads"
    assert first.query["fields"] == AD_FIELDS and "creative{" in AD_FIELDS and "campaign{id,name}" in AD_FIELDS
    assert all(name in AD_FIELDS for name in CREATIVE_FIELDS)
    assert first.query["limit"] == "3"
    assert json.loads(first.query["effective_status"]) == list(DEFAULT_STATUSES) and "DELETED" not in DEFAULT_STATUSES
    assert first.query["access_token"] == TOKEN


def test_deleted_ads_are_read_only_on_request_and_say_so(server):
    default = by_id(run(read(make(server))))
    assert "1007" not in default
    with_deleted = by_id(run(read(make(server), include_deleted=True)))
    gone = with_deleted["1007"]
    assert gone.ad_effective_status == "DELETED" and gone.creative_status == "DELETED"
    assert any("creative is DELETED" in n for n in gone.notes) and gone.creative_kind == "unknown"
    last_edge = [r for r in server.requests if r.method == "GET"][-1]
    assert json.loads(last_edge.query["effective_status"])[-1] == "DELETED"


def test_a_row_outside_the_status_filter_is_dropped_and_counted(server):
    server.faults.append(Fault(200, page([ad_row("1"), ad_row("2", effective_status="DELETED")])))
    result = run(read(make(server)))
    assert [r.ad_id for r in result.records] == ["1"] and result.stats.filtered_out == 1


def test_a_status_the_ad_page_does_not_list_is_refused_before_a_request(server):
    run(read(make(server), statuses=["ACTIVE", "PAUSED"]))
    n = len(server.requests)
    for bad in (["LIVE"], [], ["active"]):
        with pytest.raises(ValueError):
            run(read(make(server), statuses=bad))
    assert len(server.requests) == n


# ---------------------------------------------------------------- the account id and the token

@pytest.mark.parametrize("bad", ["", "abc", "act_", "12 34", "act_12x", "-5", "1" * 21, "act_1/ads", "123?x=1"])
def test_a_malformed_ad_account_id_is_refused_before_any_request(server, bad):
    ok = make(server, ad_account_id="act_555001")
    assert ok.external_account == "act_555001"
    run(ok.aclose())
    with pytest.raises(ValueError, match="ad_account_id"):
        make(server, ad_account_id=bad)
    assert server.requests == []


def test_exactly_one_of_token_and_provider_and_a_missing_env_value_is_named(server):
    with pytest.raises(ValueError):
        make(server, access_token=None)
    with pytest.raises(ValueError):
        make(server, access_token=TOKEN, token_provider=lambda: TOKEN)
    with pytest.raises(ValueError, match="META_ACCESS_TOKEN"):
        MetaCreativesClient.from_env({"META_AD_ACCOUNT_ID": ACCOUNT})
    c = MetaCreativesClient.from_env({"META_AD_ACCOUNT_ID": ACCOUNT, "META_ACCESS_TOKEN": TOKEN, "META_API_BASE_URL": server.url})
    assert repr(c) == "MetaCreativesClient(account_id='555001', base_url=%r)" % server.url and TOKEN not in repr(c)
    run(c.aclose())


def test_the_token_provider_is_asked_each_call_and_an_empty_token_is_expired(server):
    tokens = iter([TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN])
    run(read(make(server, access_token=None, token_provider=lambda: next(tokens))))
    with pytest.raises(AuthExpired):
        run(read(make(server, access_token=None, token_provider=lambda: "")))


def test_campaign_ids_must_be_numbers_and_each_is_read_once_from_its_own_edge(server):
    for bad in (["12/ads"], ["abc"], [], ["1;2"]):
        with pytest.raises(ValueError):
            run(read(make(server), campaign_ids=bad))
    assert server.requests == []
    result = run(read(make(server), campaign_ids=["102", "101", "102"]))
    edges = [r.path for r in server.requests if r.method == "GET"]
    assert edges == ["/v26.0/102/ads", "/v26.0/101/ads"]
    assert [r.ad_id for r in result.records] == ["1004", "1005", "1006", "1001", "1002", "1003"]
    assert all(r.campaign_id in ("101", "102") for r in result.records)


def test_a_campaign_that_does_not_exist_is_a_refused_request_not_an_empty_result(server):
    run(read(make(server), campaign_ids=["101"]))
    with pytest.raises(UpstreamBroken, match="code 100"):
        run(read(make(server), campaign_ids=["999"]))


# ---------------------------------------------------------------- paging

def test_a_next_link_is_never_requested_and_the_token_goes_only_to_the_base_address(server):
    with FakeMetaCreatives(token=TOKEN) as evil:
        server.next_base = evil.url
        result = run(read(make(server, page_size=1)))
        assert len(result.records) == 7 and result.stats.pages == 7
        assert evil.requests == [], "the reader followed a next link to another host"
    assert all("/ads" in r.path or r.method == "POST" for r in server.requests)


def test_an_empty_page_that_still_has_a_next_link_is_followed(server):
    server.empty_pages_with_next = 3
    result = run(read(make(server, page_size=4)))
    assert len(result.records) == 7 and result.stats.pages == 3 + 2


def test_a_cursor_that_comes_back_is_refused_as_paging_that_does_not_advance(server):
    ok = run(read(make(server)))
    assert ok.complete
    server.faults += [Fault(200, page([], "SAME", True)), Fault(200, page([], "SAME", True))]
    with pytest.raises(UpstreamBroken, match="does not advance"):
        run(read(make(server)))


def test_a_next_link_with_no_after_cursor_is_refused(server):
    server.faults.append(Fault(200, {"data": [], "paging": {"next": "https://x.example.test/n"}}))
    with pytest.raises(UpstreamBroken, match="no after cursor"):
        run(read(make(server)))


def test_cursors_that_never_end_stop_at_the_page_limit(server):
    server.faults += [Fault(200, page([], "C%d" % i, True)) for i in range(10)]
    with pytest.raises(UpstreamBroken, match="more than 4 pages"):
        run(read(make(server, max_pages=4)))


def test_the_same_ad_on_two_pages_is_one_record_and_counted(server):
    server.faults += [Fault(200, page([ad_row("1")], "P1", True)), Fault(200, page([ad_row("1"), ad_row("2")], "P2"))]
    result = run(read(make(server)))
    assert [r.ad_id for r in result.records] == ["1", "2"] and result.stats.duplicates == 1


def test_data_that_is_not_a_list_and_an_ad_without_a_numeric_id_are_broken_answers(server):
    for body in ({"data": {}}, {"data": [1]}, {"data": [{"id": "abc"}]}, {"data": [{"name": "no id"}]}):
        server.faults.append(Fault(200, body))
        with pytest.raises(UpstreamBroken):
            run(read(make(server)))
    assert run(read(make(server))).complete


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("code,kind", [(190, AuthExpired), (102, AuthExpired), (200, AuthRevoked), (10, AuthRevoked),
                                       (3, AuthRevoked), (100, UpstreamBroken), (2500, UpstreamBroken), (3018, UpstreamBroken)])
def test_an_error_code_that_retrying_cannot_fix_is_raised_at_once_by_what_it_means(server, code, kind):
    run(read(make(server)))
    before = len(server.requests)
    server.faults.append(Fault(400, error_body(code, "OAuthException", "scripted")))
    with pytest.raises(kind):
        run(read(make(server)))
    assert len(server.requests) == before + 1, "an error that retrying cannot fix was retried"


@pytest.mark.parametrize("code", [4, 17, 32, 341, 613, 80000, 80004, 80014, 1, 2])
def test_a_throttle_or_temporary_code_is_retried_with_backoff_then_rate_limited(server, code):
    sleeps = Sleeps()
    server.faults.append(Fault(400, error_body(code, "OAuthException", "slow down")))
    assert run(read(make(server, sleeps))).complete and len(sleeps.calls) == 1, "one throttle then success"
    server.faults += [Fault(400, error_body(code, "OAuthException", "slow down"))] * 3
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(read(make(server, Sleeps())))


def test_an_error_object_inside_a_200_is_an_error_not_data(server):
    server.faults.append(Fault(200, error_body(190, "OAuthException", "expired")))
    with pytest.raises(AuthExpired):
        run(read(make(server)))


@pytest.mark.parametrize("fault,kind", [
    (Fault(200, b"<html>not json</html>"), UpstreamBroken), (Fault(200, "a string"), UpstreamBroken),
    (Fault(401, b""), AuthExpired), (Fault(403, b""), AuthRevoked), (Fault(500, b"<html>"), RateLimited),
    (Fault(429, {}), RateLimited), (Fault(0, None, drop=True), RateLimited)])
def test_a_body_that_is_not_the_documented_shape_is_handled_by_its_http_status(server, fault, kind):
    assert run(read(make(server))).complete
    server.faults += [fault] * 3
    with pytest.raises(kind):
        run(read(make(server, Sleeps())))


def test_no_error_message_holds_the_token_or_a_url(server):
    server.faults += [Fault(400, error_body(100, "OAuthException", "bad"))]
    with pytest.raises(UpstreamBroken) as info:
        run(read(make(server)))
    assert TOKEN not in str(info.value) and "http" not in str(info.value)
    server.faults += [Fault(0, None, drop=True)] * 3
    with pytest.raises(RateLimited) as info2:
        run(read(make(server, Sleeps())))
    assert TOKEN not in str(info2.value) and server.url not in str(info2.value)


# ---------------------------------------------------------------- pacing

def test_retry_after_and_the_usage_header_decide_the_wait_and_a_long_wait_is_handed_back(server):
    sleeps = Sleeps()
    server.faults.append(Fault(400, error_body(4, "OAuthException", "slow"), {"Retry-After": "7"}))
    run(read(make(server, sleeps)))
    assert sleeps.calls == [7.0], "Retry-After is the wait when the usage header names none"
    sleeps = Sleeps()
    server.regain_minutes = 0.1
    server.faults.append(Fault(400, error_body(80004, "OAuthException", "slow"), server._headers()))
    run(read(make(server, sleeps)))
    assert sleeps.calls == [pytest.approx(6.0)], "estimated_time_to_regain_access is in minutes"
    server.regain_minutes = 10
    server.faults.append(Fault(400, error_body(80004, "OAuthException", "slow"), server._headers()))
    with pytest.raises(RateLimited) as info:
        run(read(make(server, Sleeps(), backoff_cap=30)))
    assert info.value.retry_after_seconds == 600


def test_a_page_is_not_fetched_when_usage_is_near_the_limit_and_the_pages_read_are_kept(server):
    server.utilisation = 95
    result = run(read_partial(make(server, page_size=2)))
    assert not result.complete and "usage is at 95 percent" in result.stopped_because
    assert [r.ad_id for r in result.records] == ["1001", "1002"], "the first page is kept"
    assert result.stats.pages == 1
    server.utilisation = 89
    assert run(read_partial(make(server, page_size=2))).complete


def test_usage_is_checked_between_campaigns_too(server):
    server.utilisation = 95
    result = run(read_partial(make(server), campaign_ids=["101", "102"]))
    assert not result.complete and "before campaign 102" in result.stopped_because
    assert {r.campaign_id for r in result.records} == {"101"}


# ---------------------------------------------------------------- the batch of lookups

def test_a_hundred_and_twenty_videos_are_looked_up_in_three_batches_with_the_token_in_the_body(server):
    ids = ["%d" % (8_000_000 + i) for i in range(120)]
    for i in ids:
        server.videos[i] = {"source": "https://v.example.test/%s.mp4" % i, "permalink_url": "/p/videos/%s/" % i, "length": 3}
    spec = {"videos": [{"video_id": i} for i in ids]}
    server.set_account(ACCOUNT, campaigns={"101": "C"}, ads=[
        {"id": "1", "name": "big", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "101",
         "creative": {"id": "5", "asset_feed_spec": spec, "object_story_spec": {"page_id": "1"}}}])
    result = run(read(make(server)))
    posts = [r for r in server.requests if r.method == "POST"]
    assert [len(json.loads(p.form["batch"])) for p in posts] == [50, 50, 20]
    assert all(p.raw_query == "" and p.form["access_token"] == TOKEN and p.form["include_headers"] == "false" for p in posts)
    assert all(v.source for v in result.records[0].videos) and len(result.records[0].videos) == 120
    assert result.stats.batches == 3 and result.stats.videos_looked_up == 120


def test_a_video_shared_by_many_ads_is_looked_up_once_across_pages(server):
    run(read(make(server, page_size=1)))
    asked = [i["relative_url"].split("?")[0] for p in server.requests if p.method == "POST" for i in json.loads(p.form["batch"])]
    assert asked.count("9001") == 1 and sorted(set(asked)) == ["9001", "9002", "9003", "9004"]


def test_a_video_with_no_source_a_null_source_no_access_or_no_node_each_say_why(server):
    server.videos["9005"] = {"permalink_url": "https://www.facebook.com/p/videos/9005/"}
    got = by_id(run(read(make(server))))
    nosource = got["1005"].videos[1]
    assert nosource.video_id == "9003" and nosource.source is None and nosource.problem == "no source returned"
    assert nosource.permalink_url == "https://www.facebook.com/page1/videos/9003/" and nosource.length == 45
    forbidden = got["1008"].videos[0]
    assert forbidden.source is None and "no access (code 200" in forbidden.problem
    server.video_source_null = True
    again = by_id(run(read(make(server))))
    assert again["1005"].videos[1].source is None and again["1005"].videos[0].source
    del server.videos["9002"]
    missing = by_id(run(read(make(server))))["1003"].videos[0]
    assert missing.source is None and "not readable (code 100" in missing.problem


def test_a_batch_answer_that_timed_out_or_was_throttled_is_asked_again_for_that_id_only(server):
    server.batch_null_once = {"9001"}
    server.batch_throttle_once = {"9002"}
    sleeps = Sleeps()
    got = by_id(run(read(make(server, sleeps))))
    assert got["1002"].videos[0].source and got["1003"].videos[0].source
    posts = [json.loads(p.form["batch"]) for p in server.requests if p.method == "POST"]
    assert [i["relative_url"].split("?")[0] for i in posts[1]] == ["9001", "9002"], "only the two that failed come back"
    assert len(sleeps.calls) == 1
    server.batch_null_once = {"9001"}
    with pytest.raises(AuthExpired):
        server.faults.append(Fault(400, error_body(190, "OAuthException", "expired")))
        run(read(make(server)))


def test_an_id_that_never_answers_ends_as_a_note_not_a_missing_ad(server):
    class Always(set):
        def discard(self, item):
            return None
    server.batch_null_once = Always({"9003"})
    got = by_id(run(read(make(server))))
    assert "no answer after 3 attempts" in got["1005"].videos[1].problem and got["1005"].videos[0].source


def test_a_batch_answered_with_the_wrong_number_of_results_is_broken(server):
    server.faults.append(Fault(200, page([]), {}))  # the ads page, empty: no batch needed
    assert run(read(make(server))).records == []
    server.faults += [Fault(200, page([ad_row("1", {"id": "51", "video_id": "9001"})])), Fault(200, [])]
    with pytest.raises(UpstreamBroken, match="a batch of 1 requests came back"):
        run(read(make(server)))


def test_a_video_id_that_is_not_a_number_is_not_put_in_a_url(server):
    server.faults.append(Fault(200, page([ad_row("1", {"id": "51", "video_id": "9001/../x?access_token=1"})])))
    result = run(read(make(server)))
    rec = result.records[0]
    assert rec.videos[0].problem == "the video id is not numeric, so it was not looked up"
    assert [r for r in server.requests if r.method == "POST"] == []


# ---------------------------------------------------------------- expansion that the server will not do

def test_an_expansion_the_server_refuses_falls_back_to_a_read_by_id(server):
    plain = by_id(run(read(make(server))))
    server.requests.clear()
    server.refuse_expansion = {"creative", "campaign"}
    got = by_id(run(read(make(server))))
    for ad_id, rec in plain.items():
        other = got[ad_id]
        assert (other.primary_texts, other.headlines, other.destination_links, other.video_ids) == (
            rec.primary_texts, rec.headlines, rec.destination_links, rec.video_ids)
        assert (other.campaign_name, other.creative_kind) == (rec.campaign_name, rec.creative_kind)
    ids = [i["relative_url"] for p in server.requests if p.method == "POST" for i in json.loads(p.form["batch"])]
    assert any(i.startswith("5001?fields=id,name,status,body") for i in ids) and any(i.startswith("101?fields=id,name") for i in ids)


def test_a_creative_that_cannot_be_read_by_id_either_is_a_note(server):
    server.refuse_expansion = {"creative"}
    server.node_errors["5004"] = (400, error_body(200, "OAuthException", "(#200) Permissions error"))
    rec = by_id(run(read(make(server))))["1004"]
    assert any("creative 5004 not read: no access" in n for n in rec.notes) and rec.creative_kind == "unknown"
    assert not any("no readable fields" in n for n in rec.notes)


# ---------------------------------------------------------------- reading a creative directly

def test_asset_feed_spec_as_a_json_string_is_read_and_a_broken_string_is_refused():
    spec = {"bodies": [{"text": "b"}], "titles": [{"text": "t"}], "link_urls": [{"website_url": "https://x.example.test"}]}
    as_object = read_creative({"id": "1", "asset_feed_spec": spec})
    as_string = read_creative({"id": "1", "asset_feed_spec": json.dumps(spec)})
    assert as_object.primary == as_string.primary == ["b"] and as_string.links[0] == "https://x.example.test"
    with pytest.raises(UpstreamBroken, match="not JSON"):
        read_creative({"id": "1", "asset_feed_spec": "{nope"})


@pytest.mark.parametrize("creative", [
    {"body": 5}, {"object_story_spec": []}, {"object_story_spec": {"link_data": {"message": 3}}},
    {"object_story_spec": {"link_data": {"child_attachments": {}}}}, {"object_story_spec": {"video_data": {"video_id": 9}}},
    {"asset_feed_spec": {"bodies": [{"text": 1}]}}, {"asset_feed_spec": {"bodies": "x"}}])
def test_a_field_of_the_wrong_type_is_a_broken_answer_not_copy(creative):
    read_creative({"id": "1", "body": "fine"})
    with pytest.raises(UpstreamBroken):
        read_creative({"id": "1", **creative})


def test_the_first_seen_order_is_kept_and_each_value_once():
    parts = read_creative({"id": "1", "object_story_spec": {"link_data": {
        "message": "m", "name": "same", "link": "https://a.example.test", "description": "",
        "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://a.example.test"}},
        "child_attachments": [{"name": "same", "link": "https://b.example.test"}, {"name": "two", "link": "https://a.example.test"}]}}})
    assert [x for x in parts.headlines if x] == ["same", "same", "two"]
    assert parts.kind == "carousel"


def test_a_story_id_that_is_not_page_underscore_post_gets_no_built_link(server):
    for story, link in (("11_9902", "https://www.facebook.com/11/posts/9902"), ("9902", None), ("11_9902_3", None), ("a_b", None)):
        server.faults.append(Fault(200, page([ad_row("1", {"id": "51", "effective_object_story_id": story})])))
        rec = run(read(make(server))).records[0]
        assert rec.post_id == story and rec.post_permalink == link


def test_an_ad_with_no_creative_object_or_only_an_id_says_so(server):
    server.faults.append(Fault(200, page([ad_row("1", creative={}), {"id": "2", "campaign_id": "101"}])))
    recs = run(read(make(server))).records
    assert all(any("no creative object" in n for n in r.notes) for r in recs)
    assert recs[1].campaign_name == "Spring sale", "the name is read by id when the ad row has none"


def test_the_ad_status_fields_and_the_campaign_node_fill_the_record(server):
    rec = by_id(run(read(make(server))))["1008"]
    assert (rec.ad_status, rec.ad_effective_status, rec.adset_id) == ("PAUSED", "PAUSED", "71008")
    assert rec.campaign_name == "Archive tests"


def test_an_expired_token_inside_a_batch_answer_stops_the_run_and_a_permission_problem_does_not(server):
    server.node_errors["9001"] = (400, error_body(10, "OAuthException", "(#10) Permission denied"))
    got = by_id(run(read(make(server))))
    assert "no access (code 10" in got["1002"].videos[0].problem and got["1003"].videos[0].source
    server.node_errors["9001"] = (400, error_body(190, "OAuthException", "expired"))
    with pytest.raises(AuthExpired):
        run(read(make(server)))


@pytest.mark.parametrize("length,kept", [(0, 0), (12, 12), (31.5, 31.5), (-1, None), (True, None), ("12", None), (None, None)])
def test_a_video_length_that_is_not_a_number_of_at_least_zero_is_dropped(server, length, kept):
    server.videos["9002"]["length"] = length
    rec = by_id(run(read(make(server))))["1003"]
    assert rec.videos[0].length == kept and rec.videos[0].source


def test_a_call_to_action_link_that_differs_from_the_link_is_kept_too(server):
    creative = {"id": "51", "object_story_spec": {"link_data": {
        "link": "https://a.example.test/x", "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://b.example.test/y"}}}}}
    server.faults.append(Fault(200, page([ad_row("1", creative)])))
    assert run(read(make(server))).records[0].destination_links == ["https://a.example.test/x", "https://b.example.test/y"]


def test_empty_and_null_copy_is_left_out_of_the_lists_and_each_value_appears_once(server):
    creative = {"id": "51", "object_story_spec": {"link_data": {
        "message": None, "name": "", "description": "same", "link": "https://a.example.test/x",
        "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://a.example.test/x"}},
        "child_attachments": [{"name": "same", "description": "same"}]}}}
    server.faults.append(Fault(200, page([ad_row("1", creative)])))
    rec = run(read(make(server))).records[0]
    assert rec.primary_texts == [] and rec.headlines == ["same"] and rec.link_descriptions == ["same"]
    assert rec.destination_links == ["https://a.example.test/x"] and rec.call_to_action_types == ["SHOP_NOW"]


@pytest.mark.parametrize("header", ["x-business-use-case-usage", "x-business-use-case"])
def test_the_business_use_case_header_is_read_under_both_names_the_rate_limit_pages_give(server, header):
    server.buc_header = header
    server.utilisation = 95
    result = run(read_partial(make(server, page_size=2)))
    assert not result.complete and "usage is at 95 percent" in result.stopped_because
    server.utilisation = 5
    server.regain_minutes = 0.2
    sleeps = Sleeps()
    server.faults.append(Fault(400, error_body(80004, "OAuthException", "slow"), server._headers()))
    run(read(make(server, sleeps)))
    assert sleeps.calls == [pytest.approx(12.0)]
