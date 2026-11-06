# LIFESPAN: permanent
"""Helpers the Google Ads, TikTok, Square, QuickBooks and Shopmonkey connector tests share."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.connectors.common import Retrier


class Sleeps:
    """Stands in for asyncio.sleep: records the waits and returns at once."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, verify=False, timeout=10)


def retrier(sleeps: Sleeps | None = None, **kw: Any) -> Retrier:
    kw.setdefault("rng", lambda: 1.0)                          # full delay, no jitter, so waits are exact
    return Retrier(sleep=sleeps or Sleeps(), **kw)


def run(coro):
    return asyncio.run(coro)


async def collect(conn, start, end, cursor=None):
    pages = []
    try:
        async for page in conn.fetch(window_start=start, window_end=end, cursor=cursor):
            pages.append(page)
    finally:
        await conn.aclose()
    return pages


def points_of(pages):
    return [p for page in pages for p in page.points]


def as_set(points):
    return {(p.metric_key, p.observed_on, p.value_numeric, p.currency) for p in points}
