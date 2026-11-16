"""Microsoft Graph connector: a SharePoint list register, Teams channel messages and an Excel register workbook.

The thirteenth connector behind the contract in `base.py`, and the first for Microsoft 365. It signs in as an app (the OAuth 2.0 client credentials flow, application
permissions) and writes three daily keys:

    sp_register_items_created    rows of the SharePoint list created that day (the UTC day of `createdDateTime`)
    sp_register_items_modified   rows of the list last modified that day (`lastModifiedDateTime`)
    teams_channel_messages       top level messages posted in one Teams channel that day (`createdDateTime`), written only when a team and channel are given

and three typed reads for the workflows that sit on top of it:

    register_items()    the list rows themselves, each with its `fields` (a project register kept as a SharePoint list)
    channel_messages()  the channel's messages (author, text, link) inside a window
    workbook_rows()     an Excel register: the .xlsx file is downloaded and read with openpyxl, one dict per row, keyed by the header row

It has been run only against the stand-in server in `msgraph_fake.py`, never against a live tenant: none is held. Every fact about the calls, with the page it came from and
what the pages do not say, is in docs/HARVEST_GRAPH_SOURCES.md (section Microsoft Graph).

THE TOKEN. `POST {login}/{tenant}/oauth2/v2.0/token` with `client_id`, `client_secret`, `scope=https://graph.microsoft.com/.default` and `grant_type=client_credentials`, the
secrets in the BODY. The answer carries `token_type`, `expires_in` and `access_token`; the token is renewed a minute before it expires and on a 401, once, behind a lock, and
the request repeated once. A refused client (`invalid_client`, `unauthorized_client`, `invalid_scope`) means a person must fix the app registration: `AuthRevoked`.

WHAT IT CALLS (all GET, `Authorization: Bearer`). `/v1.0/sites/{site}/lists/{list}/items?expand=fields`, `/v1.0/teams/{team}/channels/{channel}/messages?$top=50` and
`/v1.0/drives/{drive}/items/{item}/content`. Paging follows `@odata.nextLink` exactly as given (the paging page says never to take the skip token out of it) and never follows
one that is not on the Graph host under `/v1.0/`, so the token cannot be sent elsewhere.

WHY THE EXCEL REGISTER IS DOWNLOADED. The workbook table and range endpoints (`.../workbook/tables/{t}/rows`, `.../usedRange`) list Application permissions as "Not supported"
on their pages, so an app signed in as itself cannot use them. `GET .../content` lists the application permission `Files.Read.All`, answers 302 to a pre-authenticated URL, and
that URL is fetched WITHOUT the token. The file is then read here.

LIMITS. A 429 carries `Retry-After`; a value longer than the retrier's cap is handed to the runner as `RateLimited`, never slept. 500, 502, 503 and 504 are retried with a bound.
The list is read whole (the pages promise `$filter` only works best on indexed columns, and give no rule for combining two filters) and counted here. The channel's messages come
sorted by the last change of the whole reply chain, not by creation, so every page is read and the days are counted here.

THE TOKEN AND THE TENANT'S DATA never appear in an error message: only the HTTP status and Graph's `error.code` (a word) are quoted.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import Retrier, Retry, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

#: The public surface: the connector and its three record types, for the workflows and the dashboard routes that call the typed reads.
__all__ = ["MicrosoftGraphConnector", "RegisterItem", "ChannelMessage", "WorkbookRow", "METRICS", "SUPPORTED", "parse_graph_datetime", "settled_window_end"]

GRAPH_BASE = "https://graph.microsoft.com"
LOGIN_BASE = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
METRICS = ("sp_register_items_created", "sp_register_items_modified", "teams_channel_messages")
SUPPORTED = frozenset(METRICS)
#: "You can extend up to 50 channel messages per page."
TEAMS_PAGE = 50
MAX_PAGES = 500
#: The largest workbook that is read. The pages give no limit; this is the connector's own bound, so one huge file cannot fill the worker's memory.
MAX_WORKBOOK_BYTES = 25 * 1024 * 1024
#: Renew the token this long before it expires.
TOKEN_SKEW_SECONDS = 60

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,254}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._,:@!{}%=-]{1,300}$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.]{1,80}$")
_OAUTH_CODE_RE = re.compile(r"^[a-z_]{1,40}$")
_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})$")


def parse_graph_datetime(text: Any, what: str) -> datetime:
    """Graph's JSON datetime (`2021-03-28T21:11:12.395Z`, up to seven fraction digits) as an aware UTC datetime."""
    m = _DATETIME_RE.match(text) if isinstance(text, str) else None
    if not m:
        raise UpstreamBroken("%s is not an ISO 8601 datetime" % what)
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    fraction = int((m.group(7) or "0").ljust(6, "0")[:6])
    zone = m.group(8)
    offset = timedelta(0) if zone == "Z" else (-1 if zone[0] == "-" else 1) * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    try:
        local = datetime(year, month, day, hour, minute, second, fraction, tzinfo=timezone(offset))
    except ValueError:
        raise UpstreamBroken("%s is not a calendar datetime" % what) from None
    return local.astimezone(timezone.utc)


