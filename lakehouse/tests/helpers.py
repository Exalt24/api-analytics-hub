"""Builders for raw bronze records, and the small steps the tests repeat."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from lakehouse import silver
from lakehouse.bronze import land_records

NS = uuid.UUID("11111111-2222-3333-4444-555555555555")
TENANT = str(uuid.uuid5(NS, "tenant"))
CONN = str(uuid.uuid5(NS, "connection"))
CONN2 = str(uuid.uuid5(NS, "connection-2"))
BASE_DAY = date(2026, 10, 1)


def day(n: int) -> str:
    return (BASE_DAY + timedelta(days=n)).isoformat()


def synced(n: int, hour: int = 3) -> str:
    return f"{day(n + 1)}T{hour:02d}:00:00+00:00"


def snap(n: int, value, *, metric: str = "orders", conn: str = CONN, tenant: str = TENANT, sync: str | None = None, currency=None, **extra) -> dict:
    """A raw metric_snapshot row for day number n (0 is BASE_DAY), as the Postgres read would send it."""
    row = {"tenant_id": tenant, "connection_id": conn, "metric_key": metric, "observed_on": day(n), "value_numeric": value,
           "currency": currency, "synced_at": sync or synced(n)}
    row.update(extra)
    return row


def land(lh, rows: list, run_id: str = "run-1", table: str = "bronze_pg_metric_snapshot", source_table: str = "metric_snapshot", source_name: str = "postgres") -> dict:
    return land_records(lh, table, source_name=source_name, source_table=source_table, run_id=run_id, records=rows)


def silver_daily(lh, run_id: str = "silver-1") -> dict:
    return silver.run_silver(lh, silver.SPECS["silver_daily_figure"], run_id)


def figures(lh) -> dict:
    """silver_daily_figure as {(connection, metric, day): value}."""
    return {(r["connection_id"], r["metric_key"], str(r["observed_on"])): r["value_numeric"] for r in lh.read("silver_daily_figure").collect()}


def outcome_sum(counts: dict) -> int:
    return sum(counts[k] for k in ("inserted", "updated", "unchanged", "stale", "duplicate_in_batch", "quarantined"))


def lead(n: int, *, org: str = "00DST0000000001AAA", created: str = "2026-10-01T08:15:00.000+0000", modified: str | None = None, converted: bool = False,
         converted_date: str | None = None, lead_id: str | None = None, **extra) -> tuple[dict, str]:
    rec = {"attributes": {"type": "Lead", "url": "/services/data/v62.0/sobjects/Lead/x"}, "Id": lead_id or f"00Q{n:012d}AAA", "CreatedDate": created,
           "LastModifiedDate": modified or created, "LeadSource": "Web", "Status": "Open - Not Contacted", "IsConverted": converted, "ConvertedDate": converted_date}
    rec.update(extra)
    return rec, org
