"""Deliver a monthly deck to a Google Drive folder through the Drive API v3 (standard library only).

Written from Google's Drive API documentation (developers.google.com/workspace/drive/api) and tested against a
stand-in HTTP server (tests/drive_stand_in.py), plus one real run against a personal Drive; see docs/drive-delivery.md.

What it does, in the order it does it:

* Checks the folder id names a folder, so a wrong id is one clear error and not an upload into nowhere.
* Looks for a file with the same name in that folder (`name = '...' and 'FOLDER' in parents and trashed = false`).
* Creates the file when there is none, replaces its content when there is one (PATCH, same file id), and sends nothing when
  the stored md5Checksum already equals the deck's. So a rerun for the same tenant and month never leaves a second copy.
* Uses a multipart upload up to 5 MB and a resumable one above that ("Use this upload type for large files (greater than
  5 MB)"), and resumes a resumable upload from the byte the server reports after an interruption.

Retries: Google says to retry 5xx errors and 403 `rateLimitExceeded` / `userRateLimitExceeded` with exponential backoff, and a
429 the same way. The whole find-then-write step is what is retried, so a create whose answer was lost is found by name on the next
look and replaced, not duplicated. A 401 refreshes the access token once.

The access token, refresh token and client secret never reach a log line, an exception message or a repr.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

log = logging.getLogger("app.report.drive")

DEFAULT_BASE_URL = "https://www.googleapis.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
#: The documentation's line between a multipart upload ("5 MB or less") and a resumable one.
MULTIPART_LIMIT = 5 * 1024 * 1024
FILE_FIELDS = "id,name,md5Checksum,size,parents,webViewLink,trashed,createdTime"
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0


class DriveError(Exception):
    """A Drive call failed. Built from known-safe parts, never from a raw urllib error or a token."""

    def __init__(self, message: str, status: int = 0, reason: str = "", retryable: bool = False, wait: float | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable
        self.wait = wait


class DriveConfigError(DriveError):
    """A required setting is missing. Names the variable, never a value."""


@dataclass(frozen=True)
class Delivered:
    file_id: str
    name: str
    action: str  # created, replaced or unchanged
    size: int
    md5: str
    folder_id: str
    duplicates: int = 0  # other files with the same name found in the folder and left alone
    web_link: str | None = None
    upload: str = ""  # multipart, resumable or "" when nothing was sent


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()  # the checksum Drive reports; not a security use


def deck_name(month: str, label: str | None = None) -> str:
    """The stable Drive file name for one deck: the same tenant and month always give the same name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return "monthly-report-%s%s.pptx" % (slug + "-" if slug else "", month)


