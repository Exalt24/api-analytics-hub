"""The combined ad spend view: cost per conversion by platform, week by week, with the shop's booked jobs beside it.

Plain functions, no database, no model, no slide library, like `paid.py`, whose formatting helpers it reuses. It reads stored ad
snapshot rows (`ad_spend`, `ad_clicks`, `ad_impressions`, and each platform's conversion key) for the platforms that sell ads (Google Ads, Meta,
TikTok) and answers the question a weekly report is asked: what did each platform cost, how many conversions did it bring, and what did one cost.

THE RULES THAT KEEP IT HONEST
  * Money is never added across currencies. Each platform is in its own ad account's currency. The totals table has one row per currency, and a
    platform in another currency has its own row, never a converted one.
  * A conversion is not the same thing on every platform, and the view says which it is. Google Ads counts the conversion actions the account
    includes in its conversions column, TikTok counts the optimisation event the ad group chose, and Meta counts purchases in a 7 day click
    window. A blended figure across platforms is shown only as a blended figure and the footnote names the difference.
  * Cost per conversion divides the spend of the days that HAVE a conversion figure by those conversions. A day with spend and no conversion
    figure (the platform sent none) does not pull the cost up, and the table says how many days the figure covers.
  * A platform with no conversions in the window shows no cost per conversion, not infinity and not zero.
  * Booked jobs are the Shopmonkey `work_orders` of the same window. They are the shop's own count and are never attributed to a platform here:
    the table puts them beside the ad figures and does not divide one by the other, because an order created in the window need not come from an
    ad in it.
  * A day nobody synced is not a day with zero spend. The view counts the days that carry spend and the report says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

from app.report.monthly import ReportError, format_count, format_money, fmt_day

AD_PLATFORMS = ("google_ads", "meta", "tiktok")
#: The platform that reports the shop's own bookings, shown beside the ad figures.
BOOKINGS_PLATFORM = "shopmonkey"
BOOKINGS_KEY = "work_orders"

#: platform -> (conversion key, divisor to whole conversions, what one conversion is)
CONVERSIONS: dict[str, tuple[str, int, str]] = {
    "google_ads": ("ad_conversions", 1000, "conversion actions the account counts"),
    "tiktok": ("ad_conversions", 1000, "the optimisation event the ad group chose"),
    "meta": ("ad_purchases", 1, "purchases, 7 day click"),
}
LABELS = {"google_ads": "Google Ads", "meta": "Meta Ads", "tiktok": "TikTok Ads"}
#: A window longer than this is refused: a weekly table of years is not a report anyone reads.
MAX_DAYS = 366


@dataclass(frozen=True)
class PlatformSpend:
    platform: str
    label: str
    currency: str | None
    #: days in the window that carry a spend figure
    days: int
    spend: int
    impressions: int
    clicks: int
    #: whole conversions over the days that carry a figure, None when no day does
    conversions: Decimal | None
    conversion_days: int
    conversion_basis: str
    #: spend of the days that carry a conversion figure, the numerator of the cost per conversion
    joint_spend: int
    #: minor units per click and per conversion, unrounded, None when there is nothing to divide by
    cost_per_click: Decimal | None
    cost_per_conversion: Decimal | None


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    platforms: tuple[str, ...]
    spend: int
    conversions: Decimal | None
    #: blended over the platforms in this currency that have conversions; the definitions differ, see the module note
    blended_cost_per_conversion: Decimal | None


@dataclass(frozen=True)
class Week:
    start: date
    end: date
    platforms: tuple[PlatformSpend, ...]
    booked_jobs: int | None


@dataclass
class AdSpendView:
    start: date
    end: date
    platforms: list[PlatformSpend]
    totals: list[CurrencyTotal]
    booked_jobs: int | None
    weeks: list[Week] = field(default_factory=list)


def _in(rows: Iterable[Mapping], keys: Sequence[str], start: date, end: date) -> list[Mapping]:
    return [r for r in rows if r["metric_key"] in keys and start <= r["observed_on"] < end]


def _check_window(start: date, end: date) -> None:
    if end <= start:
        raise ReportError("the window must hold at least one day")
    if (end - start).days > MAX_DAYS:
        raise ReportError("the window is %d days; at most %d are reported" % ((end - start).days, MAX_DAYS))


def summarise(platform: str, label: str, rows: Iterable[Mapping], start: date, end: date) -> PlatformSpend:
    """One ad connection's figures for [start, end). Rows of other metrics and days are ignored."""
    if platform not in CONVERSIONS:
        raise ReportError("%s is not an ad platform this view knows" % platform)
    conv_key, divisor, basis = CONVERSIONS[platform]
    window = _in(rows, ("ad_spend", "ad_impressions", "ad_clicks", conv_key), start, end)
    by_day: dict[date, dict[str, int]] = {}
    currencies: set[str] = set()
    for row in window:
        value = row["value_numeric"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReportError("a stored %s value is not an integer" % row["metric_key"])
        if row["metric_key"] == "ad_spend":
            if not row.get("currency"):
                raise ReportError("a stored ad spend row has no currency")
            currencies.add(row["currency"])
        slot = by_day.setdefault(row["observed_on"], {})
        if row["metric_key"] in slot:
            raise ReportError("two %s rows for %s" % (row["metric_key"], row["observed_on"]))
        slot[row["metric_key"]] = value
    if len(currencies) > 1:
        raise ReportError("%s spend in the window is in %s: one ad account is one currency" % (label, " and ".join(sorted(currencies))))
    spend_days = {d: v for d, v in by_day.items() if "ad_spend" in v}
    spend = sum(v["ad_spend"] for v in spend_days.values())
    clicks = sum(v.get("ad_clicks", 0) for v in spend_days.values())
    impressions = sum(v.get("ad_impressions", 0) for v in spend_days.values())
    joint = [v for v in spend_days.values() if conv_key in v]
    conv_total = sum(v[conv_key] for v in joint)
    joint_spend = sum(v["ad_spend"] for v in joint)
    conversions = Decimal(conv_total) / divisor if joint else None
    return PlatformSpend(
        platform=platform, label=label, currency=next(iter(currencies), None), days=len(spend_days), spend=spend,
        impressions=impressions, clicks=clicks, conversions=conversions, conversion_days=len(joint), conversion_basis=basis,
        joint_spend=joint_spend,
        cost_per_click=None if clicks == 0 else Decimal(spend) / clicks,
        cost_per_conversion=None if not conversions else Decimal(joint_spend) / conversions,
    )


def combine(platforms: Sequence[PlatformSpend]) -> list[CurrencyTotal]:
    """One total per currency. Spend in two currencies is two rows, never a sum."""
    out: list[CurrencyTotal] = []
    for currency in sorted({p.currency for p in platforms if p.currency}):
        group = [p for p in platforms if p.currency == currency]
        with_conv = [p for p in group if p.conversions]
        conv = sum((p.conversions for p in with_conv), Decimal(0)) if with_conv else None
        joint = sum(p.joint_spend for p in with_conv)
        out.append(CurrencyTotal(
            currency=currency, platforms=tuple(p.label for p in group), spend=sum(p.spend for p in group), conversions=conv,
            blended_cost_per_conversion=None if not conv else Decimal(joint) / conv))
    return out


def booked_jobs(rows: Iterable[Mapping], start: date, end: date) -> int | None:
    """The shop's `work_orders` over the window, or None when no day of it was synced (not zero)."""
    found = [r for r in _in(rows, (BOOKINGS_KEY,), start, end)]
    days = {r["observed_on"] for r in found}
    if len(days) != len(found):
        raise ReportError("two work_orders rows for one day")
    return sum(r["value_numeric"] for r in found) if found else None


def week_starts(start: date, end: date) -> list[date]:
    """The Mondays of every week that overlaps [start, end), the first one possibly before `start`."""
    first = start - timedelta(days=start.weekday())
    out = []
    while first < end:
        out.append(first)
        first += timedelta(days=7)
    return out


def build(
    ad_rows: Mapping[tuple[str, str], Iterable[Mapping]],
    start: date,
    end: date,
    *,
    bookings: Iterable[Mapping] = (),
) -> AdSpendView:
    """The view for [start, end). `ad_rows` maps (platform, connection label) to that connection's stored rows."""
    _check_window(start, end)
    materialised = {key: list(rows) for key, rows in ad_rows.items()}
    bookings = list(bookings)
    platforms = [summarise(p, label, rows, start, end) for (p, label), rows in sorted(materialised.items())]
    weeks: list[Week] = []
    for monday in week_starts(start, end):
        lo, hi = max(monday, start), min(monday + timedelta(days=7), end)
        weeks.append(Week(lo, hi, tuple(summarise(p, label, rows, lo, hi) for (p, label), rows in sorted(materialised.items())),
                          booked_jobs(bookings, lo, hi)))
    return AdSpendView(start, end, platforms, combine(platforms), booked_jobs(bookings, start, end), weeks)


# ---------------------------------------------------------------- wording

def money_of(minor: Decimal | int | None, currency: str | None) -> str:
    """A per unit cost in minor units as money, rounded half up to a whole minor unit; n/a when there is none."""
    if minor is None or currency is None:
        return "n/a"
    return format_money(int(Decimal(minor).quantize(Decimal(1), rounding=ROUND_HALF_UP)), currency)


def conversions_of(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    whole = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return format_count(int(whole)) if value == whole else format(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), ",f")


def render_text(view: AdSpendView) -> str:
    """The table a weekly report pastes: one block for the window, then one line per platform per week."""
    lines = ["Ad spend %s to %s" % (fmt_day(view.start), fmt_day(view.end - timedelta(days=1))), ""]
    header = "%-22s %-5s %12s %12s %14s %8s" % ("Platform", "Cur", "Spend", "Conversions", "Cost per conv.", "Days")
    lines.append(header)
    for p in view.platforms:
        lines.append("%-22s %-5s %12s %12s %14s %8s" % (
            p.label[:22], p.currency or "n/a", money_of(p.spend, p.currency) if p.currency else "n/a", conversions_of(p.conversions),
            money_of(p.cost_per_conversion, p.currency), "%d/%d" % (p.conversion_days, p.days)))
    lines.append("")
    for t in view.totals:
        lines.append("Total in %s: %s across %s; conversions %s; blended cost per conversion %s" % (
            t.currency, money_of(t.spend, t.currency), ", ".join(t.platforms), conversions_of(t.conversions),
            money_of(t.blended_cost_per_conversion, t.currency)))
    lines.append("Booked jobs (Shopmonkey work orders): %s" % ("n/a" if view.booked_jobs is None else format_count(view.booked_jobs)))
    lines += ["", "By week", "%-18s %-22s %12s %12s %14s %10s" % ("Week", "Platform", "Spend", "Conversions", "Cost per conv.", "Booked")]
    for w in view.weeks:
        for i, p in enumerate(w.platforms):
            lines.append("%-18s %-22s %12s %12s %14s %10s" % (
                ("%s to %s" % (fmt_day(w.start), fmt_day(w.end - timedelta(days=1)))) if i == 0 else "", p.label[:22],
                money_of(p.spend, p.currency) if p.currency else "n/a", conversions_of(p.conversions), money_of(p.cost_per_conversion, p.currency),
                ("n/a" if w.booked_jobs is None else format_count(w.booked_jobs)) if i == 0 else ""))
    lines += ["", "Days is the days with a conversion figure over the days with spend.",
              "A conversion is each platform's own: " + "; ".join("%s: %s" % (LABELS[k], v[2]) for k, v in CONVERSIONS.items()) + ".",
              "Money is in each ad account's own currency and is never converted or added across currencies."]
    return "\n".join(lines)


def to_dict(view: AdSpendView) -> dict:
    """The view as plain JSON values: money as minor units with the currency beside it, counts as numbers, ratios as strings."""
    def num(value: Decimal | None) -> str | None:
        return None if value is None else format(value.quantize(Decimal("0.0001")), "f")

    def plat(p: PlatformSpend) -> dict:
        return {"platform": p.platform, "label": p.label, "currency": p.currency, "days_with_spend": p.days, "spend_minor": p.spend,
                "impressions": p.impressions, "clicks": p.clicks, "conversions": num(p.conversions), "days_with_conversions": p.conversion_days,
                "conversion_basis": p.conversion_basis, "cost_per_click_minor": num(p.cost_per_click),
                "cost_per_conversion_minor": num(p.cost_per_conversion)}

    return {
        "start": view.start.isoformat(), "end": view.end.isoformat(), "platforms": [plat(p) for p in view.platforms],
        "totals": [{"currency": t.currency, "platforms": list(t.platforms), "spend_minor": t.spend, "conversions": num(t.conversions),
                    "blended_cost_per_conversion_minor": num(t.blended_cost_per_conversion)} for t in view.totals],
        "booked_jobs": view.booked_jobs,
        "weeks": [{"start": w.start.isoformat(), "end": w.end.isoformat(), "booked_jobs": w.booked_jobs,
                   "platforms": [plat(p) for p in w.platforms]} for w in view.weeks],
    }
