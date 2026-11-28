#!/usr/bin/env python
"""Write the invented order, refund, customer, product and campaign rows to data/raw/*.csv.

The hub stores one figure per source, metric and day (orders, gross sales, refunds, customers, ad spend ...). It holds no order lines and no campaigns.
An analytics-engineering model needs those grains, so this script INVENTS them: for every day it splits the hub's own daily figures into rows that add back
to them exactly (orders, gross sales in cents, refunds, distinct customers, ad spend, impressions, clicks, purchases, purchase value). Nothing here is
a real store or a real ad account. No randomness and no clock, so two runs write the same bytes; `--check` compares the files on disk with a fresh build.

    python analytics-engineering/data/generate_raw.py           # writes analytics-engineering/data/raw/*.csv
    python analytics-engineering/data/generate_raw.py --check   # exit 1 if the files differ from the formulas
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RAW = HERE / "raw"
sys.path.insert(0, str(REPO / "powerbi"))
sys.path.insert(0, str(HERE))
import export_data as hub  # noqa: E402  (only its pure functions are used: sample_points)
from schema import TABLES  # noqa: E402

PRODUCTS = [
    ("P001", "Everyday tee", "apparel", 2400), ("P002", "Heavy hoodie", "apparel", 6800), ("P003", "Canvas cap", "apparel", 2200),
    ("P004", "Insulated bottle", "home", 3200), ("P005", "Ceramic mug", "home", 1800), ("P006", "Desk lamp", "home", 5400),
    ("P007", "Trail pack", "outdoor", 7900), ("P008", "Camp stool", "outdoor", 4600), ("P009", "Rain shell", "outdoor", 9200),
    ("P010", "Notebook set", "stationery", 1500), ("P011", "Pen roll", "stationery", 2600), ("P012", "Wall planner", "stationery", 3100),
]
COUNTRIES = ["US", "CA", "GB", "AU", "DE"]
CHANNELS = ["paid_social", "organic", "email", "direct"]
REASONS = ["damaged", "not_as_described", "changed_mind", "late_delivery"]
CAMPAIGNS = [("cmp_prospecting", "Prospecting", 50), ("cmp_retargeting", "Retargeting", 30), ("cmp_brand", "Brand search", 20)]


def split(total: int, weights: list[int]) -> list[int]:
    """Integer parts in the ratio of the weights that add back to `total` exactly (the remainder cents go one each to the first parts)."""
    s = sum(weights)
    parts = [total * w // s for w in weights]
    for k in range(total - sum(parts)):
        parts[k % len(parts)] += 1
    return parts


def hash_key(o: str, d: str, c: int) -> int:
    """A fixed pseudo random order for choosing which order a refund lands on (not Python's hash(), which changes per process)."""
    h = 2166136261
    for ch in f"{o}|{d}|{c}":
        h = ((h ^ ord(ch)) * 16777619) % (1 << 32)
    return h


def build() -> dict[str, list[list]]:
    points = hub.sample_points()
    by_day: dict[str, dict[str, int]] = {}
    for p in points:
        by_day.setdefault(p["observed_on"], {})[p["metric_key"]] = p["value_numeric"]
    days = sorted(by_day)
    loaded = datetime(2026, 11, 1, 2, 0, 0).isoformat(sep="T")  # the invented rows count as loaded the morning after the last day

    orders, lines, refunds, customers, ads = [], [], [], [], []
    order_total: dict[str, int] = {}
    refunded: dict[str, int] = {}
    orders_by_day: dict[str, list[str]] = {}
    pool: list[str] = []  # customer ids in creation order
    order_seq = line_seq = refund_seq = 0

    for i, d in enumerate(days):
        f = by_day[d]
        n, gross, refund_total, k = f["orders"], f["gross_sales"], f["refunds"], f["customers"]
        # who orders today: about 55% of the day's distinct customers already exist, the rest are new today
        returning = min(len(pool), k * 55 // 100)
        todays: list[str] = []
        stride = 7 + (i % 5)
        pos = (i * 17) % max(len(pool), 1)
        taken: set[str] = set()
        while len(todays) < returning:
            cid = pool[pos % len(pool)]
            pos += stride
            if cid in taken:
                pos += 1
                continue
            taken.add(cid)
            todays.append(cid)
        for _ in range(k - returning):
            cid = f"C{len(pool) + 1:06d}"
            pool.append(cid)
            todays.append(cid)
            customers.append([cid, f"{d}T09:00:00", COUNTRIES[len(pool) % 5], CHANNELS[(len(pool) * 3 + i) % 4]])

        # split the day's gross sales across its orders, then each order across its lines
        totals = split(gross, [60 + ((i * 7 + j * 13) % 80) for j in range(n)])
        ids = []
        for j in range(n):
            order_seq += 1
            oid = f"O{order_seq:06d}"
            ids.append(oid)
            cust = todays[j] if j < k else todays[(j - k) % k]
            hh = 8 + (j * 7 + i) % 14
            orders.append([oid, cust, f"{d}T{hh:02d}:{(j * 13) % 60:02d}:00", "USD", "paid", CHANNELS[(i + j) % 4], loaded])
            order_total[oid] = totals[j]
            nl = 1 + (j % 3 == 0) + (j % 7 == 0)
            for l, amount in enumerate(split(totals[j], [3, 2, 1][:nl])):
                line_seq += 1
                qty = 2 if (amount % 2 == 0 and (i + j + l) % 5 == 0) else 1
                lines.append([f"L{line_seq:06d}", oid, PRODUCTS[(i * 3 + j * 5 + l * 7) % len(PRODUCTS)][0], qty, amount // qty])
        orders_by_day[d] = ids

        # the day's refunds (booked today) go against orders from the last two weeks that still have something left to refund
        components = [c for c, hit in ((3500, i % 9 == 0), (6000, i % 17 == 0), (9000, i % 23 == 0)) if hit]
        assert sum(components) == refund_total, (d, components, refund_total)
        window = [o for back in range(0, 15) if i - back >= 0 for o in orders_by_day[days[i - back]]]
        for c, comp in enumerate(components):
            remaining = comp
            for o in sorted(window, key=lambda o: hash_key(o, d, c)):
                room = order_total[o] - refunded.get(o, 0)
                if room <= 0:
                    continue
                amount = min(room, remaining)
                refund_seq += 1
                refunded[o] = refunded.get(o, 0) + amount
                refunds.append([f"R{refund_seq:05d}", o, f"{d}T{15 + c:02d}:{(refund_seq * 7) % 60:02d}:00", amount, REASONS[(refund_seq + i) % 4]])
                remaining -= amount
                if remaining == 0:
                    break
            assert remaining == 0

        # the ad account's day, split over three campaigns
        shares = [w for _, _, w in CAMPAIGNS]
        parts = {m: split(f[m], shares) for m in ("ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value")}
        for c, (cid, name, _) in enumerate(CAMPAIGNS):
            ads.append(["act_0000000000", cid, name, d, parts["ad_spend"][c], parts["ad_impressions"][c], parts["ad_clicks"][c], parts["ad_purchases"][c],
                        parts["ad_purchase_value"][c], "USD", loaded])

    for o in orders:
        got, tot = refunded.get(o[0], 0), order_total[o[0]]
        o[4] = "refunded" if got == tot else ("partially_refunded" if got else "paid")

    products = [[pid, name, cat, price, "true"] for pid, name, cat, price in PRODUCTS]
    return {"orders": orders, "order_lines": lines, "refunds": refunds, "products": products, "customers": customers, "ads_campaign_daily": ads}


def to_csv(table: str, rows: list[list]) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c for c, _ in TABLES[table]])
    w.writerows(rows)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = {t: to_csv(t, rows) for t, rows in build().items()}
    if args.check:
        bad = [t for t, text in files.items() if not (RAW / f"{t}.csv").exists() or (RAW / f"{t}.csv").read_bytes() != text.encode()]
        print("raw files match the formulas" if not bad else "differs: " + ", ".join(bad))
        return 1 if bad else 0
    RAW.mkdir(exist_ok=True)
    for t, text in files.items():
        (RAW / f"{t}.csv").write_bytes(text.encode())
        print(f"wrote {t}.csv: {text.count(chr(10)) - 1} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
