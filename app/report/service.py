"""Read the stored snapshots of one tenant's connections and produce the month's report.

The only database code in the report stage. It reads `metric_snapshot` and `sync_run` inside a
tenant-scoped transaction, so row level security filters both; nothing here calls a vendor API, the
same property the dashboard has. The arithmetic is in `monthly.py`, the narrative in `insights.py`,
the slides in `deck.py`.

A tenant has one store, and may have a website (Google Analytics 4), a paid media account (Meta) and a
search property (Search Console). Each of the three extras is optional and is read from its own rows and its
own sync runs, so a source's days are never judged by another's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.db import Database
from app.report.deck import save_deck
from app.report.insights import Insights, ReportInsightWriter, write_insights
from app.report.monthly import MonthlyReport, ReportError, build_report, month_bounds, previous_month
from app.report.paid import attach_paid
from app.report.search import attach_search
from app.report.web import attach_web

#: Platforms that report website traffic, paid media and search. Every other connection is the store the report is about.
WEB_PLATFORMS = ("ga4",)
PAID_PLATFORMS = ("meta",)
SEARCH_PLATFORMS = ("gsc",)
EXTRA_PLATFORMS = WEB_PLATFORMS + PAID_PLATFORMS + SEARCH_PLATFORMS


@dataclass
class ReportInputs:
    label: str
    rows: list[dict]
    #: half-open (window_start, window_end) of every sync run that succeeded or was partial
    covered: list[tuple[date, date]]
    #: the website, paid media and search connections' rows and coverage, or None when the tenant has none
    web: "ReportInputs | None" = None
    paid: "ReportInputs | None" = None
    search: "ReportInputs | None" = None
    #: the per day top queries and pages of the search connection, for the month asked for
    top_rows: list[dict] | None = None


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


async def _only_one(conn, platforms: tuple[str, ...], connection_id: str | None, flag: str, noun: str):
    """The one active connection of these platforms, or None. Two would be added into one doubled total, so two is refused."""
    found = await conn.fetch(
        "select id, coalesce(display_name, external_account) as label from platform_connection "
        "where status = 'active' and platform = any($2::text[]) and ($1::uuid is null or id = $1::uuid) "
        "order by created_at",
        connection_id, list(platforms),
    )
    if connection_id and not found:
        raise ReportError("no active %s connection found with that id" % noun)
    if len(found) > 1:
        raise ReportError("this tenant has %d active %s connections, name one with %s" % (len(found), noun, flag))
    return found[0] if found else None


async def load_inputs(db: Database, tenant_id: str, year: int, month: int,
                      connection_id: str | None = None, web_connection_id: str | None = None,
                      paid_connection_id: str | None = None, search_connection_id: str | None = None) -> ReportInputs:
    """The month and the month before it for the store and, when the tenant has them, the website, ads and search.

    `connection_id` names the store, `web_connection_id` the website, `paid_connection_id` the ad account and
    `search_connection_id` the search property. Two stores, or two of any other source, would be added into one
    doubled total, so with more than one of any and no name the report is refused.
    """
    start = date(*previous_month(year, month), 1)
    first, end = month_bounds(year, month)
    async with db.tenant(tenant_id) as conn:
        stores = await conn.fetch(
            "select id, coalesce(display_name, external_account) as label from platform_connection "
            "where status = 'active' and platform <> all($2::text[]) and ($1::uuid is null or id = $1::uuid) "
            "order by created_at",
            connection_id, list(EXTRA_PLATFORMS),
        )
        if not stores:
            raise ReportError("no active connection found for this tenant")
        if len(stores) > 1:
            # Two connections would be summed into one doubled total.
            raise ReportError("this tenant has %d active connections, name one with --connection" % len(stores))
        site = await _only_one(conn, WEB_PLATFORMS, web_connection_id, "--web-connection", "website")
        ads = await _only_one(conn, PAID_PLATFORMS, paid_connection_id, "--paid-connection", "paid media")
        search = await _only_one(conn, SEARCH_PLATFORMS, search_connection_id, "--search-connection", "search")
        inputs = await _read(conn, stores[0]["id"], start, end, stores[0]["label"])
        if site:
            inputs.web = await _read(conn, site["id"], start, end, site["label"])
        if ads:
            inputs.paid = await _read(conn, ads["id"], start, end, ads["label"])
        if search:
            inputs.search = await _read(conn, search["id"], start, end, search["label"])
            top = await conn.fetch(
                "select observed_on, dimension, key, clicks, impressions, position_milli from search_top_row "
                "where connection_id = $1 and observed_on >= $2 and observed_on < $3 order by observed_on, dimension, key",
                search["id"], first, end,
            )
            inputs.top_rows = [dict(r) for r in top]
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
    paid_connection_id: str | None = None,
    search_connection_id: str | None = None,
    writer: ReportInsightWriter | None = None,
    data_note: str = "",
    as_of: date | None = None,
) -> tuple[Path, MonthlyReport, Insights]:
    """Load, compute, write the narrative, save the deck. Raises ReportError when it cannot."""
    # A source's name is passed only when one was given, so a caller that wraps load_inputs for the store alone is unaffected.
    named = {key: value for key, value in (("web_connection_id", web_connection_id), ("paid_connection_id", paid_connection_id),
                                           ("search_connection_id", search_connection_id)) if value}
    inputs = await load_inputs(db, tenant_id, year, month, connection_id, **named)
    report = build_report(inputs.rows, year, month, label=inputs.label, covered=inputs.covered, as_of=as_of)
    if inputs.web is not None:
        attach_web(report, inputs.web.rows, label=inputs.web.label, covered=inputs.web.covered, as_of=as_of)
    if inputs.paid is not None:
        attach_paid(report, inputs.paid.rows, label=inputs.paid.label, covered=inputs.paid.covered, as_of=as_of)
    if inputs.search is not None:
        attach_search(report, inputs.search.rows, inputs.top_rows or [], label=inputs.search.label,
                      covered=inputs.search.covered, as_of=as_of)
    insights = await write_insights(report, writer)
    try:
        saved = save_deck(report, insights, out, data_note=data_note)
    except OSError as exc:
        # Not a database fault: most often the previous deck is still open in PowerPoint.
        raise ReportError("the deck could not be saved to %s (%s); close it if it is open" % (out, type(exc).__name__)) from exc
    return saved, report, insights
