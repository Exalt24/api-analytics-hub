"""Meta Marketing API ad creative reader: every ad in an ad account, with its copy, links, video and preview.

This is a reader, not a `Connector` for the daily snapshot table: creative data is a list of ads, not a number per
day, so it has its own small client and its own result type. It sits beside `meta.py`, reuses that file's error
object reading, error code sets, account id rule and rate limit header reader, and changes nothing in it.

WHAT IT CALLS (every fact, the page it came from and what is not confirmed is in docs/META_SOURCES.md):

  GET  {base}/v26.0/act_{id}/ads          the ads of an ad account, `fields` with `creative{...}` and `campaign{...}`
  GET  {base}/v26.0/{campaign}/ads        the same for one campaign, when a campaign filter is given
  POST {base}/v26.0                       a batch of up to 50 reads: the video lookups (`{video}?fields=source,...`),
                                          and, only when a response does not expand the creative or the campaign, the
                                          reads of those nodes by id

It has been run only against the stand-in server in `meta_creatives_fake.py`, never against a live ad account: no
account is held.

THE SHAPES OF A CREATIVE. The same question, "what does this ad say and where does it go", has a different answer in
each shape, so each is read from the place the pages put it: `object_story_spec.link_data` (message, name,
description, link, call_to_action, picture), `link_data.child_attachments` for a carousel, `object_story_spec.video_data`
(message, title, link_description, call_to_action, video_id, image_url), `object_story_spec.photo_data` (caption, url),
`asset_feed_spec` for a dynamic creative (bodies, titles, descriptions, link_urls, videos, images, call_to_action_types),
and the creative's own `body`, `title`, `call_to_action_type`, `object_url`, `video_id`, `image_url`. A creative that
only points at an existing page post (`object_story_id`) carries no copy the pages let this client read.

WHY A `next` LINK IS NEVER FOLLOWED. A Graph `next` link is a full URL that carries the access token, so following
whatever a response names would send the token wherever the response points. `next` only says another page exists; the
request for it is built here from the same base address with the `after` cursor. The page can also be empty and still
carry a `next`, which the paging page allows.

PACING. A call is tried `max_attempts` times with a bounded, jittered wait. A wait the response asks for that is
longer than `backoff_cap` (Meta's usage header gives minutes) is handed back as `RateLimited`, and so is the next
page when the usage headers say the account is near its limit. The pages already read are kept by `fetch_all`.

THE TOKEN. An already issued access token in a `Secret`. A GET carries it as the `access_token` query parameter, which
is how the pages show it; a batch carries it in the form body, so it is never in a URL this client builds. No URL,
request or response body is put in an error message.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken
from .meta import (
    _ACCOUNT_RE,
    _EXPIRED_CODES,
    _PERMISSION_CODES,
    _TEMPORARY_CODES,
    _THROTTLE_CODES,
    API_VERSION,
    BASE_URL,
    UTILISATION_CEILING,
    _MetaError,
    _read_error,
    read_usage,
)

#: The values of `effective_status` on the Ad page.
AD_STATUSES = (
    "ACTIVE", "PAUSED", "DELETED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO",
    "CAMPAIGN_PAUSED", "ARCHIVED", "ADSET_PAUSED", "IN_PROCESS", "WITH_ISSUES",
)
#: What is asked for when the caller names nothing: every status but DELETED. A deleted ad is only read on request.
DEFAULT_STATUSES = tuple(s for s in AD_STATUSES if s != "DELETED")

#: The creative fields read, every one of them on the Ad Creative page.
CREATIVE_FIELDS = (
    "id", "name", "status", "body", "title", "call_to_action_type", "object_type", "object_url", "link_url",
    "image_url", "image_hash", "thumbnail_url", "video_id", "object_story_id", "effective_object_story_id",
    "object_story_spec", "asset_feed_spec", "instagram_permalink_url",
)
#: Fields of the Ad page, plus the two nested reads. `campaign` and `creative` are typed nodes on the Ad page.
AD_FIELDS = (
    "id,name,status,effective_status,campaign_id,adset_id,preview_shareable_link,"
    "campaign{id,name},creative{%s}" % ",".join(CREATIVE_FIELDS)
)
#: The fields asked of a video. `source` is on the Video page; `permalink_url` and `length` are not (UNVERIFIED).
VIDEO_FIELDS = "source,permalink_url,length"

#: Batch requests are limited to 50 requests per batch (the Batch Requests page).
BATCH_LIMIT = 50
DEFAULT_PAGE_SIZE = 50
#: A safety stop for an edge whose cursors keep changing: 2000 pages of 50 is far past the 5000 ads of a regular account.
MAX_PAGES = 2000

#: The rate limit error codes the Rate Limits page lists for the Marketing API, joined to the ones `meta.py` knows.
_THROTTLE = _THROTTLE_CODES | frozenset({80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014})

_ID_RE = re.compile(r"^\d{1,25}$")
_STORY_RE = re.compile(r"^(\d{1,25})_(\d{1,25})$")
FACEBOOK = "https://www.facebook.com"
_SEPARATOR = " | "


def _ids_in(values: Iterable[Any]) -> list[str]:
    return [v for v in values if isinstance(v, str) and _ID_RE.match(v)]


def _text(value: Any, what: str) -> str | None:
    """A string field. Absent or null is None; any other type is a broken answer, not copy."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise UpstreamBroken("%s is %s, not a string" % (what, type(value).__name__))
    return value


