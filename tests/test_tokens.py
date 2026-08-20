"""Token refresh tests, including the concurrency case that is the whole point.

The single-refresh-under-contention test is the one worth having. It is the bug
that makes an integration look like the provider is rejecting valid credentials,
and it only appears under parallelism, which is exactly when nobody is watching.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.tokens import (
    REFRESH_SKEW_SECONDS,
    TokenExpiredAndUnrefreshable,
    TokenManager,
    TokenSet,
)


def test_a_non_expiring_token_is_never_refreshed():
    # Still the case for custom-distribution apps, so it must not be treated as
    # an error or refreshed pointlessly.
    mgr = TokenManager(TokenSet("tok", expires_at=None))
    assert asyncio.run(mgr.valid_token()) == "tok"
    assert mgr.refresh_count == 0


def test_a_healthy_token_is_returned_untouched():
    mgr = TokenManager(TokenSet("tok", expires_at=time.time() + 3600))
    assert asyncio.run(mgr.valid_token()) == "tok"
    assert mgr.refresh_count == 0


def test_an_expired_token_is_refreshed():
    async def refresher(rt):
        assert rt == "refresh-me"
        return TokenSet("fresh", expires_at=time.time() + 3600, refresh_token="next")

    mgr = TokenManager(
        TokenSet("stale", expires_at=time.time() - 1, refresh_token="refresh-me"),
        refresher=refresher,
    )
    assert asyncio.run(mgr.valid_token()) == "fresh"
    assert mgr.refresh_count == 1


def test_a_token_expiring_inside_the_skew_window_is_refreshed_early():
    """Valid for another few seconds is not usable: the request will be in flight
    when it dies. This is the intermittent failure nobody can reproduce."""
    async def refresher(rt):
        return TokenSet("fresh", expires_at=time.time() + 3600, refresh_token="next")

    # A FIXED 60 seconds, deliberately not derived from REFRESH_SKEW_SECONDS.
    # The first version computed this as skew/2, so mutating the skew to 0 moved
    # the test window with it and the test passed against the broken code. A test
    # must not measure a constant using that constant.
    assert REFRESH_SKEW_SECONDS > 60, "this test assumes the skew exceeds 60s"
    almost = time.time() + 60
    mgr = TokenManager(TokenSet("stale", expires_at=almost, refresh_token="r"), refresher=refresher)
    assert asyncio.run(mgr.valid_token()) == "fresh"
    assert mgr.refresh_count == 1


def test_concurrent_workers_refresh_exactly_once():
    """THE case. Ten workers notice expiry together.

    Without the lock plus the re-check inside it, each mints a token, and on
    providers where a mint invalidates the previous one they knock each other out
    in a loop that reads as the provider rejecting valid credentials.
    """
    calls = {"n": 0}

    async def refresher(rt):
        calls["n"] += 1
        # A real refresh is a network round trip, so yield to let the other
        # waiters pile up behind the lock. Without the await this test can pass
        # by accident on a single scheduler tick.
        await asyncio.sleep(0.02)
        return TokenSet("fresh", expires_at=time.time() + 3600, refresh_token="next")

    mgr = TokenManager(
        TokenSet("stale", expires_at=time.time() - 1, refresh_token="r"), refresher=refresher
    )

    async def run():
        return await asyncio.gather(*(mgr.valid_token() for _ in range(10)))

    results = asyncio.run(run())
    assert results == ["fresh"] * 10
    assert calls["n"] == 1, f"refreshed {calls['n']} times under contention, must be exactly 1"
    assert mgr.refresh_count == 1


def test_expired_with_no_refresh_token_raises_rather_than_retrying():
    mgr = TokenManager(TokenSet("stale", expires_at=time.time() - 1, refresh_token=None))
    with pytest.raises(TokenExpiredAndUnrefreshable):
        asyncio.run(mgr.valid_token())


def test_a_refresh_returning_a_stale_token_refuses_to_loop():
    """Otherwise this spins against the provider forever and presents as a rate
    limit rather than as the broken refresh it actually is."""
    async def bad_refresher(rt):
        return TokenSet("still-stale", expires_at=time.time() - 1, refresh_token="r")

    mgr = TokenManager(
        TokenSet("stale", expires_at=time.time() - 1, refresh_token="r"), refresher=bad_refresher
    )
    with pytest.raises(TokenExpiredAndUnrefreshable):
        asyncio.run(mgr.valid_token())


def test_expiry_is_absolute_not_relative():
    """A relative expires_in is wrong the moment it is read back from disk.

    Simulated by constructing the set from an absolute instant computed in the
    past, the way a value persisted an hour ago would look now.
    """
    persisted_an_hour_ago = time.time() - 3600 + 1800  # was "valid 30 more minutes" then
    mgr = TokenManager(TokenSet("tok", expires_at=persisted_an_hour_ago, refresh_token=None))
    # It must be recognised as expired NOW, not treated as still having 30 minutes.
    with pytest.raises(TokenExpiredAndUnrefreshable):
        asyncio.run(mgr.valid_token())