def _decimal_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _checked_origin(value: str, what: str) -> str:
    parsed = urlparse(value.strip())
    loopback = parsed.hostname in ("127.0.0.1", "localhost")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("%s must be a bare https address such as https://graph.microsoft.com, got %r" % (what, value))
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
        raise ValueError("%s must use https" % what)
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def _error_code(body: Any) -> str:
    """Graph's error body is `{"error": {"code": ..., "message": ...}}`. Only the code word is kept."""
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE_RE.match(code) else ""


@dataclass(frozen=True)
class RegisterItem:
    id: str
    created: datetime
    modified: datetime
    fields: Mapping[str, Any]
    web_url: str | None


@dataclass(frozen=True)
class ChannelMessage:
    id: str
    created: datetime
    author: str | None
    body_type: str
    text: str
    web_url: str | None


@dataclass(frozen=True)
class WorkbookRow:
    """One row of a workbook sheet or table. `row` is the 1 based row number in the sheet, so a person can find it; `values` is keyed by the header row."""

    row: int
    values: Mapping[str, Any]


class GraphTokens:
    """The client credentials flow, behind one lock. See the module note."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, login_base: str, client: httpx.AsyncClient, retrier: Retrier,
                 clock: Callable[[], float] = time.monotonic):
        self._tenant, self._id = tenant_id, client_id
        self._secret = Secret(client_secret)
        self._login = login_base
        self._client, self._retrier, self._clock = client, retrier, clock
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.mint_calls = 0

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        if response.status_code == 200:
            return None
        try:
            body = response.json()
        except ValueError:
            body = None
        code = body.get("error") if isinstance(body, dict) else None
        code = code if isinstance(code, str) and _OAUTH_CODE_RE.match(code) else ""
        http = response.status_code
        if code in ("invalid_client", "unauthorized_client", "invalid_scope", "invalid_request", "unsupported_grant_type"):
            raise AuthRevoked("the app registration was refused (%s), a person must fix it" % code)
        if http == 429 or http >= 500:
            return Retry(retry_after(response), "HTTP %d %s" % (http, code))
        raise UpstreamBroken("the token endpoint refused the request (HTTP %d %s)" % (http, code))

    async def _mint(self) -> str:
        async def send() -> httpx.Response:
            return await self._client.post(
                "%s/%s/oauth2/v2.0/token" % (self._login, self._tenant),
                data={"client_id": self._id, "client_secret": self._secret.reveal(), "scope": SCOPE, "grant_type": "client_credentials"},
                headers={"Accept": "application/json"})

        response, _, _ = await self._retrier.run(send, self._classify)
        self.mint_calls += 1
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("the token endpoint answered 200 with a body that is not JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        expires = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise UpstreamBroken("the token endpoint answered 200 with no access_token")
        if body.get("token_type") != "Bearer":
            raise UpstreamBroken("the token endpoint answered a token_type that is not Bearer")
        if isinstance(expires, bool) or not isinstance(expires, int) or expires <= 0:
            raise UpstreamBroken("the token endpoint answered an expires_in that is not a positive whole number")
        self._access = token
        self._expires_at = self._clock() + expires
        return token

    async def access_token(self) -> str:
        async with self._lock:
            if self._access is not None and self._clock() < self._expires_at - TOKEN_SKEW_SECONDS:
                return self._access
            return await self._mint()

    async def refresh_after(self, rejected: str) -> str:
        """A new token after `rejected` was refused. If another request already replaced it, that one is used and no second token is requested."""
        async with self._lock:
            if self._access is not None and self._access != rejected:
                return self._access
            return await self._mint()


class MicrosoftGraphConnector(Connector):
    platform = "msgraph"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        site_id: str,
        list_id: str,
        team_id: str | None = None,
        channel_id: str | None = None,
        graph_base: str = GRAPH_BASE,
        login_base: str = LOGIN_BASE,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        clock: Callable[[], float] = time.monotonic,
        **retry_options: Any,
    ):
        if not _TENANT_RE.match(str(tenant_id)):
            raise ValueError("tenant_id must be the directory (tenant) id or its domain name")
        for name, value in (("client_id", client_id), ("client_secret", client_secret)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s is required" % name)
        for name, value in (("site_id", site_id), ("list_id", list_id)):
            if not isinstance(value, str) or not _ID_RE.match(value):
                raise ValueError("%s is not a usable Graph id" % name)
        if (team_id is None) != (channel_id is None):
            raise ValueError("give team_id and channel_id together, or neither")
        for name, value in (("team_id", team_id), ("channel_id", channel_id)):
            if value is not None and (not isinstance(value, str) or not _ID_RE.match(value)):
                raise ValueError("%s is not a usable Graph id" % name)
        self._site, self._list, self._team, self._channel = site_id, list_id, team_id, channel_id
        self._base = _checked_origin(graph_base, "graph_base")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._tokens = GraphTokens(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
                                   login_base=_checked_origin(login_base, "login_base"), client=self._client, retrier=self._retrier, clock=clock)
        self._tenant = tenant_id
        self._spent = [0, 0]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "MicrosoftGraphConnector":
        """Build from MSGRAPH_TENANT_ID, MSGRAPH_CLIENT_ID, MSGRAPH_CLIENT_SECRET, MSGRAPH_SITE_ID, MSGRAPH_LIST_ID and optionally MSGRAPH_TEAM_ID and MSGRAPH_CHANNEL_ID."""
        env = os.environ if env is None else env
        names = ("MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET", "MSGRAPH_SITE_ID", "MSGRAPH_LIST_ID")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        for var, kw in (("MSGRAPH_GRAPH_BASE", "graph_base"), ("MSGRAPH_LOGIN_BASE", "login_base"), ("MSGRAPH_TEAM_ID", "team_id"), ("MSGRAPH_CHANNEL_ID", "channel_id")):
            if env.get(var):
                kwargs.setdefault(kw, env[var])
        return cls(tenant_id=env["MSGRAPH_TENANT_ID"], client_id=env["MSGRAPH_CLIENT_ID"], client_secret=env["MSGRAPH_CLIENT_SECRET"],
                   site_id=env["MSGRAPH_SITE_ID"], list_id=env["MSGRAPH_LIST_ID"], **kwargs)

    def __repr__(self) -> str:
        return "MicrosoftGraphConnector(tenant=%r)" % self._tenant

    @property
    def external_account(self) -> str:
        return self._tenant

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        if response.status_code == 200:
            return None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        http = response.status_code
        text = ("HTTP %d %s" % (http, _error_code(body))).strip()
        if http == 401:
            raise AuthExpired("the access token was refused (%s)" % text)
        if http == 403:
            raise AuthRevoked("the app is not allowed to read that, an administrator must grant the permission (%s)" % text)
        if http == 429 or http in (500, 502, 503, 504):
            return Retry(retry_after(response), text)
        raise UpstreamBroken("Microsoft Graph refused the request (%s)" % text)

    def _checked_next(self, url: Any) -> str:
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or "%s://%s" % (parsed.scheme, parsed.netloc) != self._base or not parsed.path.startswith("/v1.0/") or ".." in parsed.path:
            raise UpstreamBroken("the answer names a next page outside Microsoft Graph, refusing to send the token there")
        return url

    async def _authorized(self, send_with: Callable[[str], Any]) -> tuple[httpx.Response, int, int]:
        """Send with the current token; on a 401 renew it once and send again."""
        calls = waits = 0
        for attempt in (0, 1):
            token = await self._tokens.access_token()

            async def send() -> httpx.Response:
                return await send_with(token)

            try:
                response, c, w = await self._retrier.run(send, self._classify)
            except AuthExpired:
                if attempt == 0:
                    await self._tokens.refresh_after(token)
                    continue
                raise
            return response, calls + c, waits + w
        raise AssertionError("unreachable")  # pragma: no cover

    async def _get(self, path_or_url: str, params: Mapping[str, str] | None = None) -> tuple[Any, int, int]:
        url = path_or_url if path_or_url.startswith("http") else self._base + path_or_url
        if not url.startswith(self._base + "/v1.0/"):
            raise UpstreamBroken("refusing to call a path outside /v1.0/")

        async def send_with(token: str) -> httpx.Response:
            return await self._client.get(url, params=params, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})

        response, calls, waits = await self._authorized(send_with)
        try:
            return _decimal_json(response.text), calls, waits
        except ValueError:
            raise UpstreamBroken("Microsoft Graph answered 200 with a body that is not JSON") from None

    async def _values(self, path: str, params: Mapping[str, str]) -> tuple[list[dict], int, int]:
        """Every element of a paged `value` list, following `@odata.nextLink` as given."""
        out: list[dict] = []
        body, calls, waits = await self._get(path, params)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            if not isinstance(body, dict) or not isinstance(body.get("value"), list):
                raise UpstreamBroken("the answer has no value list")
            for element in body["value"]:
                if not isinstance(element, dict):
                    raise UpstreamBroken("an element is %s, not an object" % type(element).__name__)
            out += body["value"]
            nxt = body.get("@odata.nextLink")
            if nxt is None:
                return out, calls, waits
            nxt = self._checked_next(nxt)
            if nxt in seen:
                raise UpstreamBroken("a next page names itself")
            seen.add(nxt)
            body, c, w = await self._get(nxt)
            calls, waits = calls + c, waits + w
        raise UpstreamBroken("more than %d pages in one list" % MAX_PAGES)

    def _items_path(self) -> str:
        return "/v1.0/sites/%s/lists/%s/items" % (self._site, self._list)

    async def verify(self) -> str:
        """The cheapest documented call that proves the app can read the list: one item."""
        body, _, _ = await self._get(self._items_path(), {"$top": "1"})            # one page only: a next link is not followed to prove a login
        if not isinstance(body, dict) or not isinstance(body.get("value"), list):
            raise UpstreamBroken("the answer has no value list")
        return "Microsoft 365 tenant %s, list %s" % (self._tenant, self._list)

    # ------------------------------------------------------------------ SharePoint list

    async def register_items(self) -> list[RegisterItem]:
        """Every row of the list with its fields. A row with no id, no dates or a duplicate id is a broken answer."""
        rows, c, w = await self._values(self._items_path(), {"expand": "fields"})
        self._spent[0] += c
        self._spent[1] += w
        items: list[RegisterItem] = []
        ids: set[str] = set()
        for r in rows:
            ident = r.get("id")
            if not isinstance(ident, str) or not ident:
                raise UpstreamBroken("a list item has no id")
            if ident in ids:
                raise UpstreamBroken("list item %s appears twice" % ident)
            ids.add(ident)
            fields = r.get("fields")
            if not isinstance(fields, dict):
                raise UpstreamBroken("list item %s has no fields (the request asks for them with expand=fields)" % ident)
            created = parse_graph_datetime(r.get("createdDateTime"), "the createdDateTime of list item %s" % ident)
            modified = parse_graph_datetime(r.get("lastModifiedDateTime"), "the lastModifiedDateTime of list item %s" % ident)
            if modified < created:
                raise UpstreamBroken("list item %s was modified before it was created" % ident)
            url = r.get("webUrl")
            items.append(RegisterItem(ident, created, modified, fields, url if isinstance(url, str) else None))
        return items

    # ------------------------------------------------------------------ Teams

    async def channel_messages(self, *, window_start: date, window_end: date) -> list[ChannelMessage]:
        """The channel's own messages created in [window_start, window_end). System messages and deleted ones are left out; replies are not in this list."""
        if self._team is None or self._channel is None:
            raise ValueError("this connection has no team and channel")
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        rows, c, w = await self._values("/v1.0/teams/%s/channels/%s/messages" % (self._team, self._channel), {"$top": str(TEAMS_PAGE)})
        self._spent[0] += c
        self._spent[1] += w
        out: list[ChannelMessage] = []
        ids: set[str] = set()
        for r in rows:
            ident = r.get("id")
            if not isinstance(ident, str) or not ident:
                raise UpstreamBroken("a message has no id")
            if ident in ids:
                raise UpstreamBroken("message %s appears twice" % ident)
            ids.add(ident)
            created = parse_graph_datetime(r.get("createdDateTime"), "the createdDateTime of message %s" % ident)
            if r.get("messageType") != "message" or r.get("deletedDateTime") is not None:
                continue
            if not window_start <= created.date() < window_end:
                continue
            body = r.get("body")
            if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not isinstance(body.get("contentType"), str):
                raise UpstreamBroken("message %s has no body" % ident)
            sender = r.get("from")
            user = sender.get("user") if isinstance(sender, dict) else None
            name = user.get("displayName") if isinstance(user, dict) else None
            url = r.get("webUrl")
            out.append(ChannelMessage(ident, created, name if isinstance(name, str) else None, body["contentType"], body["content"], url if isinstance(url, str) else None))
        return out

    # ------------------------------------------------------------------ Excel register

    async def workbook_rows(self, *, drive_id: str, item_id: str, sheet: str | None = None, table: str | None = None) -> list[WorkbookRow]:
        """Download an .xlsx file and read one sheet (the first by default) or one named table as rows keyed by the header row.

        Blank header cells and repeated headers are refused (a dict would silently drop a column). A row with every cell empty is skipped.
        """
        import openpyxl

        for name, value in (("drive_id", drive_id), ("item_id", item_id)):
            if not isinstance(value, str) or not _ID_RE.match(value):
                raise ValueError("%s is not a usable Graph id" % name)
        if sheet is not None and table is not None:
            raise ValueError("give a sheet or a table, not both")
        path = "/v1.0/drives/%s/items/%s/content" % (drive_id, item_id)

        async def send_with(token: str) -> httpx.Response:
            return await self._client.get(self._base + path, headers={"Authorization": "Bearer " + token}, follow_redirects=False)

        def classify(response: httpx.Response) -> Retry | None:
            return None if response.status_code == 302 else self._classify(response)

        calls = waits = 0
        for attempt in (0, 1):
            token = await self._tokens.access_token()

            async def send() -> httpx.Response:
                return await send_with(token)

            try:
                redirect, c, w = await self._retrier.run(send, classify)
            except AuthExpired:
                if attempt == 0:
                    await self._tokens.refresh_after(token)
                    continue
                raise
            calls, waits = calls + c, waits + w
            break
        self._spent[0] += calls
        self._spent[1] += waits
        location = redirect.headers.get("Location")
        target = urlparse(location) if location else None
        loopback = target is not None and target.hostname in ("127.0.0.1", "localhost")
        if target is None or not target.hostname or target.username or target.password or (target.scheme != "https" and not (loopback and target.scheme == "http")):
            raise UpstreamBroken("the content call answered 302 with no usable https Location")

        async def download() -> httpx.Response:
            # The pre-authenticated address carries its own permission: the token is never sent to it.
            return await self._client.get(location, headers={"Accept": "*/*"}, follow_redirects=False)

        def classify_download(response: httpx.Response) -> Retry | None:
            if response.status_code == 200:
                return None
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                return Retry(retry_after(response), "HTTP %d" % response.status_code)
            raise UpstreamBroken("the download address refused the request (HTTP %d)" % response.status_code)

        file, c, w = await self._retrier.run(download, classify_download)
        self._spent[0] += c
        self._spent[1] += w
        data = file.content
        if len(data) > MAX_WORKBOOK_BYTES:
            raise UpstreamBroken("the workbook is larger than %d bytes, which is the most this connector reads" % MAX_WORKBOOK_BYTES)
        if not data.startswith(b"PK"):
            raise UpstreamBroken("the file is not an .xlsx workbook")
        try:
            book = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        except Exception:
            raise UpstreamBroken("the file could not be read as an .xlsx workbook") from None
        try:
            return _rows_of(book, sheet, table)
        finally:
            book.close()

    # ------------------------------------------------------------------ the daily keys

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Two or three canonical rows per day for [window_start, window_end), written once every page is read.

        There is no resume cursor, so a cursor passed in is refused rather than ignored. A day with nothing is written as zeros because the whole list was read.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Microsoft Graph connector issues no cursor, so none can be resumed")
        self._spent = [0, 0]
        items = await self.register_items()
        created: dict[date, int] = {}
        modified: dict[date, int] = {}
        for item in items:
            day = item.created.date()
            if window_start <= day < window_end:
                created[day] = created.get(day, 0) + 1
            day = item.modified.date()
            if window_start <= day < window_end:
                modified[day] = modified.get(day, 0) + 1
        messages: dict[date, int] | None = None
        if self._team is not None:
            messages = {}
            for m in await self.channel_messages(window_start=window_start, window_end=window_end):
                messages[m.created.date()] = messages.get(m.created.date(), 0) + 1
        raw = {"source": "graph", "tenant": self._tenant}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            points.append(CanonicalPoint("sp_register_items_created", day, created.get(day, 0), None, raw))
            points.append(CanonicalPoint("sp_register_items_modified", day, modified.get(day, 0), None, raw))
            if messages is not None:
                points.append(CanonicalPoint("teams_channel_messages", day, messages.get(day, 0), None, raw))
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=self._spent[0] + self._tokens.mint_calls, throttle_waits=self._spent[1])


