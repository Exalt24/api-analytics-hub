#!/usr/bin/env python
# LIFESPAN: permanent (writes the four CSV files the Power BI project in this folder reads; run again whenever the invented figures or the schema change)
"""Write the Power BI project's data: invented store, ad and website figures, loaded through the hub's own Postgres tables and read back out as CSV.

The figures come from fixed formulas (no randomness, no clock), so two runs write byte for byte the same files. They go through the real migrations, the real
`upsert_snapshots` function and the same constraints as any connector's output (the currency check, one row per connection, metric and day), then the files are read
back from the tables under the tenant's row level security setting. That way the model in Power BI sits on what the hub stores, not on a hand written spreadsheet.

    docker compose up -d --wait db
    python powerbi/export_data.py            # writes powerbi/data/*.csv
    python powerbi/export_data.py --check    # rebuilds in memory and compares with the files on disk, no database needed

Three invented sources for one invented tenant: a store (orders, gross sales, refunds, net sales, customers), an ad account (spend, impressions, clicks, purchases,
purchase value) and a website (sessions, users, page views, key events). Four things are planted on purpose so the model's checks have something to find:
the website sync missed two days (and has a failed sync run saying so), the ad account double counted purchases on three days (purchase value above the store's
net sales that day), and a few days carry a refund spike.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

END = date(2026, 10, 31)
DAYS = 184  # 1 May to 31 October 2026
NS = uuid.UUID("5a1f0e0e-7b0c-4d19-9d62-0f3c8f3a9a11")
TENANT_ID = uuid.uuid5(NS, "tenant:powerbi-demo")
CONNECTIONS = {
    "shopify": (uuid.uuid5(NS, "conn:shopify"), "demo-store.invalid", "Invented store"),
    "meta": (uuid.uuid5(NS, "conn:meta"), "act_0000000000", "Invented ad account"),
    "ga4": (uuid.uuid5(NS, "conn:ga4"), "properties/000000000", "Invented website"),
}
#: the website sync missed these two day indexes; the ad account double counted on these three
GA4_MISSING = (120, 121)
OVERCLAIM_DAYS = (40, 95, 150)

FILES = {
    "fact_snapshot.csv": ["observed_on", "connection_id", "metric_key", "value", "currency"],
    "dim_metric.csv": ["metric_key", "label", "unit", "kind"],
    "dim_connection.csv": ["connection_id", "platform", "display_name", "external_account"],
    "sync_run.csv": ["connection_id", "trigger", "window_start", "window_end", "status", "rows_written", "error_code", "started_at", "finished_at"],
}


def invented_points() -> list[dict]:
    """Every (platform, metric, day) value, money in minor units (cents). A pure function of the constants above."""
    out: list[dict] = []
    for i in range(DAYS):
        day = END - timedelta(days=DAYS - 1 - i)
        orders = 20 + (i % 7) * 3 + i // 12 + (15 if i % 30 in (26, 27, 28) else 0)
        aov = 4200 + (i % 5) * 150 + (i // 30) * 100
        gross = orders * aov
        refunds = (3500 if i % 9 == 0 else 0) + (6000 if i % 17 == 0 else 0) + (9000 if i % 23 == 0 else 0)
        net = gross - refunds
        customers = orders - orders // 5
        shop = [("orders", orders, None), ("gross_sales", gross, "USD"), ("refunds", refunds, "USD"), ("net_sales", net, "USD"), ("customers", customers, None)]

        spend = 6000 + (i % 7) * 400 + i * 10
        impressions = spend // 3
        clicks = impressions // 45
        purchases = max(1, clicks // 30)
        purchase_value = purchases * (5100 + (i % 4) * 200)
        if i in OVERCLAIM_DAYS:
            purchases += 3
            purchase_value = net + 91234
        meta = [("ad_spend", spend, "USD"), ("ad_impressions", impressions, None), ("ad_clicks", clicks, None), ("ad_purchases", purchases, None), ("ad_purchase_value", purchase_value, "USD")]

        sessions = 900 + (i % 7) * 120 + i * 3
        ga4 = [("sessions", sessions, None), ("users", sessions * 72 // 100, None), ("page_views", sessions * 3 + (i % 5) * 40, None), ("key_events", orders + orders // 4, None)]

        for platform, rows in (("shopify", shop), ("meta", meta), ("ga4", ga4)):
            if platform == "ga4" and i in GA4_MISSING:
                continue
            for key, value, cur in rows:
                out.append({"platform": platform, "metric_key": key, "observed_on": day.isoformat(), "value_numeric": value, "currency": cur})
    return out


def invented_sync_runs() -> list[dict]:
    """One run per connection per 28 day window, plus the failed website run that explains the two missing days."""
    runs = []
    start = END - timedelta(days=DAYS - 1)
    for platform, (cid, _, _) in CONNECTIONS.items():
        w = start
        while w <= END:
            we = min(w + timedelta(days=28), END + timedelta(days=1))
            failed = platform == "ga4" and w <= END - timedelta(days=DAYS - 1 - GA4_MISSING[0]) < we
            runs.append({
                "platform": platform, "trigger": "schedule", "window_start": w, "window_end": we,
                "status": "partial" if failed else "succeeded",
                "error_code": "upstream_503" if failed else None,
                "started_at": datetime(we.year, we.month, we.day, 1, 0, tzinfo=timezone.utc) - timedelta(days=1),
            })
            w = we
    return runs


def _csv_text(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def expected_files() -> dict[str, str]:
    """The four CSVs built straight from the formulas, with no database. `--check` and the tests compare the database's output to this."""
    from_metrics = {}
    points = invented_points()
    conn_id = {p: str(v[0]) for p, v in CONNECTIONS.items()}
    fact = sorted(([p["observed_on"], conn_id[p["platform"]], p["metric_key"], p["value_numeric"], p["currency"] or ""] for p in points), key=lambda r: (r[0], r[1], r[2]))
    for p in points:
        from_metrics[p["metric_key"]] = True
    metrics = [[k, *METRIC_META[k]] for k in sorted(from_metrics)]
    conns = sorted([[str(cid), plat, name, ext] for plat, (cid, ext, name) in CONNECTIONS.items()], key=lambda r: r[1])
    runs = []
    for r in invented_sync_runs():
        rows_written = sum(1 for p in points if p["platform"] == r["platform"] and r["window_start"].isoformat() <= p["observed_on"] < r["window_end"].isoformat())
        finished = r["started_at"] + timedelta(minutes=3)
        runs.append([conn_id[r["platform"]], r["trigger"], r["window_start"].isoformat(), r["window_end"].isoformat(), r["status"], rows_written, r["error_code"] or "", r["started_at"].isoformat(), finished.isoformat()])
    runs.sort(key=lambda r: (r[0], r[2]))
    return {
        "fact_snapshot.csv": _csv_text(FILES["fact_snapshot.csv"], fact),
        "dim_metric.csv": _csv_text(FILES["dim_metric.csv"], metrics),
        "dim_connection.csv": _csv_text(FILES["dim_connection.csv"], conns),
        "sync_run.csv": _csv_text(FILES["sync_run.csv"], runs),
    }


