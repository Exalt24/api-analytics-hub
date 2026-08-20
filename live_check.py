#!/usr/bin/env python
"""Run the connector against the LIVE dev store and print what it returns.

This is the only artifact in the project that proves the connector works on real
responses rather than on a transport we wrote ourselves. A mocked test can only
ever confirm that the code agrees with our own idea of the platform, which is the
exact assumption that was wrong four separate times on this build (the bucket
size, the token kind, ten scope names, and the shape of ProductInput).

It writes nothing to the store and nothing to the database. Read-only on purpose,
so it is safe to re-run while iterating.
"""
from __future__ import annotations

import asyncio
import io
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.shopify import ShopifyConnector

SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")


def creds() -> tuple[str, str]:
    text = io.open(SECRETS, encoding="utf-8").read()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    return shop, token


async def main() -> int:
    shop, token = creds()
    conn = ShopifyConnector(shop_domain=shop, access_token=token)

    print("verify:", await conn.verify())

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=14)
    print(f"window: {start} to {end} (half open)")
    print()

    pages = 0
    calls = 0
    waits = 0
    points = []
    async for page in conn.fetch(window_start=start, window_end=end):
        pages += 1
        calls += page.api_calls
        waits += page.throttle_waits
        points.extend(page.points)

    by_day: dict[date, dict[str, object]] = {}
    for p in points:
        by_day.setdefault(p.observed_on, {})[p.metric_key] = (p.value_numeric, p.currency)

    print(f"{'day':12} {'orders':>7} {'gross':>10} {'refunds':>9} {'net':>10} {'aov':>9} {'buyers':>7}")
    for day in sorted(by_day):
        row = by_day[day]

        def cell(key: str) -> str:
            v = row.get(key)
            if v is None:
                return "-"
            value, currency = v
            if currency:
                # Minor units back to a human figure only for display. The stored
                # value stays an integer.
                return f"{value / 100:.2f}"
            return str(value)

        print(f"{day!s:12} {cell('orders'):>7} {cell('gross_sales'):>10} "
              f"{cell('refunds'):>9} {cell('net_sales'):>10} "
              f"{cell('avg_order_value'):>9} {cell('customers'):>7}")

    print()
    print(f"pages: {pages}  api_calls: {calls}  throttle_waits: {waits}")
    print(f"points: {len(points)}  days: {len(by_day)}")
    print(f"degraded metrics: {sorted(conn.degraded_metrics) or 'none'}")
    print(f"available metrics: {len(conn.available_metrics)} of {len(conn.supported_metrics)}")

    if not points:
        print()
        print("NO POINTS. Either the window is wrong or the store holds no orders,")
        print("and both look identical from a green exit code, so this fails loudly.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
