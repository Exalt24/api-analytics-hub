"""Seeded sample figures in the shape of the hub's own tables, and the ways they reach the lakehouse.

Everything here is generated from a seed, a start day and a number of days: no clock, no network. Three sample tenants have a store, an ad account and
a website each (the figures the hub's Shopify, Meta and GA4 connectors write into `metric_snapshot`), and one of them has a Salesforce org whose leads
are served as records by the stand-in REST server in `stand_in.py`.

    python -m lakehouse.sample_data --seed 7 --days 60 --end 2026-11-01 --csv-dir lakehouse/data/export                    # CSV export
    python -m lakehouse.sample_data --seed 7 --days 60 --end 2026-11-01 --postgres postgresql://analytics:analytics@127.0.0.1:5433/postgres
                                                                                                # creates database lakehouse_src and fills it

The Postgres route applies the repo's own migrations to a new database, so the tables, the constraints (one row per connection, metric and day,
currency only on money) and the metric vocabulary are the hub's, and the figures go in through them.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import config

NS = uuid.UUID("9d5f3a52-6a6f-4b3e-8a0b-3c0a7d1c2e11")

# (key, label, unit, kind), as the hub's migrations 001, 006, 007 and 017 insert them
METRICS = [
    ("orders", "Orders", "count", "delta"), ("gross_sales", "Gross sales", "minor_currency", "delta"), ("refunds", "Refunds", "minor_currency", "delta"),
    ("net_sales", "Net sales", "minor_currency", "delta"), ("customers", "Customers", "count", "gauge"),
    ("sessions", "Sessions", "count", "delta"), ("users", "Users", "count", "gauge"), ("page_views", "Page views", "count", "delta"),
    ("key_events", "Key events", "count", "delta"),
    ("ad_spend", "Ad spend", "minor_currency", "delta"), ("ad_impressions", "Impressions", "count", "delta"), ("ad_clicks", "Clicks", "count", "delta"),
    ("ad_purchases", "Purchases", "count", "delta"), ("ad_purchase_value", "Purchase value", "minor_currency", "delta"),
    ("sf_leads_created", "Leads created", "count", "delta"), ("sf_leads_converted", "Leads converted", "count", "delta"),
]
MONEY = {k for k, _, unit, _ in METRICS if unit == "minor_currency"}

SF_ORG_ID = "00DST0000000001AAA"
TENANTS = [("alpha-outdoor", "Alpha Outdoor", "USD", SF_ORG_ID), ("bright-pantry", "Bright Pantry", "PHP", None), ("cedar-fitness", "Cedar Fitness", "USD", None)]


@dataclass
class World:
    seed: int
    end: date
    days: int
    tenants: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)
    leads: dict[str, list[dict]] = field(default_factory=dict)  # org id -> Salesforce lead records


def sf_dt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def build_world(seed: int = 7, days: int = 60, end: date = date(2026, 11, 1), stale_ga4_days: int = 0) -> World:
    """The figures. `stale_ga4_days` makes every website source stop that many days before `end`, to show a stale source."""
    rng = random.Random(seed)
    w = World(seed, end, days)
    w.metrics = [dict(zip(("key", "label", "unit", "kind"), m)) for m in METRICS]
    created = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    for ti, (slug, name, currency, org) in enumerate(TENANTS):
        tid = str(uuid.uuid5(NS, "tenant:" + slug))
        w.tenants.append({"id": tid, "slug": slug, "name": name, "created_at": created.isoformat()})
        conns = [("shopify", f"{slug}.example-store", name + " store"), ("meta", f"act_{1000 + ti}", name + " ads"), ("ga4", f"properties/{2000 + ti}", name + " website")]
        if org:
            conns.append(("salesforce", org, name + " CRM"))
        for platform, ext, display in conns:
            cid = str(uuid.uuid5(NS, f"conn:{slug}:{platform}"))
            w.connections.append({"id": cid, "tenant_id": tid, "platform": platform, "external_account": ext, "display_name": display,
                                  "status": "active", "created_at": created.isoformat(), "currency": currency})
            if platform == "salesforce":
                w.leads[org] = []
                continue
            last = end - timedelta(days=stale_ga4_days if platform == "ga4" else 0)
            for i in range(days):
                day = end - timedelta(days=days - 1 - i)
                if day > last:
                    continue
                synced = datetime.combine(day + timedelta(days=1), time(3, 0), tzinfo=timezone.utc) + timedelta(minutes=ti * 7)
                for key, value in _day_values(rng, platform, i):
                    w.snapshots.append({
                        "tenant_id": tid, "connection_id": cid, "metric_key": key, "observed_on": day.isoformat(), "value_numeric": value,
                        "currency": currency if key in MONEY else None, "synced_at": synced.isoformat(),
                    })
        if org:
            w.leads[org] = _leads(rng, org, ti, end, days)
    return w


def _day_values(rng: random.Random, platform: str, i: int) -> list[tuple[str, int]]:
    weekday = i % 7
    if platform == "shopify":
        orders = 18 + weekday * 3 + i // 10 + rng.randint(0, 6)
        aov = 4200 + (i % 5) * 150 + rng.randint(0, 300)
        gross = orders * aov
        refunds = rng.choice([0, 0, 0, 3500, 6000])
        return [("orders", orders), ("gross_sales", gross), ("refunds", refunds), ("net_sales", gross - refunds), ("customers", orders - orders // 5)]
    if platform == "meta":
        spend = 6000 + weekday * 400 + i * 10 + rng.randint(0, 500)
        impressions = spend // 3
        clicks = impressions // 45
        purchases = max(1, clicks // 30)
        return [("ad_spend", spend), ("ad_impressions", impressions), ("ad_clicks", clicks), ("ad_purchases", purchases), ("ad_purchase_value", purchases * (5100 + rng.randint(0, 400)))]
    sessions = 900 + weekday * 120 + i * 3 + rng.randint(0, 80)
    return [("sessions", sessions), ("users", sessions * 72 // 100), ("page_views", sessions * 3 + rng.randint(0, 120)), ("key_events", sessions // 40)]


def _leads(rng: random.Random, org: str, ti: int, end: date, days: int) -> list[dict]:
    """Salesforce Lead records as the REST query returns them: ISO datetimes with a +0000 offset, a boolean, a converted date only when converted."""
    out: list[dict] = []
    sources = ["Web", "Phone Inquiry", "Partner Referral", "Advertisement", None]
    n = 0
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        for _ in range(rng.randint(1, 4)):
            n += 1
            created = datetime.combine(day, time(rng.randint(0, 23), rng.randint(0, 59), 0), tzinfo=timezone.utc)
            converted = rng.random() < 0.25 and day < end
            conv_day = day + timedelta(days=rng.randint(0, 3)) if converted else None
            if conv_day and conv_day > end:
                conv_day = end
            modified = created + timedelta(minutes=30) if not converted else max(created + timedelta(minutes=30), datetime.combine(conv_day, time(12, 0), tzinfo=timezone.utc))
            out.append({
                "Id": sf_id("00Q", ti * 100000 + n), "CreatedDate": sf_dt(created), "LastModifiedDate": sf_dt(modified),
                "LeadSource": rng.choice(sources), "Status": "Converted" if converted else rng.choice(["Open - Not Contacted", "Working - Contacted"]),
                "IsConverted": converted, "ConvertedDate": conv_day.isoformat() if conv_day else None,
            })
    return out


def sf_id(prefix: str, n: int) -> str:
    """An 18 character Salesforce style id: a 3 letter object prefix and 15 alphanumerics."""
    return prefix + format(n, "012d") + "AAA"


# ---------------------------------------------------------------- the ways the figures leave


TABLE_COLUMNS = {
    "tenant": ["id", "slug", "name", "created_at"],
    "platform_connection": ["id", "tenant_id", "platform", "external_account", "display_name", "status", "created_at"],
    "metric": ["key", "label", "unit", "kind"],
    "metric_snapshot": ["tenant_id", "connection_id", "metric_key", "observed_on", "value_numeric", "currency", "synced_at"],
}


def rows_of(world: World, table: str) -> list[dict]:
    return {"tenant": world.tenants, "platform_connection": world.connections, "metric": world.metrics, "metric_snapshot": world.snapshots}[table]


def export_csv(world: World, out_dir: Path) -> None:
    """The fallback source: the same four tables as CSV files, for a machine with no Postgres."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for table, cols in TABLE_COLUMNS.items():
        with (out_dir / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(rows_of(world, table))


def seed_postgres(world: World, admin_dsn: str, database: str = "lakehouse_src") -> str:
    """Create `database` fresh, apply the repo's migrations, and insert the figures through the hub's own tables. Returns the DSN of the new database."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    base, _, _ = admin_dsn.rpartition("/")
    dsn = f"{base}/{database}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in sorted((config.REPO / "migrations").glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        have = {r[0] for r in conn.execute("select key from metric").fetchall()}
        for m in world.metrics:
            if m["key"] not in have:
                conn.execute("insert into metric (key, label, unit, kind) values (%s, %s, %s, %s)", (m["key"], m["label"], m["unit"], m["kind"]))
    with psycopg.connect(dsn) as conn:
        for t in world.tenants:
            conn.execute("insert into tenant (id, slug, name, created_at) values (%s, %s, %s, %s)", (t["id"], t["slug"], t["name"], t["created_at"]))
        for c in world.connections:
            # credentials_enc is a required column; a sample row carries one placeholder byte, never a credential
            conn.execute("insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc, status, created_at) "
                         "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                         (c["id"], c["tenant_id"], c["platform"], c["external_account"], c["display_name"], b"\x00", c["status"], c["created_at"]))
        with conn.cursor() as cur:
            cur.executemany(
                "insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency, synced_at) values (%s, %s, %s, %s, %s, %s, %s)",
                [(s["tenant_id"], s["connection_id"], s["metric_key"], s["observed_on"], s["value_numeric"], s["currency"], s["synced_at"]) for s in world.snapshots])
        conn.commit()
    return dsn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 1))
    ap.add_argument("--stale-ga4-days", type=int, default=0)
    ap.add_argument("--csv-dir", type=Path)
    ap.add_argument("--postgres", help="admin DSN of a server, for example postgresql://analytics:analytics@127.0.0.1:5433/postgres")
    ap.add_argument("--leads-json", type=Path, help="write the Salesforce style leads, one org per file name, for the stand-in server")
    a = ap.parse_args()
    world = build_world(a.seed, a.days, a.end, a.stale_ga4_days)
    print(f"seed {a.seed}: {len(world.tenants)} tenants, {len(world.connections)} sources, {len(world.snapshots)} daily figures, "
          f"{sum(len(v) for v in world.leads.values())} leads")
    if a.csv_dir:
        export_csv(world, a.csv_dir)
        print("csv written to", a.csv_dir)
    if a.postgres:
        print("postgres ready:", seed_postgres(world, a.postgres).rsplit("@", 1)[-1])
    if a.leads_json:
        a.leads_json.write_text(json.dumps(world.leads), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