def _rows_of(book: Any, sheet: str | None, table: str | None) -> list[WorkbookRow]:
    """The rows of one sheet or table. The header row is the first row of the sheet, or of the table's own range."""
    if table is not None:
        found = None
        for ws in book.worksheets:
            if table in ws.tables:
                found = (ws, ws.tables[table].ref)
                break
        if found is None:
            raise UpstreamBroken("the workbook has no table named %r" % table)
        ws, ref = found
        cells = ws[ref]
        first_row = cells[0][0].row
        grid = [[c.value for c in row] for row in cells]
    else:
        if sheet is None:
            ws = book.worksheets[0] if book.worksheets else None
        else:
            ws = book[sheet] if sheet in book.sheetnames else None
        if ws is None:
            raise UpstreamBroken("the workbook has no sheet named %r" % sheet)
        first_row = 1
        grid = [list(row) for row in ws.iter_rows(values_only=True)]
    if not grid:
        return []
    headers = grid[0]
    names: list[str] = []
    for h in headers:
        if h is None or (isinstance(h, str) and not h.strip()):
            raise UpstreamBroken("the header row has an empty cell")
        names.append(str(h).strip())
    if len(set(names)) != len(names):
        raise UpstreamBroken("the header row repeats a name")
    out: list[WorkbookRow] = []
    for offset, row in enumerate(grid[1:], start=1):
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
            continue
        values = {name: (row[i] if i < len(row) else None) for i, name in enumerate(names)}
        out.append(WorkbookRow(first_row + offset, values))
    return out
