"""The parts the five later connectors (Google Ads, TikTok, Square, QuickBooks, Shopmonkey) share.

Meta, GA4 and Search Console each carry their own copy of these rules, written when only one connector
existed. Five more would be five more copies, so the rules live here once: the integer-minor-unit money
parse, the whole-number parse, and one bounded retry loop. The older three are not changed to use it.

MONEY. An amount arrives as text, an int or a `Decimal` (a connector that reads a JSON number parses it
with `parse_float=Decimal`, because `float("19.99")` is `19.989999999999998`). A float is refused, a negative
amount is refused unless the caller says negatives are allowed (a refund line can be), and an amount finer than
the currency's minor unit is rounded half up, as every other amount in this hub is.

THE RETRY LOOP. `Retrier.run(send, classify)` calls `send()` up to `max_attempts` times. `classify(response)`
returns None for an answer to keep, raises the typed error for a failure retrying cannot fix, and returns a
`Retry` for one that may pass. A wait the response asks for that is longer than `backoff_cap` is handed back as
`RateLimited` for the runner to own, never slept here, so a vendor that says "wait 60 seconds" cannot hold a
sync worker for a minute per call.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import httpx

from .base import RateLimited, UpstreamBroken
from .shopify import _ZERO_DECIMAL

#: The largest value the snapshot table's bigint column holds. A response past it is broken, not a figure.
BIGINT_MAX = 9_223_372_036_854_775_807


def minor_exponent(currency: str) -> int:
    return 0 if currency.upper() in _ZERO_DECIMAL else 2


def to_decimal(value: Any, what: str) -> Decimal:
    """A finite Decimal from text, an int or a Decimal. A float, a bool, None or text that is not a number is refused."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise UpstreamBroken("%s is %s, not a number or numeric string" % (what, type(value).__name__))
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not number.is_finite():
        raise UpstreamBroken("%s is not a finite number" % what)
    return number


def minor_units(value: Any, currency: str, what: str, *, allow_negative: bool = False) -> int:
    """An amount in major units to an integer of minor units, rounded half up."""
    amount = to_decimal(value, what)
    if amount < 0 and not allow_negative:
        raise UpstreamBroken("%s must be an amount of at least 0, got %s" % (what, value))
    exponent = minor_exponent(currency)
    if abs(amount) > BIGINT_MAX // (10 ** exponent):
        raise UpstreamBroken("%s is larger than any amount this hub stores" % what)
    return int((amount * (10 ** exponent)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micros_to_minor(value: Any, currency: str, what: str) -> int:
    """An amount in millionths of the major unit (Google's `cost_micros`) to minor units, rounded half up."""
    micros = to_decimal(value, what)
    if micros < 0:
        raise UpstreamBroken("%s must be at least 0, got %s" % (what, value))
    if micros != micros.to_integral_value():
        raise UpstreamBroken("%s must be a whole number of micros, got %s" % (what, value))
    if micros > BIGINT_MAX:
        raise UpstreamBroken("%s is larger than any amount this hub stores" % what)
    return int((micros / Decimal(10) ** (6 - minor_exponent(currency))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def whole_number(value: Any, what: str) -> int:
    """A count that must be a whole number of at least 0 (text, int or Decimal; "3.0" is accepted, "3.5" is not)."""
    number = to_decimal(value, what)
    if number != number.to_integral_value() or number < 0:
        raise UpstreamBroken("%s must be a whole number of at least 0, got %s" % (what, value))
    if number > BIGINT_MAX:
        raise UpstreamBroken("%s is larger than any count this hub stores" % what)
    return int(number)


def milli_units(value: Any, what: str) -> int:
    """A count that may be fractional (Google's data driven conversions) in thousandths, rounded half up."""
    number = to_decimal(value, what)
    if number < 0:
        raise UpstreamBroken("%s must be at least 0, got %s" % (what, value))
    if number > BIGINT_MAX // 1000:
        raise UpstreamBroken("%s is larger than any count this hub stores" % what)
    return int((number * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass
class Retry:
    """A failure that may pass. `wait` is the vendor's own figure in seconds, or None to back off."""

    wait: float | None
    problem: str


class Retrier:
    def __init__(self, *, max_attempts: int = 3, backoff_base: float = 1.0, backoff_cap: float = 30.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: Callable[[], float] = random.random):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts, self.backoff_base, self.backoff_cap = max_attempts, backoff_base, backoff_cap
        self._sleep, self._rng = sleep, rng

    def delay(self, attempt: int, wait: float | None) -> float:
        if wait is not None:
            return wait
        return min(self.backoff_cap, self.backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)

    async def run(self, send: Callable[[], Awaitable[httpx.Response]],
                  classify: Callable[[httpx.Response], Retry | None]) -> tuple[httpx.Response, int, int]:
        """(response, http attempts, waits). Raises RateLimited when the attempts are spent or the wait is too long."""
        waits = 0
        for attempt in range(self.max_attempts):
            last = attempt == self.max_attempts - 1
            wait: float | None = None
            try:
                response = await send()
            except httpx.HTTPError as exc:
                problem = "transport failure (%s)" % type(exc).__name__
            else:
                verdict = classify(response)
                if verdict is None:
                    return response, attempt + 1, waits
                wait, problem = verdict.wait, verdict.problem
            if last:
                raise RateLimited(wait, "gave up after %d attempts: %s" % (self.max_attempts, problem))
            delay = self.delay(attempt, wait)
            if delay > self.backoff_cap:
                raise RateLimited(wait, "asked to wait %.0f seconds: %s" % (delay, problem))
            waits += 1
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def retry_after(response: httpx.Response) -> float | None:
    """A numeric Retry-After header in seconds, else None. A date form is not read."""
    raw = response.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def text_of(response: httpx.Response, limit: int = 160) -> str:
    """HTTP status only. A body is never put in an error message: it can carry the token or customer data."""
    return "HTTP %d" % response.status_code