def _object(value: Any, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UpstreamBroken("%s is %s, not an object" % (what, type(value).__name__))
    return value


def _list(value: Any, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UpstreamBroken("%s is %s, not a list" % (what, type(value).__name__))
    return value


def _unique(values: Iterable[str | None]) -> list[str]:
    """The non empty values in first seen order, each once."""
    seen: dict[str, None] = {}
    for v in values:
        if v:
            seen.setdefault(v, None)
    return list(seen)


@dataclass
class VideoInfo:
    video_id: str
    source: str | None = None
    permalink_url: str | None = None
    length: float | int | None = None
    #: Why a field is missing, in words, when it is: "no source returned", "no access (code 200)", ...
    problem: str | None = None


@dataclass
class AdCreativeRecord:
    ad_id: str
    ad_name: str | None
    ad_status: str | None
    ad_effective_status: str | None
    campaign_id: str | None
    campaign_name: str | None
    adset_id: str | None
    creative_id: str | None
    creative_name: str | None
    creative_status: str | None
    #: link, carousel, video, photo, dynamic, post (an existing page post) or unknown.
    creative_kind: str
    primary_texts: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    link_descriptions: list[str] = field(default_factory=list)
    call_to_action_types: list[str] = field(default_factory=list)
    destination_links: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    image_hashes: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None
    video_ids: list[str] = field(default_factory=list)
    videos: list[VideoInfo] = field(default_factory=list)
    #: The cards of a carousel, one object each.
    cards: list[dict[str, Any]] = field(default_factory=list)
    preview_link: str | None = None
    #: The page post behind the ad, `effective_object_story_id`, and a link to it built from that id (UNVERIFIED form).
    post_id: str | None = None
    post_permalink: str | None = None
    instagram_permalink_url: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


#: The CSV columns, in order. A list holds many values in one cell, joined with " | "; the JSON file keeps lists as lists.
COLUMNS = (
    "ad_id", "ad_name", "ad_status", "ad_effective_status", "campaign_id", "campaign_name", "adset_id",
    "creative_id", "creative_kind", "creative_status", "primary_text", "headline", "link_description",
    "call_to_action_type", "destination_links", "image_urls", "image_hashes", "thumbnail_url", "video_ids", "video_source_urls",
    "video_permalinks", "video_lengths", "ad_preview_link", "post_id", "post_permalink", "instagram_permalink_url",
    "notes",
)


def to_row(rec: AdCreativeRecord) -> dict[str, str]:
    """One CSV row: every value a string, a list joined with " | ", a missing value empty."""
    def join(values: Iterable[Any]) -> str:
        return _SEPARATOR.join(str(v) for v in values)

    def one(value: Any) -> str:
        return "" if value is None else str(value)

    return {
        "ad_id": rec.ad_id, "ad_name": one(rec.ad_name), "ad_status": one(rec.ad_status),
        "ad_effective_status": one(rec.ad_effective_status), "campaign_id": one(rec.campaign_id),
        "campaign_name": one(rec.campaign_name), "adset_id": one(rec.adset_id), "creative_id": one(rec.creative_id),
        "creative_kind": rec.creative_kind, "creative_status": one(rec.creative_status),
        "primary_text": join(rec.primary_texts), "headline": join(rec.headlines),
        "link_description": join(rec.link_descriptions), "call_to_action_type": join(rec.call_to_action_types),
        "destination_links": join(rec.destination_links), "image_urls": join(rec.image_urls),
        "image_hashes": join(rec.image_hashes),
        "thumbnail_url": one(rec.thumbnail_url), "video_ids": join(rec.video_ids),
        "video_source_urls": join(v.source or "" for v in rec.videos),
        "video_permalinks": join(v.permalink_url or "" for v in rec.videos),
        "video_lengths": join("" if v.length is None else v.length for v in rec.videos),
        "ad_preview_link": one(rec.preview_link), "post_id": one(rec.post_id), "post_permalink": one(rec.post_permalink),
        "instagram_permalink_url": one(rec.instagram_permalink_url), "notes": join(rec.notes),
    }


@dataclass
class RunStats:
    api_calls: int = 0
    batches: int = 0
    throttle_waits: int = 0
    pages: int = 0
    ads_seen: int = 0
    duplicates: int = 0
    filtered_out: int = 0
    nodes_read_by_id: int = 0
    videos_looked_up: int = 0
    peak_utilisation: float = 0.0


@dataclass
class CreativeRun:
    records: list[AdCreativeRecord]
    stats: RunStats
    complete: bool = True
    stopped_because: str | None = None


@dataclass
class _Looked:
    """What one id of a batch came back as: data, or the words for why not."""
    data: dict | None = None
    problem: str | None = None


# ---------------------------------------------------------------------------------------------- reading a creative

@dataclass
class _Parts:
    kind: str = "unknown"
    primary: list[str | None] = field(default_factory=list)
    headlines: list[str | None] = field(default_factory=list)
    descriptions: list[str | None] = field(default_factory=list)
    ctas: list[str | None] = field(default_factory=list)
    links: list[str | None] = field(default_factory=list)
    images: list[str | None] = field(default_factory=list)
    hashes: list[str | None] = field(default_factory=list)
    videos: list[str | None] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)


def _cta(cta: Any, what: str, parts: _Parts) -> None:
    """`call_to_action` is {"type": ..., "value": {"link": ...}} in link_data, video_data and a child attachment."""
    cta = _object(cta, what)
    parts.ctas.append(_text(cta.get("type"), what + ".type"))
    parts.links.append(_text(_object(cta.get("value"), what + ".value").get("link"), what + ".value.link"))


def _read_link_data(data: dict, parts: _Parts) -> None:
    parts.primary.append(_text(data.get("message"), "link_data.message"))
    parts.headlines.append(_text(data.get("name"), "link_data.name"))
    parts.descriptions.append(_text(data.get("description"), "link_data.description"))
    parts.links.append(_text(data.get("link"), "link_data.link"))
    parts.images.append(_text(data.get("picture"), "link_data.picture"))
    parts.hashes.append(_text(data.get("image_hash"), "link_data.image_hash"))
    _cta(data.get("call_to_action"), "link_data.call_to_action", parts)
    for i, child in enumerate(_list(data.get("child_attachments"), "link_data.child_attachments")):
        what = "child_attachments[%d]" % i
        child = _object(child, what)
        cta = _object(child.get("call_to_action"), what + ".call_to_action")
        card = {
            "name": _text(child.get("name"), what + ".name"),
            "description": _text(child.get("description"), what + ".description"),
            "link": _text(child.get("link"), what + ".link"),
            "picture": _text(child.get("picture"), what + ".picture"),
            "image_hash": _text(child.get("image_hash"), what + ".image_hash"),
            "video_id": _text(child.get("video_id"), what + ".video_id"),
            "call_to_action_type": _text(cta.get("type"), what + ".call_to_action.type"),
        }
        parts.cards.append(card)
        parts.headlines.append(card["name"])
        parts.descriptions.append(card["description"])
        parts.links.append(card["link"])
        parts.images.append(card["picture"])
        parts.hashes.append(card["image_hash"])
        parts.videos.append(card["video_id"])
        _cta(cta, what + ".call_to_action", parts)


def _read_video_data(data: dict, parts: _Parts) -> None:
    parts.primary.append(_text(data.get("message"), "video_data.message"))
    parts.headlines.append(_text(data.get("title"), "video_data.title"))
    parts.descriptions.append(_text(data.get("link_description"), "video_data.link_description"))
    parts.images.append(_text(data.get("image_url"), "video_data.image_url"))
    parts.hashes.append(_text(data.get("image_hash"), "video_data.image_hash"))
    parts.videos.append(_text(data.get("video_id"), "video_data.video_id"))
    _cta(data.get("call_to_action"), "video_data.call_to_action", parts)


def _read_photo_data(data: dict, parts: _Parts) -> None:
    parts.primary.append(_text(data.get("caption"), "photo_data.caption"))
    parts.images.append(_text(data.get("url"), "photo_data.url"))
    parts.hashes.append(_text(data.get("image_hash"), "photo_data.image_hash"))


def _asset_texts(items: Any, key: str, what: str) -> list[str | None]:
    return [_text(_object(item, "%s[%d]" % (what, i)).get(key), "%s[%d].%s" % (what, i, key))
            for i, item in enumerate(_list(items, what))]


def _read_asset_feed(spec: dict, parts: _Parts) -> None:
    parts.primary += _asset_texts(spec.get("bodies"), "text", "asset_feed_spec.bodies")
    parts.headlines += _asset_texts(spec.get("titles"), "text", "asset_feed_spec.titles")
    parts.descriptions += _asset_texts(spec.get("descriptions"), "text", "asset_feed_spec.descriptions")
    parts.links += _asset_texts(spec.get("link_urls"), "website_url", "asset_feed_spec.link_urls")
    parts.videos += _asset_texts(spec.get("videos"), "video_id", "asset_feed_spec.videos")
    parts.images += _asset_texts(spec.get("images"), "url", "asset_feed_spec.images")
    parts.hashes += _asset_texts(spec.get("images"), "hash", "asset_feed_spec.images")
    for t in _list(spec.get("call_to_action_types"), "asset_feed_spec.call_to_action_types"):
        parts.ctas.append(_text(t, "asset_feed_spec.call_to_action_types[]"))
    parts.ctas += _asset_texts(spec.get("call_to_actions"), "type", "asset_feed_spec.call_to_actions")


def read_creative(creative: Mapping[str, Any]) -> _Parts:
    """The copy, links and media of one creative object, whichever of the documented shapes it has."""
    parts = _Parts()
    spec = _object(creative.get("object_story_spec"), "object_story_spec")
    feed = creative.get("asset_feed_spec")
    if isinstance(feed, str):
        # The Ad Creative page says the field is "Formatted as a JSON string"; its own read example shows an object.
        try:
            feed = json.loads(feed)
        except ValueError:
            raise UpstreamBroken("asset_feed_spec is a string that is not JSON") from None
    feed = _object(feed, "asset_feed_spec")
    link_data = _object(spec.get("link_data"), "link_data")
    video_data = _object(spec.get("video_data"), "video_data")
    photo_data = _object(spec.get("photo_data"), "photo_data")

    if feed:
        parts.kind = "dynamic"
    elif link_data and link_data.get("child_attachments"):
        parts.kind = "carousel"
    elif link_data:
        parts.kind = "link"
    elif video_data:
        parts.kind = "video"
    elif photo_data:
        parts.kind = "photo"
    elif creative.get("effective_object_story_id") or creative.get("object_story_id"):
        parts.kind = "post"
    elif creative.get("video_id"):
        parts.kind = "video"
    elif creative.get("image_url") or creative.get("image_hash") or creative.get("body") or creative.get("object_url"):
        parts.kind = "link"

    if feed:
        _read_asset_feed(feed, parts)
    if link_data:
        _read_link_data(link_data, parts)
    if video_data:
        _read_video_data(video_data, parts)
    if photo_data:
        _read_photo_data(photo_data, parts)

    # The creative's own fields: used when the spec gives none of that kind, which is how an ad built without a spec reads.
    if not parts.primary or not any(parts.primary):
        parts.primary.append(_text(creative.get("body"), "body"))
    if not parts.headlines or not any(parts.headlines):
        parts.headlines.append(_text(creative.get("title"), "title"))
    if not any(parts.ctas):
        parts.ctas.append(_text(creative.get("call_to_action_type"), "call_to_action_type"))
    parts.links.append(_text(creative.get("object_url"), "object_url"))
    parts.links.append(_text(creative.get("link_url"), "link_url"))
    parts.images.append(_text(creative.get("image_url"), "image_url"))
    parts.hashes.append(_text(creative.get("image_hash"), "image_hash"))
    parts.videos.append(_text(creative.get("video_id"), "video_id"))
    return parts


def read_usage_any(response: httpx.Response) -> tuple[float | None, float | None]:
    """`meta.read_usage`, and also the business use case header under the name the Marketing API rate limit page gives it.

    The Graph rate limit page calls the header `X-Business-Use-Case-Usage` and the Marketing API page calls it
    `X-Business-Use-Case`, with the same contents (UNVERIFIED which one the live API sends), so both are read.
    """
    level, regain = read_usage(response)
    raw = response.headers.get("x-business-use-case")
    if raw is not None:
        other, other_regain = read_usage(httpx.Response(200, headers={"x-business-use-case-usage": raw}))
        if other is not None:
            level = other if level is None else max(level, other)
        if other_regain is not None:
            regain = other_regain if regain is None else max(regain, other_regain)
    return level, regain


def _post_permalink(story_id: str | None) -> str | None:
    """A link to a page post from its `{page id}_{post id}` id. UNVERIFIED: the pages give the id's structure only as
    "Post ID", not the address form, so a link is built only from the common two part id and is labelled as built."""
    match = _STORY_RE.match(story_id or "")
    return "%s/%s/posts/%s" % (FACEBOOK, match.group(1), match.group(2)) if match else None


def _video_info(video_id: str, looked: _Looked | None) -> VideoInfo:
    if looked is None:
        return VideoInfo(video_id, problem="not looked up")
    if looked.data is None:
        return VideoInfo(video_id, problem=looked.problem or "not readable")
    data = looked.data
    source = data.get("source")
    permalink = data.get("permalink_url")
    length = data.get("length")
    problems = []
    if not isinstance(source, str) or not source:
        source = None
        problems.append("no source returned")
    if isinstance(permalink, str) and permalink:
        # A relative path ("/page/videos/1/") is not a link anyone can open on its own.
        permalink = FACEBOOK + permalink if permalink.startswith("/") else permalink
    else:
        permalink = None
    if isinstance(length, bool) or not isinstance(length, (int, float)) or length < 0:
        length = None
    return VideoInfo(video_id, source, permalink, length, "; ".join(problems) or None)


# ------------------------------------------------------------------------------------------------------- the client

class MetaCreativesClient:
    def __init__(
        self,
        *,
        ad_account_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        utilisation_ceiling: float = UTILISATION_CEILING,
        max_pages: int = MAX_PAGES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        match = _ACCOUNT_RE.match(str(ad_account_id).strip())
        if not match:
            # Before any network call: a wrong id would come back as a permissions error that reads like a bad token.
            raise ValueError("ad_account_id must be the numeric ad account id, with or without act_, got %r" % (ad_account_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.account_id = match.group(1)
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._ceiling = utilisation_ceiling
        self._max_pages = max_pages
        self._sleep = sleep
        self._rng = rng
        self.utilisation: float | None = None
        self.stats = RunStats()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "MetaCreativesClient":
        """Build from META_AD_ACCOUNT_ID and META_ACCESS_TOKEN (and META_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [name for name in ("META_AD_ACCOUNT_ID", "META_ACCESS_TOKEN") if not env.get(name)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        base = env.get("META_API_BASE_URL")
        if base:
            kwargs.setdefault("base_url", base)
        return cls(ad_account_id=env["META_AD_ACCOUNT_ID"], access_token=env["META_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "MetaCreativesClient(account_id=%r, base_url=%r)" % (self.account_id, self._base)

    @property
    def external_account(self) -> str:
        return "act_" + self.account_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------------------------------------ transport

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _delay(self, attempt: int, wait: float | None) -> float:
        if wait is not None:
            return wait
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)

    @staticmethod
    def _wait_from(response: httpx.Response, regain_minutes: float | None) -> float | None:
        """Seconds to wait: the business usage header's minutes, else a numeric Retry-After (UNVERIFIED), else none."""
        if regain_minutes:
            return regain_minutes * 60
        raw = response.headers.get("Retry-After")
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            return None
        return value if value is not None and value >= 0 else None

    def _raise_if_final(self, err: _MetaError) -> None:
        """Raise the typed error for a failure retrying cannot fix. Returns when the failure may pass."""
        code = err.code
        if code in _EXPIRED_CODES or (code is None and err.http == 401):
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if code in _PERMISSION_CODES or (code is None and err.http == 403):
            raise AuthRevoked("no access to ad account %s (%s)" % (self.account_id, err.text()))
        if code in _THROTTLE or code in _TEMPORARY_CODES:
            return
        if code is None and (err.http == 429 or err.http >= 500):
            return
        raise UpstreamBroken("Meta refused the request (%s)" % err.text())

    async def _send(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                    form: Mapping[str, str] | None = None) -> dict:
        """One request with bounded retries. An error is read from the body first, whatever the HTTP status."""
        url = "%s/%s/%s" % (self._base, API_VERSION, path) if path else "%s/%s" % (self._base, API_VERSION)
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            wait: float | None = None
            try:
                token = self._bearer()
                self.stats.api_calls += 1
                if method == "GET":
                    response = await self._client.get(url, params={**(params or {}), "access_token": token})
                else:
                    response = await self._client.post(url, data={**(form or {}), "access_token": token})
            except httpx.HTTPError as exc:
                problem = "transport failure (%s)" % type(exc).__name__
            else:
                try:
                    body: Any = response.json()
                except ValueError:
                    body = None
                self.utilisation, regain = read_usage_any(response)
                if self.utilisation is not None:
                    self.stats.peak_utilisation = max(self.stats.peak_utilisation, self.utilisation)
                err = _read_error(body, response.status_code)
                if err is None and response.status_code == 200:
                    if not isinstance(body, (dict, list)):
                        raise UpstreamBroken("Meta answered 200 with %s, not an object" % (
                            "a body that is not JSON" if body is None else type(body).__name__))
                    return {"body": body, "regain": regain, "response": response}
                if err is None:
                    err = _MetaError(None, None, "", response.status_code)
                self._raise_if_final(err)
                wait = self._wait_from(response, regain)
                problem = "%s (%s)" % ("rate limit" if err.code in _THROTTLE or err.http == 429 else "temporary error",
                                       err.text())
            if last:
                raise RateLimited(wait, "gave up after %d attempts: %s" % (self._max_attempts, problem))
            delay = self._delay(attempt, wait)
            if delay > self._backoff_cap:
                raise RateLimited(wait, "asked to wait %.0f seconds: %s" % (delay, problem))
            self.stats.throttle_waits += 1
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------------------------------------ batches

    def _read_item(self, item: Any) -> tuple[str, _Looked | _MetaError | None]:
        """("ok"|"retry"|"done", ...) for one answer of a batch. A null answer is a request that timed out."""
        if item is None:
            return "retry", None
        if not isinstance(item, dict) or isinstance(item.get("code"), bool) or not isinstance(item.get("code"), int):
            return "done", _Looked(problem="the batch answered with something that is not a result")
        raw = item.get("body")
        try:
            body = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            body = None
        err = _read_error(body, item["code"])
        if err is None and item["code"] == 200:
            if not isinstance(body, dict):
                return "done", _Looked(problem="the answer is not an object")
            return "ok", _Looked(data=body)
        if err is None:
            err = _MetaError(None, None, "", item["code"])
        code = err.code
        if code in _EXPIRED_CODES or (code is None and err.http == 401):
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if code in _THROTTLE or code in _TEMPORARY_CODES or (code is None and (err.http == 429 or err.http >= 500)):
            return "retry", err
        if code in _PERMISSION_CODES or (code is None and err.http == 403):
            return "done", _Looked(problem="no access (%s)" % err.text())
        return "done", _Looked(problem="not readable (%s)" % err.text())

    async def _lookup(self, ids: Sequence[str], fields: str) -> dict[str, _Looked]:
        """Read nodes by id, 50 to a request, `GET {id}?fields=...` each. A timed out or throttled id is tried again.

        Returns one entry per id: its data, or the words for why there is none. A permission problem on one id does
        not stop the others; an expired token stops the run.
        """
        out: dict[str, _Looked] = {}
        pending = list(dict.fromkeys(i for i in ids if i))
        for attempt in range(self._max_attempts):
            retry: list[str] = []
            wait: float | None = None
            for start in range(0, len(pending), BATCH_LIMIT):
                chunk = pending[start:start + BATCH_LIMIT]
                batch = json.dumps([{"method": "GET", "relative_url": "%s?fields=%s" % (i, fields)} for i in chunk],
                                   separators=(",", ":"))
                self.stats.batches += 1
                sent = await self._send("POST", "", form={"batch": batch, "include_headers": "false"})
                body = sent["body"]
                if not isinstance(body, list) or len(body) != len(chunk):
                    raise UpstreamBroken("a batch of %d requests came back with %s" % (
                        len(chunk), "%d answers" % len(body) if isinstance(body, list) else "something that is not a list"))
                for node_id, item in zip(chunk, body):
                    state, value = self._read_item(item)
                    if state == "retry":
                        retry.append(node_id)
                        if isinstance(value, _MetaError):
                            wait = self._wait_from(sent["response"], sent["regain"])
                    else:
                        out[node_id] = value  # type: ignore[assignment]
            if not retry:
                return out
            pending = retry
            if attempt + 1 < self._max_attempts:
                delay = self._delay(attempt, wait)
                if delay > self._backoff_cap:
                    raise RateLimited(wait, "asked to wait %.0f seconds" % delay)
                self.stats.throttle_waits += 1
                await self._sleep(delay)
        for node_id in pending:
            out[node_id] = _Looked(problem="no answer after %d attempts" % self._max_attempts)
        return out

    # ------------------------------------------------------------------------------------------------ the ads edge

    def _check_statuses(self, statuses: Sequence[str] | None, include_deleted: bool) -> tuple[str, ...]:
        chosen = tuple(DEFAULT_STATUSES if statuses is None else statuses)
        bad = [s for s in chosen if s not in AD_STATUSES]
        if bad or not chosen:
            raise ValueError("statuses must be some of %s, got %r" % (", ".join(AD_STATUSES), list(chosen)))
        if include_deleted and "DELETED" not in chosen:
            chosen += ("DELETED",)
        return chosen

    async def _ad_pages(self, path: str, statuses: tuple[str, ...]) -> AsyncIterator[list[dict]]:
        """The raw ad objects of one `/ads` edge, page by page, with the paging rules of the Paginated Results page."""
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self._max_pages):
            if after and self.utilisation is not None and self.utilisation >= self._ceiling:
                raise RateLimited(None, "usage is at %.0f percent, stopping before the page after %s"
                                  % (self.utilisation, after[:12]))
            params = {"fields": AD_FIELDS, "limit": str(self._page_size),
                      "effective_status": json.dumps(list(statuses), separators=(",", ":"))}
            if after:
                params["after"] = after
            body = (await self._send("GET", path, params=params))["body"]
            if not isinstance(body, dict):
                raise UpstreamBroken("the ads edge answered with %s, not an object" % type(body).__name__)
            rows = body.get("data")
            if not isinstance(rows, list):
                raise UpstreamBroken("data is not a list")
            self.stats.pages += 1
            paging = body.get("paging")
            has_next = isinstance(paging, dict) and bool(paging.get("next"))
            following: str | None = None
            if has_next:
                cursors = paging.get("cursors")
                following = cursors.get("after") if isinstance(cursors, dict) else None
                if not isinstance(following, str) or not following:
                    raise UpstreamBroken("paging has a next page and no after cursor")
                if following == after or following in seen_cursors:
                    # A page that points back at one already read would loop for ever, spending the rate limit.
                    raise UpstreamBroken("paging does not advance: the cursor %s came back" % following[:20])
                seen_cursors.add(following)
            yield rows
            if not has_next:
                return
            after = following
        raise UpstreamBroken("more than %d pages: the cursors are not ending" % self._max_pages)

    # ------------------------------------------------------------------------------------------------ assembling

    async def _records(self, rows: list[dict], names: dict[str, str | None], videos: dict[str, VideoInfo],
                       statuses: tuple[str, ...], seen: set[str]) -> list[AdCreativeRecord]:
        ads: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                raise UpstreamBroken("an ad is %s, not an object" % type(row).__name__)
            ad_id = row.get("id")
            if not isinstance(ad_id, str) or not _ID_RE.match(ad_id):
                raise UpstreamBroken("an ad has the id %r, not a numeric string" % (ad_id,))
            self.stats.ads_seen += 1
            if ad_id in seen:
                # A cursor can slide when ads are added or removed between pages, so the same ad can come twice.
                self.stats.duplicates += 1
                continue
            effective = _text(row.get("effective_status"), "effective_status")
            if effective is not None and effective not in statuses:
                self.stats.filtered_out += 1
                continue
            seen.add(ad_id)
            ads.append(row)

        # Reads by id, only for what the response did not expand: a creative with nothing but an id, a campaign with no name.
        need_creative = [str(_object(a.get("creative"), "creative").get("id")) for a in ads
                         if isinstance(a.get("creative"), dict) and set(a["creative"]) <= {"id"}
                         and isinstance(a["creative"].get("id"), str) and _ID_RE.match(a["creative"]["id"])]
        creatives: dict[str, _Looked] = {}
        if need_creative:
            self.stats.nodes_read_by_id += len(need_creative)
            creatives = await self._lookup(need_creative, ",".join(CREATIVE_FIELDS))
        for a in ads:
            cid, cname = self._campaign_of(a)
            if cid and cname is not None:
                names[cid] = cname
        need_campaign = [c for c in _unique(self._campaign_of(a)[0] for a in ads) if c not in names and _ID_RE.match(c)]
        if need_campaign:
            self.stats.nodes_read_by_id += len(need_campaign)
            for cid, looked in (await self._lookup(need_campaign, "id,name")).items():
                names[cid] = _text(looked.data.get("name"), "campaign name") if looked.data else None

        parsed: list[tuple[dict, dict, _Parts, list[str]]] = []
        wanted: list[str] = []
        for a in ads:
            notes: list[str] = []
            creative = _object(a.get("creative"), "creative")
            cid = creative.get("id")
            if set(creative) <= {"id"} and isinstance(cid, str) and cid in creatives:
                looked = creatives[cid]
                if looked.data is not None:
                    creative = {**looked.data}
                else:
                    notes.append("creative %s not read: %s" % (cid, looked.problem))
            if not creative:
                notes.append("the ad has no creative object")
            if creative.get("status") == "DELETED":
                notes.append("creative is DELETED")
            if creative and set(creative) <= {"id"} and not any(n.startswith("creative %s not read" % cid) for n in notes):
                notes.append("the creative came back with no readable fields")
            parts = read_creative(creative)
            if parts.kind == "post" and not any(parts.primary):
                notes.append("the ad runs an existing page post; its copy is not on the creative (see post_id)")
            parsed.append((a, creative, parts, notes))
            wanted += _ids_in(parts.videos)
        todo = [v for v in dict.fromkeys(wanted) if v not in videos]
        if todo:
            self.stats.videos_looked_up += len(todo)
            for vid, looked in (await self._lookup(todo, VIDEO_FIELDS)).items():
                videos[vid] = _video_info(vid, looked)

        records: list[AdCreativeRecord] = []
        for a, creative, parts, notes in parsed:
            vids = _unique(parts.videos)
            infos = []
            for v in vids:
                if not _ID_RE.match(v):
                    infos.append(VideoInfo(v, problem="the video id is not numeric, so it was not looked up"))
                else:
                    infos.append(videos[v])
            for info in infos:
                if info.problem:
                    notes.append("video %s: %s" % (info.video_id, info.problem))
            story = _text(creative.get("effective_object_story_id"), "effective_object_story_id")
            campaign_id, _ = self._campaign_of(a)
            if campaign_id is None and isinstance(a.get("campaign_id"), str):
                campaign_id = a["campaign_id"]
            kind = parts.kind
            records.append(AdCreativeRecord(
                ad_id=a["id"], ad_name=_text(a.get("name"), "ad name"), ad_status=_text(a.get("status"), "status"),
                ad_effective_status=_text(a.get("effective_status"), "effective_status"),
                campaign_id=campaign_id, campaign_name=names.get(campaign_id or ""),
                adset_id=_text(a.get("adset_id"), "adset_id"),
                creative_id=_text(creative.get("id"), "creative id"), creative_name=_text(creative.get("name"), "creative name"),
                creative_status=_text(creative.get("status"), "creative status"), creative_kind=kind,
                primary_texts=_unique(parts.primary), headlines=_unique(parts.headlines),
                link_descriptions=_unique(parts.descriptions), call_to_action_types=_unique(parts.ctas),
                destination_links=_unique(parts.links), image_urls=_unique(parts.images),
                image_hashes=_unique(parts.hashes), thumbnail_url=_text(creative.get("thumbnail_url"), "thumbnail_url"),
                video_ids=vids, videos=infos, cards=parts.cards,
                preview_link=_text(a.get("preview_shareable_link"), "preview_shareable_link"),
                post_id=story, post_permalink=_post_permalink(story),
                instagram_permalink_url=_text(creative.get("instagram_permalink_url"), "instagram_permalink_url"),
                notes=notes,
            ))
        return records

    @staticmethod
    def _campaign_of(ad: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """(campaign id, campaign name) of an ad row: the expanded `campaign` object first, then `campaign_id`."""
        campaign = ad.get("campaign")
        cid = campaign.get("id") if isinstance(campaign, dict) else None
        name = campaign.get("name") if isinstance(campaign, dict) else None
        if not isinstance(cid, str):
            cid = ad.get("campaign_id") if isinstance(ad.get("campaign_id"), str) else None
        if name is not None and not isinstance(name, str):
            raise UpstreamBroken("campaign name is %s, not a string" % type(name).__name__)
        return cid, name

    # ------------------------------------------------------------------------------------------------ public

    async def fetch(
        self,
        *,
        campaign_ids: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        include_deleted: bool = False,
    ) -> AsyncIterator[list[AdCreativeRecord]]:
        """The creatives of the account's ads, one list per page, in the order Meta returns them.

        `campaign_ids` restricts it to those campaigns, read from each campaign's own `/ads` edge. `statuses` is the
        `effective_status` filter (default every status but DELETED); `include_deleted` adds DELETED.
        """
        chosen = self._check_statuses(statuses, include_deleted)
        edges: list[str]
        if campaign_ids is None:
            edges = ["act_%s/ads" % self.account_id]
        else:
            ids = [str(c).strip() for c in campaign_ids]
            bad = [c for c in ids if not _ID_RE.match(c)]
            if bad or not ids:
                raise ValueError("campaign_ids must be numeric campaign ids, got %r" % (list(campaign_ids),))
            edges = ["%s/ads" % c for c in dict.fromkeys(ids)]
        self.stats = RunStats()
        names: dict[str, str | None] = {}
        videos: dict[str, VideoInfo] = {}
        seen: set[str] = set()
        for n, edge in enumerate(edges):
            if n and self.utilisation is not None and self.utilisation >= self._ceiling:
                raise RateLimited(None, "usage is at %.0f percent, stopping before campaign %s" % (self.utilisation, edge.split("/")[0]))
            async for rows in self._ad_pages(edge, chosen):
                yield await self._records(rows, names, videos, chosen, seen)

    async def fetch_all(self, **kwargs: Any) -> CreativeRun:
        """Every record, with the run's counts. A rate limit stops the run and keeps what was read (`complete` False)."""
        records: list[AdCreativeRecord] = []
        try:
            async for page in self.fetch(**kwargs):
                records += page
        except RateLimited as exc:
            return CreativeRun(records, self.stats, False, str(exc))
        return CreativeRun(records, self.stats)
