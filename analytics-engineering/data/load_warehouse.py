#!/usr/bin/env python
"""Load the raw tables (see schema.py) into a warehouse: a Postgres schema or a BigQuery dataset. The two get the same rows.

The four `hub` tables are the CSV files in powerbi/data (written from the hub's own Postgres tables by powerbi/export_data.py); `metric_snapshot.synced_at`
is the load time, which is what the dbt source freshness check reads. The other tables are data/raw/*.csv from generate_raw.py.

    python analytics-engineering/data/load_warehouse.py postgres --dsn postgresql://ae:ae@127.0.0.1:5441/warehouse --schema raw
    python analytics-engineering/data/load_warehouse.py bigquery --project my-project --dataset ae_raw
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
from schema import HUB_TABLES, TABLES  # noqa: E402

PG_TYPES = {"text": "text", "bigint": "bigint", "date": "date", "timestamp": "timestamp", "bool": "boolean"}
BQ_TYPES = {"text": "STRING", "bigint": "INT64", "date": "DATE", "timestamp": "TIMESTAMP", "bool": "BOOL"}


def table_rows(table: str, synced_at: str) -> list[list[str]]:
    """The rows of one raw table as CSV strings, in schema column order (an empty string is a null)."""
    if table in HUB_TABLES:
        src = REPO / "powerbi" / "data"
        if table == "metric_snapshot":
            rows = list(csv.DictReader((src / "fact_snapshot.csv").open(encoding="utf-8", newline="")))
            return [[r["connection_id"], r["metric_key"], r["observed_on"], r["value"], r["currency"], synced_at] for r in rows]
        name = {"metric": "dim_metric.csv", "platform_connection": "dim_connection.csv", "sync_run": "sync_run.csv"}[table]
        cols = [c for c, _ in TABLES[table]]
        return [[r[c] for c in cols] for r in csv.DictReader((src / name).open(encoding="utf-8", newline=""))]
    rows = list(csv.reader((HERE / "raw" / f"{table}.csv").open(encoding="utf-8", newline="")))[1:]
    names = [c for c, _ in TABLES[table]]
    if "_loaded_at" in names:  # the invented rows count as loaded now, so the freshness check on them is a real check
        at = names.index("_loaded_at")
        for r in rows:
            r[at] = synced_at
    return rows


def load_postgres(dsn: str, schema: str, synced_at: str) -> dict[str, int]:
    import psycopg2

    conn = psycopg2.connect(dsn)
    counts = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f'drop schema if exists "{schema}" cascade')
            cur.execute(f'create schema "{schema}"')
            for table, cols in TABLES.items():
                cur.execute(f'create table "{schema}"."{table}" (' + ", ".join(f'"{c}" {PG_TYPES[t]}' for c, t in cols) + ")")
                buf = io.StringIO(newline="")
                csv.writer(buf, lineterminator="\n").writerows(table_rows(table, synced_at))
                buf.seek(0)
                cur.copy_expert(f'copy "{schema}"."{table}" from stdin with (format csv, null \'\')', buf)
                cur.execute(f'select count(*) from "{schema}"."{table}"')
                counts[table] = cur.fetchone()[0]
    finally:
        conn.close()
    return counts


def load_bigquery(project: str, dataset: str, synced_at: str, location: str) -> dict[str, int]:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    client.delete_dataset(f"{project}.{dataset}", delete_contents=True, not_found_ok=True)
    ds = bigquery.Dataset(f"{project}.{dataset}")
    ds.location = location
    client.create_dataset(ds)
    counts = {}
    for table, cols in TABLES.items():
        schema = [bigquery.SchemaField(c, BQ_TYPES[t]) for c, t in cols]
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator="\n").writerows(table_rows(table, synced_at))
        job_config = bigquery.LoadJobConfig(schema=schema, source_format=bigquery.SourceFormat.CSV, skip_leading_rows=0, write_disposition="WRITE_TRUNCATE")
        job = client.load_table_from_file(io.BytesIO(buf.getvalue().encode("utf-8")), f"{project}.{dataset}.{table}", job_config=job_config)
        job.result()
        counts[table] = client.get_table(f"{project}.{dataset}.{table}").num_rows
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="target", required=True)
    p = sub.add_parser("postgres")
    p.add_argument("--dsn", default="postgresql://ae:ae@127.0.0.1:5441/warehouse")
    p.add_argument("--schema", default="raw")
    b = sub.add_parser("bigquery")
    b.add_argument("--project", required=True)
    b.add_argument("--dataset", default="ae_raw")
    b.add_argument("--location", default="US")
    args = ap.parse_args()
    synced_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    counts = load_postgres(args.dsn, args.schema, synced_at) if args.target == "postgres" else load_bigquery(args.project, args.dataset, synced_at, args.location)
    for t, n in counts.items():
        print(f"{t}: {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
