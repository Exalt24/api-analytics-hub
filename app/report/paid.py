"""The paid media half of a monthly report: Meta ad figures next to the store's and the website's.

Plain functions, no database, no model, no slide library, like `monthly.py`, whose day accounting and wording
helpers it reuses. A month of ad rows is reduced with the same `month_stats` (a day is data, quiet or missing),
so a day with no row is told apart the same way: a sync run covers it (quiet, a count of zero) or no run does
(missing, left out of the totals and drawn as a gap).

THE FIGURES
  Ad spend, Impressions, Clicks     sums of the daily values over the days that have data
  Click through rate                clicks over impressions of the month, as a percent. Computed from the sums,
                                    never an average of daily rates, and never taken from a platform's own rate.
  Cost per click                    ad spend over clicks, in the ad account's currency
  Purchases, Purchase value         sums over the days that carry them. A day whose row has no purchase figure
                                    is not a zero, so the table says how many days the figure covers.
  Return on ad spend                purchase value over ad spend, over the days that have BOTH, so a day with
                                    spend and no purchase figure does not pull the ratio down.

MONEY HERE IS NOT THE STORE'S MONEY. Ad spend and purchase value are in the ad account's currency, which need
not be the store's. They are shown in their own currency, never added to, subtracted from or divided by a store
figure, and a month whose rows name two currencies, or two months in two currencies, is refused as the store's
are. The purchase figures are Meta's own, in the attribution window the request named (7 day click), which is
not the store's orders: the two are never reconciled here and the slide says so.

PROVISIONAL DAYS. Purchases can still arrive after a day is over, inside the click window, so the last
`LAG_DAYS` days before the report date are marked provisional (see `sources.provisional_days`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

from app.report.monthly import (
    KpiRow,
    MonthlyReport,
    MonthStats,
    ReportError,
    format_count,
    format_money,
    format_signed_money,
    month_stats,
    previous_month,
    utc_today,
)
from app.report.sources import (
    TWO_PLACES,
    build_kpi_rows,
    claim_title,
    partial_clause,
    provisional_days,
    quantised,
    ratio,
    rows_with,
)

PAID_SUM = ("ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value")
#: The figures that are totals over every day, so a month with fewer reported days is smaller for that reason
#: alone. Purchases are not here: they cover only the days that carry them, so a per reported day figure would divide by the wrong days.
PAID_TOTAL_KEYS = frozenset({"ad_spend", "ad_impressions", "ad_clicks"})
PAID_ORDER = ("ad_spend", "ad_impressions", "ad_clicks", "ctr", "cpc", "ad_purchases", "ad_purchase_value", "roas")
PAID_LABELS = {
    "ad_spend": "Ad spend",
    "ad_impressions": "Impressions",
    "ad_clicks": "Clicks",
    "ctr": "Click through rate",
    "cpc": "Cost per click",
    "ad_purchases": "Purchases",
    "ad_purchase_value": "Purchase value",
    "roas": "Return on ad spend",
}
MONEY_KEYS = frozenset({"ad_spend", "ad_purchase_value", "cpc"})
#: The click window the Meta connector pins in every request, as it reads on a slide. The connector's own
#: constant is `app.connectors.meta.ATTRIBUTION_WINDOW`, and a test holds the two together.
WINDOW_TEXT = "7 day click"
#: Purchases can arrive inside the click window after the day is over, so a day this close to the report date can still change.
LAG_DAYS = 7


@dataclass
class PaidMonth(MonthStats):
    """A month of ad rows: the day accounting of MonthStats plus the figures that need more than a sum."""

    #: days that carry a purchase count, and a purchase value
    purchase_days: int = 0
    value_days: int = 0
    #: spend and purchase value over the days that have BOTH, the basis of the return on ad spend
    joint_spend: int = 0
    joint_value: int = 0
    joint_days: int = 0


def paid_month(
    rows: Iterable[Mapping],
    year: int,
    month: int,
    *,
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> PaidMonth:
    """Reduce the ad snapshot rows of one month. Rows of other metrics and months are ignored."""
    base = month_stats(rows_with(rows, PAID_SUM), year, month, covered=covered, as_of=as_of)
    month_ = PaidMonth(**{f.name: getattr(base, f.name) for f in fields(MonthStats)})
    # month_stats totals the store's keys, which are not in these rows; the ad figures replace them.
    month_.totals = {key: sum(d.get(key, 0) for d in base.daily.values()) for key in PAID_SUM}
    month_.purchase_days = sum(1 for d in base.daily.values() if "ad_purchases" in d)
    month_.value_days = sum(1 for d in base.daily.values() if "ad_purchase_value" in d)
    both = [d for d in base.daily.values() if "ad_spend" in d and "ad_purchase_value" in d]
    month_.joint_spend = sum(d["ad_spend"] for d in both)
    month_.joint_value = sum(d["ad_purchase_value"] for d in both)
    month_.joint_days = len(both)
    return month_


# ---------------------------------------------------------------- the report

@dataclass
class PaidReport:
    label: str
    current: PaidMonth
    previous: PaidMonth | None
    kpis: list[KpiRow]
    #: the ad account's currency, from the rows of this month or, failing that, the month before
    currency: str | None = None
    #: days of this month that were looked at and may still change
    provisional: list[date] = field(default_factory=list)

    def kpi(self, key: str) -> KpiRow:
        return next(k for k in self.kpis if k.key == key)


def _values(m: PaidMonth) -> tuple[dict[str, int | Decimal | None], dict[str, int | Decimal | None]]:
    """(displayed values, exact values). A month nobody synced has no figures, which is not zero."""
    if not m.has_baseline:
        none: dict[str, int | Decimal | None] = {key: None for key in PAID_ORDER}
        return dict(none), dict(none)
    spend, impressions, clicks = m.totals["ad_spend"], m.totals["ad_impressions"], m.totals["ad_clicks"]
    ctr = ratio(clicks, impressions)
    cpc = None if clicks == 0 else Decimal(spend) / clicks
    roas = ratio(m.joint_value, m.joint_spend, 1)
    purchases = m.totals["ad_purchases"] if m.purchase_days else None
    value = m.totals["ad_purchase_value"] if m.value_days else None
    shown: dict[str, int | Decimal | None] = {
        "ad_spend": spend, "ad_impressions": impressions, "ad_clicks": clicks,
        "ctr": quantised(ctr, TWO_PLACES),
        "cpc": None if cpc is None else int(cpc.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
        "ad_purchases": purchases, "ad_purchase_value": value,
        "roas": quantised(roas, TWO_PLACES),
    }
    exact: dict[str, int | Decimal | None] = {
        "ad_spend": Decimal(spend), "ad_impressions": Decimal(impressions), "ad_clicks": Decimal(clicks),
        "ctr": ctr, "cpc": cpc,
        "ad_purchases": None if purchases is None else Decimal(purchases),
        "ad_purchase_value": None if value is None else Decimal(value),
        "roas": roas,
    }
    return shown, exact


def show_paid(key: str, value: int | Decimal | None, currency: str | None, *, signed: bool = False) -> str:
    """One figure as the deck prints it. Money keeps the ad account's currency, never the store's."""
    if value is None:
        return "n/a"
    plus = "+" if signed and value > 0 else ""
    if key in MONEY_KEYS:
        if currency is None:
            return "0" if int(value) == 0 else "n/a"
        return format_signed_money(int(value), currency) if signed else format_money(int(value), currency)
    if key == "ctr":
        return ("%s%s pts" % (plus, format(value, ",f"))) if signed else format(value, ",f") + "%"
    if key == "roas":
        return plus + format(value, ",f")
    return plus + format_count(int(value))


def build_paid(
    rows: Iterable[Mapping],
    report: MonthlyReport,
    *,
    label: str = "",
    covered: Sequence[tuple[date, date]] = (),
    as_of: date | None = None,
) -> PaidReport:
    """The ad KPI table for the month of `report`, from stored ad rows."""
    rows = list(rows)
    shop = report.current
    today = as_of or utc_today()
    cur = paid_month(rows, shop.year, shop.month, covered=covered, as_of=today)
    py, pm = previous_month(shop.year, shop.month)
    prev: PaidMonth | None = paid_month(rows, py, pm, covered=covered, as_of=today)
    if not prev.has_baseline:
        prev = None
    if prev is not None and cur.currency and prev.currency and cur.currency != prev.currency:
        raise ReportError("ad spend in %s is in %s and in %s in %s, so they cannot be compared"
                          % (cur.name, cur.currency, prev.name, prev.currency))
    currency = cur.currency or (prev.currency if prev else None)
    cur_values, cur_exact = _values(cur)
    prev_values, prev_exact = _values(prev) if prev is not None else (None, None)
    kpis = build_kpi_rows(
        PAID_ORDER, PAID_LABELS, cur, prev, cur_values, cur_exact, prev_values, prev_exact,
        lambda key, value, signed=False: show_paid(key, value, currency, signed=signed),
        per_day_keys=PAID_TOTAL_KEYS, per_day_currency=currency)
    return PaidReport(label, cur, prev, kpis, currency, provisional_days(cur, today, LAG_DAYS) if cur.has_baseline else [])


def attach_paid(report: MonthlyReport, rows: Iterable[Mapping], *, label: str = "",
                covered: Sequence[tuple[date, date]] = (), as_of: date | None = None) -> MonthlyReport:
    """Put the ad figures on the report. The store's own figures are not touched."""
    report.paid = build_paid(rows, report, label=label, covered=covered, as_of=as_of)
    return report


# ---------------------------------------------------------------- wording shared by the deck and the narrative

def paid_partial_clause(paid: PaidReport) -> str:
    return partial_clause(paid, "ad")


def paid_claim_title(paid: PaidReport, key: str = "ad_spend") -> str:
    return claim_title(paid, key, "ad", PAID_TOTAL_KEYS, "Ad figures are not available")