def q_escape(text: str) -> str:
    """Escape a file name for a Drive `q` string: a backslash and an apostrophe each get a backslash before them."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------- credentials

class StaticToken:
    """A fixed access token, for a short job or a test. It cannot refresh."""

    def __init__(self, token: str):
        self._token = token

    def __call__(self) -> str:
        return self._token

    def refresh(self) -> str:
        raise DriveError("the access token was refused and a fixed token cannot be refreshed", 401, "authError")

    def __repr__(self) -> str:
        return "StaticToken(<set>)"


class OAuthTokenFile:
    """An authorized-user token file (token, refresh_token, client_id, client_secret, token_uri, expiry).

    Reads the file, keeps what it refreshes in memory and never writes the file back, so another tool that shares it is unaffected.
    """

    def __init__(self, path: str | os.PathLike, now: Callable[[], float] = time.time, timeout: float = 30.0):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise DriveConfigError("the Google token file could not be read (%s)" % type(exc).__name__) from None
        self._refresh_token = raw.get("refresh_token")
        self._client_id, self._client_secret = raw.get("client_id"), raw.get("client_secret")
        self._token_uri = raw.get("token_uri") or TOKEN_URI
        self._access = raw.get("token") or raw.get("access_token")
        self._expiry = self._parse_expiry(raw.get("expiry"))
        self._now = now
        self._timeout = timeout

    @staticmethod
    def _parse_expiry(value) -> float:
        """The file's expiry as epoch seconds; Google writes it as UTC with a trailing Z and no offset. Unreadable means expired."""
        if not isinstance(value, str):
            return 0.0
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()

    def __call__(self) -> str:
        if self._access and self._expiry - self._now() > 60:
            return self._access
        return self.refresh()

    def refresh(self) -> str:
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise DriveConfigError("the Google token file has no refresh token, client id and client secret to renew with")
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": self._refresh_token,
            "client_id": self._client_id, "client_secret": self._client_secret,
        }).encode()
        req = urllib.request.Request(self._token_uri, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise DriveError("Google refused to renew the access token (HTTP %d)" % exc.code, exc.code, "tokenRefused") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DriveError("the access token could not be renewed (%s)" % type(exc).__name__, 0, "network", retryable=True) from None
        self._access = data.get("access_token")
        if not self._access:
            raise DriveError("Google's token answer had no access token", 0, "tokenRefused")
        self._expiry = self._now() + float(data.get("expires_in", 3600))
        return self._access

    def __repr__(self) -> str:
        return "OAuthTokenFile(<set>)"


# ---------------------------------------------------------------- the client

class DriveClient:
    def __init__(
        self, token_source: Callable[[], str], base_url: str = DEFAULT_BASE_URL, sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4, timeout: float = 60.0, backoff_base: float = 1.0, backoff_cap: float = 16.0,
        max_resume: int = 3,
    ):
        self._token = token_source
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self.max_attempts = max_attempts
        self._timeout = timeout
        self._backoff_base, self._backoff_cap = backoff_base, backoff_cap
        self._max_resume = max_resume

    def __repr__(self) -> str:
        return "DriveClient(base_url=%r)" % self._base

    # -- plumbing -------------------------------------------------------------------------------------------------------

    @staticmethod
    def _reason(data) -> str:
        try:
            return str(data["error"]["errors"][0]["reason"])
        except (KeyError, IndexError, TypeError):
            try:
                return str(data["error"]["status"])
            except (KeyError, TypeError):
                return ""

    def _raise_for(self, status: int, headers: Mapping[str, str], raw: bytes, what: str):
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        reason = self._reason(data)
        wait = None
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                wait = max(0.0, min(MAX_RETRY_AFTER, float(retry_after)))
            except ValueError:
                wait = None
        retryable = status in RETRY_STATUSES or (status == 403 and reason in RATE_LIMIT_REASONS)
        raise DriveError("Drive %s failed with HTTP %d%s" % (what, status, " (%s)" % reason if reason else ""), status, reason, retryable, wait)

    def _send(self, method: str, url: str, body: bytes | None, headers: dict, what: str, ok=(200, 201)):
        """One request. Returns (status, headers, bytes). A 401 renews the access token once and repeats the request."""
        for renewed in (False, True):
            req = urllib.request.Request(url, data=body, method=method, headers={**headers, "Authorization": "Bearer " + self._token()})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                raw, hdrs = exc.read(), dict(exc.headers)
                if exc.code in ok:
                    return exc.code, hdrs, raw
                if exc.code == 401 and not renewed and hasattr(self._token, "refresh"):
                    self._token.refresh()
                    continue
                self._raise_for(exc.code, hdrs, raw, what)
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as exc:
                raise DriveError("Drive %s did not complete (%s)" % (what, type(getattr(exc, "reason", exc)).__name__), 0, "network", retryable=True) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def _json(self, method: str, path: str, query: dict | None = None, body: dict | None = None, what: str = "request") -> dict:
        url = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json; charset=UTF-8"} if data is not None else {}
        status, _, raw = self._send(method, url, data, headers, what)
        return json.loads(raw) if raw else {}

    # -- reads ----------------------------------------------------------------------------------------------------------

    def get(self, file_id: str) -> dict:
        return self._json("GET", "/drive/v3/files/" + urllib.parse.quote(file_id, safe=""), {"fields": FILE_FIELDS + ",mimeType", "supportsAllDrives": "true"}, what="get file")

    def find(self, name: str, folder_id: str) -> list[dict]:
        """Files with this exact name directly in the folder and not in the trash, oldest first."""
        q = "name = '%s' and '%s' in parents and trashed = false" % (q_escape(name), q_escape(folder_id))
        out: list[dict] = []
        token = None
        for _ in range(20):
            query = {"q": q, "fields": "nextPageToken,files(%s)" % FILE_FIELDS, "orderBy": "createdTime", "pageSize": "100",
                     "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                query["pageToken"] = token
            page = self._json("GET", "/drive/v3/files", query, what="file search")
            out.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    def download(self, file_id: str) -> bytes:
        url = "%s/drive/v3/files/%s?alt=media&supportsAllDrives=true" % (self._base, urllib.parse.quote(file_id, safe=""))
        return self._send("GET", url, None, {}, "download")[2]

    def delete(self, file_id: str) -> None:
        url = "%s/drive/v3/files/%s?supportsAllDrives=true" % (self._base, urllib.parse.quote(file_id, safe=""))
        self._send("DELETE", url, None, {}, "delete", ok=(200, 204))

    def check_folder(self, folder_id: str) -> dict:
        try:
            meta = self.get(folder_id)
        except DriveError as exc:
            if exc.status == 404:
                raise DriveError("the Drive folder %s was not found or this account cannot see it" % folder_id, 404, exc.reason) from None
            raise
        if meta.get("mimeType") != FOLDER_MIME:
            raise DriveError("%s is a file, not a folder" % folder_id, 400, "notAFolder")
        if meta.get("trashed"):
            raise DriveError("the Drive folder %s is in the trash" % folder_id, 400, "trashed")
        return meta

    # -- writes ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _multipart(metadata: dict, data: bytes, mime: str) -> tuple[bytes, str]:
        boundary = "deck" + secrets.token_hex(12)
        head = ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n--%s\r\nContent-Type: %s\r\n\r\n"
                % (boundary, json.dumps(metadata), boundary, mime)).encode()
        return head + data + ("\r\n--%s--" % boundary).encode(), "multipart/related; boundary=" + boundary

    def _upload(self, method: str, path: str, metadata: dict, data: bytes, mime: str, upload: str) -> tuple[dict, str]:
        kind = upload if upload in ("multipart", "resumable") else ("multipart" if len(data) <= MULTIPART_LIMIT else "resumable")
        query = urllib.parse.urlencode({"uploadType": kind, "fields": FILE_FIELDS, "supportsAllDrives": "true"})
        url = "%s/upload%s?%s" % (self._base, path, query)
        if kind == "multipart":
            body, ctype = self._multipart(metadata, data, mime)
            _, _, raw = self._send(method, url, body, {"Content-Type": ctype}, "multipart upload")
            return json.loads(raw), kind
        return self._resumable(method, url, metadata, data, mime), kind

    def _resumable(self, method: str, url: str, metadata: dict, data: bytes, mime: str) -> dict:
        total = len(data)
        init_headers = {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(total)}
        _, hdrs, _ = self._send(method, url, json.dumps(metadata).encode(), init_headers, "resumable upload start")
        session = next((v for k, v in hdrs.items() if k.lower() == "location"), None)
        if not session:
            raise DriveError("Drive's answer to the start of a resumable upload had no session location", 0, "noSession")
        offset = 0
        for attempt in range(self._max_resume + 1):
            try:
                chunk = data[offset:]
                headers = {"Content-Type": mime, "Content-Length": str(len(chunk))}
                if offset:
                    headers["Content-Range"] = "bytes %d-%d/%d" % (offset, total - 1, total)
                _, _, raw = self._send("PUT", session, chunk, headers, "resumable upload")
                return json.loads(raw)
            except DriveError as exc:
                if not exc.retryable or attempt == self._max_resume:
                    raise
                offset = self._received(session, total)  # "an empty PUT with Content-Range: bytes */total" asks where it stopped
        raise AssertionError("unreachable")  # pragma: no cover

    def _received(self, session: str, total: int) -> int:
        """How many bytes the server has, from the documented status query. 200 or 201 means it is complete: 404 starts over."""
        req = urllib.request.Request(session, data=b"", method="PUT", headers={
            "Content-Range": "bytes */%d" % total, "Content-Length": "0", "Authorization": "Bearer " + self._token()})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                raise DriveError("the resumable upload had already finished", 0, "alreadyComplete", retryable=True)
        except urllib.error.HTTPError as exc:
            if exc.code == 308:
                rng = exc.headers.get("Range")
                return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
            raise DriveError("the resumable session could not be resumed (HTTP %d)" % exc.code, exc.code, "sessionLost", retryable=True) from None
        except (urllib.error.URLError, OSError) as exc:
            raise DriveError("the resumable session could not be queried (%s)" % type(exc).__name__, 0, "network", retryable=True) from None

    def create(self, name: str, folder_id: str, data: bytes, mime: str = PPTX_MIME, upload: str = "auto") -> tuple[dict, str]:
        return self._upload("POST", "/drive/v3/files", {"name": name, "parents": [folder_id], "mimeType": mime}, data, mime, upload)

    def replace(self, file_id: str, data: bytes, mime: str = PPTX_MIME, upload: str = "auto") -> tuple[dict, str]:
        return self._upload("PATCH", "/drive/v3/files/" + urllib.parse.quote(file_id, safe=""), {}, data, mime, upload)

    # -- the delivery step ----------------------------------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt))

    def deliver(self, data: bytes, name: str, folder_id: str, mime: str = PPTX_MIME, upload: str = "auto") -> Delivered:
        """Put the deck in the folder under this name: create it, replace it, or leave it when it is already these bytes."""
        digest = md5_hex(data)
        for attempt in range(self.max_attempts):
            try:
                self.check_folder(folder_id)
                matches = self.find(name, folder_id)
                if matches:
                    keep, extra = matches[0], len(matches) - 1
                    if keep.get("md5Checksum") == digest:
                        log.info("deck %s is already in the folder with these bytes (id %s)", name, keep["id"])
                        return Delivered(keep["id"], name, "unchanged", len(data), digest, folder_id, extra, keep.get("webViewLink"))
                    meta, kind = self.replace(keep["id"], data, mime, upload)
                    action = "replaced"
                else:
                    meta, kind = self.create(name, folder_id, data, mime, upload)
                    action, extra = "created", 0
                log.info("deck %s %s in Drive (id %s, %d bytes, %s upload)", name, action, meta["id"], len(data), kind)
                return Delivered(meta["id"], name, action, len(data), meta.get("md5Checksum") or digest, folder_id, extra, meta.get("webViewLink"), kind)
            except DriveError as exc:
                if not exc.retryable or attempt == self.max_attempts - 1:
                    raise
                delay = exc.wait if exc.wait is not None else self._backoff(attempt)
                log.warning("Drive delivery of %s hit %s (HTTP %s), trying again in %.1f s", name, exc.reason or "an error", exc.status, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def from_env(env: Mapping[str, str] | None = None) -> tuple[DriveClient, str]:
    """A client and the folder id from the environment: GOOGLE_DRIVE_FOLDER_ID plus GOOGLE_DRIVE_TOKEN_FILE or GOOGLE_DRIVE_ACCESS_TOKEN."""
    env = os.environ if env is None else env
    folder = (env.get("GOOGLE_DRIVE_FOLDER_ID") or "").strip()
    if not folder:
        raise DriveConfigError("set GOOGLE_DRIVE_FOLDER_ID (or pass --drive-folder) to the Drive folder that should hold the decks")
    if env.get("GOOGLE_DRIVE_TOKEN_FILE"):
        source: Callable[[], str] = OAuthTokenFile(env["GOOGLE_DRIVE_TOKEN_FILE"])
    elif env.get("GOOGLE_DRIVE_ACCESS_TOKEN"):
        source = StaticToken(env["GOOGLE_DRIVE_ACCESS_TOKEN"])
    else:
        raise DriveConfigError("set GOOGLE_DRIVE_TOKEN_FILE (an authorized-user token file) or GOOGLE_DRIVE_ACCESS_TOKEN")
    return DriveClient(source, env.get("GOOGLE_API_BASE") or DEFAULT_BASE_URL), folder
