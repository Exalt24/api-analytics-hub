"""NinjaOne (RMM) connector: devices and active alerts, as daily figures and as device and alert records for the MSP backlog analysis.

The sixteenth connector behind the contract in `base.py`, and the first for a remote monitoring and management (RMM) system. It signs in as an app (the OAuth 2.0 client credentials flow)
and writes four keys:

    nj_devices           devices NinjaOne lists, when the sync ran (a gauge, written on the window's last day)
    nj_devices_offline   the same, for devices whose `offline` is true (a gauge, written on the window's last day)
    nj_alerts_active     active alerts (triggered conditions), when the sync ran (a gauge, written on the window's last day)
    nj_alerts_created    active alerts whose `createTime` falls on that day (a count for each day of the window)

`devices()` and `alerts()` return the same data as `DeviceRecord` and `AlertRecord` (`app/msp/records.py`) for `app/msp/analysis.py`.

WRITTEN FROM NINJAONE'S PUBLIC API DOCUMENTATION, AND TESTED ONLY AGAINST A STAND-IN. The OpenAPI file the API documentation links (`NinjaRMM-API-v2.json`), the OAuth pages (the overview, the
client credentials flow, the client app setup) were read on 2026-11-29 and saved in docs/vendor-contract/ninjaone/. Nothing here ran against a live NinjaOne tenant: none is held. Every fact,
its page and what the pages leave unstated is in docs/MSP_SOURCES.md (section NinjaOne).

THE TOKEN. `POST {base}/ws/oauth/token` with `grant_type=client_credentials`, `client_id`, `client_secret` and `scope` ("A space-separated list of application scopes ... This defaults to all of the
scopes configured for the application"); this asks for `monitoring` only, which "Grants read-only access to monitoring data and organization structure". The answer carries `access_token`,
`expires_in` (3600 in the page's example), `scope` and `token_type`. The token is renewed a minute before it expires and on a 401, once, behind a lock, and the request repeated once. A refused
client (`invalid_client`, `unauthorized_client`, `invalid_scope`, `invalid_request`, `unsupported_grant_type`) means a person must fix the app registration: `AuthRevoked`.

WHAT IT CALLS (all GET, `Authorization: Bearer`). `/v2/devices?pageSize=..&after=..` ("after: Last Node ID from previous page"), `/v2/alerts` ("Returns list of active alerts/triggered conditions"; the
OpenAPI file gives it no paging parameter) and, for `verify()`, `/v2/organizations?pageSize=1`. The device list is read until a page is empty, because no page says what the largest `pageSize` is and a
short page would be taken for the last one if the vendor capped it; ids must rise from page to page, or the answer is refused as a changed cursor.

WHAT THE PAGES DO NOT SAY, and what the connector does about it. The unit of `createTime`, `lastContact` and the other timestamps is not stated (the OpenAPI file calls them "number/double", and
says "epoch seconds" for some other fields and "epoch milliseconds" for others), so the unit is a required option, `epoch_unit`, never a default. The API host for a tenant's region is not stated (`https://app.ninjarmm.com` is the
host in the token example); it is `base_url`. No rate limit and no 429 are stated: the connector paces nothing, and retries a 429 or a 5xx with a bound. The `severity` and `priority` of an alert have no
listed values on the alert schema (the activity schema lists NONE, MINOR, MODERATE, MAJOR, CRITICAL), so they are kept as the text NinjaOne sends and never ranked. ONLY ACTIVE ALERTS are listed: an alert
that was reset before the sync is not seen, so `nj_alerts_created` and the alert drivers describe what is still open, not every alert raised.

The client secret and the token never appear in an error message: only the HTTP status and the OAuth `error` word are quoted.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from app.crypto import Secret
from app.msp.records import AlertRecord, DeviceRecord, RecordError

from .base import AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import Retrier, Retry, retry_after, to_decimal
from .msp_common import checked_base, days_of, decimal_json, utc_day

__all__ = ["NinjaOneConnector", "METRICS", "SUPPORTED", "PAGE_SIZE"]

METRICS = ("nj_devices", "nj_devices_offline", "nj_alerts_active", "nj_alerts_created")
SUPPORTED = frozenset(METRICS)
GAUGES = frozenset({"nj_devices", "nj_devices_offline", "nj_alerts_active"})
API_BASE = "https://app.ninjarmm.com"
#: The connector's own page size: no saved page names a default or a maximum for /v2/devices.
PAGE_SIZE = 200
MAX_PAGES = 5000
#: The token is renewed this many seconds before `expires_in` runs out.
RENEW_MARGIN = 60.0
SCOPE = "monitoring"
_REFUSED = frozenset({"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request", "unsupported_grant_type"})
#: A timestamp outside these years is not a NinjaOne timestamp in the unit given: the connector's own sanity bound.
_MIN_YEAR, _MAX_YEAR = 2000, 2100


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _error_word(response: httpx.Response) -> str:
    try:
        body = decimal_json(response.text)
    except ValueError:
        return ""
    word = body.get("error") if isinstance(body, dict) else None
    return word if isinstance(word, str) and word in _REFUSED else ""


class NinjaOneConnector(Connector):
    platform = "ninjaone"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        epoch_unit: str,
        base_url: str = API_BASE,
        page_size: int = PAGE_SIZE,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        **retry_options: Any,
    ):
        for name, value in (("client_id", client_id), ("client_secret", client_secret)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s is required" % name)
        if epoch_unit not in ("seconds", "milliseconds"):
            raise ValueError("epoch_unit must be seconds or milliseconds: the NinjaOne pages do not say which createTime uses, so it is never defaulted")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be a whole number from 1 to 1000")
        self.client_id = client_id.strip()
        self._secret = Secret(client_secret)
        self._unit = epoch_unit
        self._base = checked_base(base_url, what="base_url", allow_path=False)
        self._page_size = page_size
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._clock = clock
        self._mono = monotonic
        self._token: Secret | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self._spent = [0, 0]
        self.degraded_metrics: set[str] = set()
        self.devices_read: list[DeviceRecord] = []
        self.alerts_read: list[AlertRecord] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "NinjaOneConnector":
        """Build from NINJAONE_CLIENT_ID, NINJAONE_CLIENT_SECRET and NINJAONE_EPOCH_UNIT (NINJAONE_BASE_URL for another region or a stand-in)."""
        import os

        env = os.environ if env is None else env
        names = ("NINJAONE_CLIENT_ID", "NINJAONE_CLIENT_SECRET", "NINJAONE_EPOCH_UNIT")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        if env.get("NINJAONE_BASE_URL"):
            kwargs.setdefault("base_url", env["NINJAONE_BASE_URL"])
        return cls(client_id=env["NINJAONE_CLIENT_ID"], client_secret=env["NINJAONE_CLIENT_SECRET"], epoch_unit=env["NINJAONE_EPOCH_UNIT"], **kwargs)

    def __repr__(self) -> str:
        return "NinjaOneConnector(client=%r)" % self.client_id

    @property
    def external_account(self) -> str:
        return self.client_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ the token

    @staticmethod
    def _classify_token(response: httpx.Response) -> Retry | None:
        http = response.status_code
        if http == 200:
            return None
        if _error_word(response):
            raise AuthRevoked("NinjaOne refused the client app (%s, HTTP %d): a person must fix the app registration" % (_error_word(response), http))
        if http == 429:
            return Retry(retry_after(response), "HTTP 429")
        if http in (500, 502, 503, 504):
            return Retry(retry_after(response), "HTTP %d" % http)
        raise UpstreamBroken("NinjaOne refused the token request (HTTP %d)" % http)

    async def _mint(self) -> str:
        async def send() -> httpx.Response:
            return await self._client.post(self._base + "/ws/oauth/token", data={
                "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret.reveal(), "scope": SCOPE},
                headers={"Accept": "application/json"})

        response, calls, waits = await self._retrier.run(send, self._classify_token)
        self._spent[0] += calls
        self._spent[1] += waits
        try:
            body = decimal_json(response.text)
        except ValueError:
            raise UpstreamBroken("NinjaOne answered the token request with a body that is not JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        expires = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or not isinstance(body.get("token_type"), str) or body["token_type"].lower() != "bearer":
            raise UpstreamBroken("the token answer has no bearer access_token")
        if isinstance(expires, bool) or not isinstance(expires, (int, Decimal)) or expires <= 0:
            raise UpstreamBroken("the token answer has no expires_in")
        self._token, self._expires = Secret(token), self._mono() + float(expires)
        return token

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token is None or self._mono() >= self._expires - RENEW_MARGIN:
                return await self._mint()
            return self._token.reveal()

    async def _refresh_after(self, rejected: str) -> str:
        async with self._lock:
            if self._token is not None and self._token.reveal() != rejected:
                return self._token.reveal()                          # another request already renewed it
            return await self._mint()

    # ------------------------------------------------------------------ transport

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        http = response.status_code
        if http == 200:
            return None
        if http == 401:
            return None                                              # handled by the caller: renew once
        if http == 429:
            return Retry(retry_after(response), "HTTP 429")
        if http in (500, 502, 503, 504):
            return Retry(retry_after(response), "HTTP %d" % http)
        raise UpstreamBroken("NinjaOne refused the request (HTTP %d)" % http)

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        token = await self._access_token()
        for attempt in range(2):
            async def send(bearer: str = token) -> httpx.Response:
                return await self._client.get(self._base + path, params=params, headers={"Authorization": "Bearer " + bearer, "Accept": "application/json"})

            response, calls, waits = await self._retrier.run(send, self._classify)
            self._spent[0] += calls
            self._spent[1] += waits
            if response.status_code != 401:
                break
            if attempt == 1:
                raise AuthRevoked("NinjaOne refused a token it had just issued (HTTP 401)")
            token = await self._refresh_after(token)
        try:
            return decimal_json(response.text)
        except ValueError:
            raise UpstreamBroken("NinjaOne answered 200 with a body that is not JSON") from None

    async def verify(self) -> str:
        body = await self._get("/v2/organizations", {"pageSize": "1"})
        if not isinstance(body, list):
            raise UpstreamBroken("the organizations answer is not a list")
        return "NinjaOne client app %s" % self.client_id

    # ------------------------------------------------------------------ records

    def _instant(self, value: Any, what: str) -> datetime:
        number = to_decimal(value, what)
        seconds = number / 1000 if self._unit == "milliseconds" else number
        try:
            moment = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise UpstreamBroken("%s is not a time in %s since the epoch" % (what, self._unit)) from None
        if not _MIN_YEAR <= moment.year <= _MAX_YEAR:
            raise UpstreamBroken("%s is not a time in %s since the epoch" % (what, self._unit))
        return moment

    @staticmethod
    def _text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _device(self, r: dict) -> DeviceRecord:
        did = r["id"]
        if not isinstance(r.get("offline"), bool):
            raise UpstreamBroken("offline of device %d is not true or false" % did)
        org = r.get("organizationId")
        if org is not None and not _is_id(org):
            raise UpstreamBroken("the organizationId of device %d is not a number" % did)
        name = self._text(r.get("displayName")) or self._text(r.get("systemName")) or self._text(r.get("dnsName")) or "device %d" % did
        contact = r.get("lastContact")
        try:
            return DeviceRecord(source="ninjaone", device_id=did, name=name, node_class=self._text(r.get("nodeClass")), organization_id=org, offline=r["offline"],
                                last_contact=None if contact is None else self._instant(contact, "the lastContact of device %d" % did))
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    def _alert(self, r: dict) -> AlertRecord:
        uid = r.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise UpstreamBroken("an alert has no uid")
        if not _is_id(r.get("deviceId")):
            raise UpstreamBroken("alert %s names no device" % uid)
        source_type = self._text(r.get("sourceType"))
        if source_type is None:
            raise UpstreamBroken("alert %s has no sourceType" % uid)
        device = r.get("device") if isinstance(r.get("device"), dict) else {}
        org = device.get("organizationId")
        name = self._text(device.get("displayName")) or self._text(device.get("systemName"))
        try:
            return AlertRecord(source="ninjaone", alert_id=uid.strip(), device_id=r["deviceId"], device_name=name, organization_id=org if _is_id(org) else None,
                               source_type=source_type, source_name=self._text(r.get("sourceName")), subject=self._text(r.get("subject")), severity=self._text(r.get("severity")),
                               created_at=self._instant(r.get("createTime"), "the createTime of alert %s" % uid))
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    async def devices(self) -> list[DeviceRecord]:
        """Every device NinjaOne lists, read page by page ("after" is the last id of the page before) until a page is empty. Ids must rise, which also refuses a device listed twice."""
        rows: list[dict] = []
        after: int | None = None
        for _ in range(MAX_PAGES):
            params = {"pageSize": str(self._page_size)}
            if after is not None:
                params["after"] = str(after)
            body = await self._get("/v2/devices", params)
            if not isinstance(body, list):
                raise UpstreamBroken("the devices answer is not a list")
            if not body:
                break
            for r in body:
                if not isinstance(r, dict) or not _is_id(r.get("id")):
                    raise UpstreamBroken("a device has no usable id")
                if after is not None and r["id"] <= after:
                    raise UpstreamBroken("device %d came after device %d: the pages do not rise, refusing a changed cursor" % (r["id"], after))
                after = r["id"]
                rows.append(r)
        else:
            raise UpstreamBroken("more than %d pages of devices" % MAX_PAGES)
        return [self._device(r) for r in rows]

    async def alerts(self) -> list[AlertRecord]:
        body = await self._get("/v2/alerts")
        if not isinstance(body, list):
            raise UpstreamBroken("the alerts answer is not a list")
        out = []
        for r in body:
            if not isinstance(r, dict):
                raise UpstreamBroken("an alert is %s, not an object" % type(r).__name__)
            out.append(self._alert(r))
        ids = [a.alert_id for a in out]
        if len(set(ids)) != len(ids):
            raise UpstreamBroken("an alert appears twice")
        return out

    # ------------------------------------------------------------------ the contract

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Four keys: the three gauges on the window's last day, and the alerts created for each day. There is no resume cursor, so one passed in is refused."""
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the NinjaOne connector issues no cursor, so none can be resumed")
        self._spent = [0, 0]
        devices = await self.devices()
        alerts = await self.alerts()
        self.devices_read, self.alerts_read = devices, alerts
        created: dict[date, int] = {}
        for a in alerts:
            created[utc_day(a.created_at)] = created.get(utc_day(a.created_at), 0) + 1
        last = window_end.fromordinal(window_end.toordinal() - 1)
        raw = {"source": "devices and alerts", "client": self.client_id}
        points = [CanonicalPoint("nj_devices", last, len(devices), None, raw), CanonicalPoint("nj_devices_offline", last, sum(1 for d in devices if d.offline), None, raw),
                  CanonicalPoint("nj_alerts_active", last, len(alerts), None, raw)]
        points += [CanonicalPoint("nj_alerts_created", day, created.get(day, 0), None, raw) for day in days_of(window_start, window_end)]
        yield FetchResult(points=points, cursor=None, api_calls=self._spent[0], throttle_waits=self._spent[1])
