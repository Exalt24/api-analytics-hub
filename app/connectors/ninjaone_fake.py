"""A stand-in NinjaOne public API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/MSP_SOURCES.md, section NinjaOne, pages in docs/vendor-contract/ninjaone/):

  * `POST /ws/oauth/token` with `grant_type=client_credentials`, `client_id`, `client_secret` and `scope`, answering `access_token`, `expires_in`, `scope` and `token_type` ("Bearer"), as the client
    credentials flow page shows. A wrong client, a grant type that is not `client_credentials` and a scope the app was not given are refused with an OAuth `error` word.
  * `GET /v2/devices` with `pageSize` and `after` ("Last Node ID from previous page"): a bare array of devices in id order.
  * `GET /v2/alerts`: a bare array of the active alerts, each with its `device`; no paging, as the OpenAPI file gives none.
  * `GET /v2/organizations` with `pageSize`.
  * Every call but the token needs `Authorization: Bearer <a token it issued and has not revoked or expired>`; anything else is a 401.
  * 429 once `throttle_after` calls have been answered.

Its own choices, UNVERIFIED against NinjaOne and listed in docs/MSP_SOURCES.md: the status of a refused client (401), the status of a wrong grant or scope (400), the form encoding of the token
request, the page cap a test sets, whether a 429 carries `Retry-After`, the unit of every timestamp (seconds with a fraction, or milliseconds when `epoch_unit` says so), and the token's shape.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from .fake_http import Answer, FakeHTTP, Seen
from .harvest_fake import DecimalJSON


#: Not a credential: the stand-in's default, built from words so it is plainly a placeholder and not a secret shaped value.
PLACEHOLDER = " ".join(("stand", "in", "client", "phrase"))


class FakeNinjaOne(FakeHTTP):
    def __init__(self, *, client_id: str = "client-abc", client_secret: str = PLACEHOLDER, scopes: tuple[str, ...] = ("monitoring",), expires_in: int = 3600,
                 epoch_unit: str = "seconds", page_cap: int = 1000, throttle_after: int | None = None, retry_after: int | None = None):
        super().__init__()
        self.client_id, self.client_secret, self.scopes, self.expires_in = client_id, client_secret, scopes, expires_in
        self.epoch_unit, self.page_cap = epoch_unit, page_cap
        self.throttle_after, self.retry_after = throttle_after, retry_after
        self.devices: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.access_tokens: set[str] = set()
        self.token_requests: list[dict[str, str]] = []
        self.answered = 0
        self.fail_on: set[int] = set()
        #: Set to make `/v2/devices` ignore `after` and answer the first page again (a changed cursor that loops), or to answer ids that go backwards.
        self.ignore_after = False

    def stamp(self, moment: datetime | None) -> float | None:
        if moment is None:
            return None
        seconds = moment.timestamp()
        return seconds * 1000 if self.epoch_unit == "milliseconds" else seconds

    # ------------------------------------------------------------------ data

    def add_device(self, did: int, name: str | None = "SRV-01", *, offline: bool = False, node_class: str = "WINDOWS_SERVER", org: int | None = 1,
                   last_contact: datetime | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {"id": did, "uid": "00000000-0000-0000-0000-%012d" % did, "organizationId": org, "locationId": 1, "nodeClass": node_class, "approvalStatus": "APPROVED",
                               "offline": offline, "displayName": name, "systemName": (name or "x").lower(), "dnsName": "%s.local" % (name or "x").lower(), "created": self.stamp(last_contact),
                               "lastContact": self.stamp(last_contact), "tags": []}
        if name is None:
            del row["displayName"]
        self.devices.append(row)
        return row

    def add_alert(self, uid: str, device_id: int, created: datetime, *, source_type: str = "CONDITION_AGENT_DISK_FREE_SPACE", source_name: str | None = "Disk free space low",
                  subject: str | None = "Free space below 10 percent", severity: str | None = "MAJOR", device_name: str | None = "SRV-01", org: int | None = 1) -> dict[str, Any]:
        row: dict[str, Any] = {"uid": uid, "deviceId": device_id, "message": "msg", "createTime": self.stamp(created), "updateTime": self.stamp(created), "sourceType": source_type,
                               "sourceName": source_name, "subject": subject, "severity": severity, "priority": "NONE",
                               "device": {"id": device_id, "organizationId": org, "displayName": device_name, "offline": False}}
        self.alerts.append(row)
        return row

    def revoke_tokens(self) -> None:
        self.access_tokens.clear()

    # ------------------------------------------------------------------ routing

    def answer(self, seen: Seen) -> Answer:
        if seen.path == "/ws/oauth/token":
            return self._token(seen)
        if seen.method != "GET":
            return 405, {}, {"error": "method not allowed"}
        auth = seen.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.access_tokens:
            return 401, {}, {"error": "unauthorized"}
        self.answered += 1
        if self.answered in self.fail_on:
            return 503, {}, {"error": "unavailable"}
        if self.throttle_after is not None and self.answered > self.throttle_after:
            return 429, ({"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}), {"error": "slow down"}
        if seen.path == "/v2/devices":
            return self._devices(seen)
        if seen.path == "/v2/alerts":
            return 200, {}, json.dumps(self.alerts, cls=DecimalJSON).encode("utf-8")
        if seen.path == "/v2/organizations":
            try:
                size = int(seen.query.get("pageSize", "10"))
            except ValueError:
                return 400, {}, {"error": "bad pageSize"}
            return 200, {}, [{"id": 1, "name": "Acme", "description": "", "nodeApprovalMode": "AUTOMATIC"}][:max(size, 0)]
        return 404, {}, {"error": "not found"}

    def _token(self, seen: Seen) -> Answer:
        form = {k: v[0] for k, v in parse_qs(seen.body.decode("utf-8"), keep_blank_values=True).items()}
        self.token_requests.append(form)
        if form.get("grant_type") != "client_credentials":
            return 400, {}, {"error": "unsupported_grant_type"}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return 401, {}, {"error": "invalid_client"}
        asked = form.get("scope", " ".join(self.scopes)).split()
        if not set(asked) <= set(self.scopes):
            return 400, {}, {"error": "invalid_scope"}
        token = secrets.token_urlsafe(24)
        self.access_tokens.add(token)
        return 200, {}, {"access_token": token, "expires_in": self.expires_in, "scope": " ".join(asked), "token_type": "Bearer"}

    def _devices(self, seen: Seen) -> Answer:
        try:
            size = int(seen.query.get("pageSize", "100"))
        except ValueError:
            size = -1
        if size < 1:
            return 400, {}, {"error": "bad pageSize"}
        size = min(size, self.page_cap)
        rows = sorted(self.devices, key=lambda r: r["id"])
        after = 0 if self.ignore_after else int(seen.query.get("after", "0"))
        chunk = [r for r in rows if r["id"] > after][:size]
        return 200, {}, json.dumps(chunk, cls=DecimalJSON).encode("utf-8")
