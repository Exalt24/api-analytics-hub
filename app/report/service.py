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
from app.report.web import attach_web

#: Platforms that report website traffic. Every other connection is the store the report is about.
WEB_PLATFORMS = ("ga4",)


@dataclass
class ReportInputs:
    label: str
    rows: list[dict]
    #: half-open (window_start, window_end) of every sync run that succeeded or was partial
    covered: list[tuple[date, date]]
    #: the website connection's rows and coverage, or None when the tenant has none
    web: "ReportInputs | None" = None


async def _read(conn, connection_id, start: date, end: date, label: str) -> ReportInputs:
    """One connection's stored rows and the sync windows that count as coverage."""
    rows = await conn.fetch(
        "select observed_on, metric_key, value_numeric, currency from metric_snapshot "
        "where connection_id = $1 and observed_on >= $2 and observed_on < $3 "
        "order by observed_on, metric_key",
        connection_id, start, end,
    )
    runs = await conn.fetch(
        "select window_start, window_end from sync_run "
        "where connection_id = $1 and status in ('succeeded', 'partial') "
        "and window_end > $2 and window_start < $3 order by window_start",
        connection_id, start, end,
    )
    return ReportInputs(
        label=label,
        rows=[dict(r) for r in rows],
        covered=[(r["window_start"], r["window_end"]) for r in runs],
    )


async def load_inputs(db: Database, tenant_id: str, year: int, month: int,
                      connection_id: str | None = None, web_connection_id: str | None = None) -> ReportInputs:
    """The month and the month before it for the store and, when the tenant has one, the website.

    `connection_id` names the store and `web_connection_id` the website. Two stores, or two websites,
    would be added into one doubled total, so with more than one of either and no name the report is refused.
    """
    start = date(*previous_month(year, month), 1)
    _, end = month_bounds(year, month)
    async with db.tenant(tenant_id) as conn:
        stores = await conn.fetch(
            "select id, coalesce(display_name, external_account) as label from platform_connection "
            "where status = 'active' and platform <> all($2::text[]) and ($1::uuid is null or id = $1::uuid) "
            "order by created_at",
            connection_id, list(WEB_PLATFORMS),
        )
        if not stores:
            raise ReportError("no active connection found for this tenant")
        if len(stores) > 1:
            # Two connections would be summed into one doubled total.
            raise ReportError("this tenant has %d active connections, name one with --connection" % len(stores))
        sites = await conn.fetch(
            "select id, coalesce(display_name, external_account) as label from platform_connection "
            "where status = 'active' and platform = any($2::text[]) and ($1::uuid is null or id = $1::uuid) "
            "order by created_at",
            web_connection_id, list(WEB_PLATFORMS),
        )
        if web_connection_id and not sites:
            raise ReportError("no active website connection found with that id")
        if len(sites) > 1:
            raise ReportError("this tenant has %d active website connections, name one with --web-connection" % len(sites))
        inputs = await _read(conn, stores[0]["id"], start, end, stores[0]["label"])
        if sites:
            inputs.web = await _read(conn, sites[0]["id"], start, end, sites[0]["label"])
    return inputs


async def generate(
    db: Database,
    tenant_id: str,
    year: int,
    month: int,
    out: str | Path,
    *,
    connection_id: str | None = None,
    web_connection_id: str | None = None,
    writer: ReportInsightWriter | None = None,
    data_note: str = "",
    as_of: date | None = None,
) -> tuple[Path, MonthlyReport, Insights]:
    """Load, compute, write the narrative, save the deck. Raises ReportError when it cannot."""
    # The website's name is passed only when one was given, so a caller that wraps load_inputs for the store alone is unaffected.
    named_site = {"web_connection_id": web_connection_id} if web_connection_id else {}
    inputs = await load_inputs(db, tenant_id, year, month, connection_id, **named_site)
    report = build_report(inputs.rows, year, month, label=inputs.label, covered=inputs.covered, as_of=as_of)
    if inputs.web is not None:
        attach_web(report, inputs.web.rows, label=inputs.web.label, covered=inputs.web.covered, as_of=as_of)
    insights = await write_insights(report, writer)
    try:
        saved = save_deck(report, insights, out, data_note=data_note)
    except OSError as exc:
        # Not a database fault: most often the previous deck is still open in PowerPoint.
        raise ReportError("the deck could not be saved to %s (%s); close it if it is open" % (out, type(exc).__name__)) from exc
    return saved, report, insights
