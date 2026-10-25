"""Read the stored snapshots of one connection and produce the month's report.

The only database code in the report stage. It reads `metric_snapshot` and `sync_run`
inside a tenant-scoped transaction, so row level security filters both; nothing here
calls a vendor API, the same property the dashboard has. The arithmetic is in
`monthly.py`, the narrative in `insights.py`, the slides in `deck.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.db import Database
from app.report.deck import save_deck
from app.report.insights import Insights, ReportInsightWriter, write_insights
from app.report.monthly import MonthlyReport, ReportError, build_report, month_bounds, previous_month


@dataclass
class ReportInputs:
    label: str
    rows: list[dict]
    #: half-open (window_start, window_end) of every sync run that succeeded or was partial
    covered: list[tuple[date, date]]


async def load_inputs(db: Database, tenant_id: str, year: int, month: int,
                      connection_id: str | None = None) -> ReportInputs:
    """The month and the month before it, for one connection, as stored."""
    start = date(*previous_month(year, month), 1)
    _, end = month_bounds(year, month)
    async with db.tenant(tenant_id) as conn:
        connections = await conn.fetch(
            "select id, coalesce(display_name, external_account) as label from platform_connection "
            "where status = 'active' and ($1::uuid is null or id = $1::uuid) order by created_at",
            connection_id,
        )
        if not connections:
            raise ReportError("no active connection found for this tenant")
        if len(connections) > 1:
            # Two connections would be summed into one doubled total.
            raise ReportError("this tenant has %d active connections, name one with --connection" % len(connections))
        cid = connections[0]["id"]
        rows = await conn.fetch(
            "select observed_on, metric_key, value_numeric, currency from metric_snapshot "
            "where connection_id = $1 and observed_on >= $2 and observed_on < $3 "
            "order by observed_on, metric_key",
            cid, start, end,
        )
        runs = await conn.fetch(
            "select window_start, window_end from sync_run "
            "where connection_id = $1 and status in ('succeeded', 'partial') "
            "and window_end > $2 and window_start < $3 order by window_start",
            cid, start, end,
        )
    return ReportInputs(
        label=connections[0]["label"],
        rows=[dict(r) for r in rows],
        covered=[(r["window_start"], r["window_end"]) for r in runs],
    )


async def generate(
    db: Database,
    tenant_id: str,
    year: int,
    month: int,
    out: str | Path,
    *,
    connection_id: str | None = None,
    writer: ReportInsightWriter | None = None,
    data_note: str = "",
    as_of: date | None = None,
) -> tuple[Path, MonthlyReport, Insights]:
    """Load, compute, write the narrative, save the deck. Raises ReportError when it cannot."""
    inputs = await load_inputs(db, tenant_id, year, month, connection_id)
    report = build_report(inputs.rows, year, month, label=inputs.label, covered=inputs.covered, as_of=as_of)
    insights = await write_insights(report, writer)
    return save_deck(report, insights, out, data_note=data_note), report, insights
