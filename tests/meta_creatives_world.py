"""Seeded random ad accounts for the creative reader, built together with what the reader must return for each ad.

The generator does not run the reader's code to know the answer. It builds each creative from values it chose, so the
expected copy, links, images, videos and notes are known at the moment the creative is made, and a test compares the
reader's output to that. Every input dimension varies together in one world: the creative shape (link, carousel, video,
photo, dynamic with videos and images, an existing post, a creative with only its own fields, a deleted creative), the
ad status, the campaign, the video behaviour (source, no source key, null source, relative permalink, no access, missing),
a video shared by many ads, more videos than one batch holds, the page size, empty pages that still carry a next link,
an expansion the server will not do, the asset feed as a string, transient faults on random requests, and batch answers
that time out or throttle once.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from app.connectors.meta_creatives_fake import FakeMetaCreatives, Fault, error_body

ACCOUNT = "555001"
TOKEN = "tok-SECRET-creatives-123"
STATUS_MIX = ["ACTIVE"] * 10 + ["PAUSED"] * 4 + ["ARCHIVED"] * 2 + ["DELETED"] * 2 + ["DISAPPROVED", "WITH_ISSUES", "ADSET_PAUSED"]
ODD_TEXT = ["Save 20% today", "Zwei Wege, ein Ziel", "Line one\nline two", "a | b | c", "=SUM(1+1)", "  padded  ", "café ☕",
            "quote \"inside\" it", "comma, here", "@handle", "-5 off", "plain"]
VIDEO_KINDS = ["ok", "ok", "ok", "nosource", "nosource_null", "forbidden", "missing", "relative"]


@dataclass
class Expected:
    ad_id: str
    campaign_id: str
    campaign_name: str
    kind: str
    effective_status: str
    creative_status: str | None
    primary: set[str] = field(default_factory=set)
    headlines: set[str] = field(default_factory=set)
    descriptions: set[str] = field(default_factory=set)
    ctas: set[str] = field(default_factory=set)
    links: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)
    video_ids: set[str] = field(default_factory=set)
    preview: str | None = None
    post_id: str | None = None
    post_permalink: str | None = None
    cards: int = 0


@dataclass
class World:
    seed: int
    campaigns: dict[str, str]
    ads: list[dict[str, Any]]
    expected: dict[str, Expected]
    #: video id -> (kind, expected source, expected permalink, expected length)
    video_truth: dict[str, tuple[str, str | None, str | None, Any]]
    page_size: int
    empties: int
    refuse_expansion: set[str]
    asset_feed_as_string: bool
    faults: dict[int, Fault]
    batch_null_once: set[str]
    batch_throttle_once: set[str]

    def install(self, server: FakeMetaCreatives) -> None:
        server.set_account(ACCOUNT, campaigns=self.campaigns, ads=self.ads)
        server.empty_pages_with_next = self.empties
        server.refuse_expansion = set(self.refuse_expansion)
        server.asset_feed_as_string = self.asset_feed_as_string
        server.fault_at = dict(self.faults)
        server.batch_null_once = set(self.batch_null_once)
        server.batch_throttle_once = set(self.batch_throttle_once)
        for vid, (kind, source, permalink, length) in self.video_truth.items():
            if kind == "missing":
                continue
            if kind == "forbidden":
                server.node_errors[vid] = (400, error_body(200, "OAuthException", "(#200) Permissions error"))
                continue
            node: dict[str, Any] = {"length": length}
            if permalink is not None:
                node["permalink_url"] = permalink_raw(vid, kind)
            if kind in ("ok", "relative"):
                node["source"] = source
            elif kind == "nosource_null":
                server.video_source_null = True
            server.videos[vid] = node


def permalink_raw(video_id: str, kind: str) -> str:
    return "/page1/videos/%s/" % video_id if kind == "relative" else "https://www.facebook.com/page1/videos/%s/" % video_id


def expected_permalink(video_id: str, kind: str) -> str:
    return "https://www.facebook.com/page1/videos/%s/" % video_id


def _text(rng: random.Random, n: int) -> str:
    return "%s #%d" % (rng.choice(ODD_TEXT), n)


def build_world(seed: int) -> World:
    rng = random.Random(seed)
    campaigns = {str(100 + i): "Campaign %d %s" % (i, rng.choice(["Spring", "Brand | Q4", "=Test", "café"])) for i in range(rng.randint(1, 4))}
    big = rng.random() < 0.12
    n_ads = rng.randint(0, 14) if not big else rng.randint(14, 20)
    video_pool = ["%d" % (9_000_000 + i) for i in range(rng.randint(0, 6) if not big else rng.randint(55, 70))]
    video_truth: dict[str, tuple[str, str | None, str | None, Any]] = {}
    for vid in video_pool:
        kind = rng.choice(VIDEO_KINDS)
        length = rng.choice([0, 7, 31.5, 120])
        source = "https://video.example.test/v/%s.mp4" % vid if kind in ("ok", "relative") else None
        permalink = expected_permalink(vid, kind) if kind in ("ok", "relative", "nosource", "nosource_null") else None
        video_truth[vid] = (kind, source, permalink, length if kind not in ("forbidden", "missing") else None)
    unused_videos = list(video_pool)
    rng.shuffle(unused_videos)

    def pick_video() -> str | None:
        if not video_pool:
            return None
        if big and unused_videos:
            return unused_videos.pop()
        return rng.choice(video_pool)

    ads: list[dict[str, Any]] = []
    expected: dict[str, Expected] = {}
    for i in range(n_ads):
        ad_id = str(10_000 + i * 7)
        campaign_id = rng.choice(list(campaigns))
        effective = rng.choice(STATUS_MIX) if not (big and i < 4) else "ACTIVE"
        creative_id = str(50_000 + i)
        exp = Expected(ad_id, campaign_id, campaigns[campaign_id], "unknown", effective, "ACTIVE")
        exp.preview = "https://fb.me/preview/%s" % ad_id if rng.random() < 0.85 else None
        shape = rng.choice(["link", "carousel", "video", "photo", "dynamic", "post", "plain", "deleted", "dynamic_images"])
        if big and i < 4:
            shape = "dynamic"      # a big world has dynamic creatives that name many videos, so a batch has to be split
        creative: dict[str, Any] = {"id": creative_id, "name": "Creative %d" % i, "status": "ACTIVE"}
        n = i * 10

        def cta(kind: str, link: str) -> dict:
            exp.ctas.add(kind)
            exp.links.add(link)
            return {"type": kind, "value": {"link": link}}

        if shape == "link":
            msg, head, desc = _text(rng, n), _text(rng, n + 1), _text(rng, n + 2)
            link, pic = "https://shop.example.test/p/%d" % i, "https://img.example.test/%d.jpg" % i
            exp.kind = "link"
            exp.primary, exp.headlines, exp.descriptions, exp.links, exp.images = {msg}, {head}, {desc}, {link}, {pic}
            creative["object_story_spec"] = {"page_id": "11", "link_data": {
                "message": msg, "name": head, "description": desc, "link": link, "picture": pic,
                "call_to_action": cta(rng.choice(["SHOP_NOW", "LEARN_MORE", "SIGN_UP"]), link)}}
        elif shape == "carousel":
            msg = _text(rng, n)
            top = "https://shop.example.test/c/%d" % i
            exp.kind, exp.primary, exp.links = "carousel", {msg}, {top}
            cards = []
            for k in range(rng.randint(2, 5)):
                name, desc, link = _text(rng, n + k), _text(rng, n + 50 + k), "https://shop.example.test/c/%d/%d" % (i, k)
                card: dict[str, Any] = {"name": name, "description": desc, "link": link,
                                        "call_to_action": cta(rng.choice(["SHOP_NOW", "LEARN_MORE"]), link)}
                exp.headlines.add(name)
                exp.descriptions.add(desc)
                if rng.random() < 0.5:
                    card["picture"] = "https://img.example.test/c/%d/%d.jpg" % (i, k)
                    exp.images.add(card["picture"])
                else:
                    card["image_hash"] = "hash%d%d" % (i, k)
                vid = pick_video() if rng.random() < 0.4 else None
                if vid:
                    card["video_id"] = vid
                    exp.video_ids.add(vid)
                cards.append(card)
            exp.cards = len(cards)
            creative["object_story_spec"] = {"page_id": "11", "link_data": {"message": msg, "link": top, "child_attachments": cards}}
        elif shape == "video":
            msg, title, desc = _text(rng, n), _text(rng, n + 1), _text(rng, n + 2)
            link, thumb = "https://shop.example.test/v/%d" % i, "https://img.example.test/v/%d.jpg" % i
            vid = pick_video()
            exp.kind = "video" if vid else "video"
            exp.primary, exp.headlines, exp.descriptions, exp.images = {msg}, {title}, {desc}, {thumb}
            data: dict[str, Any] = {"message": msg, "title": title, "link_description": desc, "image_url": thumb,
                                    "call_to_action": cta("WATCH_MORE", link)}
            if vid:
                data["video_id"] = vid
                exp.video_ids.add(vid)
            creative["object_story_spec"] = {"page_id": "11", "video_data": data}
        elif shape == "photo":
            caption, url = _text(rng, n), "https://img.example.test/p/%d.jpg" % i
            exp.kind, exp.primary, exp.images = "photo", {caption}, {url}
            creative["object_story_spec"] = {"page_id": "11", "photo_data": {"caption": caption, "url": url}}
        elif shape in ("dynamic", "dynamic_images"):
            exp.kind = "dynamic"
            spec: dict[str, Any] = {"bodies": [], "titles": [], "descriptions": [], "link_urls": [], "call_to_action_types": []}
            for k in range(rng.randint(1, 3)):
                b, t = _text(rng, n + k), _text(rng, n + 20 + k)
                spec["bodies"].append({"text": b})
                spec["titles"].append({"text": t})
                exp.primary.add(b)
                exp.headlines.add(t)
            if rng.random() < 0.6:
                d = _text(rng, n + 40)
                spec["descriptions"].append({"text": d})
                exp.descriptions.add(d)
            for k in range(rng.randint(1, 2)):
                u = "https://shop.example.test/d/%d/%d" % (i, k)
                spec["link_urls"].append({"website_url": u})
                exp.links.add(u)
            ctype = rng.choice(["LEARN_MORE", "SHOP_NOW"])
            spec["call_to_action_types"].append(ctype)
            exp.ctas.add(ctype)
            if shape == "dynamic":
                spec["videos"] = []
                for _ in range(rng.randint(1, 4) if not big else rng.randint(16, 20)):
                    vid = pick_video()
                    if vid:
                        spec["videos"].append({"video_id": vid, "thumbnail_url": "https://img.example.test/t/%s.jpg" % vid})
                        exp.video_ids.add(vid)
            else:
                spec["images"] = [{"hash": "h%d%d" % (i, k), "url": "https://img.example.test/d/%d/%d.jpg" % (i, k)}
                                  for k in range(rng.randint(1, 3))]
                exp.images = {im["url"] for im in spec["images"]}
            creative["object_story_spec"] = {"page_id": "11"}
            creative["asset_feed_spec"] = spec
        elif shape == "post":
            exp.kind = "post"
            story = "11_%d" % (9_900 + i)
            creative["object_story_id"] = story
            creative["effective_object_story_id"] = story
            exp.post_id, exp.post_permalink = story, "https://www.facebook.com/11/posts/%d" % (9_900 + i)
            if rng.random() < 0.3:
                creative["instagram_permalink_url"] = "https://www.instagram.com/p/X%d/" % i
        elif shape == "plain":
            body, title = _text(rng, n), _text(rng, n + 1)
            url, img = "https://shop.example.test/o/%d" % i, "https://img.example.test/o/%d.jpg" % i
            creative.update({"body": body, "title": title, "object_url": url, "image_url": img,
                             "call_to_action_type": "LEARN_MORE"})
            exp.kind, exp.primary, exp.headlines, exp.links, exp.images, exp.ctas = "link", {body}, {title}, {url}, {img}, {"LEARN_MORE"}
            vid = pick_video() if rng.random() < 0.4 else None
            if vid:
                creative["video_id"] = vid
                exp.kind = "video"
                exp.video_ids.add(vid)
        else:  # deleted
            creative = {"id": creative_id, "status": "DELETED"}
            exp.kind, exp.creative_status = "unknown", "DELETED"
        ads.append({
            "id": ad_id, "name": "Ad %d" % i, "status": effective if effective in ("ACTIVE", "PAUSED", "DELETED", "ARCHIVED") else "ACTIVE",
            "effective_status": effective, "campaign_id": campaign_id, "adset_id": "7%s" % ad_id,
            **({"preview_shareable_link": exp.preview} if exp.preview else {}), "creative": creative})
        expected[ad_id] = exp

    faults: dict[int, Fault] = {}
    for number in range(0, 40):
        if rng.random() < 0.12 and not (number - 1 in faults and number - 2 in faults):
            faults[number] = rng.choice([
                Fault(400, error_body(4, "OAuthException", "(#4) Application request limit reached")),
                Fault(400, error_body(2, "OAuthException", "temporary")),
                Fault(503, {"unexpected": "html-ish"}),
                Fault(0, None, drop=True),
            ])
    lookups = [v for v in video_pool] + ["%d" % (50_000 + i) for i in range(n_ads)] + list(campaigns)
    return World(
        seed=seed, campaigns=campaigns, ads=ads, expected=expected, video_truth=video_truth,
        page_size=rng.choice([1, 2, 3, 5, 50]), empties=rng.choice([0, 0, 1, 2]),
        refuse_expansion=set(rng.sample(["creative", "campaign"], rng.randint(0, 2))),
        asset_feed_as_string=rng.random() < 0.3, faults=faults,
        batch_null_once=set(rng.sample(lookups, min(len(lookups), rng.randint(0, 3)))),
        batch_throttle_once=set(rng.sample(lookups, min(len(lookups), rng.randint(0, 2)))))
