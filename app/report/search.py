"""The search half of a monthly report: Google Search Console figures next to the store's and the website's.

Plain functions, no database, no model, no slide library, like `monthly.py`, whose day accounting and wording
helpers it reuses. A month of search rows is reduced with the same `month_stats`, so a day with no row is told
apart the same way: a sync run covers it (quiet, a count of zero) or no run does (missing).

THE FIGURES
  Search clicks, Search impressions   sums of the daily values over the days that have data
  Search click through rate           clicks over impressions of the month, as a percent, from the sums
  Average position                    each day's average position weighted by that day's impressions. An average
                                      of daily averages would let a day with 10 impressions count as much as a
                                      day with 10,000. A lower number is nearer the top of the results.

THE TOP QUERIES AND PAGES. Search Console returns the top rows of a day, "not all data rows". The report adds a
query's or a page's daily top rows over the month. So a query that is never among a day's top rows is not
counted at all, and the figures here are a lower bound for each key and never a total of the month. The slide
says so. A key is shown cut to fit, its start and its end with "..." between, because the end of a page address or a long
query is where one differs from the next; the figures are exact.

PROVISIONAL DAYS. Search Console data takes a few days to settle, so the last `LAG_DAYS` days before the report
date are marked provisional (see `sources.provisional_days`). Days are Pacific days, which the page states.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

from app.report.monthly import (
    KpiRow,
    MonthlyReport,
    MonthStats,
    format_count,
    month_stats,
    previous_month,
    utc_today,
)
from app.report.sources import (
    ONE_PLACE,
    TWO_PLACES,
    build_kpi_rows,
    claim_title,
    partial_clause,
    provisional_days,
    quantised,
    ratio,
    rows_with,
)

SEARCH_SUM = ("search_clicks", "search_impressions")
SEARCH_ROW_KEYS = ("search_clicks", "search_impressions", "search_position")
SEARCH_TOTAL_KEYS = frozenset(SEARCH_SUM)
SEARCH_ORDER = ("search_clicks", "search_impressions", "ctr", "position")
SEARCH_LABELS = {
    "search_clicks": "Search clicks",
    "search_impressions": "Search impressions",
    "ctr": "Search click through rate",
    "position": "Average position",
}
#: Search Console data is "typically available after 2-3 days", so a day this close to the report date can still change.
LAG_DAYS = 3
#: Rows shown for each of the top queries and the top pages, and the longest key printed before it is cut.
TOP_SHOWN = 5
KEY_WIDTH = 30
#: A key longer than KEY_WIDTH keeps its first KEY_HEAD and its last KEY_TAIL characters. Five pages of one site share their first
#: 30 characters, so a cut that keeps only the start prints them as one text; the last 15 are where they differ.
KEY_TAIL = 15
KEY_HEAD = KEY_WIDTH - KEY_TAIL - 3


@dataclass
class SearchMonth(MonthStats):
    """A month of search rows: the day accounting of MonthStats plus the impression weighted average position."""

    position_exact: Decimal | None = None
    position_shown: Decimal | None = None


def search_month(
    rows: Iterable[Mapping],
    year: int,
    month: int,
    *,
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> SearchMonth:
    """Reduce the search snapshot rows of one month. Rows of other metrics and months are ignored."""
    base = month_stats(rows_with(rows, SEARCH_ROW_KEYS), year, month, covered=covered, as_of=as_of)
    month_ = SearchMonth(**{f.name: getattr(base, f.name) for f in fields(MonthStats)})
    month_.totals = {key: sum(d.get(key, 0) for d in base.daily.values()) for key in SEARCH_SUM}
    # position is in thousandths, weighted by the impressions of the same day
    both = [d for d in base.daily.values() if "search_position" in d and "search_impressions" in d]
    weight = sum(d["search_impressions"] for d in both)
    if weight:
        month_.position_exact = Decimal(sum(d["search_position"] * d["search_impressions"] for d in both)) / weight / 1000
        month_.position_shown = month_.position_exact.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)
    return month_


# ---------------------------------------------------------------- the top rows

@dataclass(frozen=True)
class TopRow:
    key: str
    #: the key as printed, cut to fit a table cell
    shown_key: str
    clicks: int
    impressions: int
    ctr: str
    position: str


def _cut(key: str) -> str:
    one_line = " ".join(key.split())
    return one_line if len(one_line) <= KEY_WIDTH else one_line[:KEY_HEAD].rstrip() + "..." + one_line[-KEY_TAIL:]


def top_rows(rows: Iterable[Mapping], stats: SearchMonth, dimension: str, limit: int = TOP_SHOWN) -> list[TopRow]:
    """The `limit` keys with the most clicks over the counted days of `stats`, from the stored per day top rows."""
    first = date(stats.year, stats.month, 1)
    last = first + timedelta(days=stats.days_expected - 1)
    merged: dict[str, list[int]] = {}
    for row in rows:
        if row["dimension"] != dimension or not first <= row["observed_on"] <= last:
            continue
        m = merged.setdefault(row["key"], [0, 0, 0])
        m[0] += row["clicks"]
        m[1] += row["impressions"]
        m[2] += row["position_milli"] * row["impressions"]
    ranked = sorted(merged.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))[:limit]
    out = []
    for key, (clicks, impressions, weighted) in ranked:
        rate = ratio(clicks, impressions)
        position = None if impressions == 0 else (Decimal(weighted) / impressions / 1000).quantize(ONE_PLACE, rounding=ROUND_HALF_UP)
        out.append(TopRow(key, _cut(key), clicks, impressions,
                          "n/a" if rate is None else format(rate.quantize(TWO_PLACES, rounding=ROUND_HALF_UP), ",f") + "%",
                          "n/a" if position is None else format(position, ",f")))
    return out


# ---------------------------------------------------------------- the report

@dataclass
class SearchReport:
    label: str
    current: SearchMonth
    previous: SearchMonth | None
    kpis: list[KpiRow]
    top_queries: list[TopRow] = field(default_factory=list)
    top_pages: list[TopRow] = field(default_factory=list)
    #: days of this month that have top rows stored, of the days counted
    top_row_days: int = 0
    #: days of this month that were looked at and may still change
    provisional: list[date] = field(default_factory=list)

    def kpi(self, key: str) -> KpiRow:
        return next(k for k in self.kpis if k.key == key)


def _values(m: SearchMonth) -> tuple[dict[str, int | Decimal | None], dict[str, int | Decimal | None]]:
    """(displayed values, exact values). A month nobody synced has no figures, which is not zero."""
    if not m.has_baseline:
        none: dict[str, int | Decimal | None] = {key: None for key in SEARCH_ORDER}
        return dict(none), dict(none)
    clicks, impressions = m.totals["search_clicks"], m.totals["search_impressions"]
    ctr = ratio(clicks, impressions)
    shown: dict[str, int | Decimal | None] = {
        "search_clicks": clicks, "search_impressions": impressions,
        "ctr": quantised(ctr, TWO_PLACES), "position": m.position_shown,
    }
    exact: dict[str, int | Decimal | None] = {
        "search_clicks": Decimal(clicks), "search_impressions": Decimal(impressions),
        "ctr": ctr, "position": m.position_exact,
    }
    return shown, exact


def show_search(key: str, value: int | Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    plus = "+" if signed and value > 0 else ""
    if key == "ctr":
        return ("%s%s pts" % (plus, format(value, ",f"))) if signed else format(value, ",f") + "%"
    if key == "position":
        return plus + format(value, ",f")
    return plus + format_count(int(value))


def build_search(
    rows: Iterable[Mapping],
    top: Iterable[Mapping],
    report: MonthlyReport,
    *,
    label: str = "",
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> SearchReport:
    """The search KPI table and the top rows for the month of `report`, from stored search rows."""
    rows, top = list(rows), list(top)
    shop = report.current
    today = as_of or utc_today()
    cur = search_month(rows, shop.year, shop.month, covered=covered, as_of=today)
    py, pm = previous_month(shop.year, shop.month)
    prev: SearchMonth | None = search_month(rows, py, pm, covered=covered, as_of=today)
    if not prev.has_baseline:
        prev = None
    cur_values, cur_exact = _values(cur)
    prev_values, prev_exact = _values(prev) if prev is not None else (None, None)
    kpis = build_kpi_rows(
        SEARCH_ORDER, SEARCH_LABELS, cur, prev, cur_values, cur_exact, prev_values, prev_exact,
        lambda key, value, signed=False: show_search(key, value, signed=signed),
        per_day_keys=SEARCH_TOTAL_KEYS, per_day_currency=None)
    report_ = SearchReport(label, cur, prev, kpis)
    if cur.has_baseline:
        report_.top_queries = top_rows(top, cur, "query")
        report_.top_pages = top_rows(top, cur, "page")
        first = date(cur.year, cur.month, 1)
        counted = {first + timedelta(days=i) for i in range(cur.days_expected)}
        report_.top_row_days = len({r["observed_on"] for r in top if r["observed_on"] in counted})
        report_.provisional = provisional_days(cur, today, LAG_DAYS)
    return report_


def attach_search(report: MonthlyReport, rows: Iterable[Mapping], top: Iterable[Mapping], *, label: str = "",
                  covered: Sequence[tuple[date, date]] = (), as_of: date | None = None) -> MonthlyReport:
    """Put the search figures on the report. The store's own figures are not touched."""
    report.search = build_search(rows, top, report, label=label, covered=covered, as_of=as_of)
    return report


# ---------------------------------------------------------------- wording shared by the deck and the narrative

def search_partial_clause(search: SearchReport) -> str:
    return partial_clause(search, "search")


def search_claim_title(search: SearchReport, key: str = "search_clicks") -> str:
    return claim_title(search, key, "search", SEARCH_TOTAL_KEYS, "Search figures are not available")
