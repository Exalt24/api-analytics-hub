"""The website half of a monthly report: Google Analytics 4 figures next to the store's.

Plain functions, no database, no model, no slide library, like `monthly.py`, whose day
accounting and wording helpers it reuses. A month of website rows is reduced with the same
`month_stats` (a day is data, quiet or missing), so a day with no row is told apart the same
way: a sync run covers it (quiet, a count of zero) or no run does (missing, left out of the
totals and drawn as a gap).

THE FIGURES
  Sessions, Page views, Key events  sums of the daily values over the days that have data
  Users per active day              the mean of the daily distinct user counts. Distinct counts
                                    cannot be summed across days (a person who visits on two days
                                    is two in a sum), so there is no monthly total, the same
                                    rule `customers` follows.
  Order rate                        orders divided by sessions, as a percent, over the days BOTH
                                    sources reported. A day only one source covers is left out of
                                    both sides, because a sum over unequal days is not a rate. It
                                    is not a GA4 conversion rate: no GA4 event is in it.

TWO SOURCES, TWO CALENDARS. Shopify days are UTC days (the connector converts), GA4 days are the
property's reporting time zone (the response says which). The report lines them up by date label
and says so in its footnote; it does not shift either one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

from app.report.monthly import (
    ONE_PLACE,
    KpiRow,
    MonthlyReport,
    MonthStats,
    _per_day,
    change_verb,
    fmt_day,
    format_count,
    format_pct,
    month_stats,
    move_phrase,
    pct_change,
    per_day_phrase,
    plural,
    previous_month,
    total_phrase,
)

WEB_SUM = ("sessions", "page_views", "key_events")
#: The website figures that are totals over days, so a month with fewer reported days is smaller for that reason alone.
WEB_TOTAL_KEYS = frozenset(WEB_SUM)
WEB_ROW_KEYS = ("sessions", "users", "page_views", "key_events")

WEB_LABELS = {
    "sessions": "Sessions",
    "users_per_day": "Users per active day",
    "page_views": "Page views",
    "key_events": "Key events",
    "order_rate": "Order rate",
}
WEB_ORDER = ("sessions", "users_per_day", "page_views", "key_events", "order_rate")

#: Weeks of a month are day 1 to 7, 8 to 14, and so on, the last one short.
WEEK_DAYS = 7


@dataclass
class WebMonth(MonthStats):
    """A month of website rows: the day accounting of MonthStats plus the website figures."""

    users_exact: Decimal | None = None
    users_per_active_day: Decimal | None = None


def web_month(
    rows: Iterable[Mapping],
    year: int,
    month: int,
    *,
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> WebMonth:
    """Reduce the website snapshot rows of one month. Rows of other metrics and months are ignored."""
    wanted = [r for r in rows if r["metric_key"] in WEB_ROW_KEYS]
    base = month_stats(wanted, year, month, covered=covered, as_of=as_of)
    month_ = WebMonth(**{f.name: getattr(base, f.name) for f in fields(MonthStats)})
    # month_stats totals the store's keys, which are not in these rows; the website's replace them.
    month_.totals = {key: sum(d.get(key, 0) for d in base.daily.values()) for key in WEB_SUM}
    users = [d["users"] for d in base.daily.values() if "users" in d]
    if users:
        month_.users_exact = Decimal(sum(users)) / len(users)
        month_.users_per_active_day = month_.users_exact.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)
    return month_


# ---------------------------------------------------------------- the report

@dataclass(frozen=True)
class WeekRow:
    label: str
    #: "5 of 7": days both sources reported, of the days in the week that are counted
    days: str
    sessions: str
    orders: str
    rate: str


@dataclass
class WebReport:
    label: str
    current: WebMonth
    previous: WebMonth | None
    kpis: list[KpiRow]
    weeks: list[WeekRow] = field(default_factory=list)
    #: days both sources reported, the basis of the order rate
    joint_current: int = 0
    joint_previous: int | None = None

    def kpi(self, key: str) -> KpiRow:
        return next(k for k in self.kpis if k.key == key)


def _reported(stats: MonthStats) -> set[date]:
    return set(stats.data_days) | set(stats.quiet_days)


def joint_days(shop: MonthStats, web: MonthStats) -> list[date]:
    """Days both sources reported (a quiet day counts as a zero), in date order."""
    return sorted(_reported(shop) & _reported(web))


def _joint_sums(shop: MonthStats, web: MonthStats, days: Iterable[date]) -> tuple[int, int]:
    orders = sum(shop.daily.get(d, {}).get("orders", 0) for d in days)
    sessions = sum(web.daily.get(d, {}).get("sessions", 0) for d in days)
    return orders, sessions


def _rate(orders: int, sessions: int) -> Decimal | None:
    """Orders as a percent of sessions, exact. None with no sessions: a percent of zero is not a number."""
    return None if sessions == 0 else Decimal(orders) * 100 / sessions


def _values(web: WebMonth, shop: MonthStats) -> tuple[dict[str, int | Decimal | None], dict[str, Decimal | None]]:
    """(displayed values, exact values). A month nobody synced has no figures, which is not zero."""
    if not web.has_baseline:
        none = {key: None for key in WEB_ORDER}
        return dict(none), dict(none)
    shown: dict[str, int | Decimal | None] = {key: web.totals[key] for key in WEB_SUM}
    exact: dict[str, Decimal | None] = {key: Decimal(web.totals[key]) for key in WEB_SUM}
    shown["users_per_day"], exact["users_per_day"] = web.users_per_active_day, web.users_exact
    orders, sessions = _joint_sums(shop, web, joint_days(shop, web))
    rate = _rate(orders, sessions)
    exact["order_rate"] = rate
    shown["order_rate"] = None if rate is None else rate.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)
    return shown, exact


def show_web(key: str, value: int | Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    plus = "+" if signed and value > 0 else ""
    if key == "order_rate":
        return ("%s%s pts" % (plus, format(value, ",f"))) if signed else format(value, ",f") + "%"
    if key == "users_per_day":
        return plus + format(value, ",f")
    return plus + format_count(int(value))


def _kpi_rows(cur: WebMonth, prev: WebMonth | None, shop_cur: MonthStats, shop_prev: MonthStats | None) -> list[KpiRow]:
    cur_values, cur_exact = _values(cur, shop_cur)
    use_prev = prev is not None and prev.has_baseline and shop_prev is not None
    prev_values, prev_exact = _values(prev, shop_prev) if use_prev else (None, None)
    partial = bool(cur.missing_days or not cur.complete or (prev is not None and prev.missing_days))
    rows = []
    for key in WEB_ORDER:
        c = cur_values[key]
        p = prev_values[key] if prev_values is not None else None
        notes: list[str] = []
        change: int | Decimal | None = None
        pct: Decimal | None = None
        if c is None:
            notes.append("not available")
        elif prev_values is None:
            notes.append("no previous month")
        elif p is None:
            notes.append("no previous value")
        else:
            change = c - p
            pct = pct_change(cur_exact[key], prev_exact[key])
            if pct is None:
                notes.append("zero baseline")
        if partial and c is not None:
            notes.append("partial data")
        per_day = _per_day(key, cur, prev, None, WEB_TOTAL_KEYS) if partial and change is not None else None
        rows.append(KpiRow(
            key, WEB_LABELS[key], c, p, change, pct,
            show_web(key, c), show_web(key, p), show_web(key, change, signed=True) if change is not None else "n/a",
            format_pct(pct), tuple(notes), per_day,
            (cur_exact[key] == prev_exact[key]) if change is not None else None,
        ))
    return rows


def _weeks(shop: MonthStats, web: WebMonth) -> list[WeekRow]:
    """Sessions against orders for each week of the month, over the days both sources reported."""
    first = date(shop.year, shop.month, 1)
    joint = set(joint_days(shop, web))
    out: list[WeekRow] = []
    for start_index in range(0, shop.days_expected, WEEK_DAYS):
        last_index = min(start_index + WEEK_DAYS, shop.days_expected) - 1
        days = [first + timedelta(days=i) for i in range(start_index, last_index + 1)]
        counted = [d for d in days if d in joint]
        orders, sessions = _joint_sums(shop, web, counted)
        rate = _rate(orders, sessions)
        label = "%s to %d" % (fmt_day(days[0]), days[-1].day) if len(days) > 1 else fmt_day(days[0])
        out.append(WeekRow(
            label, "%d of %d" % (len(counted), len(days)),
            format_count(sessions) if counted else "n/a", format_count(orders) if counted else "n/a",
            "n/a" if rate is None else show_web("order_rate", rate.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)),
        ))
    return out


def build_web(
    rows: Iterable[Mapping],
    report: MonthlyReport,
    *,
    label: str = "",
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> WebReport:
    """The website KPI table and the weeks, for the month of `report`, from stored website rows."""
    rows = list(rows)
    shop = report.current
    cur = web_month(rows, shop.year, shop.month, covered=covered, as_of=as_of)
    py, pm = previous_month(shop.year, shop.month)
    prev: WebMonth | None = web_month(rows, py, pm, covered=covered, as_of=as_of)
    if not prev.has_baseline:
        prev = None
    shop_prev = report.previous
    web = WebReport(label, cur, prev, _kpi_rows(cur, prev, shop, shop_prev), _weeks(shop, cur) if cur.has_baseline else [])
    web.joint_current = len(joint_days(shop, cur))
    web.joint_previous = len(joint_days(shop_prev, prev)) if prev is not None and shop_prev is not None else None
    return web


def attach_web(report: MonthlyReport, rows: Iterable[Mapping], *, label: str = "",
               covered: Sequence[tuple[date, date]] = (), as_of: date | None = None) -> MonthlyReport:
    """Put the website figures on the report. The store's own figures are not touched."""
    report.web = build_web(rows, report, label=label, covered=covered, as_of=as_of)
    return report


