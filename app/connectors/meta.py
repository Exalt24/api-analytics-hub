"""Meta Marketing API connector: daily paid media figures per ad account.

The third connector behind the contract in `base.py`. It is a class, a registry entry and one migration
that adds five metric keys and teaches the currency check about two of them; the sync runner and the
snapshot table are the ones Shopify and GA4 already use.

WHAT IT CALLS. `GET {base}/v26.0/act_{id}/insights` at account level, one row per day (`time_increment=1`),
the fields `spend`, `impressions`, `clicks`, `account_currency`, `actions` and `action_values`, and the
attribution window named in the request. Every fact about that call, with the page it came from and what is
not confirmed, is in docs/META_SOURCES.md. It has been run only against the stand-in server in
`meta_fake.py`, never against a live ad account: no account is held.

MONEY AND CURRENCY. `spend` and the purchase value arrive as numeric strings in major units of the AD
ACCOUNT's currency, which is not necessarily the store's. They are stored as integers in minor units with the
currency beside them, and the currency comes from the response (`account_currency`), never from a default:
a row that does not name one is refused, and so is a window whose rows name two.

WHAT A PURCHASE IS. The action type list on the AdsActionStats page names `omni_purchase`,
`offsite_conversion.fb_pixel_purchase` and `onsite_conversion.purchase` as purchases. The pages read do not say
how they overlap (UNVERIFIED), so adding them could count one purchase more than once. Only `omni_purchase` is
read, the one whose name does not tie it to a single channel, and only in the window the request names
(`7d_click`): the entries carry a number for every window asked for, and `value` is the number for "the
default window", which is not what the request pins. A day whose row carries no `actions` list at all gets no
purchase figure, not a zero: nothing says whether there were none or the account does not report them.

WHY A `next` LINK IS NEVER FOLLOWED. The paging page says a `next` link marks another page, and a Graph `next`
link is a full URL that carries the access token. Following whatever address the response names would send
that token wherever the response points. So `next` only says "there is another page"; the request for it is
built here, from the same base address, with the `after` cursor.

WHY THE RETRIES ARE BOUNDED. A call is tried `max_attempts` times with an exponential, jittered wait, a wait
the response asks for that is longer than `backoff_cap` is handed back as `RateLimited` for the runner to own
(Meta's header gives minutes), and the response headers that report how close the app and the ad account are
to their limits stop a further page before the limit is hit.

THE TOKEN. An already issued access token (from the environment by `from_env`, or a callable that returns the
current one), held in a `Secret`. The pages show it as the `access_token` request parameter, so that is where
it goes; no URL, request or response body is ever put in an error message.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

import httpx

from app.crypto import Secret

from .base import (
    AuthExpired,
    AuthRevoked,
    CanonicalPoint,
    Connector,
    FetchResult,
    RateLimited,
    UpstreamBroken,
)
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script
from .shopify import _ZERO_DECIMAL

BASE_URL = "https://graph.facebook.com"
#: Pinned in the URL. v26.0 is the version the Insights overview page's own example uses.
API_VERSION = "v26.0"
#: The one attribution window the report reads. Named in every request so a change of Meta's default cannot
#: change what a purchase means, and printed on the slide.
ATTRIBUTION_WINDOW = "7d_click"
#: The one purchase action type read. The others the page lists may describe the same purchase (UNVERIFIED), so they are not added.
PURCHASE_ACTION = "omni_purchase"
FIELDS = ("account_currency", "spend", "impressions", "clicks", "actions", "action_values")

#: (canonical key, kind) of the values read from a row. Money is `spend` and the purchase value.
SUPPORTED = frozenset({"ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value"})

DEFAULT_PAGE_SIZE = 500
#: Stop before a further page when any utilisation the headers report is this high (percent). Meta's page says to
#: back off "when you come close to hitting 100%" and names no number, so 90 is this connector's own choice.
UTILISATION_CEILING = 90

#: The largest value the snapshot table's bigint column holds. A response past it is broken, not a figure.
BIGINT_MAX = 9_223_372_036_854_775_807

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ACCOUNT_RE = re.compile(r"^(?:act_)?(\d{1,20})$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

#: Error codes by what they mean, from the error handling and rate limit pages (docs/META_SOURCES.md).
_EXPIRED_CODES = frozenset({190, 102})
_PERMISSION_CODES = frozenset({10, 3}) | frozenset(range(200, 300))
_THROTTLE_CODES = frozenset({4, 17, 32, 341, 613})
_TEMPORARY_CODES = frozenset({1, 2})


class _MetaError:
    """A Graph error object read into the few fields the connector acts on."""

    def __init__(self, code: int | None, subcode: int | None, message: str, status: int):
        self.code, self.subcode, self.message, self.http = code, subcode, message, status

    def text(self) -> str:
        head = "code %s" % self.code if self.code is not None else "HTTP %d" % self.http
        if self.subcode:
            head += " subcode %s" % self.subcode
        return ("%s: %s" % (head, self.message)).strip().rstrip(":")[:200]


def _read_error(body: Any, status: int) -> _MetaError | None:
    """The error object of a body, or None when the body carries none. Tolerates a list and a non-object error."""
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict) or "error" not in body:
        return None
    err = body["error"]
    if not isinstance(err, dict):
        return _MetaError(None, None, str(err)[:120], status)
    code, sub = err.get("code"), err.get("error_subcode")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    sub = sub if isinstance(sub, int) and not isinstance(sub, bool) else None
    return _MetaError(code, sub, str(err.get("message") or "")[:150], status)


def _minor_units(value: Any, currency: str, what: str) -> int:
    """An amount in major units, as the numeric string Meta sends, to an integer of minor units.

    A float is refused (it cannot be exact), a negative amount is refused (spend and purchase value cannot be
    negative), and an amount finer than the currency's minor unit is rounded half up, the way every other
    amount in this hub is.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UpstreamBroken("%s is %s, not a numeric string" % (what, type(value).__name__))
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not amount.is_finite() or amount < 0:
        raise UpstreamBroken("%s must be an amount of at least 0, got %s" % (what, value))
    exponent = 0 if currency in _ZERO_DECIMAL else 2
    if amount > BIGINT_MAX // (10 ** exponent):
        raise UpstreamBroken("%s is larger than any amount this hub stores" % what)
    return int((amount * (10 ** exponent)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _whole_number(value: Any, what: str) -> int:
    """A count arrives as a numeric string. It must be a whole number of at least 0."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UpstreamBroken("%s is %s, not a numeric string" % (what, type(value).__name__))
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise UpstreamBroken("%s must be a whole number of at least 0, got %s" % (what, value))
    if number > BIGINT_MAX:
        raise UpstreamBroken("%s is larger than any count this hub stores" % what)
    return int(number)


def _json_header(response: httpx.Response, name: str) -> Any:
    raw = response.headers.get(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1000 else None


def read_usage(response: httpx.Response) -> tuple[float | None, float | None]:
    """(highest utilisation percent, minutes until access returns) from the rate limit headers.

    Three headers carry it (docs/META_SOURCES.md): `x-fb-ads-insights-throttle` and `x-ad-account-usage`
    (`app_id_util_pct`, `acc_id_util_pct`), and `x-business-use-case-usage` (per business, a list of objects with
    `call_count`, `total_time`, `total_cputime` and `estimated_time_to_regain_access` in minutes). A header that
    is not JSON, or holds something else, is ignored: pacing is a courtesy and must not crash a sync.
    """
    levels: list[float] = []
    regain: float | None = None
    for name in ("x-fb-ads-insights-throttle", "x-ad-account-usage", "x-app-usage"):
        body = _json_header(response, name)
        if isinstance(body, dict):
            for key in ("app_id_util_pct", "acc_id_util_pct", "call_count", "total_time", "total_cputime"):
                pct = _percent(body.get(key))
                if pct is not None:
                    levels.append(pct)
    buc = _json_header(response, "x-business-use-case-usage")
    if isinstance(buc, dict):
        for entries in buc.values():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                for key in ("call_count", "total_time", "total_cputime"):
                    pct = _percent(entry.get(key))
                    if pct is not None:
                        levels.append(pct)
                minutes = entry.get("estimated_time_to_regain_access")
                if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
                    regain = max(regain or 0.0, float(minutes))
    return (max(levels) if levels else None), regain


class MetaConnector(Connector):
    platform = "meta"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        ad_account_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        utilisation_ceiling: float = UTILISATION_CEILING,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        match = _ACCOUNT_RE.match(str(ad_account_id).strip())
        if not match:
            # Before any network call: a wrong id would come back as a permissions error that reads like a bad token.
            raise ValueError("ad_account_id must be the numeric ad account id, with or without act_, got %r" % (ad_account_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.account_id = match.group(1)
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._ceiling = utilisation_ceiling
        self._sleep = sleep
        self._rng = rng
        #: The ad account's currency and time zone as `verify()` read them, for the label and the report's footnote.
        self.currency: str | None = None
        self.time_zone: str | None = None
        #: The highest utilisation percent the last response's headers reported.
        self.utilisation: float | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "MetaConnector":
        """Build from META_AD_ACCOUNT_ID and META_ACCESS_TOKEN (and META_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [name for name in ("META_AD_ACCOUNT_ID", "META_ACCESS_TOKEN") if not env.get(name)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        base = env.get("META_API_BASE_URL")
        if base:
            kwargs.setdefault("base_url", base)
        return cls(ad_account_id=env["META_AD_ACCOUNT_ID"], access_token=env["META_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "MetaConnector(account_id=%r, base_url=%r)" % (self.account_id, self._base)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    def _url(self, suffix: str) -> str:
        return "%s/%s/act_%s%s" % (self._base, API_VERSION, self.account_id, suffix)

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _delay(self, attempt: int, wait: float | None) -> float:
        if wait is not None:
            return wait
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)

    async def _get(self, suffix: str, params: Mapping[str, str]) -> tuple[dict, int, int]:
        """One GET with bounded retries. Returns (payload, http attempts, waits).

        An error is read from the body first, whatever the HTTP status, because the pages describe the error
        object and not the status codes, and Graph can answer 200 with an error object inside.
        """
        waits = 0
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            wait: float | None = None
            try:
                response = await self._client.get(self._url(suffix), params={**params, "access_token": self._bearer()})
            except httpx.HTTPError as exc:
                problem = "transport failure (%s)" % type(exc).__name__
            else:
                try:
                    body: Any = response.json()
                except ValueError:
                    body = None
                self.utilisation, regain = read_usage(response)
                err = _read_error(body, response.status_code)
                if err is None and response.status_code == 200:
                    if not isinstance(body, dict):
                        raise UpstreamBroken("Meta answered 200 with %s, not an object" % (
                            "a body that is not JSON" if body is None else type(body).__name__))
                    return body, attempt + 1, waits
                if err is None:
                    err = _MetaError(None, None, "", response.status_code)
                self._raise_if_final(err)
                wait = self._wait_from(response, regain)
                problem = "%s (%s)" % ("rate limit" if err.code in _THROTTLE_CODES or err.http == 429
                                       else "temporary error", err.text())
            if last:
                raise RateLimited(wait, "gave up after %d attempts: %s" % (self._max_attempts, problem))
            delay = self._delay(attempt, wait)
            if delay > self._backoff_cap:
                raise RateLimited(wait, "asked to wait %.0f seconds: %s" % (delay, problem))
            waits += 1
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _wait_from(response: httpx.Response, regain_minutes: float | None) -> float | None:
        """Seconds to wait: the business usage header's minutes, else a numeric Retry-After (UNVERIFIED), else none."""
        if regain_minutes:
            return regain_minutes * 60
        raw = response.headers.get("Retry-After")
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            return None
        return value if value is not None and value >= 0 else None

    def _raise_if_final(self, err: _MetaError) -> None:
        """Raise the typed error for a failure that retrying cannot fix. Returns when the failure may pass."""
        code = err.code
        if code in _EXPIRED_CODES or (code is None and err.http == 401):
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if code in _PERMISSION_CODES or (code is None and err.http == 403):
            raise AuthRevoked("no access to ad account %s (%s)" % (self.account_id, err.text()))
        if code in _THROTTLE_CODES or code in _TEMPORARY_CODES:
            return
        if code is None and (err.http == 429 or err.http >= 500):
            return
        raise UpstreamBroken("Meta refused the request (%s)" % err.text())

    # ------------------------------------------------------------------ parsing

    def _params(self, start: date, end: date, after: str | None) -> dict[str, str]:
        params = {
            "level": "account",
            "time_increment": "1",
            "time_range": json.dumps({"since": start.isoformat(), "until": (end - timedelta(days=1)).isoformat()},
                                     separators=(",", ":")),
            "fields": ",".join(FIELDS),
            "action_attribution_windows": json.dumps([ATTRIBUTION_WINDOW]),
            "limit": str(self._page_size),
        }
        if after:
            params["after"] = after
        return params

    def _day(self, row: Mapping[str, Any], start: date, end: date, seen: set[date]) -> date:
        first, last = row.get("date_start"), row.get("date_stop")
        for text in (first, last):
            if not isinstance(text, str) or not _DATE_RE.match(text):
                raise UpstreamBroken("a row's date is %r, not YYYY-MM-DD" % (text,))
        if first != last:
            raise UpstreamBroken("a row covers %s to %s, not one day" % (first, last))
        try:
            day = date.fromisoformat(first)
        except ValueError:
            raise UpstreamBroken("a row's date %s is not a calendar day" % first) from None
        if not start <= day < end:
            raise UpstreamBroken("a row for %s is outside the window %s to %s" % (day, start, end))
        if day in seen:
            # Two rows for one day would be written as one, silently keeping either.
            raise UpstreamBroken("two rows for %s" % day)
        return day

    @staticmethod
    def _purchase(entries: Any, what: str) -> str | None:
        """The text of the purchase total in the named window, None when `entries` is absent."""
        if entries is None:
            return None
        if not isinstance(entries, list):
            raise UpstreamBroken("%s is %s, not a list" % (what, type(entries).__name__))
        found = [e for e in entries if isinstance(e, dict) and e.get("action_type") == PURCHASE_ACTION]
        if not found:
            return "0"
        if len(found) > 1:
            raise UpstreamBroken("%s has two %s entries" % (what, PURCHASE_ACTION))
        if ATTRIBUTION_WINDOW not in found[0]:
            raise UpstreamBroken("%s has no %s value for %s" % (what, ATTRIBUTION_WINDOW, PURCHASE_ACTION))
        return found[0][ATTRIBUTION_WINDOW]

    def _points(self, payload: Mapping[str, Any], start: date, end: date, seen: set[date],
                currencies: set[str]) -> list[CanonicalPoint]:
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise UpstreamBroken("data is not a list")
        raw = {"source": "insights", "attribution_window": ATTRIBUTION_WINDOW}
        points: list[CanonicalPoint] = []
        for row in rows:
            if not isinstance(row, dict):
                raise UpstreamBroken("a row is %s, not an object" % type(row).__name__)
            day = self._day(row, start, end, seen)
            currency = row.get("account_currency")
            if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
                # Never a default: money without its real currency is the bug this connector exists to avoid.
                raise UpstreamBroken("a row for %s has no account_currency" % day)
            currencies.add(currency)
            if len(currencies) > 1:
                raise UpstreamBroken("rows name two currencies: %s" % ", ".join(sorted(currencies)))
            for key in ("spend", "impressions", "clicks"):
                if key not in row:
                    raise UpstreamBroken("a row for %s has no %s" % (day, key))
            seen.add(day)
            points += [
                CanonicalPoint("ad_spend", day, _minor_units(row["spend"], currency, "spend on %s" % day), currency, raw),
                CanonicalPoint("ad_impressions", day, _whole_number(row["impressions"], "impressions on %s" % day), None, raw),
                CanonicalPoint("ad_clicks", day, _whole_number(row["clicks"], "clicks on %s" % day), None, raw),
            ]
            count = self._purchase(row.get("actions"), "actions on %s" % day)
            if count is not None:
                points.append(CanonicalPoint(
                    "ad_purchases", day, _whole_number(count, "purchases on %s" % day), None, raw))
            value = self._purchase(row.get("action_values"), "action_values on %s" % day)
            if value is not None:
                points.append(CanonicalPoint(
                    "ad_purchase_value", day, _minor_units(value, currency, "purchase value on %s" % day), currency, raw))
        return points

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The ad account itself: its status, currency and time zone, the cheapest authenticated call."""
        payload, _, _ = await self._get("", {"fields": "account_status,currency,timezone_name"})
        status = payload.get("account_status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise UpstreamBroken("account_status is %r, not a number" % (status,))
        if status != 1:
            raise UpstreamBroken("ad account %s is not active (account_status %d)" % (self.account_id, status))
        currency = payload.get("currency")
        if isinstance(currency, str) and _CURRENCY_RE.match(currency):
            self.currency = currency
        if isinstance(payload.get("timezone_name"), str):
            self.time_zone = payload["timezone_name"]
        return "Meta ad account %s%s" % (self.account_id, " (%s)" % ", ".join(
            x for x in (self.currency, self.time_zone) if x) if self.currency or self.time_zone else "")

    async def fetch(
        self,
        *,
        window_start: date,
        window_end: date,
        cursor: str | None = None,
    ) -> AsyncIterator[FetchResult]:
        """One canonical row per day and metric for [window_start, window_end), page by page.

        The window is half open here and Meta's `until` is inclusive (UNVERIFIED on the pages read), so the end
        is moved back one day. The cursor is Meta's `after` cursor for the next page.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        after = cursor or None
        seen: set[date] = set()
        currencies: set[str] = set()
        cursors: set[str] = set()
        while True:
            if after and self.utilisation is not None and self.utilisation >= self._ceiling:
                raise RateLimited(None, "usage is at %.0f percent, stopping before the page after %s"
                                  % (self.utilisation, after))
            payload, calls, waits = await self._get("/insights", self._params(window_start, window_end, after))
            points = self._points(payload, window_start, window_end, seen, currencies)
            paging = payload.get("paging")
            has_next = isinstance(paging, dict) and bool(paging.get("next"))
            following: str | None = None
            if has_next:
                cursors_obj = paging.get("cursors")
                following = cursors_obj.get("after") if isinstance(cursors_obj, dict) else None
                if not isinstance(following, str) or not following:
                    raise UpstreamBroken("paging has a next page and no after cursor")
                if following == after or following in cursors:
                    # A page that points back at one already read would loop for ever, spending the rate limit.
                    raise UpstreamBroken("paging does not advance: the cursor %s came back" % following[:20])
                cursors.add(following)
            yield FetchResult(points=points, cursor=following, api_calls=calls, throttle_waits=waits)
            if not has_next:
                return
            after = following
