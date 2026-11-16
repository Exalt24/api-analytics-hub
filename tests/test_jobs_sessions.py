"""Sessions that do not look alike: proxy rotation, a fingerprint for each session and typing at a human pace.

The rotation is shown with two real forward proxies (a stand-in that relays and records) and real Chromium: each new session goes out through the next proxy, a proxy that
failed is skipped until its cool-down ends, and the page really was fetched through it. Typing is shown by a page that counts key events: fill() sets a value with none, the
paced typing makes one for every character."""
from __future__ import annotations

import random

import pytest

pytest.importorskip("playwright")

from app.jobs import formfill  # noqa: E402
from app.jobs.browser import USER_AGENTS, VIEWPORTS, NoProxyAvailable, ProxyRotator, open_page, pick_fingerprint  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from app.jobs.fake_browserbase import FakeBrowserbase  # noqa: E402
from app.jobs.fake_proxy import FakeProxy  # noqa: E402
from jobs_fixtures import APPLICANT, run  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_rotator_hands_out_the_proxies_in_turn_and_skips_one_that_failed_until_its_cooldown_ends():
    clock = Clock()
    r = ProxyRotator(["http://a:1", "http://b:2", "http://c:3"], cooldown=60, clock=clock)
    assert [r.pick() for _ in range(4)] == ["http://a:1", "http://b:2", "http://c:3", "http://a:1"]
    r.failed("http://b:2")
    assert [r.pick() for _ in range(4)] == ["http://c:3", "http://a:1", "http://c:3", "http://a:1"]
    clock.t += 59.9
    assert "http://b:2" not in [r.pick() for _ in range(4)]
    clock.t += 0.2
    assert "http://b:2" in [r.pick() for _ in range(3)]


def test_all_proxies_cooling_down_is_an_error_not_a_direct_connection_and_an_empty_list_is_refused():
    r = ProxyRotator(["http://a:1"], cooldown=60, clock=Clock())
    r.failed("http://a:1")
    with pytest.raises(NoProxyAvailable):
        r.pick()
    with pytest.raises(ValueError):
        ProxyRotator([])


def test_fingerprints_vary_between_sessions_and_a_seed_repeats_them():
    seen = {tuple(sorted((k, str(v)) for k, v in pick_fingerprint(random.Random(i)).items())) for i in range(40)}
    assert len(seen) > 5
    assert pick_fingerprint(random.Random(3)) == pick_fingerprint(random.Random(3))
    fp = pick_fingerprint(random.Random(1))
    assert fp["user_agent"] in USER_AGENTS and (fp["viewport"]["width"], fp["viewport"]["height"]) in VIEWPORTS


def test_each_new_session_goes_out_through_the_next_proxy_and_wears_its_own_fingerprint():
    with FakeProxy() as p1, FakeProxy() as p2, FakeATS() as ats:
        rot = ProxyRotator([p1.url, p2.url])

        async def one(seed):
            async with open_page("local", proxies=rot, rng=random.Random(seed)) as op:
                await op.page.goto(ats.url + "/gh/jobs/1")
                ua = await op.page.evaluate("navigator.userAgent")
                width = await op.page.evaluate("window.innerWidth")
                return op.proxy, ua, width, op.fingerprint

        a, b, c = run(one(1)), run(one(2)), run(one(3))
        assert (a[0], b[0], c[0]) == (p1.url, p2.url, p1.url)
        assert len(p1.hits) >= 2 and len(p2.hits) >= 1 and all("/gh/jobs/1" in h for h in p1.hits[:1] + p2.hits[:1])
        for proxy, ua, width, fp in (a, b, c):
            assert ua == fp["user_agent"] and width == fp["viewport"]["width"]
        assert a[3] != b[3]


def test_paced_typing_makes_a_key_event_for_every_character_and_the_values_still_arrive():
    kb = {**APPLICANT}
    for k in ("work_authorization", "referral_source", "linkedin"):
        kb.pop(k)

    async def go(url, delay):
        async with open_page("local") as op:
            await op.page.goto(url)
            out = await formfill.fill_form(op.page, kb, submit=False, typing_delay=delay)
            keys = await op.page.evaluate("window.__keys")
            vals = await op.page.evaluate("[...document.querySelectorAll('input:not([type=file]):not([type=hidden])')].map(e => e.value)")
            return out, keys, vals

    with FakeATS() as ats:
        paced, keys, vals = run(go(ats.url + "/gh/jobs/1", (2, 6)))
        plain, plain_keys, _ = run(go(ats.url + "/gh/jobs/1", None))
    typed = ["Sam", "Rivera", "sam.rivera@example.test", "+15555550123"]
    assert all(t in vals for t in typed)
    assert keys >= sum(len(t) for t in typed) and plain_keys == 0
    assert paced.reviews and plain.reviews, "the same fields are still left to a person with or without pacing"


def test_a_browserbase_session_is_created_with_the_proxy_in_its_documented_setting():
    with FakeBrowserbase() as bb, FakeProxy() as p, FakeATS() as ats:
        rot = ProxyRotator(["http://user:secret@%s" % p.url.split("//")[1]])

        async def go():
            async with open_page("browserbase", browserbase_api_key="bb_test_key", browserbase_base_url=bb.url, proxies=rot) as op:
                await op.page.goto(ats.url + "/gh/jobs/1")
                return op.proxy

        run(go())
        import json

        body = json.loads([r for r in bb.requests if r.method == "POST" and r.path == "/v1/sessions"][0].body)
        (cfg,) = body["proxies"]
        assert cfg["type"] == "external" and cfg["server"].startswith("http://127.0.0.1:") and cfg["username"] == "user" and cfg["password"] == "secret"
