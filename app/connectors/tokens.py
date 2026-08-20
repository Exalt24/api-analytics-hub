"""Offline access tokens that survive expiry.

WHY THIS EXISTS RATHER THAN A LONG-LIVED STRING IN A CONFIG FILE.

Shopify offline access tokens used to be permanent, revoked only on uninstall.
They can now expire after 60 minutes and arrive with a refresh token, and
expiring offline tokens are already mandatory for new public apps and become
mandatory for all public apps on 2027-01-01, after which a non-expiring token
returns authentication errors.

So "get a permanent token" is chasing something the platform is retiring. The
durable answer is to make expiry a non-event, which is what this does.

THREE DECISIONS, each because the obvious version breaks quietly:

  1. THE EXPIRY IS STORED AS AN ABSOLUTE INSTANT, never the relative
     `expires_in` the provider returns. A relative value is already wrong the
     moment it is read back from disk, and the failure looks like a bad token
     rather than a stale clock.

  2. REFRESH HAPPENS BEHIND ONE LOCK. Five parallel sync workers noticing
     expiry at the same moment will each mint a new token, and on many providers
     each mint invalidates the previous one, so the workers knock each other out
     in a loop that looks like the provider rejecting valid credentials.

  3. IT REFRESHES EARLY, on a skew margin. A token that is valid for another two
     seconds is not usable: the request will be in flight when it dies. Treating
     "nearly expired" as "expired" removes a whole class of intermittent failure
     that is almost impossible to reproduce.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

#: Refresh this far before the real deadline. Covers clock skew between us and
#: the provider plus the round trip of the request about to be made.
REFRESH_SKEW_SECONDS = 120.0


@dataclass
class TokenSet:
    access_token: str
    #: Absolute epoch seconds. None means the token does not expire, which is
    #: still the case for custom-distribution apps today.
    expires_at: float | None = None
    refresh_token: str | None = None

    @property
    def expires_soon(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - REFRESH_SKEW_SECONDS)

    @property
    def is_refreshable(self) -> bool:
        return bool(self.refresh_token)


class TokenExpiredAndUnrefreshable(RuntimeError):
    """The token is dead and there is no refresh token to recover with.

    Deliberately distinct from a transient auth failure: this needs a human to
    reconnect the store, and retrying it just burns quota while looking busy.
    """


@dataclass
class TokenManager:
    """Hands out a valid access token, refreshing when needed.

    `refresher` is injected rather than hardcoded so the same logic serves any
    provider and so the tests can drive it without a network.
    """

    tokens: TokenSet
    refresher: Callable[[str], Awaitable[TokenSet]] | None = None
    #: Guards the refresh so concurrent callers cannot each mint a token.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Counted so a caller can assert refresh happened once rather than N times.
    refresh_count: int = 0

    async def valid_token(self) -> str:
        if not self.tokens.expires_soon:
            return self.tokens.access_token

        async with self._lock:
            # Re-check inside the lock. Without this the workers that queued
            # behind the first one each refresh in turn, which is the exact bug
            # the lock was added to prevent.
            if not self.tokens.expires_soon:
                return self.tokens.access_token

            if not self.tokens.is_refreshable or self.refresher is None:
                raise TokenExpiredAndUnrefreshable(
                    "offline token expired with no refresh token; the store must be reconnected"
                )

            self.tokens = await self.refresher(self.tokens.refresh_token or "")
            self.refresh_count += 1

            if self.tokens.expires_soon:
                # A refresh that returns an already-stale token would otherwise
                # loop forever, hammering the provider and looking like a
                # rate-limit problem.
                raise TokenExpiredAndUnrefreshable(
                    "refresh returned a token that is already expiring; refusing to loop"
                )
            return self.tokens.access_token
