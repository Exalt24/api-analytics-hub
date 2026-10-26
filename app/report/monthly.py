"""Monthly report arithmetic over stored daily snapshots.

Plain functions, no database, no model, no slide library. Everything a report states
as a number is computed here, in integers and Decimal, and the text and the deck
only format what this module returned. A model never sees a raw row and never does a
sum.

THE HARD PART IS THE DAYS, NOT THE SUMS. The Shopify connector writes a row only for
a day that had orders, so "no row" is either a quiet day (a sync ran and found
nothing) or a hole (no sync covered it). Reading both as zero hides a missed sync
inside a plausible total, so a day is one of three things:

  data     the day has at least one snapshot row
  quiet    no row, and a succeeded or partial sync run's window covers the day
  missing  no row and no run covers it

Totals use the days that have data (a quiet day adds zero). A month with a missing
day is understated by that day, and the report says so instead of comparing silently.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

from app.connectors.base import MONEY_METRICS
from app.connectors.shopify import _ZERO_DECIMAL

#: Metrics that are summed over a month. `customers` is a per-day distinct count and
#: cannot be summed (README, limit 5), so it is averaged over the days that report it.
SUM_METRICS = ("orders", "gross_sales", "refunds", "net_sales")

#: The KPIs that are totals over the days of a month, so a month with fewer reported days is
#: smaller for that reason alone. AOV and buyers per active day are already per order or per day.
TOTAL_KEYS = frozenset(SUM_METRICS)

#: One decimal place for percentages and averages, rounded half up so 0.25 reads 0.3
#: the way a person checking it by hand would round it.
ONE_PLACE = Decimal("0.1")

#: How many missing days a slide lists before it says how many more there are.
MAX_DAYS_LISTED = 10

KPI_LABELS = {
    "orders": "Orders",
    "gross_sales": "Gross sales",
    "refunds": "Refunds",
    "net_sales": "Net sales",
    "avg_order_value": "Average order value",
    "buyers_per_day": "Buyers per active day",
}
KPI_ORDER = ("orders", "gross_sales", "refunds", "net_sales", "avg_order_value", "buyers_per_day")


class ReportError(ValueError):
    """The stored data cannot honestly produce this report, so none is produced."""


# ---------------------------------------------------------------- formatting

def format_money(minor: int, currency: str) -> str:
    """`12345` and USD read `$123.45`, EUR reads `EUR 123.45`; JPY is not scaled. Sign first, never a float."""
    code = currency.upper()
    exponent = 0 if code in _ZERO_DECIMAL else 2
    amount = (Decimal(abs(minor)) / (10**exponent)).quantize(Decimal(1).scaleb(-exponent))
    prefix = "$" if code == "USD" else code + " "
    return "%s%s%s" % ("-" if minor < 0 else "", prefix, format(amount, ",f"))


def fmt_day(day: date) -> str:
    """`Oct 14`, the way a reader writes a date, never `2026-10-14`."""
    return "%s %d" % (calendar.month_abbr[day.month], day.day)


def plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def utc_today() -> date:
    """Today in UTC, because the connector buckets every order by its UTC day."""
    return datetime.now(timezone.utc).date()


def format_signed_money(minor: int, currency: str) -> str:
    text = format_money(minor, currency)
    return text if minor <= 0 else "+" + text


def format_count(value: int) -> str:
    return format(value, ",d")


def format_pct(pct: Decimal | None) -> str:
    if pct is None:
        return "n/a"
    if pct == 0:
        pct = Decimal("0.0")  # a rounded -0.0 must not print its sign
    return ("+" if pct > 0 else "") + format(pct, ",f") + "%"


def pct_change(current: int | Decimal, previous: int | Decimal) -> Decimal | None:
    """Change as a percent of the previous value, or None when there is no base.

    A zero baseline has no percentage: infinity is not a number to put on a slide and
    "100 percent" would be invented. The divisor is the absolute value so a month of
    negative net sales (refunds above sales) does not flip the sign of the change.
    """
    if previous == 0:
        return None
    return ((Decimal(current) - Decimal(previous)) * 100 / abs(Decimal(previous))).quantize(
        ONE_PLACE, rounding=ROUND_HALF_UP
    )


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First day of the month and the first day of the next (a half-open window)."""
    if not (1 <= month <= 12) or year < 1:
        raise ReportError("month must be 1 to 12, got %r-%r" % (year, month))
    first = date(year, month, 1)
    return first, first + timedelta(days=calendar.monthrange(year, month)[1])


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_name(year: int, month: int) -> str:
    return "%s %d" % (calendar.month_name[month], year)


