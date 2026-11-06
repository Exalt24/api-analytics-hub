"""TikTok Business API (Marketing API v1.3) connector: daily advertiser figures from a synchronous basic report.

The sixth connector behind the contract in `base.py`, and the third paid media one. It writes the same
`ad_spend`, `ad_impressions` and `ad_clicks` keys Meta and Google Ads do, plus `ad_conversions`, so the combined ad
spend view needs no TikTok special case. It has been run only against the stand-in server in `tiktok_fake.py`, never
against a live advertiser account: no account is held. Every fact about the calls, with the page it came from and what is
not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md.

WHAT IT CALLS. `GET {base}/open_api/v1.3/report/integrated/get/` with `report_type=BASIC`, `service_type=AUCTION`,
`data_level=AUCTION_ADVERTISER`, the dimensions `advertiser_id` and `stat_time_day` and the metrics `spend`, `impressions`,
`clicks`, `conversion` and `currency`, over `start_date` and `end_date`, paged with `page` and `page_size`. The `Access-Token`
header carries the token.

THIRTY DAYS AT A TIME. The synchronous report page says a daily breakdown ("stat_time_day" among the dimensions) can
retrieve "a maximum of 30 days". A longer window is cut into runs of at most 30 days, each paged to its end, and the cursor
names the run and the page so a failure resumes where it stopped.

MONEY AND CURRENCY. `spend` is a string in the advertiser's currency. The `currency` metric needs `advertiser_id` among the
dimensions, which it is. It is stored as integer minor units with the currency beside it, the currency comes from the row,
never from a default, and a window whose rows name two is refused.

A DAY WITH NO DATA. The reporting FAQ says that with `stat_time_day` among the dimensions a day with no data is probably left
out of the list rather than sent as zeros. So a missing day is a quiet day, and the report tells it from a day nobody looked at
by the sync runs, as for Meta.

WHY THE RETRIES ARE BOUNDED. TikTok answers a rate limit with a return code (40016, 40100, 40133) and its `code` is read before
the HTTP status. A call is tried `max_attempts` times with an exponential, jittered wait, and a wait longer than `backoff_cap`
is handed back as `RateLimited` for the runner to own.

THE TOKEN. An already issued access token, held in a `Secret`, sent only in the `Access-Token` header. No URL, query or
response body is put in an error message.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import Retrier, Retry, milli_units, minor_units, retry_after, whole_number
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://business-api.tiktok.com"
API_PATH = "/open_api/v1.3"
SUPPORTED = frozenset({"ad_spend", "ad_impressions", "ad_clicks", "ad_conversions"})
METRICS = ("spend", "impressions", "clicks", "conversion", "currency")
DIMENSIONS = ("advertiser_id", "stat_time_day")
#: "If you include stat_time_day in the value of the dimensions field to query daily breakdown data, you can retrieve data for a maximum of 30 days."
MAX_DAYS_PER_REPORT = 30
DEFAULT_PAGE_SIZE = 200
#: A guard against a response whose page_info never ends.
MAX_PAGES = 500

_ADVERTISER_RE = re.compile(r"^\d{1,25}$")
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2}:\d{2})?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CURSOR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d+)$")

#: Return codes by what they mean, from the Return codes appendix (docs/AD_AND_BUSINESS_SOURCES.md).
_EXPIRED_CODES = frozenset({40105})
_PERMISSION_CODES = frozenset({40001, 40106})
_RETRY_CODES = frozenset({40016, 40100, 40133, 50000, 50002, 51305, 60001})


class _TikTokError:
    def __init__(self, code: int | None, message: str, http: int):
        self.code, self.message, self.http = code, message, http

    def text(self) -> str:
        head = "code %s" % self.code if self.code is not None else "HTTP %d" % self.http
        return ("%s: %s" % (head, self.message)).strip().rstrip(":")[:200]


def _read_error(body: Any, http: int) -> _TikTokError | None:
    """The `code` and `message` of a body whose code is not 0, or None for a success."""
    if not isinstance(body, dict) or "code" not in body:
        return None
    code = body["code"]
    if isinstance(code, bool) or not isinstance(code, int):
        return _TikTokError(None, "code is %s, not a number" % type(code).__name__, http)
    if code == 0:
        return None
    return _TikTokError(code, str(body.get("message") or "")[:150], http)


class TikTokConnector(Connector):
    platform = "tiktok"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        advertiser_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not _ADVERTISER_RE.match(str(advertiser_id).strip()):
            raise ValueError("advertiser_id must be the numeric advertiser id, got %r" % (advertiser_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self.advertiser_id = str(advertiser_id).strip()
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._retrier = retrier or Retrier(**retry_options)
        self.currency: str | None = None
        self.time_zone: str | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "TikTokConnector":
        """Build from TIKTOK_ADVERTISER_ID and TIKTOK_ACCESS_TOKEN (and TIKTOK_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [n for n in ("TIKTOK_ADVERTISER_ID", "TIKTOK_ACCESS_TOKEN") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        if env.get("TIKTOK_API_BASE_URL"):
            kwargs.setdefault("base_url", env["TIKTOK_API_BASE_URL"])
        return cls(advertiser_id=env["TIKTOK_ADVERTISER_ID"], access_token=env["TIKTOK_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "TikTokConnector(advertiser_id=%r, base_url=%r)" % (self.advertiser_id, self._base)

    @property
    def external_account(self) -> str:
        return self.advertiser_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _classify(self, response: httpx.Response) -> Retry | None:
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        err = _read_error(body, response.status_code)
        if err is None and response.status_code == 200:
            return None
        if err is None:
            err = _TikTokError(None, "", response.status_code)
        code, http = err.code, err.http
        if code in _EXPIRED_CODES or (code is None and http == 401):
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if code in _PERMISSION_CODES or (code is None and http == 403):
            raise AuthRevoked("no access to advertiser %s (%s)" % (self.advertiser_id, err.text()))
        if code in _RETRY_CODES or (code is None and (http == 429 or http >= 500)):
            return Retry(retry_after(response), err.text())
        raise UpstreamBroken("TikTok refused the request (%s)" % err.text())

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict, httpx.Response, int, int]:
        url = self._base + API_PATH + path

        async def send() -> httpx.Response:
            return await self._client.get(url, params=params, headers={"Access-Token": self._bearer()})

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("TikTok answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("TikTok answered 200 with %s, not an object" % type(body).__name__)
        return body, response, calls, waits

    # ------------------------------------------------------------------ parsing

    def _params(self, start: date, last: date, page: int) -> dict[str, str]:
        return {
            "advertiser_id": self.advertiser_id,
            "service_type": "AUCTION",
            "report_type": "BASIC",
            "data_level": "AUCTION_ADVERTISER",
            "dimensions": json.dumps(list(DIMENSIONS), separators=(",", ":")),
            "metrics": json.dumps(list(METRICS), separators=(",", ":")),
            "start_date": start.isoformat(),
            "end_date": last.isoformat(),
            "page": str(page),
            "page_size": str(self._page_size),
        }

    def _points(self, rows: Any, start: date, end: date, seen: set[date], currencies: set[str]) -> list[CanonicalPoint]:
        if not isinstance(rows, list):
            raise UpstreamBroken("data.list is not a list")
        raw = {"source": "report/integrated/get", "report_type": "BASIC", "data_level": "AUCTION_ADVERTISER"}
        points: list[CanonicalPoint] = []
        for row in rows:
            dims = row.get("dimensions") if isinstance(row, dict) else None
            metrics = row.get("metrics") if isinstance(row, dict) else None
            if not isinstance(dims, dict) or not isinstance(metrics, dict):
                raise UpstreamBroken("a row has no dimensions or metrics object")
            if str(dims.get("advertiser_id")) != self.advertiser_id:
                # Rows of another advertiser would be stored under this connection's history.
                raise UpstreamBroken("a row names advertiser %s, not the connection's" % (dims.get("advertiser_id"),))
            m = _DAY_RE.match(dims["stat_time_day"]) if isinstance(dims.get("stat_time_day"), str) else None
            if not m:
                raise UpstreamBroken("a row's stat_time_day is %r, not a day" % (dims.get("stat_time_day"),))
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                raise UpstreamBroken("a row's day %s is not a calendar day" % m.group(1)) from None
            if not start <= day < end:
                raise UpstreamBroken("a row for %s is outside the window %s to %s" % (day, start, end))
            if day in seen:
                raise UpstreamBroken("two rows for %s" % day)
            currency = metrics.get("currency")
            if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
                raise UpstreamBroken("a row for %s has no currency" % day)
            currencies.add(currency)
            if len(currencies) > 1:
                raise UpstreamBroken("rows name two currencies: %s" % ", ".join(sorted(currencies)))
            for key in ("spend", "impressions", "clicks", "conversion"):
                if key not in metrics:
                    raise UpstreamBroken("a row for %s has no %s" % (day, key))
            seen.add(day)
            points += [
                CanonicalPoint("ad_spend", day, minor_units(metrics["spend"], currency, "spend on %s" % day), currency, raw),
                CanonicalPoint("ad_impressions", day, whole_number(metrics["impressions"], "impressions on %s" % day), None, raw),
                CanonicalPoint("ad_clicks", day, whole_number(metrics["clicks"], "clicks on %s" % day), None, raw),
                CanonicalPoint("ad_conversions", day, milli_units(metrics["conversion"], "conversion on %s" % day), None, raw),
            ]
        return points

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The advertiser account itself: its status, currency and time zone, the cheapest authenticated call."""
        body, _, _, _ = await self._get("/advertiser/info/", {
            "advertiser_ids": json.dumps([self.advertiser_id], separators=(",", ":")),
            "fields": json.dumps(["advertiser_id", "name", "currency", "timezone", "status"], separators=(",", ":"))})
        data = body.get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        info = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if info is None:
            raise UpstreamBroken("the advertiser query returned no advertiser")
        if str(info.get("advertiser_id")) != self.advertiser_id:
            raise UpstreamBroken("the advertiser query answered for another advertiser")
        if info.get("status") != "STATUS_ENABLE":
            raise UpstreamBroken("advertiser %s is not enabled (status %s)" % (self.advertiser_id, info.get("status")))
        currency = info.get("currency")
        if isinstance(currency, str) and _CURRENCY_RE.match(currency):
            self.currency = currency
        if isinstance(info.get("timezone"), str):
            self.time_zone = info["timezone"]
        return "TikTok advertiser %s%s" % (self.advertiser_id, " (%s)" % ", ".join(
            x for x in (self.currency, self.time_zone) if x) if self.currency or self.time_zone else "")

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """One canonical row per day and metric for [window_start, window_end), page by page, 30 days at a time.

        The window is half open here and TikTok's `end_date` is inclusive (UNVERIFIED on the pages read), so each run's
        end is moved back one day. The cursor is `<first day of the run>:<next page>`.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        run_start, page = window_start, 1
        if cursor:
            m = _CURSOR_RE.match(cursor)
            if not m:
                raise ValueError("this cursor was not issued by the TikTok connector")
            run_start, page = date.fromisoformat(m.group(1)), int(m.group(2))
            if not window_start <= run_start < window_end or page < 1:
                raise ValueError("the cursor is outside the window")
        currencies: set[str] = set()
        while run_start < window_end:
            run_end = min(run_start + timedelta(days=MAX_DAYS_PER_REPORT), window_end)
            seen: set[date] = set()
            pages = 0
            while True:
                pages += 1
                if pages > MAX_PAGES:
                    raise UpstreamBroken("more than %d pages in one run" % MAX_PAGES)
                body, response, calls, waits = await self._get(
                    "/report/integrated/get/", self._params(run_start, run_end - timedelta(days=1), page))
                if response.headers.get("x-tt-ads-throttle"):
                    # The FAQ says this header marks a report cut to its newest 20,000 ids: the figures are incomplete.
                    raise UpstreamBroken("TikTok truncated the report (x-tt-ads-throttle)")
                data = body.get("data")
                if not isinstance(data, dict):
                    raise UpstreamBroken("data is not an object")
                points = self._points(data.get("list", []), run_start, run_end, seen, currencies)
                info = data.get("page_info")
                total = info.get("total_page") if isinstance(info, dict) else None
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise UpstreamBroken("page_info.total_page is %r, not a number" % (total,))
                more = page < total
                if more:
                    following = "%s:%d" % (run_start.isoformat(), page + 1)
                elif run_end < window_end:
                    following = "%s:1" % run_end.isoformat()
                else:
                    following = None
                yield FetchResult(points=points, cursor=following, api_calls=calls, throttle_waits=waits)
                if more:
                    page += 1
                    continue
                break
            run_start, page = run_end, 1
