"""Pieces the paid media half and the search half of a monthly report share.

Plain functions, no database, no model, no slide library, like `monthly.py`, whose day accounting they reuse.
The website half (`web.py`) predates this module and keeps its own copy of the table builder; the two sources
added after it use this one.

PROVISIONAL DAYS. A day that is over can still change. Meta restates conversions that arrive inside the click
window, and Search Console data is still being collected for a few days (docs/META_SOURCES.md and
docs/GSC_SOURCES.md say what the pages do and do not promise). The report does not read a flag from the stored
row for this: it marks the days within a lag of the report date, by arithmetic, so the same stored rows and the
same report date always give the same marks. A month reported a week after it ended has none.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.report.monthly import (
    KpiRow,
    MonthStats,
    _per_day,
    format_pct,
    move_phrase,
    pct_change,
    per_day_phrase,
    plural,
    total_phrase,
)

TWO_PLACES = Decimal("0.01")
ONE_PLACE = Decimal("0.1")


def provisional_days(stats: MonthStats, as_of: date, lag: int) -> list[date]:
    """Days of the month that were looked at and are within `lag` days of `as_of`, so may still change.

    A day with no data is not provisional, it is missing, and the data health slide says so separately.
    """
    first = date(stats.year, stats.month, 1)
    days = (first + timedelta(days=i) for i in range(stats.days_expected))
    return [d for d in days if d not in stats.missing_days and (as_of - d).days <= lag]


def ratio(numerator: int, denominator: int, scale: int = 100) -> Decimal | None:
    """`numerator` over `denominator` times `scale`, exact. None for a zero denominator: a rate of nothing is not a number."""
    return None if denominator == 0 else Decimal(numerator) * scale / denominator


def quantised(value: Decimal | None, places: Decimal) -> Decimal | None:
    return None if value is None else value.quantize(places, rounding=ROUND_HALF_UP)


def build_kpi_rows(
    keys: Sequence[str],
    labels: Mapping[str, str],
    cur: MonthStats,
    prev: MonthStats | None,
    cur_values: Mapping[str, int | Decimal | None],
    cur_exact: Mapping[str, int | Decimal | None],
    prev_values: Mapping[str, int | Decimal | None] | None,
    prev_exact: Mapping[str, int | Decimal | None] | None,
    show: Callable[..., str],
    *,
    per_day_keys: frozenset[str],
    per_day_currency: str | None,
) -> list[KpiRow]:
    """The KPI table of one source: a row for each key with its change on the month before and its notes.

    The notes mean what they mean for the store's table: "not available" (no figure this month), "no previous
    month", "no previous value", "zero baseline" (a percent of nothing), and "partial data" when a day is missing
    or the month is not over, in which case a total also carries its change per reported day.
    """
    partial = bool(cur.missing_days or not cur.complete or (prev is not None and prev.missing_days))
    rows = []
    for key in keys:
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
        per_day = (_per_day(key, cur, prev, per_day_currency, per_day_keys)
                   if partial and change is not None and prev is not None else None)
        rows.append(KpiRow(
            key, labels[key], c, p, change, pct,
            show(key, c), show(key, p), show(key, change, signed=True) if change is not None else "n/a",
            format_pct(pct), tuple(notes), per_day,
            (cur_exact[key] == prev_exact[key]) if change is not None else None,
        ))
    return rows


def rows_with(rows: Iterable[Mapping], keys: Iterable[str]) -> list[Mapping]:
    """The stored rows of the given metric keys, in order. Rows of any other metric are not this source's."""
    wanted = set(keys)
    return [r for r in rows if r["metric_key"] in wanted]


# ---------------------------------------------------------------- wording shared by the deck and the narrative

def partial_clause(src: Any, noun: str) -> str:
    """Why a source's change figures are partial, or "" when they are not. Month names only: a title already sits in a dated report.

    `src` is any source report: it has `current`, `previous` and `kpis`. `noun` names the data in a sentence,
    such as "ad" or "search".
    """
    cur, prev = src.current, src.previous
    parts = []
    if cur.missing_days:
        parts.append("%s is missing %s of %s data" % (cur.short, plural(len(cur.missing_days), "day"), noun))
    if not cur.complete:
        parts.append("only %d of %d days of %s are counted" % (cur.days_expected, cur.days_in_month, cur.short))
    if prev is not None and prev.missing_days:
        parts.append("%s is missing %s of %s data" % (prev.short, plural(len(prev.missing_days), "day"), noun))
    return " and ".join(parts)


def states_total_with_per_day(src: Any, key: str, noun: str, total_keys: frozenset[str]) -> bool:
    """True when the headline for `key` must give the total and the per reported day change together."""
    row = src.kpi(key)
    return bool(src.previous is not None and partial_clause(src, noun) and key in total_keys
                and row.per_day is not None and row.pct is not None)


def claim_title(src: Any, key: str, noun: str, total_keys: frozenset[str], unavailable: str) -> str:
    """A headline made of the table's own strings, with the data caveat inside it, as the store's and the website's are.

    A fall stated as a fact while days are missing is the sentence a reader repeats, so for a total in a partial
    month the headline gives the total plainly and the per reported day change next to it.
    """
    row, cur, prev = src.kpi(key), src.current, src.previous
    clause = partial_clause(src, noun)
    suffix = " (%s)" % clause if clause else ""
    if row.current is None:
        return "%s for %s" % (unavailable, cur.short)
    if prev is None or row.change is None:
        return "%s came to %s in %s%s" % (row.label, row.current_display, cur.short, suffix)
    if states_total_with_per_day(src, key, noun, total_keys):
        return "%s %s total is %s, but %s: per reported day it is %s" % (
            cur.short, row.label.lower(), total_phrase(row, prev), clause, per_day_phrase(row.per_day))
    if row.unchanged:
        return "%s did not change from %s%s" % (row.label, prev.short, suffix)
    if row.flat:
        return "%s stayed %s from %s%s" % (row.label, move_phrase(row), prev.short, suffix)
    if row.pct is None:
        return "%s came to %s in %s, %s from %s%s" % (
            row.label, row.current_display, cur.short, move_phrase(row), prev.short, suffix)
    return "%s %s %s from %s%s" % (row.label, "rose" if row.pct > 0 else "fell", row.pct_display.lstrip("+-"),
                                   prev.short, suffix)
