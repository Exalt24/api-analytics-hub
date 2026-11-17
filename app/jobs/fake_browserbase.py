"""A stand-in Browserbase API on 127.0.0.1, written from the Sessions API pages (docs.browserbase.com, "Create a browser session", "Using a browser
session", "Manage a browser session") and driven by the official `browserbase` Python SDK, which is why the SDK's own paths and field names are the spec.

What it follows from the pages: `POST /v1/sessions` with the `x-bb-api-key` header creates a session and answers its `id`, `status`, `connectUrl`
(a CDP websocket a Playwright `connect_over_cdp` takes) and the other session fields; `POST /v1/sessions/{id}` with `status: REQUEST_RELEASE` ends
it; `GET /v1/sessions/{id}`. A missing or wrong key is 401. What it invents: the Chromium behind `connectUrl` is a local one started with a DevTools
port (the real service runs its own fleet), the session id format, and the error bodies. It records every request, so a test can show the SDK
created one session and released it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from app.connectors.fake_http import Answer, FakeHTTP, Seen


def _chromium_path() -> str:
    with sync_playwright() as p:
        return p.chromium.executable_path


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBrowserbase(FakeHTTP):
    def __init__(self, *, api_key: str = "bb_test_key") -> None:
        super().__init__()
        self.api_key = api_key
        self.sessions: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._profile_root = Path(__import__("tempfile").mkdtemp(prefix="fake-bb-"))

    def _start_browser(self, sid: str) -> str:
        port = _port()
        # A CI runner (Ubuntu 24.04) refuses Chromium's sandbox for an unprivileged user, so the stand-in's own throwaway Chromium never opens its DevTools port there. The flag is added only
        # when CI is set: this browser is a local test double on an empty profile that only loads about:blank and the stand-in forms, never a real site.
        sandbox = ["--no-sandbox"] if os.environ.get("CI") else []
        self._procs[sid] = subprocess.Popen(
            [_chromium_path(), "--headless=new", "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % (self._profile_root / sid), "--no-first-run"] + sandbox + ["about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=1) as r:
                    return json.load(r)["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("the stand-in's Chromium did not open its DevTools port")

    def answer(self, seen: Seen) -> Answer:
        if seen.headers.get("x-bb-api-key") != self.api_key:
            return 401, {}, {"message": "Unauthorized", "statusCode": 401}
        body = json.loads(seen.body or b"{}")
        parts = [p for p in seen.path.split("/") if p]
        if seen.method == "POST" and parts == ["v1", "sessions"]:
            sid = str(uuid.uuid4())
            ws = self._start_browser(sid)
            session = {"id": sid, "status": "RUNNING", "connectUrl": ws, "projectId": body.get("projectId", "proj-test"), "keepAlive": bool(body.get("keepAlive")),
                       "createdAt": "2026-11-17T00:00:00.000Z", "region": body.get("region", "us-west-2"), "seleniumRemoteUrl": "", "signingKey": "stand-in"}
            self.sessions[sid] = session
            return 201, {}, session
        if len(parts) == 3 and parts[:2] == ["v1", "sessions"] and seen.method == "POST":
            sid = parts[2]
            if sid not in self.sessions:
                return 404, {}, {"message": "Session not found"}
            if body.get("status") == "REQUEST_RELEASE":
                self.sessions[sid]["status"] = "COMPLETED"
                proc = self._procs.pop(sid, None)
                if proc:
                    proc.terminate()
            return 200, {}, self.sessions[sid]
        if len(parts) == 3 and parts[:2] == ["v1", "sessions"] and seen.method == "GET":
            return (200, {}, self.sessions[parts[2]]) if parts[2] in self.sessions else (404, {}, {"message": "Session not found"})
        return 404, {}, {"message": "not found"}

    def __exit__(self, *exc: Any) -> None:
        for proc in self._procs.values():
            proc.terminate()
        super().__exit__(*exc)
