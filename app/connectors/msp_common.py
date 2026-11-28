"""What the three MSP connectors (ConnectWise Manage, Autotask, NinjaOne) share: strict date parsing, the bare address check, hours to whole seconds, and the day arithmetic.

Kept apart from `common.py` so the older connectors are not touched. Every function refuses what it cannot read: a date that is not a date, a duration with a sign, an address
that could send a credential to another host.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlparse

from .base import UpstreamBroken
from .common import to_decimal

__all__ = ["decimal_json", "checked_base", "parse_instant", "seconds_from_hours", "days_of", "utc_day", "LOOPBACK"]

LOOPBACK = ("127.0.0.1", "localhost")
SECONDS_PER_HOUR = Decimal(3600)
_INSTANT = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})?$")


def decimal_json(text: str | bytes) -> Any:
    """JSON with every number that has a fraction read as an exact Decimal, so an hours figure of 2.1 is never 2.0999999999999996."""
    return json.loads(text, parse_float=Decimal)


def checked_base(value: str, *, what: str, allow_path: bool) -> str:
    """The scheme, host and (when `allow_path`) path of an API address, no trailing slash. Only https, or http to a loopback stand-in. No user, query or fragment."""
    parsed = urlparse(str(value).strip())
    loopback = parsed.hostname in LOOPBACK
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("%s must be a plain address, got %r" % (what, value))
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
        raise ValueError("%s must use https" % what)
    path = parsed.path.rstrip("/")
    if not allow_path and path:
        raise ValueError("%s must be a bare address with no path, got %r" % (what, value))
    if ".." in path or "//" in path:
        raise ValueError("%s has a path that is not plain: %r" % (what, value))
    return "%s://%s%s" % (parsed.scheme, parsed.netloc, path)


def parse_instant(value: Any, what: str, *, naive_is_utc: bool) -> datetime:
    """An ISO 8601 date and time as an aware UTC datetime.

    A value with no zone is read as UTC only when the vendor says its times are UTC (`naive_is_utc`, Autotask: "The REST API stores and returns all time data in UTC"); a vendor
    that says nothing about it gets a refusal. A date with no time, a space for the T, a month 13 and text are refused.
    """
    if not isinstance(value, str):
        raise UpstreamBroken("%s is %s, not a date and time" % (what, type(value).__name__))
    m = _INSTANT.match(value.strip())
    if m is None:
        raise UpstreamBroken("%s is not an ISO 8601 date and time" % what)
    day, clock, fraction, zone = m.groups()
    if zone is None and not naive_is_utc:
        raise UpstreamBroken("%s has no time zone, and this vendor's pages do not say it is UTC" % what)
    try:
        moment = datetime.fromisoformat("%sT%s%s" % (day, clock, (fraction or "")[:7]))
    except ValueError:
        raise UpstreamBroken("%s is not a calendar date and time" % what) from None
    if zone is None or zone == "Z":
        return moment.replace(tzinfo=timezone.utc)
    sign = 1 if zone[0] == "+" else -1
    hours, minutes = int(zone[1:3]), int(zone[4:6])
    if hours > 23 or minutes > 59:
        raise UpstreamBroken("%s has an impossible zone offset" % what)
    return moment.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))).astimezone(timezone.utc)


def seconds_from_hours(value: Any, what: str, *, maximum_hours: int = 24) -> int:
    """A decimal number of hours as whole seconds, rounded half up. Below 0 and above `maximum_hours` is a broken answer (the bound is this hub's own: no page gives one)."""
    hours = to_decimal(value, what)
    if hours < 0 or hours > maximum_hours:
        raise UpstreamBroken("%s is outside 0 to %d hours" % (what, maximum_hours))
    return int((hours * SECONDS_PER_HOUR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def utc_day(moment: datetime) -> date:
    return moment.astimezone(timezone.utc).date()


def days_of(start: date, end: date) -> list[date]:
    """Every day of the half open window [start, end)."""
    return [start + timedelta(days=i) for i in range((end - start).days)]
