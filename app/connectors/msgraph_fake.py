"""A stand-in Microsoft Graph server and sign-in endpoint on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/HARVEST_GRAPH_SOURCES.md, section Microsoft Graph):

  * `POST /{tenant}/oauth2/v2.0/token` with `client_id`, `client_secret`, `scope=https://graph.microsoft.com/.default` and `grant_type=client_credentials` in the BODY,
    answering `token_type`, `expires_in`, `ext_expires_in` and `access_token`; `{"error": "invalid_client", "error_codes": [7000215]}` for a secret it does not know.
  * `GET /v1.0/sites/{site}/lists/{list}/items`, with `expand=fields` adding each row's `fields`, answering `value` and `@odata.nextLink` until the list is read.
  * `GET /v1.0/teams/{team}/channels/{channel}/messages` with `$top` (a value over 50 is a 400), sorted by the last change of the reply chain, newest first, with
    `@odata.nextLink` until the channel is read.
  * `GET /v1.0/drives/{drive}/items/{item}/content` answering 302 with a `Location` that is a pre-authenticated address on this server; the address answers the file and
    refuses a request that carries an Authorization header, which a real pre-authenticated address would not need (the stand-in records it as `leaked_token`).
  * Application permissions as roles on the token: `Sites.Read.All` for the list, `ChannelMessage.Read.All` for the channel, `Files.Read.All` for the content; a token without
    the role is a 403 `accessDenied`. Every `/workbook/` path answers 403 to an app token, since the pages list Application as "Not supported" for them.
  * 401 `InvalidAuthenticationToken` for a token it did not issue or one that has expired (`expire_access_tokens()`), and 429 with `Retry-After` through scripted faults.
  * The error body is `{"error": {"code": ..., "message": ...}}`.

It is a stand-in written from documentation, not a recording of Microsoft Graph. Its own choices, marked UNVERIFIED in the sources page: the skip token's form, the page size of
the list (`page_size`), the access token's shape, the HTTP status of a bad token request, and the order of the fields.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode

from .fake_http import Answer, FakeHTTP, Seen
from .harvest_fake import DecimalJSON


def graph_error(code: str, message: str = "stand-in") -> dict:
    return {"error": {"code": code, "message": message, "innerError": {"request-id": "00000000-0000-0000-0000-000000000000"}}}


def gdt(moment: datetime, *, fraction: bool = True) -> str:
    """The JSON form Graph writes a datetime in: `2021-03-28T21:11:12.395Z`."""
    text = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return text + (".%03dZ" % (moment.microsecond // 1000) if fraction else "Z")


class FakeGraph(FakeHTTP):
    def __init__(self, *, tenant: str = "contoso.onmicrosoft.com", client_id: str = "app-client-id", client_secret: str = "app-client-secret",
                 roles: tuple[str, ...] = ("Sites.Read.All", "ChannelMessage.Read.All", "Files.Read.All"), expires_in: int = 3599, page_size: int = 200,
                 site: str = "contoso.sharepoint.com,site-1,web-1", list_id: str = "list-1", team: str = "team-1", channel: str = "19:chan@thread.tacv2"):
        super().__init__()
        self.tenant, self.client_id, self.client_secret = tenant, client_id, client_secret
        self.roles = set(roles)
        self.expires_in = expires_in
        self.page_size = page_size
        self.site, self.list_id, self.team, self.channel = site, list_id, team, channel
        self.access_tokens: dict[str, set[str]] = {}
        self.items: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.downloads: dict[str, bytes] = {}
        self.leaked_token = False
        self.token_requests = 0
        self.token_faults: list = []

    # ------------------------------------------------------------------ data

    def add_item(self, ident: int, created: datetime, *, modified: datetime | None = None, fields: dict[str, Any] | None = None) -> None:
        self.items.append({"id": str(ident), "createdDateTime": gdt(created), "lastModifiedDateTime": gdt(modified or created),
                           "webUrl": "https://contoso.sharepoint.com/Lists/Register/%d_.000" % ident, "fields": fields if fields is not None else {"Title": "Job %d" % ident}})

    def add_message(self, ident: int, created: datetime, *, text: str = "Hello", author: str | None = "Robin Kline", modified: datetime | None = None,
                    kind: str = "message", deleted: datetime | None = None) -> None:
        self.messages.append({"id": str(ident), "replyToId": None, "messageType": kind, "createdDateTime": gdt(created), "lastModifiedDateTime": gdt(modified or created),
                              "deletedDateTime": gdt(deleted) if deleted else None, "importance": "normal",
                              "webUrl": "https://teams.microsoft.com/l/message/%d" % ident,
                              "from": {"application": None, "device": None, "user": {"id": "u-1", "displayName": author, "userIdentityType": "aadUser"}} if author else None,
                              "body": {"contentType": "html", "content": text}})

    def add_file(self, drive: str, item: str, content: bytes) -> None:
        self.files[(drive, item)] = content

    def expire_access_tokens(self) -> None:
        with self._lock:
            self.access_tokens.clear()

    # ------------------------------------------------------------------ routing

    def _handle(self, seen: Seen):
        """Scripted faults (`faults`) hit Graph requests only; the sign-in endpoint has its own list (`token_faults`), so a test that queues a Graph fault is not
        spent on the token request that always comes first."""
        if seen.path == "/%s/oauth2/v2.0/token" % self.tenant:
            with self._lock:
                self.requests.append(seen)
                if self.token_faults:
                    fault = self.token_faults.pop(0)
                    return fault.status, dict(fault.headers), fault.body if fault.body is not None else {}, False
                status, headers, payload = self.answer(seen)
                return status, headers, payload, False
        return super()._handle(seen)

    def answer(self, seen: Seen) -> Answer:
        if seen.path == "/%s/oauth2/v2.0/token" % self.tenant:
            return self._token(seen)
        if seen.path.startswith("/download/"):
            if seen.headers.get("authorization"):
                self.leaked_token = True
                return 400, {}, graph_error("InvalidRequest", "a pre-authenticated address takes no Authorization header")
            data = self.downloads.get(seen.path[len("/download/"):])
            return (200, {}, data) if data is not None else (404, {}, graph_error("itemNotFound"))
        if not seen.path.startswith("/v1.0/") or seen.method != "GET":
            return 404, {}, graph_error("BadRequest", "Resource not found for the segment")
        auth = seen.headers.get("authorization", "")
        roles = self.access_tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if roles is None:
            return 401, {}, graph_error("InvalidAuthenticationToken", "Access token has expired or is not yet valid.")
        if "/workbook/" in seen.path:
            return 403, {}, graph_error("accessDenied", "Access is denied: the workbook APIs do not support application permissions")
        base = "/v1.0/sites/%s/lists/%s/items" % (self.site, self.list_id)
        if seen.path == base:
            return self._items(seen, roles)
        m = re.match(r"^/v1\.0/teams/([^/]+)/channels/([^/]+)/messages$", seen.path)
        if m and m.group(1) == self.team and m.group(2) == self.channel:
            return self._messages(seen, roles)
        m = re.match(r"^/v1\.0/drives/([^/]+)/items/([^/]+)/content$", seen.path)
        if m:
            return self._content(m.group(1), m.group(2), roles)
        return 404, {}, graph_error("itemNotFound", "The specified resource does not exist")

    def _token(self, seen: Seen) -> Answer:
        self.token_requests += 1
        form = {k: v[0] for k, v in parse_qs(seen.body.decode("utf-8"), keep_blank_values=True).items()}
        if seen.method != "POST" or seen.query or form.get("grant_type") != "client_credentials":
            return 400, {}, {"error": "unsupported_grant_type", "error_description": "grant type not supported"}
        if form.get("scope") != "https://graph.microsoft.com/.default":
            return 400, {}, {"error": "invalid_scope", "error_description": "the scope must be https://graph.microsoft.com/.default"}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return 401, {}, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided.", "error_codes": [7000215]}
        token = "eyJ0eXAi.%s" % secrets.token_urlsafe(32)
        self.access_tokens[token] = set(self.roles)
        return 200, {}, {"token_type": "Bearer", "expires_in": self.expires_in, "ext_expires_in": self.expires_in, "access_token": token}

    def _page(self, rows: list[dict], seen: Seen, size: int) -> tuple[list[dict], str | None]:
        offset = 0
        if "$skiptoken" in seen.query:
            offset = int(base64.urlsafe_b64decode(seen.query["$skiptoken"] + "==").decode("ascii"))
        chunk = rows[offset:offset + size]
        nxt = None
        if offset + size < len(rows):
            token = base64.urlsafe_b64encode(str(offset + size).encode("ascii")).decode("ascii").rstrip("=")
            query = {k: v for k, v in seen.query.items() if k != "$skiptoken"}
            query["$skiptoken"] = token
            nxt = "%s%s?%s" % (self.url, seen.path, urlencode(query))
        return chunk, nxt

    def _items(self, seen: Seen, roles: set[str]) -> Answer:
        if "Sites.Read.All" not in roles:
            return 403, {}, graph_error("accessDenied", "Access denied")
        chunk, nxt = self._page(self.items, seen, self.page_size if "$top" not in seen.query else int(seen.query["$top"]))
        expand = seen.query.get("expand") == "fields" or seen.query.get("$expand") == "fields"
        value = [dict(i) if expand else {k: v for k, v in i.items() if k != "fields"} for i in chunk]
        body: dict[str, Any] = {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#items", "value": value}
        if nxt:
            body["@odata.nextLink"] = nxt
        return 200, {}, json.dumps(body, cls=DecimalJSON).encode("utf-8")

    def _messages(self, seen: Seen, roles: set[str]) -> Answer:
        if "ChannelMessage.Read.All" not in roles:
            return 403, {}, graph_error("accessDenied", "Access denied")
        top = int(seen.query.get("$top", "20"))
        if not 1 <= top <= 50:
            return 400, {}, graph_error("BadRequest", "Invalid $top")
        ordered = sorted(self.messages, key=lambda m: (m["lastModifiedDateTime"], m["id"]), reverse=True)
        chunk, nxt = self._page(ordered, seen, top)
        body: dict[str, Any] = {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#messages", "@odata.count": len(chunk), "value": chunk}
        if nxt:
            body["@odata.nextLink"] = nxt
        return 200, {}, body

    def _content(self, drive: str, item: str, roles: set[str]) -> Answer:
        if "Files.Read.All" not in roles:
            return 403, {}, graph_error("accessDenied", "Access denied")
        data = self.files.get((drive, item))
        if data is None:
            return 404, {}, graph_error("itemNotFound", "The specified resource does not exist")
        key = secrets.token_urlsafe(16)
        self.downloads[key] = data
        return 302, {"Location": "%s/download/%s" % (self.url, key)}, b""