#: label, unit, kind for each metric the three sources use, copied from migrations 001, 006 and 007 (a test compares them with the migrations)
METRIC_META = {
    "orders": ("Orders", "count", "delta"),
    "gross_sales": ("Gross sales", "minor_currency", "delta"),
    "refunds": ("Refunds", "minor_currency", "delta"),
    "net_sales": ("Net sales", "minor_currency", "delta"),
    "customers": ("Customers", "count", "gauge"),
    "ad_spend": ("Ad spend", "minor_currency", "delta"),
    "ad_impressions": ("Impressions", "count", "delta"),
    "ad_clicks": ("Clicks", "count", "delta"),
    "ad_purchases": ("Purchases", "count", "delta"),
    "ad_purchase_value": ("Purchase value", "minor_currency", "delta"),
    "sessions": ("Sessions", "count", "delta"),
    "users": ("Users", "count", "gauge"),
    "page_views": ("Page views", "count", "delta"),
    "key_events": ("Key events", "count", "delta"),
}


async def load_and_export(dsn: str) -> dict[str, str]:
    import asyncpg

    sys.path.insert(0, str(ROOT))
    from app.crypto import CredentialCipher, generate_key as gen_crypto_key

    admin_dsn = dsn.rsplit("/", 1)[0] + "/postgres"
    dbname = dsn.rsplit("/", 1)[1]
    admin = await asyncpg.connect(admin_dsn)
    try:
        if not await admin.fetchval("select 1 from pg_database where datname = $1", dbname):
            await admin.execute(f'create database "{dbname}"')
    finally:
        await admin.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("drop schema if exists public cascade; create schema public")
        has_vector = await conn.fetchval("select count(*) from pg_available_extensions where name = 'vector'")
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name.startswith("010_") and not has_vector:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))

        await conn.execute("select set_config('app.tenant_id', $1, false)", str(TENANT_ID))
        await conn.execute("insert into tenant (id, slug, name) values ($1,$2,$3)", TENANT_ID, "powerbi-demo", "Power BI demo (invented data)")
        os.environ.setdefault("CREDENTIAL_KEYS", "1:" + gen_crypto_key())
        cipher = CredentialCipher()
        for platform, (cid, ext, name) in CONNECTIONS.items():
            blob = cipher.encrypt(json.dumps({"access_token": "invented"}), CredentialCipher.aad(str(TENANT_ID), str(cid), platform))
            await conn.execute(
                "insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc, scopes) values ($1,$2,$3,$4,$5,$6,$7)",
                cid, TENANT_ID, platform, ext, name, blob, [])
        payload = [
            {"tenant_id": str(TENANT_ID), "connection_id": str(CONNECTIONS[p["platform"]][0]), "metric_key": p["metric_key"], "observed_on": p["observed_on"],
             "value_numeric": p["value_numeric"], "currency": p["currency"], "raw": "{}"}
            for p in invented_points()]
        await conn.fetchval("select upsert_snapshots($1::jsonb)", json.dumps(payload))
        points = invented_points()
        for r in invented_sync_runs():
            cid = CONNECTIONS[r["platform"]][0]
            n = sum(1 for p in points if p["platform"] == r["platform"] and r["window_start"].isoformat() <= p["observed_on"] < r["window_end"].isoformat())
            await conn.execute(
                """insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, rows_written, error_code, started_at, finished_at)
                   values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                TENANT_ID, cid, r["trigger"], r["window_start"], r["window_end"], r["status"], n, r["error_code"], r["started_at"], r["started_at"] + timedelta(minutes=3))

        fact = await conn.fetch(
            """select observed_on, connection_id, metric_key, value_numeric, coalesce(currency, '') as currency from metric_snapshot
               order by observed_on, connection_id::text collate "C", metric_key collate "C" """)
        metrics = await conn.fetch("""select key, label, unit, kind from metric where key in (select distinct metric_key from metric_snapshot) order by key collate "C" """)
        conns = await conn.fetch("""select id, platform, display_name, external_account from platform_connection order by platform collate "C" """)
        runs = await conn.fetch(
            """select connection_id, trigger, window_start, window_end, status, rows_written, coalesce(error_code, '') as error_code, started_at, finished_at
               from sync_run order by connection_id::text collate "C", window_start""")
    finally:
        await conn.close()

    def iso(v):
        return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else (v.isoformat() if isinstance(v, date) else str(v))

    return {
        "fact_snapshot.csv": _csv_text(FILES["fact_snapshot.csv"], [[iso(r[0]), str(r[1]), r[2], r[3], r[4]] for r in fact]),
        "dim_metric.csv": _csv_text(FILES["dim_metric.csv"], [[r[0], r[1], r[2], r[3]] for r in metrics]),
        "dim_connection.csv": _csv_text(FILES["dim_connection.csv"], [[str(r[0]), r[1], r[2], r[3]] for r in conns]),
        "sync_run.csv": _csv_text(FILES["sync_run.csv"], [[str(r[0]), r[1], iso(r[2]), iso(r[3]), r[4], r[5], r[6], iso(r[7]), iso(r[8])] for r in runs]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="rebuild from the formulas and compare with powerbi/data, no database")
    ap.add_argument("--dsn", default=os.environ.get("POWERBI_DEMO_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/powerbi_demo"))
    args = ap.parse_args()
    if args.check:
        want = expected_files()
        bad = [n for n, text in want.items() if not (DATA / n).exists() or (DATA / n).read_bytes() != text.encode("utf-8")]
        print("data files match the formulas" if not bad else "differs from the formulas: " + ", ".join(bad))
        return 1 if bad else 0
    got = asyncio.run(load_and_export(args.dsn))
    DATA.mkdir(exist_ok=True)
    for name, text in got.items():
        (DATA / name).write_bytes(text.encode("utf-8"))
        print(f"wrote {name}: {text.count(chr(10)) - 1} rows")
    want = expected_files()
    diff = [n for n in want if want[n] != got[n]]
    if diff:
        print("DATABASE OUTPUT DIFFERS FROM THE FORMULAS:", ", ".join(diff))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
