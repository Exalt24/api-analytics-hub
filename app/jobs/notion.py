"""A Notion board for the review queue: a page for each question a person has to answer, marked when it is answered.

Written from the Notion API reference pages (Create a page, Update a page, Request limits, Versioning) and tested against app/jobs/fake_notion.py, a stand-in
that follows those pages. It has not been run against a real Notion workspace. What it follows: `POST /v1/pages` with a `parent` of type `data_source_id`, the
`Authorization: Bearer` and `Notion-Version` headers, rich text properties as arrays of `{type: text, text: {content}}`, `PATCH /v1/pages/{id}` to update, and a 429
answered by waiting the `Retry-After` the answer carries before trying again. The Notion board is a view of the queue and never the queue itself: the hub's
own table decides what is pending, so a failed Notion call never loses or answers a task.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger("hub.jobs")


class NotionError(RuntimeError):
    pass


def _text(s: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": s[:1900]}}]}


class NotionBoard:
    def __init__(self, token: str, data_source_id: str, *, base_url: str = "https://api.notion.com", version: str = "2026-03-11", sleep=time.sleep, attempts: int = 3):
        self._token, self._ds, self._base, self._version, self._sleep, self._attempts = token, data_source_id, base_url.rstrip("/"), version, sleep, attempts

    def _call(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self._token, "Notion-Version": self._version, "Content-Type": "application/json"}
        for attempt in range(self._attempts):
            r = httpx.request(method, self._base + path, json=body, headers=headers, timeout=20)
            if r.status_code == 429:
                if attempt + 1 == self._attempts:
                    break
                self._sleep(min(float(r.headers.get("retry-after", 1)), 60.0))
                continue
            if r.status_code >= 400:
                raise NotionError("%s %s answered %d: %s" % (method, path, r.status_code, r.text[:200]))
            return r.json()
        raise NotionError("%s %s was rate limited on every attempt" % (method, path))

    def post_review(self, run_id: str, field_label: str, confidence: float, reason: str, proposed: str | None) -> str:
        page = self._call("POST", "/v1/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self._ds},
            "properties": {"Name": {"title": [{"type": "text", "text": {"content": field_label[:200] or "(unlabelled)"}}]}, "Status": {"select": {"name": "Needs an answer"}},
                           "Confidence": {"number": round(confidence, 3)}, "Run": _text(run_id), "Reason": _text(reason), "Proposed": _text(proposed or "")}})
        return str(page["id"])

    def mark_answered(self, page_id: str, answer: str) -> None:
        self._call("PATCH", "/v1/pages/%s" % page_id, {"properties": {"Status": {"select": {"name": "Answered"}}, "Answer": _text(answer)}})
