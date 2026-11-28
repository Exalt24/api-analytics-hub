"""Seeded random ad accounts, read by the creative reader over real HTTP, checked against what the generator built.

tests/meta_creatives_world.py makes each world and knows the answer while it builds it (it never calls the reader), so
this file is a brute force check of the whole read: every ad the filters select, none the filters exclude, each once, in
the order Meta serves them; each ad's copy, links, images, video ids, preview link and post link; each video's source,
permalink and length, or the words for why not; the page count; the batch requests (never more than 50, never one id
twice in a request, only ids a returned ad needs); the token never in a URL of a batch. Faults land on random requests
in the same worlds, so a retry that loses or doubles a page shows up here.

WHAT THIS PROVES: the reader agrees with an independent construction of the documented shapes. It does not prove Meta
sends these shapes: the server is written from the saved pages (docs/META_SOURCES.md).
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.meta_creatives import BATCH_LIMIT, DEFAULT_STATUSES, MetaCreativesClient  # noqa: E402
from app.connectors.meta_creatives_fake import FakeMetaCreatives  # noqa: E402
from meta_creatives_world import ACCOUNT, TOKEN, build_world, expected_permalink  # noqa: E402

#: 80 worlds by default; tests/mutation_check_creatives.py sets this lower for each mutant (its control runs all 80).
SEEDS = range(int(os.environ.get("CREATIVES_ORACLE_SEEDS", "80")))


class NoSleep:
    async def __call__(self, seconds: float) -> None:
        return None


def read_world(seed: int, *, campaign_ids=None, include_deleted=False):
    world = build_world(seed)
    with FakeMetaCreatives(token=TOKEN) as server:
        world.install(server)

        async def go():
            client = MetaCreativesClient(
                ad_account_id=ACCOUNT, access_token=TOKEN, base_url=server.url, page_size=world.page_size,
                max_attempts=6, sleep=NoSleep(), rng=lambda: 1.0,
                client=httpx.AsyncClient(trust_env=False, timeout=10))
            try:
                return await client.fetch_all(campaign_ids=campaign_ids, include_deleted=include_deleted)
            finally:
                await client.aclose()

        run = asyncio.run(go())
        requests = list(server.requests)
    return world, run, requests


def eligible(world, campaign_ids, include_deleted):
    allowed = set(DEFAULT_STATUSES) | ({"DELETED"} if include_deleted else set())
    chosen = [a for a in world.ads if a["effective_status"] in allowed]
    if campaign_ids is None:
        return chosen
    out = []
    for cid in dict.fromkeys(campaign_ids):
        out += [a for a in chosen if a["campaign_id"] == cid]
    return out


def check_record(world, rec):
    exp = world.expected[rec.ad_id]
    assert rec.campaign_id == exp.campaign_id
    assert rec.campaign_name == exp.campaign_name
    assert rec.ad_effective_status == exp.effective_status
    assert rec.creative_kind == exp.kind, (rec.ad_id, rec.creative_kind, exp.kind)
    assert rec.creative_status == exp.creative_status
    assert set(rec.primary_texts) == exp.primary
    assert set(rec.headlines) == exp.headlines
    assert set(rec.link_descriptions) == exp.descriptions
    assert set(rec.call_to_action_types) == exp.ctas
    assert set(rec.destination_links) == exp.links
    assert set(rec.image_urls) == exp.images
    assert set(rec.video_ids) == exp.video_ids
    assert len(rec.video_ids) == len(set(rec.video_ids)), "a video listed twice"
    assert rec.preview_link == exp.preview
    assert rec.post_id == exp.post_id and rec.post_permalink == exp.post_permalink
    assert len(rec.cards) == exp.cards
    for info in rec.videos:
        kind, source, permalink, length = world.video_truth[info.video_id]
        assert info.source == source, (rec.ad_id, info)
        if kind in ("ok", "relative", "nosource", "nosource_null"):
            assert info.permalink_url == expected_permalink(info.video_id, kind)
            assert info.length == length
        else:
            assert info.permalink_url is None and info.length is None
        marker = {"ok": None, "relative": None, "nosource": "no source returned", "nosource_null": "no source returned",
                  "forbidden": "no access", "missing": "not readable"}[kind]
        mine = [n for n in rec.notes if n.startswith("video %s:" % info.video_id)]
        if marker is None:
            assert not mine and info.problem is None
        else:
            assert len(mine) == 1 and marker in mine[0], (info, rec.notes)
    if exp.creative_status == "DELETED":
        assert any("DELETED" in n for n in rec.notes)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_world_reads_back_what_the_generator_built(seed):
    world, run, requests = read_world(seed)
    want = eligible(world, None, False)
    assert run.complete, run.stopped_because
    assert [r.ad_id for r in run.records] == [a["id"] for a in want]
    for rec in run.records:
        check_record(world, rec)
    # pages: the empty pages that still carry a next, then ceil(rows / limit) pages (one when there are no rows)
    assert run.stats.pages == world.empties + max(1, math.ceil(len(want) / world.page_size))
    assert run.stats.ads_seen == len(want) and run.stats.duplicates == 0 and run.stats.filtered_out == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_a_campaign_filter_and_deleted_ads_are_honoured_in_the_same_worlds(seed):
    base = build_world(seed)
    ids = sorted(base.campaigns)
    campaign_ids = ids[::-1][: max(1, (seed % len(ids)) + 1)]
    include_deleted = seed % 3 == 0
    world, run, requests = read_world(seed, campaign_ids=campaign_ids, include_deleted=include_deleted)
    want = eligible(world, campaign_ids, include_deleted)
    assert run.complete, run.stopped_because
    assert [r.ad_id for r in run.records] == [a["id"] for a in want]
    for rec in run.records:
        check_record(world, rec)
        assert rec.campaign_id in campaign_ids
    ad_edges = [r for r in requests if r.method == "GET" and r.path.endswith("/ads") and "after" not in r.query]
    assert {r.path.split("/")[2] for r in ad_edges} >= set(campaign_ids), "every named campaign's own edge is read"


@pytest.mark.parametrize("seed", SEEDS)
def test_batches_are_small_distinct_and_only_for_what_a_returned_ad_needs(seed):
    world, run, requests = read_world(seed)
    returned = {r.ad_id for r in run.records}
    needed_videos = set().union(*[world.expected[a].video_ids for a in returned]) if returned else set()
    posts = [r for r in requests if r.method == "POST"]
    import json
    asked_videos: set[str] = set()
    for post in posts:
        assert post.raw_query == "", "a batch must not put anything in the URL, the token least of all"
        assert post.form.get("access_token") == TOKEN
        items = json.loads(post.form["batch"])
        assert 1 <= len(items) <= BATCH_LIMIT
        ids = [i["relative_url"].split("?")[0] for i in items]
        assert len(ids) == len(set(ids)), "one id twice in one batch"
        asked_videos |= {i for i in ids if i in world.video_truth}
    assert asked_videos == needed_videos, "a video was looked up that no returned ad needs, or one was missed"
    for r in requests:
        if r.method == "GET":
            assert r.query.get("access_token") == TOKEN