# ---------------------------------------------------------------- one month

@dataclass
class MonthStats:
    year: int
    month: int
    days_in_month: int
    days_expected: int
    data_days: list[date] = field(default_factory=list)
    quiet_days: list[date] = field(default_factory=list)
    missing_days: list[date] = field(default_factory=list)
    rows_used: int = 0
    currency: str | None = None
    totals: dict[str, int] = field(default_factory=dict)
    buyers_per_active_day: Decimal | None = None
    #: the same mean before rounding, so a percent change is not computed from a rounded figure
    buyers_exact: Decimal | None = None
    #: day -> {metric: value}, only for days with data
    daily: dict[date, dict[str, int]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return month_name(self.year, self.month)

    @property
    def short(self) -> str:
        """`October`, for a sentence that already sits inside a dated report."""
        return calendar.month_name[self.month]

    @property
    def reported_days(self) -> int:
        """Days a sync looked at: days with orders plus days with none."""
        return len(self.data_days) + len(self.quiet_days)

    @property
    def has_baseline(self) -> bool:
        """True when the month can serve as a base: it has data or a sync that found none.

        A month with neither was never synced, which is "no previous month", a different
        fact from "a previous month of zero".
        """
        return bool(self.data_days or self.quiet_days)

    @property
    def complete(self) -> bool:
        return self.days_expected == self.days_in_month

    @property
    def fully_covered(self) -> bool:
        return not self.missing_days and self.complete

    @property
    def avg_order_value(self) -> int | None:
        orders = self.totals.get("orders", 0)
        if not orders:
            return None
        return int((Decimal(self.totals["gross_sales"]) / orders).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _covered(day: date, windows: Sequence[tuple[date, date]]) -> bool:
    return any(start <= day < end for start, end in windows)


def month_stats(
    rows: Iterable[Mapping],
    year: int,
    month: int,
    *,
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> MonthStats:
    """Reduce the snapshot rows of one month to totals and a day by day status.

    `rows` are dicts with observed_on, metric_key, value_numeric and currency (the shape
    `fetch_kpis` returns); rows of other months are ignored. `covered` is the list of
    half-open (window_start, window_end) of the sync runs that succeeded. `as_of` is today:
    a day that is not over yet is not expected, so a mid-month report is marked incomplete
    instead of reading the rest of the month as missing.
    """
    first, next_first = month_bounds(year, month)
    today = as_of or utc_today()
    last_complete = min(next_first - timedelta(days=1), today - timedelta(days=1))
    if last_complete < first:
        raise ReportError("%s has not finished its first day yet" % month_name(year, month))
    days_in_month = (next_first - first).days
    expected = [first + timedelta(days=i) for i in range((last_complete - first).days + 1)]

    stats = MonthStats(year, month, days_in_month, len(expected))
    seen: set[tuple[date, str]] = set()
    currencies: set[str] = set()
    by_day: dict[date, dict[str, int]] = {}

    for row in rows:
        day = row["observed_on"]
        if not (first <= day <= last_complete):
            continue
        key = row["metric_key"]
        if (day, key) in seen:
            # Two connections both reporting the same day would be summed into a
            # doubled total without a word, so the caller must name one.
            raise ReportError("two rows for %s on %s: report one connection at a time" % (key, day))
        seen.add((day, key))
        value = row["value_numeric"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReportError("%s on %s is %s, not an integer" % (key, day, type(value).__name__))
        if key in MONEY_METRICS:
            if not row.get("currency"):
                raise ReportError("%s on %s is money with no currency" % (key, day))
            currencies.add(row["currency"].upper())
        by_day.setdefault(day, {})[key] = value
        stats.rows_used += 1

    if len(currencies) > 1:
        raise ReportError("more than one currency in %s: %s" % (stats.name, ", ".join(sorted(currencies))))
    stats.currency = next(iter(currencies), None)
    stats.daily = dict(sorted(by_day.items()))

    for day in expected:
        if day in by_day:
            stats.data_days.append(day)
        elif _covered(day, covered):
            stats.quiet_days.append(day)
        else:
            stats.missing_days.append(day)

    for key in SUM_METRICS:
        stats.totals[key] = sum(d.get(key, 0) for d in by_day.values())

    buyers = [d["customers"] for d in by_day.values() if "customers" in d]
    if buyers:
        stats.buyers_exact = Decimal(sum(buyers)) / len(buyers)
        stats.buyers_per_active_day = stats.buyers_exact.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)
    return stats


# ---------------------------------------------------------------- the report

@dataclass(frozen=True)
class PerDay:
    """A total over the days a sync looked at (days with orders plus days with none).

    Computed only when a month has days with no data or is not over, because then the raw
    totals are not over the same number of days and their direction can mislead.
    """

    current: Decimal
    previous: Decimal
    pct: Decimal | None
    current_display: str
    previous_display: str
    pct_display: str

    @property
    def unchanged(self) -> bool:
        return self.current == self.previous

    @property
    def flat(self) -> bool:
        return self.pct is not None and self.pct == 0 and not self.unchanged


@dataclass(frozen=True)
class KpiRow:
    key: str
    label: str
    current: int | Decimal | None
    previous: int | Decimal | None
    change: int | Decimal | None
    pct: Decimal | None
    current_display: str
    previous_display: str
    change_display: str
    pct_display: str
    #: any of "no previous month", "no previous value", "zero baseline", "not available" (at most
    #: one), then "partial data" when a day is missing or the month is not over. Both are kept.
    notes: tuple[str, ...] = ()
    #: the same comparison per reported day, for a total whose months do not have the same days
    per_day: PerDay | None = None
    #: whether the two UNROUNDED values are equal; the displayed ones can be equal when these are not
    exact_equal: bool | None = None

    @property
    def note(self) -> str:
        return "; ".join(self.notes)

    @property
    def flat(self) -> bool:
        """The values differ, but the percent change rounds to zero or the displayed change is zero."""
        if self.change is None or self.unchanged:
            return False
        return (self.pct is not None and self.pct == 0) or self.change == 0

    @property
    def unchanged(self) -> bool:
        """Only true when the two exact (unrounded) values are equal."""
        return self.exact_equal is True

    @property
    def flat_detail(self) -> str:
        """How much a flat figure moved: the amount, or the percent when the amount rounds to zero."""
        return self.change_display if self.change != 0 else self.pct_display


@dataclass
class MonthlyReport:
    label: str
    current: MonthStats
    previous: MonthStats | None
    kpis: list[KpiRow]
    top_days: list[tuple[date, int]]
    #: the website figures of a second source (app/report/web.py), or None for a store with no such source
    web: "WebReport | None" = None
    #: the Meta ad figures (app/report/paid.py) and the Search Console figures (app/report/search.py), or None
    paid: "PaidReport | None" = None
    search: "SearchReport | None" = None

    @property
    def currency(self) -> str | None:
        return self.current.currency or (self.previous.currency if self.previous else None)

    def kpi(self, key: str) -> KpiRow:
        return next(k for k in self.kpis if k.key == key)


def _values(stats: MonthStats) -> dict[str, int | Decimal | None]:
    out: dict[str, int | Decimal | None] = {k: stats.totals.get(k) for k in SUM_METRICS}
    out["avg_order_value"] = stats.avg_order_value
    out["buyers_per_day"] = stats.buyers_per_active_day
    return out


def _exact_values(stats: MonthStats) -> dict[str, int | Decimal | None]:
    """The same values with the mean left unrounded, for the percent column only."""
    out = _values(stats)
    out["buyers_per_day"] = stats.buyers_exact
    return out


def _show(key: str, value: int | Decimal | None, currency: str | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    if key in MONEY_METRICS:
        if currency is None:
            return "0" if int(value) == 0 else "n/a"
        return format_signed_money(int(value), currency) if signed else format_money(int(value), currency)
    if key == "buyers_per_day":
        text = format(value, ",f")
        return "+" + text if signed and value > 0 else text
    return ("+" if signed and value > 0 else "") + format_count(int(value))


def _per_day(key: str, cur: MonthStats, prev: MonthStats, currency: str | None,
             total_keys: frozenset[str] = TOTAL_KEYS) -> PerDay | None:
    """`key` per reported day in each month, and the change between them from the exact values.

    `total_keys` names the figures that are totals over days; the website figures pass their own.
    """
    if key not in total_keys or not cur.reported_days or not prev.reported_days:
        return None
    c = Decimal(cur.totals[key]) / cur.reported_days
    p = Decimal(prev.totals[key]) / prev.reported_days

    def show(value: Decimal) -> str:
        if key in MONEY_METRICS:
            if currency is None:
                return "0"
            return format_money(int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)), currency)
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",f")

    pct = pct_change(c, p)
    return PerDay(c, p, pct, show(c), show(p), format_pct(pct))


def _kpi_rows(cur: MonthStats, prev: MonthStats | None, currency: str | None) -> list[KpiRow]:
    cur_values = _values(cur)
    prev_values = _values(prev) if prev is not None and prev.has_baseline else None
    cur_exact = _exact_values(cur)
    prev_exact = _exact_values(prev) if prev is not None and prev.has_baseline else None
    partial = bool(cur.missing_days or not cur.complete or (prev is not None and prev.missing_days))
    rows = []
    for key in KPI_ORDER:
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
        per_day = _per_day(key, cur, prev, currency) if partial and change is not None else None
        rows.append(KpiRow(
            key, KPI_LABELS[key], c, p, change, pct,
            _show(key, c, currency), _show(key, p, currency),
            _show(key, change, currency, signed=True) if change is not None else "n/a",
            format_pct(pct), tuple(notes), per_day,
            (cur_exact[key] == prev_exact[key]) if change is not None else None,
        ))
    return rows


def build_report(
    rows: Iterable[Mapping],
    year: int,
    month: int,
    *,
    label: str = "",
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> MonthlyReport:
    """The KPI table and month over month changes for one month, from the stored rows."""
    rows = list(rows)
    cur = month_stats(rows, year, month, covered=covered, as_of=as_of)
    if not cur.has_baseline:
        raise ReportError("no snapshots and no successful sync cover %s, nothing to report" % cur.name)
    py, pm = previous_month(year, month)
    prev: MonthStats | None = month_stats(rows, py, pm, covered=covered, as_of=as_of)
    if not prev.has_baseline:
        prev = None
    if prev is not None and cur.currency and prev.currency and cur.currency != prev.currency:
        raise ReportError("%s is in %s and %s in %s, so they cannot be compared"
                          % (cur.name, cur.currency, prev.name, prev.currency))
    currency = cur.currency or (prev.currency if prev else None)
    top = sorted(((d, v["net_sales"]) for d, v in cur.daily.items() if "net_sales" in v),
                 key=lambda item: (-item[1], item[0]))[:3]
    return MonthlyReport(label, cur, prev, _kpi_rows(cur, prev, currency), top)


# ---------------------------------------------------------------- series for charts

def daily_series(stats: MonthStats, metric: str) -> list[tuple[date, int | None]]:
    """One point per expected day: the value, 0 for a quiet day, None for a missing one.

    None is a gap in the chart. Drawing a missing day as zero would be the same lie as
    summing it.
    """
    first, _ = month_bounds(stats.year, stats.month)
    out: list[tuple[date, int | None]] = []
    for i in range(stats.days_expected):
        day = first + timedelta(days=i)
        if day in stats.daily:
            out.append((day, stats.daily[day].get(metric, 0)))
        elif day in stats.missing_days:
            out.append((day, None))
        else:
            out.append((day, 0))
    return out


def days_text(days: Sequence[date], *, count_rest: bool = True) -> str:
    """Dates as `Oct 14`, comma separated, cut after MAX_DAYS_LISTED with the count of the rest.

    The narrative passes count_rest=False: the figure check only accepts a count that is a
    fact, and "N more" is not one, so a sentence that printed it was refused and the whole run
    died for any month with more than MAX_DAYS_LISTED days with no data.
    """
    shown = ", ".join(fmt_day(d) for d in days[:MAX_DAYS_LISTED])
    extra = len(days) - MAX_DAYS_LISTED
    if extra > 0:
        shown += " and %d more" % extra if count_rest else " and others"
    return shown if days else "none"


# ---------------------------------------------------------------- wording shared by the deck and the narrative

def partial_clause(report: MonthlyReport) -> str:
    """Why this report's change figures are partial, in a phrase, or "" when they are not.

    Month names only: a title and a sentence already sit inside a dated report.
    """
    cur, prev = report.current, report.previous
    parts = []
    if cur.missing_days:
        parts.append("%s is missing %s" % (cur.short, plural(len(cur.missing_days), "day")))
    if not cur.complete:
        parts.append("only %d of %d days of %s are counted" % (cur.days_expected, cur.days_in_month, cur.short))
    if prev is not None and prev.missing_days:
        parts.append("%s is missing %s" % (prev.short, plural(len(prev.missing_days), "day")))
    return " and ".join(parts)


def largest_move(report: MonthlyReport) -> KpiRow | None:
    """The KPI whose percent change is biggest in size; the first in table order wins a tie."""
    movers = [k for k in report.kpis if k.pct is not None]
    return max(movers, key=lambda k: abs(k.pct), default=None)


def move_phrase(row: KpiRow) -> str:
    """`up 8.4%`, `down 12.4%`, `unchanged`, `essentially flat (+$4.00)`, or `up 20` from a zero base.

    `unchanged` is only said when the two exact values are equal. A change that merely
    rounds to 0.0% is flat, and says by how much.
    """
    if row.change is None:
        return ""
    if row.unchanged:
        return "unchanged"
    if row.flat:
        return "essentially flat (%s)" % row.flat_detail
    if row.pct is None:
        return "%s %s" % ("up" if row.change > 0 else "down", row.change_display.lstrip("+-"))
    return "%s %s" % ("up" if row.pct > 0 else "down", row.pct_display.lstrip("+-"))


def total_phrase(row: KpiRow, prev: MonthStats) -> str:
    """The raw change of a total, as a plain relation: `1.8% below September`, never `fell`."""
    if row.unchanged:
        return "level with %s" % prev.short
    if row.flat:
        return "essentially flat (%s) against %s" % (row.flat_detail, prev.short)
    return "%s %s %s" % (row.pct_display.lstrip("+-"), "above" if row.pct > 0 else "below", prev.short)


def per_day_phrase(per_day: PerDay) -> str:
    if per_day.unchanged:
        return "unchanged"
    if per_day.flat:
        return "essentially flat"
    if per_day.pct is None:
        return "up" if per_day.current > per_day.previous else "down"
    return "%s %s" % ("up" if per_day.pct > 0 else "down", per_day.pct_display.lstrip("+-"))


def states_total_with_per_day(report: MonthlyReport, key: str) -> bool:
    """True when the headline for `key` must give the total and the per reported day change together."""
    row = report.kpi(key)
    return bool(report.previous is not None and partial_clause(report) and key in TOTAL_KEYS
                and row.per_day is not None and row.pct is not None)


def claim_title(report: MonthlyReport, key: str) -> str:
    """A headline made of the table's own strings, with the data caveat inside it.

    A fall stated as a fact while days are missing is the sentence a reader repeats, and a
    total over 29 days against one over 30 can fall while every reported day rose. So for a
    total in a partial month the headline gives the total plainly and the per reported day
    change next to it, and states no direction from the raw totals alone.
    """
    row, cur, prev = report.kpi(key), report.current, report.previous
    clause = partial_clause(report)
    suffix = " (%s)" % clause if clause else ""
    if row.current is None:
        return "%s is not available for %s" % (row.label, cur.short)
    if prev is None or row.change is None:
        return "%s came to %s in %s%s" % (row.label, row.current_display, cur.short, suffix)
    if states_total_with_per_day(report, key):
        return "%s %s total is %s, but %s: per reported day it is %s" % (
            cur.short, row.label.lower(), total_phrase(row, prev), clause, per_day_phrase(row.per_day))
    move = move_phrase(row)
    if row.unchanged:
        return "%s did not change from %s%s" % (row.label, prev.short, suffix)
    if row.flat:
        return "%s stayed %s from %s%s" % (row.label, move, prev.short, suffix)
    if row.pct is None:
        return "%s came to %s in %s, %s from %s%s" % (row.label, row.current_display, cur.short, move, prev.short, suffix)
    return "%s %s %s from %s%s" % (row.label, "rose" if row.pct > 0 else "fell", row.pct_display.lstrip("+-"), prev.short, suffix)


def insights_title(report: MonthlyReport) -> str:
    """The point of the insights slide: the biggest mover, or the net sales claim when nothing compares."""
    mover = largest_move(report)
    if mover is None or report.previous is None:
        return claim_title(report, "net_sales")
    if states_total_with_per_day(report, mover.key):
        return claim_title(report, mover.key)
    clause = partial_clause(report)
    return "%s moved most, %s from %s%s" % (
        mover.label, move_phrase(mover), report.previous.short, " (%s)" % clause if clause else "")
