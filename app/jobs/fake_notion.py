"""A stand-in Notion API on 127.0.0.1, written from the Notion reference pages for Create a page, Update a page and Request limits.

What it follows from the pages: `Authorization: Bearer` is required (401 `unauthorized` without), `Notion-Version` is required (400 `missing_version`), a page is created
under a `parent` of type `data_source_id` that exists (404 `object_not_found` for an unknown one), properties must exist in the data source's schema and have the shape of their
type (400 `validation_error`), a page answers its `id`, `object: page` and its properties, `PATCH /v1/pages/{id}` updates, and a 429 carries `Retry-After`. What it invents: the id
format, the schema (a title `Name`, selects `Status`, a number `Confidence`, rich text `Run`, `Reason`, `Proposed`, `Answer`) and every message wording past the error code.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.connectors.fake_http import Answer, FakeHTTP, Seen

SCHEMA = {"Name": "title", "Status": "select", "Confidence": "number", "Run": "rich_text", "Reason": "rich_text", "Proposed": "rich_text", "Answer": "rich_text"}


def _valid(kind: str, v: Any) -> bool:
    if kind == "title":
        return isinstance(v, dict) and isinstance(v.get("title"), list) and all(x.get("type") == "text" and "content" in x.get("text", {}) for x in v["title"])
    if kind == "rich_text":
        return isinstance(v, dict) and isinstance(v.get("rich_text"), list) and all(x.get("type") == "text" and len(x["text"]["content"]) <= 2000 for x in v["rich_text"])
    if kind == "select":
        return isinstance(v, dict) and isinstance(v.get("select"), dict) and isinstance(v["select"].get("name"), str)
    if kind == "number":
        return isinstance(v, dict) and isinstance(v.get("number"), (int, float))
    return False


class FakeNotion(FakeHTTP):
    def __init__(self, *, token: str = "secret_test", data_source_id: str = "ds-0001", version: str = "2026-03-11") -> None:
        super().__init__()
        self.token, self.data_source_id, self.version = token, data_source_id, version
        self.pages: dict[str, dict[str, Any]] = {}

    def answer(self, seen: Seen) -> Answer:
        if seen.headers.get("authorization") != "Bearer %s" % self.token:
            return 401, {}, {"object": "error", "status": 401, "code": "unauthorized", "message": "API token is invalid."}
        if not seen.headers.get("notion-version"):
            return 400, {}, {"object": "error", "status": 400, "code": "missing_version", "message": "Notion-Version header failed validation: Notion-Version header should be defined."}
        body = json.loads(seen.body or b"{}")
        if seen.method == "POST" and seen.path == "/v1/pages":
            parent = body.get("parent") or {}
            if parent.get("type") != "data_source_id" or parent.get("data_source_id") != self.data_source_id:
                return 404, {}, {"object": "error", "status": 404, "code": "object_not_found", "message": "Could not find the data source."}
            props = body.get("properties") or {}
            for name, value in props.items():
                if name not in SCHEMA:
                    return 400, {}, {"object": "error", "status": 400, "code": "validation_error", "message": "%s is not a property that exists." % name}
                if not _valid(SCHEMA[name], value):
                    return 400, {}, {"object": "error", "status": 400, "code": "validation_error", "message": "%s is expected to be %s." % (name, SCHEMA[name])}
            if "Name" not in props:
                return 400, {}, {"object": "error", "status": 400, "code": "validation_error", "message": "Name is required."}
            pid = str(uuid.uuid4())
            self.pages[pid] = {"object": "page", "id": pid, "parent": parent, "properties": props, "archived": False}
            return 200, {}, self.pages[pid]
        if seen.method == "PATCH" and seen.path.startswith("/v1/pages/"):
            pid = seen.path.rsplit("/", 1)[1]
            if pid not in self.pages:
                return 404, {}, {"object": "error", "status": 404, "code": "object_not_found", "message": "Could not find page."}
            for name, value in (body.get("properties") or {}).items():
                if name not in SCHEMA or not _valid(SCHEMA[name], value):
                    return 400, {}, {"object": "error", "status": 400, "code": "validation_error", "message": "bad property %s" % name}
                self.pages[pid]["properties"][name] = value
            return 200, {}, self.pages[pid]
        return 404, {}, {"object": "error", "status": 404, "code": "invalid_request_url", "message": "Invalid request URL."}
