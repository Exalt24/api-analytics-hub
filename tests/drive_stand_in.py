# LIFESPAN: permanent (stand-in for the Google Drive API v3, used by tests/test_drive.py and tests/mutation_check_drive.py)
"""A stand-in for the Google Drive API v3 file endpoints, on localhost, random port, no network and no Google account.

Written from Google's documentation (developers.google.com/workspace/drive/api), not from observing the real service. It serves:

    GET    /drive/v3/files                    files.list with the documented `q` terms: name = '...', 'ID' in parents, trashed = false
                                              (a backslash and an apostrophe are escaped with a backslash), orderBy, pageSize, pageToken
    GET    /drive/v3/files/{id}               files.get (metadata), or the bytes with alt=media
    DELETE /drive/v3/files/{id}               files.delete, 204
    POST   /upload/drive/v3/files             files.create: uploadType=multipart (multipart/related, metadata part first, media second)
                                              or uploadType=resumable (200 with a Location session URI and an empty body, then PUT)
    PATCH  /upload/drive/v3/files/{id}        files.update with new content, same two upload types
    PUT    <session URI>                      the content of a resumable upload; an empty PUT with `Content-Range: bytes */TOTAL` asks where
                                              the upload stopped (308 with a Range header, or 200/201 when it is complete, 404 when it expired)
    POST   /token                             the OAuth refresh grant (grant_type=refresh_token) of an authorized-user token file

Errors are Google's JSON shape: {"error": {"code": 403, "message": ..., "errors": [{"reason": "rateLimitExceeded"}]}}.

Faults, one consumed per request that matches, set as `stand_in.faults = ["PUT:interrupt", "503", ...]`:
    429 / 503 / 500 / 403rate      the documented retryable answers (a 429 carries Retry-After when `retry_after` is set)
    403perm                        a 403 that is not a rate limit (insufficientFilePermissions): never retried
    drop_before                    the connection is closed before the request is read: nothing happened
    drop_after                     the request is processed and the connection is closed with no answer: the answer was lost
    interrupt                      (resumable PUT only) the first half of the bytes is kept, then the connection is closed
    401                            the token is refused once
A fault may be prefixed with a method ("POST:503") to apply only to that method.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

FOLDER_MIME = "application/vnd.google-apps.folder"


def unescape_q(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


class StandIn:
    def __init__(self, access_token: str = "ya29.standin-access", refresh_token: str = "1//standin-refresh",
                 client_id: str = "standin-client", client_secret: str = "standin-secret"):
        self._first_access = access_token
        self.access_tokens = {access_token}
        self.valid_refresh = (refresh_token, client_id, client_secret)
        self.faults: list[str] = []
        self.retry_after: str | None = None
        self.page_size_cap = 100
        self.forget_sessions = False  # True: a resumable status query answers 404, as an expired session does
        self.files: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.refreshes = 0
        self._sessions: dict[str, dict] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.refreshed_token = "ya29.standin-renewed"

    # -- lifecycle ------------------------------------------------------------------------------------------------------

    def start(self) -> StandIn:
        handler = type("Handler", (_Handler,), {"stand_in": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_port

    def reset(self) -> None:
        with self._lock:
            self.files.clear()
            self.requests.clear()
            self.faults.clear()
            self._sessions.clear()
            self.access_tokens = {self._first_access}
            self.refreshes = 0
            self.retry_after = None
            self.forget_sessions = False
            self.page_size_cap = 100

    # -- test helpers ---------------------------------------------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return "%s%04d" % (prefix, self._seq)

    def add_folder(self, name: str = "api-analytics-hub test") -> str:
        fid = self._new_id("folder")
        self.files[fid] = {"id": fid, "name": name, "mimeType": FOLDER_MIME, "parents": [], "content": b"", "trashed": False, "order": self._seq}
        return fid

    def add_file(self, name: str, folder_id: str, content: bytes, trashed: bool = False) -> str:
        fid = self._new_id("file")
        self.files[fid] = {"id": fid, "name": name, "mimeType": "application/octet-stream", "parents": [folder_id], "content": content,
                           "trashed": trashed, "order": self._seq}
        return fid

    def in_folder(self, folder_id: str, name: str | None = None, trashed: bool = False) -> list[dict]:
        return [f for f in self.files.values() if folder_id in f["parents"] and f["trashed"] == trashed and (name is None or f["name"] == name)]

    def seen(self, method: str, prefix: str = "") -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)]

    # -- the resource view ----------------------------------------------------------------------------------------------

    @staticmethod
    def resource(f: dict) -> dict:
        out = {"kind": "drive#file", "id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "parents": f["parents"], "trashed": f["trashed"],
               "createdTime": "2026-11-10T00:%02d:00.000Z" % (f["order"] % 60)}
        if f["mimeType"] != FOLDER_MIME:
            out["md5Checksum"] = hashlib.md5(f["content"], usedforsecurity=False).hexdigest()
            out["size"] = str(len(f["content"]))
            out["webViewLink"] = "https://drive.google.com/file/d/%s/view" % f["id"]
        return out


class _Handler(BaseHTTPRequestHandler):
    stand_in: StandIn
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- plumbing -------------------------------------------------------------------------------------------------------

    _lose_answer = False  # set for a drop_after request: the work is done, the answer is never written

    def _send(self, status: int, payload=None, headers: dict | None = None, raw: bytes | None = None) -> None:
        if self._lose_answer:
            return
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, reason: str, message: str, headers: dict | None = None) -> None:
        self._send(status, {"error": {"code": status, "message": message, "errors": [{"domain": "global", "reason": reason, "message": message}]}}, headers)

    def _drop(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _take_fault(self, kinds: tuple[str, ...]) -> str | None:
        with self.stand_in._lock:
            for i, spec in enumerate(self.stand_in.faults):
                method, _, kind = spec.rpartition(":")
                if (not method or method == self.command) and kind in kinds:
                    return self.stand_in.faults.pop(i).rpartition(":")[2]
        return None

    def _route(self) -> None:
        si = self.stand_in
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        record = {"method": self.command, "path": url.path, "query": q, "auth": self.headers.get("Authorization", ""),
                  "content_type": self.headers.get("Content-Type", ""), "content_range": self.headers.get("Content-Range"),
                  "content_length": int(self.headers.get("Content-Length") or 0)}
        with si._lock:
            si.requests.append(record)
        fault = self._take_fault(("drop_before", "429", "503", "500", "403rate", "403perm", "401", "drop_after"))
        if fault == "drop_before":
            return self._drop()
        if fault in ("429", "503", "500"):
            self._body()
            return self._error(int(fault), "backendError" if fault != "429" else "rateLimitExceeded", "injected %s" % fault,
                               {"Retry-After": si.retry_after} if fault == "429" and si.retry_after else None)
        if fault == "403rate":
            self._body()
            return self._error(403, "rateLimitExceeded", "Rate Limit Exceeded")
        if fault == "403perm":
            self._body()
            return self._error(403, "insufficientFilePermissions", "The user does not have sufficient permissions for this file.")
        if url.path == "/token":
            return self._token()
        if fault == "401" or self.headers.get("Authorization", "")[7:] not in si.access_tokens:
            self._body()
            return self._error(401, "authError", "Invalid Credentials")
        self._lose_answer = fault == "drop_after"
        self._dispatch(url.path, q)
        if fault == "drop_after":
            return self._drop()

    def _token(self) -> None:
        form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
        refresh, cid, secret = self.stand_in.valid_refresh
        if form.get("grant_type") != "refresh_token" or (form.get("refresh_token"), form.get("client_id"), form.get("client_secret")) != (refresh, cid, secret):
            return self._send(400, {"error": "invalid_grant"})
        self.stand_in.refreshes += 1
        self.stand_in.access_tokens.add(self.stand_in.refreshed_token)
        self._send(200, {"access_token": self.stand_in.refreshed_token, "expires_in": 3599, "token_type": "Bearer"})

    def _dispatch(self, path: str, q: dict) -> None:
        si = self.stand_in
        m = re.fullmatch(r"/(upload/)?drive/v3/files(?:/([^/]+))?", path)
        if not m:
            session = re.fullmatch(r"/upload/session/([^/]+)", path)
            if session and self.command == "PUT":
                return self._resumable_put(session.group(1))
            return self._error(404, "notFound", "not found")
        upload, fid = bool(m.group(1)), unquote(m.group(2)) if m.group(2) else None
        if upload and self.command in ("POST", "PATCH"):
            return self._upload(self.command, fid, q)
        if not upload and self.command == "GET" and fid is None:
            return self._list(q)
        if not upload and self.command == "GET":
            f = si.files.get(fid)
            if f is None:
                return self._error(404, "notFound", "File not found: %s." % fid)
            if q.get("alt") == "media":
                return self._send(200, raw=f["content"], headers={"Content-Type": "application/octet-stream"})
            return self._send(200, si.resource(f))
        if not upload and self.command == "DELETE" and fid:
            if fid not in si.files:
                return self._error(404, "notFound", "File not found: %s." % fid)
            del si.files[fid]
            return self._send(204)
        return self._error(405, "methodNotAllowed", "no such method")

    # -- files.list -----------------------------------------------------------------------------------------------------

    def _list(self, q: dict) -> None:
        si = self.stand_in
        terms = q.get("q", "")
        name = re.search(r"name = '((?:\\.|[^'\\])*)'", terms)
        parent = re.search(r"'((?:\\.|[^'\\])*)' in parents", terms)
        rows = [f for f in si.files.values()]
        if name:
            rows = [f for f in rows if f["name"] == unescape_q(name.group(1))]
        if parent:
            rows = [f for f in rows if unescape_q(parent.group(1)) in f["parents"]]
        if "trashed = false" in terms:
            rows = [f for f in rows if not f["trashed"]]
        # with no orderBy the real order is unspecified; newest first here so a caller that forgot to ask for createdTime is caught
        rows.sort(key=lambda f: f["order"], reverse=q.get("orderBy") != "createdTime")
        start = int(q["pageToken"]) if q.get("pageToken") else 0
        size = min(int(q.get("pageSize", 100)), si.page_size_cap)
        page = rows[start:start + size]
        body = {"kind": "drive#fileList", "files": [si.resource(f) for f in page]}
        if start + size < len(rows):
            body["nextPageToken"] = str(start + size)
        self._send(200, body)

    # -- uploads --------------------------------------------------------------------------------------------------------

    def _upload(self, method: str, fid: str | None, q: dict) -> None:
        si = self.stand_in
        kind = q.get("uploadType")
        if method == "PATCH" and fid not in si.files:
            self._body()
            return self._error(404, "notFound", "File not found: %s." % fid)
        if kind == "multipart":
            body = self._body()
            parsed = self._parse_multipart(body)
            if parsed is None:
                return self._error(400, "badContent", "Multipart body is malformed")
            metadata, content, mime = parsed
            return self._finish(method, fid, metadata, content, mime)
        if kind == "resumable":
            metadata = json.loads(self._body() or b"{}")
            session = si._new_id("session")
            si._sessions[session] = {"method": method, "fid": fid, "metadata": metadata, "received": b"", "total": None,
                                     "mime": self.headers.get("X-Upload-Content-Type", "application/octet-stream"),
                                     "declared": int(self.headers.get("X-Upload-Content-Length") or 0)}
            return self._send(200, headers={"Location": "%s/upload/session/%s" % (si.url, session)})
        self._body()
        return self._error(400, "badRequest", "uploadType must be multipart or resumable here")

    def _parse_multipart(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/related") or "boundary=" not in ctype:
            return None
        msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
        parts = list(msg.iter_parts())
        if len(parts) != 2 or not parts[0].get_content_type().startswith("application/json"):
            return None  # "The metadata must come first and must have a Content-Type header set to application/json"
        return json.loads(parts[0].get_content()), parts[1].get_payload(decode=True), parts[1].get_content_type()

    def _resumable_put(self, session_id: str) -> None:
        si = self.stand_in
        s = si._sessions.get(session_id)
        if s is None:
            self._body()
            return self._error(404, "notFound", "The upload session has expired")
        cr = self.headers.get("Content-Range", "")
        body = self._body()
        status_query = re.fullmatch(r"bytes \*/(\d+|\*)", cr)
        if status_query and si.forget_sessions:
            si._sessions.pop(session_id, None)  # "A 404 Not Found response indicates the upload session has expired"
            return self._error(404, "notFound", "The upload session has expired")
        if status_query:
            if s["total"] is not None and len(s["received"]) == s["total"]:
                return self._send(200, si.resource(si.files[s["fid_done"]]))
            have = len(s["received"])
            return self._send(308, headers={"Range": "bytes=0-%d" % (have - 1)} if have else {})
        fault = self._take_fault(("interrupt",))
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr)
        start = int(m.group(1)) if m else 0
        if start != len(s["received"]):
            return self._error(400, "badContentRange", "Content-Range does not continue from the received bytes")
        s["received"] += body[: len(body) // 2] if fault == "interrupt" else body
        if fault == "interrupt":
            return self._drop()
        s["total"] = start + len(body)
        if s["declared"] and s["total"] != s["declared"]:
            return self._error(400, "badContent", "length does not match X-Upload-Content-Length")
        out = self._finish(s["method"], s["fid"], s["metadata"], s["received"], s["mime"], respond=False)
        s["fid_done"] = out["id"]
        self._send(200 if s["method"] == "PATCH" else 200, out)

    def _finish(self, method: str, fid: str | None, metadata: dict, content: bytes, mime: str, respond: bool = True):
        si = self.stand_in
        with si._lock:
            if method == "PATCH":
                f = si.files[fid]
                f["content"], f["mimeType"] = content, mime
                if "name" in metadata:
                    f["name"] = metadata["name"]
            else:
                new = si._new_id("file")
                f = {"id": new, "name": metadata.get("name", "Untitled"), "mimeType": mime, "parents": list(metadata.get("parents", [])),
                     "content": content, "trashed": False, "order": si._seq}
                si.files[new] = f
        out = si.resource(f)
        if respond:
            self._send(200, out)
        return out

    def do_GET(self):  # noqa: N802
        self._route()

    def do_POST(self):  # noqa: N802
        self._route()

    def do_PATCH(self):  # noqa: N802
        self._route()

    def do_PUT(self):  # noqa: N802
        self._route()

    def do_DELETE(self):  # noqa: N802
        self._route()
