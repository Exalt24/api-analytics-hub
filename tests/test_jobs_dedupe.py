"""The Redis sliding window against a plain Python model of the same rule, over many seeded random worlds.

The rule, written as the model: a fingerprint is `seen` for a call at time t when it was recorded at some time s with t - s < window (a sighting exactly one window old has expired), where
s is the LATEST time it was recorded (every call, hit or miss, records it again, so the window slides); a repeat inside one call counts as seen
the second time; tenants never share a window. The generator varies everything that can cross: several tenants, fingerprints that repeat within
a call and across calls, gaps of zero, just under, exactly and just over the window, and many steps in one world. The model is the oracle; the
test fails on the first call where Redis and the model disagree and prints the world's seed, so it can be replayed.

MUTATION CONTROLS at the bottom: four wrong versions of the script (no trimming, no sliding, a boundary one off, a shared key for all tenants)
must each fail this same test. A test that passes against them is not testing the window.
"""
from __future__ import annotations

import random
import uuid

import pytest

from app.jobs import dedupe
from jobs_fixtures import REDIS_URL, needs

pytestmark = needs("redis")

redis = pytest.importorskip("redis")

WINDOW = 100.0


class Model:
    def __init__(self, window: float) -> None:
        self.w = window
        self.last: dict[tuple[str, str], float] = {}

    def call(self, tenant: str, fps: list[str], now: float) -> list[bool]:
        out = []
        for fp in fps:
            s = self.last.get((tenant, fp))
            out.append(s is not None and now - s < self.w)
            self.last[(tenant, fp)] = now
        return out


def world(seed: int, script: str | None = None, shared_key: bool = False) -> None:
    rng = random.Random(seed)
    client = redis.Redis.from_url(REDIS_URL)
    prefix = "test:jobs:%s" % uuid.uuid4().hex[:8]
    d = dedupe.SlidingWindowDedupe(client, window_seconds=WINDOW, prefix=prefix)
    if script is not None:
        d._script = client.register_script(script)  # noqa: SLF001 - the mutation controls swap the script
    if shared_key:
        d._key = lambda tenant: "%s:shared" % prefix  # noqa: SLF001
    model = Model(WINDOW)
    tenants = ["t%d" % i for i in range(rng.randint(1, 3))]
    pool = ["fp%02d" % i for i in range(rng.randint(2, 8))]
    now = 1_000_000.0
    try:
        for step in range(rng.randint(15, 40)):
            now += rng.choice([0.0, 0.5, 1.0, WINDOW - 0.5, WINDOW, WINDOW + 0.5, WINDOW * 3, rng.uniform(0, WINDOW * 1.5)])
            tenant = rng.choice(tenants)
            fps = [rng.choice(pool) for _ in range(rng.randint(1, 5))]
            want = model.call(tenant, fps, now)
            got = d.seen_many(tenant, fps, now=now)
            assert got == want, "seed %d step %d tenant %s fps %s at %.1f: redis %s, model %s" % (seed, step, tenant, fps, now, got, want)
    finally:
        for k in client.scan_iter(prefix + "*"):
            client.delete(k)


SEEDS = range(60)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_window_agrees_with_the_model(seed):
    world(seed)


def test_an_empty_call_does_nothing_and_a_non_positive_window_is_refused():
    client = redis.Redis.from_url(REDIS_URL)
    d = dedupe.SlidingWindowDedupe(client, window_seconds=10, prefix="test:jobs:empty")
    assert d.seen_many("t", []) == []
    with pytest.raises(ValueError):
        dedupe.SlidingWindowDedupe(client, window_seconds=0)


def test_redis_being_down_is_a_named_error_not_a_crash():
    client = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    d = dedupe.SlidingWindowDedupe(client, window_seconds=10, prefix="test:jobs:down")
    with pytest.raises(dedupe.DedupeUnavailable):
        d.seen_many("t", ["a"])


def test_the_set_does_not_grow_past_the_window():
    client = redis.Redis.from_url(REDIS_URL)
    prefix = "test:jobs:size:%s" % uuid.uuid4().hex[:6]
    d = dedupe.SlidingWindowDedupe(client, window_seconds=50, prefix=prefix)
    try:
        for i in range(200):
            d.seen_many("t", ["fp%d" % i], now=1000.0 + i)
        assert d.size("t") <= 51
    finally:
        client.delete("%s:t" % prefix)


def _mutants() -> dict[str, tuple[str, bool]]:
    s = dedupe.SCRIPT
    return {
        "never trims": (s.replace("redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)", ""), False),
        "does not slide": (s.replace("redis.call('ZADD', key, now, ARGV[i])", "if not was then redis.call('ZADD', key, now, ARGV[i]) end"), False),
        "boundary one off": (s.replace("now - window)", "now - window + 1)"), False),
        "one key for every tenant": (s, True),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_each_wrong_script_fails_the_same_property(name):
    script, shared = _mutants()[name]
    assert script != dedupe.SCRIPT or shared, "the mutation did not change the script"
    failures = 0
    for seed in range(60):
        try:
            world(seed, script=script, shared_key=shared)
        except AssertionError:
            failures += 1
    assert failures > 0, "%s was not caught by 60 worlds" % name
