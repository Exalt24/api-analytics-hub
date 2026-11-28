"""A stand-in Meta Marketing API server for ad creatives, on 127.0.0.1, for tests and the sample run.

It speaks real HTTP to `meta_creatives.py` and implements what the saved pages describe (docs/META_SOURCES.md,
section "Ad creatives"): the ads edges `GET /v26.0/act_{id}/ads` and `GET /v26.0/{campaign}/ads` with `fields`,
`limit`, `after` and the `effective_status` filter (a list of the Ad page's status values); field expansion
(`creative{...}`, `campaign{...}`, nested to any depth, a field the node does not have refused); the creative node, the
campaign node and the video node read by id (`GET /v26.0/{id}?fields=...`); the batch endpoint
(`POST /v26.0` with a `batch` list of at most 50 `{"method", "relative_url"}` requests, each answered as
`{"code", "body"}` with the body a JSON string, and a null answer for a request that timed out); cursor paging with
`paging.cursors.after`, a `next` link that carries the access token, its absence on the last page, and pages that are
empty and still carry a `next`; the error object; and the usage headers `x-business-use-case-usage` (type
`ads_management`) and `x-app-usage`.

It is a stand-in written from documentation, not a recording of Meta. What it cannot tell you is listed in
docs/META_SOURCES.md. Where it refuses something (an unknown field, a status the Ad page does not list, a batch of more
than 50) it does so because a page says the real API would, so a client that passes here has not been let off a
documented rule. Choices that are its own are marked UNVERIFIED there: the HTTP status of an error (400), the error
code and text of an over long batch, what a read of a missing video answers, that a video without a source leaves the
key out (or sends null, `video_source_null`), and what `effective_status` filtering does when it is absent.

Faults are scripted: `server.faults.append(Fault(400, error_body(4, "OAuthException", "...")))` makes the next request
answer that way once, and `server.fault_at[3] = Fault(...)` makes the request with that number (counted from 0) do it.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .meta_fake import Fault, error_body  # noqa: F401 - re-exported so a test needs one import

AD_STATUSES = (
    "ACTIVE", "PAUSED", "DELETED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO",
    "CAMPAIGN_PAUSED", "ARCHIVED", "ADSET_PAUSED", "IN_PROCESS", "WITH_ISSUES",
)
#: Field names of each node, from the pages (Ad, Ad Creative, Video). A field not here is refused with error 100.
KNOWN = {
    "Ad": {
        "id", "account_id", "ad_active_time", "ad_review_feedback", "ad_schedule_end_time", "ad_schedule_start_time",
        "adlabels", "adset", "adset_id", "bid_amount", "bid_info", "bid_type", "campaign", "campaign_id",
        "configured_status", "conversion_domain", "conversion_specs", "created_time", "creative",
        "creative_asset_groups_spec", "demolink_hash", "display_sequence", "effective_status", "engagement_audience",
        "failed_delivery_checks", "is_autobid", "issues_info", "last_updated_by_app_id", "name",
        "preview_shareable_link", "priority", "recommendations", "source_ad", "source_ad_id", "special_ad_categories",
        "status", "targeting", "tracking_and_conversion_with_defaults", "tracking_specs", "updated_time",
    },
    "AdCreative": {
        "id", "account_id", "actor_id", "ad_disclaimer_spec", "adlabels", "applink_treatment", "asset_feed_spec",
        "authorization_category", "body", "branded_content", "call_to_action", "call_to_action_type",
        "effective_object_story_id", "effective_instagram_media_id", "image_crops", "image_hash", "image_url",
        "instagram_permalink_url", "instagram_user_id", "link_destination_display_url", "link_og_id", "link_url",
        "name", "object_id", "object_store_url", "object_story_id", "object_story_spec", "object_type", "object_url",
        "platform_customizations", "product_set_id", "source_instagram_media_id", "status", "template_url",
        "thumbnail_id", "thumbnail_url", "title", "url_tags", "video_id", "degrees_of_freedom_spec",
    },
    "Campaign": {"id", "name", "status", "effective_status", "objective", "account_id"},
    #: Video page fields, plus `permalink_url` and `length`, which the task names and the page does not (UNVERIFIED).
    "Video": {
        "id", "created_time", "description", "embed_html", "format", "event", "from", "icon", "name", "picture", "place",
        "source", "updated_time", "permalink_url", "length",
    },
}
_AD_PATH = re.compile(r"^/v26\.0/act_(\d+)/ads$")
_EDGE_PATH = re.compile(r"^/v26\.0/(\d+)/ads$")
_NODE_PATH = re.compile(r"^/v26\.0/(\d+)$")
_BATCH_PATH = re.compile(r"^/v26\.0/?$")
BATCH_LIMIT = 50


class FieldError(Exception):
    def __init__(self, field: str, node: str):
        super().__init__(field)
        self.field, self.node = field, node


def parse_fields(text: str) -> dict[str, Any]:
    """`a,b{c,d{e}},f` as {"a": None, "b": {"c": None, "d": {"e": None}}, "f": None}. Raises ValueError when unbalanced."""
    pos = 0

    def level(depth: int) -> dict[str, Any]:
        nonlocal pos
        out: dict[str, Any] = {}
        name = ""
        while pos < len(text):
            ch = text[pos]
            if ch == "{":
                if not name.strip():
                    raise ValueError("a { with no field before it")
                pos += 1
                out[name.strip()] = level(depth + 1)
                name = ""
                if pos < len(text) and text[pos] == ",":
                    pos += 1
                continue
            if ch == "}":
                if depth == 0:
                    raise ValueError("a } with no { before it")
                if name.strip():
                    out.setdefault(name.strip(), None)
                pos += 1
                return out
            if ch == ",":
                if name.strip():
                    out.setdefault(name.strip(), None)
                name = ""
                pos += 1
                continue
            name += ch
            pos += 1
        if depth:
            raise ValueError("a { that is never closed")
        if name.strip():
            out.setdefault(name.strip(), None)
        return out

    return level(0)


def project(obj: Mapping[str, Any], tree: Mapping[str, Any] | None, node: str) -> dict[str, Any]:
    """The requested fields of `obj`. A field the node does not have is refused; one it has with no value is left out."""
    known = KNOWN.get(node)
    out: dict[str, Any] = {}
    for name, sub in (tree or {}).items():
        if known is not None and name not in known:
            raise FieldError(name, node)
        if name not in obj:
            continue
        value = obj[name]
        if sub is None:
            out[name] = value
        elif isinstance(value, dict):
            out[name] = project(value, sub, {"creative": "AdCreative", "campaign": "Campaign"}.get(name, ""))
        elif isinstance(value, list):
            out[name] = [project(v, sub, "") if isinstance(v, dict) else v for v in value]
        else:
            out[name] = value
    return out


def _cursor(offset: int, empties: int = 0) -> str:
    return base64.b64encode(("o:%d:e:%d" % (offset, empties)).encode()).decode()


def _decode(cursor: str) -> tuple[int, int] | None:
    try:
        text = base64.b64decode(cursor.encode(), validate=True).decode()
    except ValueError:
        return None
    m = re.fullmatch(r"o:(\d+):e:(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    raw_query: str


class FakeMetaCreatives:
    """Start with `with FakeMetaCreatives(token="t") as server:`, then point the client at `server.url`."""

    def __init__(self, *, token: str = "test-token"):
        self.token = token
        self.faults: list[Fault] = []
        self.fault_at: dict[int, Fault] = {}
        #: account id -> {"campaigns": {campaign id: name}, "ads": [raw ad objects in the order they are served]}
        self.accounts: dict[str, dict[str, Any]] = {}
        #: video id -> the video node: {"source", "permalink_url", "length", ...}
        self.videos: dict[str, dict[str, Any]] = {}
        #: node id -> (HTTP status, error body): reading it answers that error (a video nobody may read).
        self.node_errors: dict[str, tuple[int, Any]] = {}
        #: A batch request for these ids answers null the first time, as a request that timed out does.
        self.batch_null_once: set[str] = set()
        #: A batch request for these ids answers a throttle error the first time.
        self.batch_throttle_once: set[str] = set()
        #: Every request seen.
        self.requests: list[Seen] = []
        self.utilisation = 5
        self.regain_minutes = 0
        #: The two spellings of the business use case header on the Graph and the Marketing API rate limit pages.
        self.buc_header = "x-business-use-case-usage"
        #: The `next` link points at this base address instead of the stand-in's own (a hostile response).
        self.next_base: str | None = None
        #: Answer this many empty pages that still carry a `next` link before the real ones.
        self.empty_pages_with_next = 0
        #: "creative" and/or "campaign": a nested read of that field answers only {"id": ...}, the way the Field
        #: Expansion page says some Marketing API connections cannot expand.
        self.refuse_expansion: set[str] = set()
        #: A video without a source sends `"source": null` instead of leaving the key out.
        self.video_source_null = False
        #: Send the creative's asset_feed_spec as a JSON string, as the Ad Creative page describes the field.
        self.asset_feed_as_string = False
        #: Serve the creative field `object_story_spec` etc. as stored. Every ad stored here is served in full.
        self._count = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "FakeMetaCreatives":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
                form: dict[str, str] = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length).decode("utf-8") if length else ""
                    form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                status, headers, body, drop = fake.handle(method, parsed.path, query, form, parsed.query)
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def set_account(self, account_id: str, *, campaigns: Mapping[str, str] | None = None,
                    ads: list[Mapping[str, Any]] | None = None) -> None:
        """`ads` are raw ad objects: id, name, status, effective_status, campaign_id, adset_id, preview_shareable_link, creative."""
        with self._lock:
            self.accounts[str(account_id)] = {"campaigns": dict(campaigns or {}), "ads": [dict(a) for a in (ads or [])]}

    # ------------------------------------------------------------------ the request

    def _headers(self) -> dict[str, str]:
        return {
            "x-app-usage": json.dumps({"call_count": self.utilisation, "total_time": 1, "total_cputime": 1}),
            self.buc_header: json.dumps({"123": [{
                "type": "ads_management", "call_count": self.utilisation, "total_cputime": 1, "total_time": 1,
                "estimated_time_to_regain_access": self.regain_minutes, "ads_api_access_tier": "standard_access"}]}),
        }

    def handle(self, method: str, path: str, query: dict[str, str], form: dict[str, str],
               raw_query: str) -> tuple[int, dict[str, str], Any, bool]:
        with self._lock:
            number = self._count
            self._count += 1
            self.requests.append(Seen(method, path, dict(query), dict(form), raw_query))
            fault = self.fault_at.pop(number, None)
            if fault is None and self.faults:
                fault = self.faults.pop(0)
            if fault is not None:
                if fault.drop:
                    return 0, {}, b"", True
                body = fault.body if fault.body is not None else error_body(2, "OAuthException", "scripted fault")
                return fault.status, dict(fault.headers), body, False
            return self._answer(method, path, {**query, **form})

    def _err(self, code: int, kind: str, message: str, subcode: int | None = None, status: int = 400):
        return status, {}, error_body(code, kind, message, subcode), False

    def _answer(self, method: str, path: str, params: dict[str, str]) -> tuple[int, dict[str, str], Any, bool]:
        if method == "POST":
            if not _BATCH_PATH.match(path):
                return self._err(100, "GraphMethodException", "Unsupported post request.", 33)
            if params.get("access_token") != self.token:
                return self._err(190, "OAuthException", "Invalid OAuth access token.")
            return self._batch(params)
        if params.get("access_token") != self.token:
            return self._err(190, "OAuthException", "Invalid OAuth access token.")
        if (m := _AD_PATH.match(path)):
            account = self.accounts.get(m.group(1))
            if account is None:
                return self._err(200, "OAuthException", "(#200) Permissions error")
            return self._ads(account["ads"], params, account["campaigns"], None)
        if (m := _EDGE_PATH.match(path)):
            campaign = m.group(1)
            for account in self.accounts.values():
                if campaign in account["campaigns"]:
                    ads = [a for a in account["ads"] if str(a.get("campaign_id")) == campaign]
                    return self._ads(ads, params, account["campaigns"], campaign)
            return self._err(100, "GraphMethodException", "Unsupported get request.", 33)
        if (m := _NODE_PATH.match(path)):
            status, headers, body = self._node(m.group(1), params.get("fields", "id"))
            return status, headers, body, False
        return self._err(100, "GraphMethodException", "Unsupported get request.", 33, 404)

    # ------------------------------------------------------------------ the ads edge

    def _served_ad(self, ad: Mapping[str, Any], campaigns: Mapping[str, str]) -> dict[str, Any]:
        """The ad as Meta would hold it: the stored object, with the `campaign` node filled from `campaign_id`."""
        out = dict(ad)
        cid = str(ad.get("campaign_id")) if ad.get("campaign_id") is not None else None
        if cid is not None and cid in campaigns:
            out["campaign"] = {"id": cid, "name": campaigns[cid]}
        elif cid is not None:
            out["campaign"] = {"id": cid}
        return out

    def _ads(self, ads: list[dict], params: dict[str, str], campaigns: Mapping[str, str], campaign: str | None):
        try:
            tree = parse_fields(params.get("fields", "id"))
        except ValueError as exc:
            return self._err(2500, "GraphMethodException", "Error parsing graph query: %s" % exc)
        if "effective_status" in params:
            try:
                wanted = json.loads(params["effective_status"])
            except ValueError:
                wanted = None
            if not isinstance(wanted, list) or not wanted or any(s not in AD_STATUSES for s in wanted):
                return self._err(100, "OAuthException", "(#100) effective_status must be a list of the Ad page's status values")
        else:
            wanted = [s for s in AD_STATUSES if s not in ("DELETED", "ARCHIVED")]
        limit_text = params.get("limit", "25")
        if not (limit_text.isdigit() and int(limit_text) >= 1):
            return self._err(100, "OAuthException", "(#100) limit must be a positive integer")
        if "after" in params and _decode(params["after"]) is None:
            return self._err(2642, "OAuthException", "Invalid cursors values")
        rows = [a for a in ads if a.get("effective_status", a.get("status")) in wanted]
        offset, empties = _decode(params["after"]) if "after" in params else (0, self.empty_pages_with_next)
        limit = int(limit_text)
        try:
            if empties:
                return 200, self._headers(), self._page([], offset, offset, empties - 1, params, True, campaign), False
            page = [self._project_ad(a, tree, campaigns) for a in rows[offset: offset + limit]]
        except FieldError as exc:
            return self._err(100, "GraphMethodException",
                             "(#100) Tried accessing nonexisting field (%s) on node type (%s)" % (exc.field, exc.node))
        end = offset + len(page)
        return 200, self._headers(), self._page(page, offset, end, 0, params, end < len(rows), campaign), False

    def _project_ad(self, ad: Mapping[str, Any], tree: Mapping[str, Any], campaigns: Mapping[str, str]) -> dict[str, Any]:
        served = self._served_ad(ad, campaigns)
        creative = served.get("creative")
        if isinstance(creative, dict):
            creative = dict(creative)
            if self.asset_feed_as_string and isinstance(creative.get("asset_feed_spec"), dict):
                creative["asset_feed_spec"] = json.dumps(creative["asset_feed_spec"])
            served["creative"] = creative
        out = project(served, tree, "Ad")
        for name in self.refuse_expansion:
            if name in out and tree.get(name) is not None and isinstance(out[name], dict):
                out[name] = {"id": out[name].get("id")} if out[name].get("id") is not None else {}
        return out

    def _page(self, rows: list, offset: int, end: int, empties: int, params: dict[str, str], more: bool,
              campaign: str | None) -> dict:
        out: dict[str, Any] = {"data": rows}
        paging: dict[str, Any] = {"cursors": {"before": _cursor(offset, empties), "after": _cursor(end, empties)}}
        if more:
            base = self.next_base or self.url
            edge = "%s/ads" % campaign if campaign else "act_x/ads"
            paging["next"] = "%s/v26.0/%s?after=%s&access_token=%s" % (base, edge, paging["cursors"]["after"], self.token)
        out["paging"] = paging
        return out

    # ------------------------------------------------------------------ nodes and batches

    def _find_node(self, node_id: str) -> tuple[str, dict[str, Any]] | None:
        if node_id in self.videos:
            return "Video", {"id": node_id, **self.videos[node_id]}
        for account in self.accounts.values():
            if node_id in account["campaigns"]:
                return "Campaign", {"id": node_id, "name": account["campaigns"][node_id]}
            for ad in account["ads"]:
                creative = ad.get("creative")
                if isinstance(creative, dict) and str(creative.get("id")) == node_id:
                    return "AdCreative", dict(creative)
        return None

    def _node(self, node_id: str, fields: str) -> tuple[int, dict[str, str], Any]:
        if node_id in self.node_errors:
            status, body = self.node_errors[node_id]
            return status, {}, body
        found = self._find_node(node_id)
        if found is None:
            return 400, {}, error_body(100, "GraphMethodException", "Unsupported get request.", 33)
        node, obj = found
        if node == "Video" and self.video_source_null and "source" not in obj:
            obj["source"] = None
        if node == "AdCreative" and self.asset_feed_as_string and isinstance(obj.get("asset_feed_spec"), dict):
            obj["asset_feed_spec"] = json.dumps(obj["asset_feed_spec"])
        try:
            tree = parse_fields(fields)
            return 200, self._headers(), project(obj, tree, node)
        except ValueError as exc:
            return 400, {}, error_body(2500, "GraphMethodException", "Error parsing graph query: %s" % exc)
        except FieldError as exc:
            return 400, {}, error_body(100, "GraphMethodException",
                                       "(#100) Tried accessing nonexisting field (%s) on node type (%s)" % (exc.field, exc.node))

    def _batch(self, params: dict[str, str]) -> tuple[int, dict[str, str], Any, bool]:
        try:
            items = json.loads(params.get("batch", ""))
        except ValueError:
            items = None
        if not isinstance(items, list):
            return self._err(100, "OAuthException", "(#100) The parameter batch is required and must be a list")
        if len(items) > BATCH_LIMIT:
            # UNVERIFIED: the Batch Requests page gives the limit, 50, and not the answer to a longer batch.
            return self._err(100, "OAuthException", "(#100) batch requests are limited to %d requests" % BATCH_LIMIT)
        with_headers = params.get("include_headers", "true") != "false"
        out: list[Any] = []
        for item in items:
            if not isinstance(item, dict) or item.get("method") != "GET" or not isinstance(item.get("relative_url"), str):
                out.append({"code": 400, "body": json.dumps(error_body(100, "OAuthException", "(#100) bad batch request"))})
                continue
            rel = urlparse(item["relative_url"])
            node_id = rel.path.strip("/")
            query = {k: v[0] for k, v in parse_qs(rel.query, keep_blank_values=True).items()}
            if node_id in self.batch_null_once:
                self.batch_null_once.discard(node_id)
                out.append(None)
                continue
            if node_id in self.batch_throttle_once:
                self.batch_throttle_once.discard(node_id)
                out.append({"code": 400, "body": json.dumps(error_body(80004, "OAuthException", "(#80004) There have been too many calls to this ad-account. Wait a bit and try again."))})
                continue
            if not node_id.isdigit():
                out.append({"code": 400, "body": json.dumps(error_body(100, "GraphMethodException", "Unsupported get request.", 33))})
                continue
            status, _, body = self._node(node_id, query.get("fields", "id"))
            entry: dict[str, Any] = {"code": 200 if status == 200 else status, "body": json.dumps(body)}
            if with_headers:
                entry["headers"] = [{"name": "Content-Type", "value": "text/javascript; charset=UTF-8"}]
            out.append(entry)
        return 200, self._headers(), out, False


# ---------------------------------------------------------------------------------------------- a small sample account

def load_sample(server: FakeMetaCreatives, account_id: str = "555001") -> None:
    """A small account with one of each creative shape the pages describe, for the CLI's `--stand-in` run and the docs.

    Every name, link and copy line is made up for the sample. Campaign 101 holds a link ad, a carousel and a video ad;
    campaign 102 a photo ad, a dynamic creative with two videos, and an ad on an existing page post; campaign 103 an ad
    whose creative was deleted and a paused ad. Two videos have a source, one has none, one cannot be read.
    """
    server.videos.update({
        "9001": {"source": "https://video.example.test/v/9001.mp4", "permalink_url": "https://www.facebook.com/page1/videos/9001/", "length": 31.5},
        "9002": {"source": "https://video.example.test/v/9002.mp4", "permalink_url": "/page1/videos/9002/", "length": 12},
        "9003": {"permalink_url": "https://www.facebook.com/page1/videos/9003/", "length": 45},
        "9004": {"source": "https://video.example.test/v/9004.mp4", "length": 8},
    })
    server.node_errors["9004"] = (400, error_body(200, "OAuthException", "(#200) Permissions error"))
    campaigns = {"101": "Spring sale", "102": "Brand video", "103": "Archive tests"}

    def ad(ad_id: str, name: str, campaign: str, creative: dict, status: str = "ACTIVE") -> dict:
        return {"id": ad_id, "name": name, "status": status, "effective_status": status, "campaign_id": campaign,
                "adset_id": "7%s" % ad_id, "preview_shareable_link": "https://fb.me/preview/%s" % ad_id, "creative": creative}

    def cta(kind: str, link: str) -> dict:
        return {"type": kind, "value": {"link": link}}

    ads = [
        ad("1001", "Spring link ad", "101", {
            "id": "5001", "name": "Spring link", "status": "ACTIVE", "image_hash": "hash1001",
            "object_story_spec": {"page_id": "11", "link_data": {
                "message": "Fresh deals for spring.", "name": "Spring sale", "description": "Up to 30 percent off",
                "link": "https://shop.example.test/spring", "picture": "https://img.example.test/spring.jpg",
                "call_to_action": cta("SHOP_NOW", "https://shop.example.test/spring")}},
            "effective_object_story_id": "11_9901"}),
        ad("1002", "Spring carousel", "101", {
            "id": "5002", "name": "Spring carousel", "status": "ACTIVE",
            "object_story_spec": {"page_id": "11", "link_data": {
                "message": "Three ways to start spring.", "link": "https://shop.example.test/",
                "child_attachments": [
                    {"name": "Seeds", "description": "From 2 dollars", "link": "https://shop.example.test/seeds",
                     "picture": "https://img.example.test/seeds.jpg", "call_to_action": cta("SHOP_NOW", "https://shop.example.test/seeds")},
                    {"name": "Tools", "description": "Built to last", "link": "https://shop.example.test/tools",
                     "image_hash": "hashtools", "call_to_action": cta("LEARN_MORE", "https://shop.example.test/tools")},
                    {"name": "Demo reel", "link": "https://shop.example.test/reel", "video_id": "9001",
                     "call_to_action": cta("WATCH_MORE", "https://shop.example.test/reel")}]}}}),
        ad("1003", "Spring video", "101", {
            "id": "5003", "name": "Spring video", "status": "ACTIVE", "thumbnail_url": "https://img.example.test/t5003.jpg",
            "object_story_spec": {"page_id": "11", "video_data": {
                "message": "Watch how it grows.", "title": "Growth in 30 seconds", "link_description": "See the whole season",
                "video_id": "9002", "image_url": "https://img.example.test/9002.jpg",
                "call_to_action": cta("LEARN_MORE", "https://shop.example.test/grow")}}}),
        ad("1004", "Brand photo", "102", {
            "id": "5004", "name": "Brand photo", "status": "ACTIVE",
            "object_story_spec": {"page_id": "11", "photo_data": {"caption": "Our first greenhouse.", "url": "https://img.example.test/greenhouse.jpg"}}}),
        ad("1005", "Brand dynamic", "102", {
            "id": "5005", "name": "Brand dynamic", "status": "ACTIVE", "object_story_spec": {"page_id": "11"},
            "asset_feed_spec": {
                "videos": [{"video_id": "9001", "thumbnail_url": "https://img.example.test/9001.jpg"},
                           {"video_id": "9003", "thumbnail_url": "https://img.example.test/9003.jpg"}],
                "bodies": [{"text": "Begin your season."}, {"text": "Grow something."}],
                "titles": [{"text": "Level up"}, {"text": "Swipe to grow"}],
                "descriptions": [{"text": "A first dynamic sample"}],
                "link_urls": [{"website_url": "https://shop.example.test/brand"}],
                "call_to_action_types": ["LEARN_MORE"], "ad_formats": ["SINGLE_VIDEO"], "optimization_type": "REGULAR"}}),
        ad("1006", "Boosted post", "102", {
            "id": "5006", "name": "Boosted post", "status": "ACTIVE", "object_story_id": "11_9902",
            "effective_object_story_id": "11_9902", "instagram_permalink_url": "https://www.instagram.com/p/EXAMPLE1/"}),
        ad("1007", "Old banner", "103", {"id": "5007", "status": "DELETED"}, status="DELETED"),
        ad("1008", "Paused video", "103", {
            "id": "5008", "name": "Paused video", "status": "ACTIVE", "video_id": "9004", "body": "Paused for the season.",
            "call_to_action_type": "LEARN_MORE", "object_url": "https://shop.example.test/pause"}, status="PAUSED"),
    ]
    server.set_account(account_id, campaigns=campaigns, ads=ads)