# ---------------------------------------------------------------- wording shared by the deck and the narrative

def web_partial_clause(web: WebReport) -> str:
    """Why the website change figures are partial, or "" when they are not. Month names only."""
    cur, prev = web.current, web.previous
    parts = []
    if cur.missing_days:
        parts.append("%s is missing %s of website data" % (cur.short, plural(len(cur.missing_days), "day")))
    if not cur.complete:
        parts.append("only %d of %d days of %s are counted" % (cur.days_expected, cur.days_in_month, cur.short))
    if prev is not None and prev.missing_days:
        parts.append("%s is missing %s of website data" % (prev.short, plural(len(prev.missing_days), "day")))
    return " and ".join(parts)


def web_states_total_with_per_day(web: WebReport, key: str) -> bool:
    row = web.kpi(key)
    return bool(web.previous is not None and web_partial_clause(web) and key in WEB_TOTAL_KEYS
                and row.per_day is not None and row.pct is not None)


def web_claim_title(web: WebReport, key: str = "sessions") -> str:
    """A headline made of the table's own strings, with the website data caveat inside it."""
    row, cur, prev = web.kpi(key), web.current, web.previous
    clause = web_partial_clause(web)
    suffix = " (%s)" % clause if clause else ""
    if row.current is None:
        return "Website figures are not available for %s" % cur.short
    if prev is None or row.change is None:
        return "%s came to %s in %s%s" % (row.label, row.current_display, cur.short, suffix)
    if web_states_total_with_per_day(web, key):
        return "%s %s total is %s, but %s: per reported day it is %s" % (
            cur.short, row.label.lower(), total_phrase(row, prev), clause, per_day_phrase(row.per_day))
    if row.unchanged:
        return "%s did not change from %s%s" % (row.label, prev.short, suffix)
    if row.flat:
        return "%s stayed %s from %s%s" % (row.label, move_phrase(row), prev.short, suffix)
    if row.pct is None:
        return "%s came to %s in %s, %s from %s%s" % (
            row.label, row.current_display, cur.short, move_phrase(row), prev.short, suffix)
    return "%s %s %s from %s%s" % (row.label, change_verb(row), row.pct_display.lstrip("+-"),
                                   prev.short, suffix)
