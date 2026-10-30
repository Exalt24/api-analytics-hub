"""The daily report: where a competitor's LANDED price (price plus shipping) is lower than ours, and what price we would propose.

Pure functions over rows already read from the database, so every rule is tested without one. The rules, in the order they act:

  * Only an observation from the report day counts. A page not scanned that day is listed as excluded (stale), not compared.
  * A competitor price in a different currency than ours is excluded. There is no exchange rate here and no guess at one.
  * A competitor that is out of stock is excluded: a price nobody can buy is not a reason to cut ours.
  * Shipping is the page's own figure if it stated one, else an estimate from the tenant's rate table (shipping.py), labelled
    ESTIMATED. If neither exists the line is excluded and says why. Landed = price + shipping, for them and for us.
  * If their landed price is lower than ours, the proposed price is what makes OUR landed price equal theirs (minus an optional
    undercut), and never lower than the product's floor. When the floor stops us from matching, the line says so.
    A proposal is never above the current price; a floor at or above the current price proposes nothing.
  * One proposal per product, from the competitor with the lowest landed price (ties go to a stated shipping figure).

Anything the report is unsure of is shown, not dropped: excluded lines carry their reason, and listings whose match needs a
person are listed at the end.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .money import format_minor
from .shipping import ESTIMATED, STATED, RateBand, resolve_shipping


@dataclass(frozen=True)
class ProductRow:
    id: str
    sku: str
    title: str
    price_minor: int
    currency: str
    floor_minor: int
    our_shipping_minor: int = 0
    weight_grams: int | None = None


@dataclass(frozen=True)
class Observation:
    listing_id: str
    product_id: str
    competitor: str
    url: str
    ship_zone: str
    observed_on: date
    price_minor: int | None
    currency: str | None
    availability: str = "unknown"
    shipping_minor: int | None = None
    problem: str | None = None


@dataclass(frozen=True)
class ReportLine:
    product_id: str
    sku: str
    title: str
    competitor: str
    url: str
    currency: str
    comp_price_minor: int
    comp_shipping_minor: int
    shipping_source: str          # stated or estimated
    comp_landed_minor: int
    our_price_minor: int
    our_landed_minor: int
    floor_minor: int
    proposed_price_minor: int | None   # None: nothing can be proposed
    floor_limited: bool
    note: str = ""

    @property
    def gap_minor(self) -> int:
        return self.our_landed_minor - self.comp_landed_minor


@dataclass(frozen=True)
class Excluded:
    sku: str
    competitor: str
    url: str
    reason: str


@dataclass(frozen=True)
class ReviewItem:
    competitor: str
    url: str
    reason: str


@dataclass
class Report:
    day: date
    lines: list[ReportLine] = field(default_factory=list)
    compared: int = 0                       # observations that could be compared at all
    not_lower: int = 0
    excluded: list[Excluded] = field(default_factory=list)
    needs_review: list[ReviewItem] = field(default_factory=list)

    def best_by_product(self) -> dict[str, ReportLine]:
        """The line a proposal is made from: lowest landed price, a stated shipping figure before an estimate on a tie."""
        best: dict[str, ReportLine] = {}
        for line in self.lines:
            cur = best.get(line.product_id)
            key = (line.comp_landed_minor, line.shipping_source != STATED, line.competitor)
            if cur is None or key < (cur.comp_landed_minor, cur.shipping_source != STATED, cur.competitor):
                best[line.product_id] = line
        return best

    def summary(self) -> dict[str, int]:
        return {"lower": len(self.lines), "compared": self.compared, "not_lower": self.not_lower, "excluded": len(self.excluded),
                "needs_review": len(self.needs_review), "estimated_shipping": sum(1 for l in self.lines if l.shipping_source == ESTIMATED),
                "floor_limited": sum(1 for l in self.lines if l.floor_limited)}


def build_report(day: date, products: Sequence[ProductRow], observations: Sequence[Observation], bands: Sequence[RateBand],
                 needs_review: Sequence[ReviewItem] = (), *, undercut_minor: int = 0) -> Report:
    by_id = {p.id: p for p in products}
    report = Report(day=day, needs_review=list(needs_review))
    for o in observations:
        p = by_id.get(o.product_id)
        if p is None:
            continue

        def skip(reason: str) -> None:
            report.excluded.append(Excluded(p.sku, o.competitor, o.url, reason))

        if o.observed_on != day:
            skip("not scanned on %s (last seen %s)" % (day, o.observed_on))
            continue
        if o.price_minor is None or o.price_minor <= 0:  # a 0.00 price is a parse accident, not an offer
            skip("no price on the page")
            continue
        if not o.currency:
            skip("price has no currency")
            continue
        if o.currency != p.currency:
            skip("currency %s against our %s, and there is no exchange rate here" % (o.currency, p.currency))
            continue
        if o.availability == "out_of_stock":
            skip("competitor is out of stock")
            continue
        ship = resolve_shipping(o.shipping_minor, o.currency, p.weight_grams, o.ship_zone, bands)
        if ship.cost_minor is None:
            skip("shipping unknown: %s" % ship.reason)
            continue

        report.compared += 1
        comp_landed = o.price_minor + ship.cost_minor
        our_landed = p.price_minor + p.our_shipping_minor
        if comp_landed >= our_landed:
            report.not_lower += 1
            continue
        target = comp_landed - p.our_shipping_minor - undercut_minor
        proposed = max(target, p.floor_minor)
        floor_limited = target < p.floor_minor
        note = ""
        if proposed >= p.price_minor:
            proposed_out = None
            note = "floor %s is at or above our price, nothing can be proposed" % format_minor(p.floor_minor, p.currency)
        else:
            proposed_out = proposed
            if floor_limited:
                note = "floor-limited: matching would be %s" % format_minor(target, p.currency)
        if proposed_out is not None and ship.source == ESTIMATED:
            note = (note + "; " if note else "") + "based on ESTIMATED shipping"
        report.lines.append(ReportLine(
            product_id=p.id, sku=p.sku, title=p.title, competitor=o.competitor, url=o.url, currency=p.currency,
            comp_price_minor=o.price_minor, comp_shipping_minor=ship.cost_minor, shipping_source=ship.source,
            comp_landed_minor=comp_landed, our_price_minor=p.price_minor, our_landed_minor=our_landed, floor_minor=p.floor_minor,
            proposed_price_minor=proposed_out, floor_limited=floor_limited, note=note))
    report.lines.sort(key=lambda l: (-l.gap_minor, l.sku, l.competitor))
    return report


# ----------------------------------------------------------------------------------------------- renders

def _ship_cell(line: ReportLine) -> str:
    amount = format_minor(line.comp_shipping_minor, line.currency)
    return "%s ESTIMATED" % amount if line.shipping_source == ESTIMATED else "%s (stated)" % amount


def render_markdown(report: Report) -> str:
    s = report.summary()
    out = ["# Competitor price report, %s" % report.day.isoformat(), "",
           "%d product lines where a competitor's landed price is lower than ours, out of %d compared. %d excluded, %d matches need a person."
           % (s["lower"], s["compared"], s["excluded"], s["needs_review"]), "",
           "Landed price is price plus shipping. Shipping marked ESTIMATED is our estimate from the shipping rate table, not the competitor's own figure.", ""]
    if report.lines:
        out += ["| SKU | Competitor | Their price | Their shipping | Their landed | Our landed | Gap | Proposed price | Note |",
                "|---|---|---|---|---|---|---|---|---|"]
        for l in report.lines:
            proposed = format_minor(l.proposed_price_minor, l.currency) if l.proposed_price_minor is not None else "none"
            out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                l.sku, l.competitor, format_minor(l.comp_price_minor, l.currency), _ship_cell(l),
                format_minor(l.comp_landed_minor, l.currency), format_minor(l.our_landed_minor, l.currency),
                format_minor(l.gap_minor, l.currency), proposed, l.note))
        out.append("")
    else:
        out += ["No competitor was cheaper on landed price today.", ""]
    if report.excluded:
        out += ["## Excluded", ""] + ["- %s at %s: %s" % (e.sku, e.competitor, e.reason) for e in report.excluded] + [""]
    if report.needs_review:
        out += ["## Matches that need a person", "",
                "These listings were not matched automatically and are not in the table above.", ""]
        out += ["- %s (%s): %s" % (r.competitor, r.url, r.reason) for r in report.needs_review] + [""]
    return "\n".join(out)


CSV_COLUMNS = ["date", "sku", "title", "competitor", "url", "currency", "their_price", "their_shipping", "shipping_source",
               "their_landed", "our_price", "our_landed", "gap", "floor", "proposed_price", "floor_limited", "note"]


def _cell(value: str) -> str:
    """A spreadsheet runs a cell that starts with = + - or @ as a formula; a leading apostrophe makes it text."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def render_csv(report: Report) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for l in report.lines:
        f = lambda m: format_minor(m, l.currency)  # noqa: E731
        w.writerow([report.day.isoformat(), _cell(l.sku), _cell(l.title), _cell(l.competitor), _cell(l.url), l.currency,
                    f(l.comp_price_minor), f(l.comp_shipping_minor), l.shipping_source.upper() if l.shipping_source == ESTIMATED else l.shipping_source,
                    f(l.comp_landed_minor), f(l.our_price_minor), f(l.our_landed_minor), f(l.gap_minor), f(l.floor_minor),
                    f(l.proposed_price_minor) if l.proposed_price_minor is not None else "", str(l.floor_limited).lower(), _cell(l.note)])
    return buf.getvalue()
