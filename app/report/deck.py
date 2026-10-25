"""Render a MonthlyReport as a PowerPoint deck, and read a deck back as text.

Every figure on a slide is a string `monthly.py` already formatted (the KPI rows carry
their own display text), so the deck does arithmetic nowhere. Chart series are the daily
values divided by the currency's minor unit for the axis only; the numbers a reader is
meant to quote are in the tables and titles.

Slides, in order: title, KPI summary, sales by day, orders by day, order value and buyers
with the best days, insights, data health. Slides are written for the client who reads
them, so they say "days with no data" and "data pull", not the pipeline's own words.

A day with orders is a point on the line, a day with no orders is a dot on the zero line,
and a day with no data is a break in the line with an X on the zero line. Those are three
different facts and the footnote of each chart says which are present.

`dump_deck` is the other half: it reads a saved file back through python-pptx so a test
(and a person without PowerPoint) can see exactly what a slide says, plus the file's
provenance line, which names the writer that produced the narrative.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_MARKER_STYLE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from app.connectors.shopify import _ZERO_DECIMAL
from app.report.insights import Insights
from app.report.monthly import (
    KpiRow,
    MonthlyReport,
    MonthStats,
    claim_title,
    daily_series,
    days_text,
    fmt_day,
    format_money,
    insights_title,
    per_day_phrase,
    plural,
)

NAVY = RGBColor(0x1F, 0x3A, 0x5F)
ACCENT = RGBColor(0x1B, 0x7F, 0x8C)
GREY = RGBColor(0x8A, 0x94, 0xA0)
ALERT = RGBColor(0xB4, 0x4A, 0x1E)
INK = RGBColor(0x2B, 0x2F, 0x36)
MUTED = RGBColor(0x5B, 0x64, 0x70)
BAND = RGBColor(0xF1, 0xF4, 0xF7)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "Calibri"

SLIDE_W, SLIDE_H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.6)
BODY_W = SLIDE_W - 2 * MARGIN
#: The smallest text on any slide, footnotes included.
FOOTNOTE_PT = 11

#: The line printed under the insights, whichever writer ran. Which writer it was is in the
#: file's provenance property and the text read back, not on the slide.
NARRATIVE_FOOTER = "Narrative written from the computed figures on the previous slides."


# ---------------------------------------------------------------- helpers

def _style_runs(frame, size: int, color: RGBColor, *, bold: bool = False, italic: bool = False) -> None:
    for para in frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.name = FONT
            run.font.bold = bold
            run.font.italic = italic
            run.font.color.rgb = color


def _title(slide, text: str) -> None:
    box = slide.shapes.title
    box.left, box.top, box.width, box.height = MARGIN, Inches(0.3), BODY_W, Inches(1.1)
    box.text_frame.text = text
    box.text_frame.word_wrap = True
    box.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    for para in box.text_frame.paragraphs:
        para.alignment = PP_ALIGN.LEFT
    _style_runs(box.text_frame, 28 if len(text) <= 58 else 24, NAVY, bold=True)


def _text(slide, left, top, width, height, lines: Sequence[str], size: int, color: RGBColor = INK,
          *, italic: bool = False, space_after: int = 0):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    for i, line in enumerate(lines):
        para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        para.text = line
        para.space_after = Pt(space_after)
    _style_runs(frame, size, color, italic=italic)
    return box


def _new_slide(prs: Presentation, title: str, *, title_slide: bool = False):
    slide = prs.slides.add_slide(prs.slide_layouts[0 if title_slide else 5])
    for ph in list(slide.placeholders):
        if ph.placeholder_format.idx != 0 and not title_slide:
            ph._element.getparent().remove(ph._element)
    if not title_slide:
        _title(slide, title)
    return slide


def _footer(slide, lines: Sequence[str]) -> None:
    _text(slide, MARGIN, Inches(6.55), BODY_W, Inches(0.8), list(lines), FOOTNOTE_PT, MUTED)


def _table(slide, top, rows: Sequence[Sequence[str]], widths: Sequence[float], *, size: int = 16,
           row_h: float = 0.5, left_cols: int = 1, width=BODY_W):
    """A header row in navy, banded body rows, numbers right aligned."""
    shape = slide.shapes.add_table(len(rows), len(rows[0]), MARGIN, top, width, Inches(row_h * len(rows)))
    table = shape.table
    total = sum(widths)
    for i, w in enumerate(widths):
        table.columns[i].width = int(width * w / total)
    for r, row in enumerate(rows):
        table.rows[r].height = Inches(row_h)
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.text = value
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            cell.fill.fore_color.rgb = NAVY if r == 0 else (BAND if r % 2 == 0 else WHITE)
            for para in cell.text_frame.paragraphs:
                para.alignment = PP_ALIGN.LEFT if c < left_cols else PP_ALIGN.RIGHT
            _style_runs(cell.text_frame, size, WHITE if r == 0 else INK, bold=(r == 0))
    return table


def _units(currency: str | None) -> int:
    return 1 if currency and currency.upper() in _ZERO_DECIMAL else 100


def _major(value: int | None, currency: str | None) -> float | None:
    """Chart axis value only. The slide text never uses this."""
    return None if value is None else float(Decimal(value) / _units(currency))


def _unit_name(currency: str | None) -> str:
    return "$" if (currency or "").upper() == "USD" else (currency or "").upper()


def _note_text(report: MonthlyReport, row: KpiRow) -> str:
    """The Note cell: what is behind a figure, naming the month, never a bare "partial data"."""
    cur, prev = report.current, report.previous
    parts = []
    for note in row.notes:
        if note == "partial data":
            if cur.missing_days:
                parts.append("%s partial: %s missing" % (cur.short, plural(len(cur.missing_days), "day")))
            if not cur.complete:
                parts.append("%s partial: %d of %d days" % (cur.short, cur.days_expected, cur.days_in_month))
            if prev is not None and prev.missing_days:
                parts.append("%s partial: %s missing" % (prev.short, plural(len(prev.missing_days), "day")))
            if row.per_day is not None:
                parts.append("per reported day %s" % per_day_phrase(row.per_day))
        elif note == "zero baseline":
            parts.append("%s was zero" % prev.short)
        elif note == "no previous month":
            parts.append("no earlier month stored")
        elif note == "no previous value":
            parts.append("no %s figure" % prev.short)
        elif note == "not available":
            parts.append("not reported")
    return "; ".join(parts)


def _per_day_definition(report: MonthlyReport) -> list[str]:
    if not any(k.per_day is not None for k in report.kpis):
        return []
    return ["Per reported day divides a total by the days with orders plus the days with no orders, "
            "so months with different numbers of days with data can be compared."]


def _definitions(report: MonthlyReport) -> str:
    prev = report.previous
    change = ("Change %% is the change over %s's figure, n/a when that was zero. " % prev.short) if prev else ""
    return change + ("Buyers per active day is the average of each day's distinct buyers, over days with orders. "
                     "Average order value is gross sales divided by orders.")


# ---------------------------------------------------------------- charts

def _daily_chart(slide, report: MonthlyReport, metric: str, series_name: str, axis_title: str,
                 number_format: str, alt: str, *, compare: bool, top, height) -> None:
    """A line of daily values: a dot on zero for a day with no orders, a break and an X for no data."""
    cur, prev, cc = report.current, report.previous if compare else None, report.currency
    days = max(cur.days_in_month, prev.days_in_month if prev else 0)
    to_axis = (lambda v: _major(v, cc)) if metric == "net_sales" else (lambda v: None if v is None else float(v))
    points = daily_series(cur, metric)
    values = [to_axis(v) for _, v in points]
    series = [(series_name, values, ACCENT)]
    if prev is not None:
        series.append((prev.name, [to_axis(v) for _, v in daily_series(prev, metric)], GREY))
    #: (series index, point indexes, colour): a dot on the zero line for a day with no orders, on every line
    dot_specs = [(0, [i for i, (d, v) in enumerate(points) if v is not None and d in cur.quiet_days], ACCENT)]
    if prev is not None:
        prev_points = daily_series(prev, metric)
        dot_specs.append((1, [i for i, (d, v) in enumerate(prev_points) if v is not None and d in prev.quiet_days], GREY))
    for stats, pts, color in ((cur, points, ALERT), (prev, prev_points if prev is not None else [], GREY)):
        if stats is not None and stats.missing_days:
            name = "Day with no data" if stats is cur else "%s day with no data" % stats.short
            series.append((name, [0.0 if v is None else None for _, v in pts], color))

    data = CategoryChartData()
    data.categories = [str(d) for d in range(1, days + 1)]
    for name, vals, _ in series:
        data.add_series(name, vals)
    gframe = slide.shapes.add_chart(XL_CHART_TYPE.LINE, MARGIN, top, BODY_W, height, data)
    gframe._element.nvGraphicFramePr.cNvPr.set("descr", alt)
    chart = gframe.chart
    chart.has_title = False
    chart.font.size = Pt(12)
    chart.font.name = FONT
    chart.font.color.rgb = INK
    chart.has_legend = len(series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    axis = chart.value_axis
    axis.has_major_gridlines = True
    axis.major_gridlines.format.line.color.rgb = RGBColor(0xD9, 0xDE, 0xE4)
    axis.tick_labels.number_format = number_format
    axis.tick_labels.number_format_is_linked = False
    axis.format.line.fill.background()
    axis.has_title = True
    axis.axis_title.text_frame.text = axis_title
    _style_runs(axis.axis_title.text_frame, 12, MUTED)
    chart.category_axis.format.line.color.rgb = GREY
    for plot_series, (name, _, color) in zip(chart.plots[0].series, series):
        plot_series.smooth = False
        if name.lower().endswith("day with no data"):
            plot_series.format.line.fill.background()
            plot_series.marker.style = XL_MARKER_STYLE.X
            plot_series.marker.size = 10
            plot_series.marker.format.line.color.rgb = color
            continue
        plot_series.format.line.color.rgb = color
        plot_series.format.line.width = Pt(2.5)
        plot_series.marker.style = XL_MARKER_STYLE.NONE
    for s_index, idxs, dot_color in dot_specs:
        for i in idxs:
            marker = chart.plots[0].series[s_index].points[i].marker
            marker.style = XL_MARKER_STYLE.CIRCLE
            marker.size = 9
            marker.format.fill.solid()
            marker.format.fill.fore_color.rgb = dot_color
            marker.format.line.color.rgb = dot_color


def _chart_notes(stats: MonthStats, what: str, prev: MonthStats | None = None) -> list[str]:
    lines = ["Day of the month across, %s up." % what]
    if stats.quiet_days or (prev is not None and prev.quiet_days):
        lines.append("A dot on the zero line is a day with no orders, on either line.")
    if stats.missing_days:
        lines.append("A break in the line with an X on the zero line is a day with no data (%s). %d of %d days are plotted."
                     % (days_text(stats.missing_days), stats.reported_days, stats.days_expected))
    if prev is not None and prev.missing_days:
        lines.append("A break in the grey line with a grey X is a day with no data in %s (%s)."
                     % (prev.short, days_text(prev.missing_days)))
    return lines


# ---------------------------------------------------------------- slides

def _title_slide(prs, report: MonthlyReport, data_note: str) -> None:
    slide = _new_slide(prs, "", title_slide=True)
    title = slide.shapes.title
    title.left, title.top, title.width, title.height = MARGIN, Inches(2.2), BODY_W, Inches(1.4)
    title.text_frame.text = "Monthly performance report"
    for para in title.text_frame.paragraphs:
        para.alignment = PP_ALIGN.LEFT
    _style_runs(title.text_frame, 44, NAVY, bold=True)
    sub = slide.placeholders[1]
    sub.left, sub.top, sub.width, sub.height = MARGIN, Inches(3.7), BODY_W, Inches(1.0)
    sub.text_frame.text = "%s, %s" % (report.label, report.current.name) if report.label else report.current.name
    for para in sub.text_frame.paragraphs:
        para.alignment = PP_ALIGN.LEFT
    _style_runs(sub.text_frame, 24, ACCENT)
    if data_note:
        _text(slide, MARGIN, Inches(6.5), BODY_W, Inches(0.6), [data_note], 14, MUTED, italic=True)


def _kpi_slide(prs, report: MonthlyReport) -> None:
    cur, prev = report.current, report.previous
    slide = _new_slide(prs, claim_title(report, "net_sales"))
    head = ["Metric", cur.name, prev.name if prev else "Previous month", "Change", "Change %", "Note"]
    rows = [head] + [[k.label, k.current_display, k.previous_display, k.change_display, k.pct_display,
                      _note_text(report, k)] for k in report.kpis]
    _table(slide, Inches(1.6), rows, [3.0, 2.0, 2.0, 1.8, 1.3, 3.7], size=14, row_h=0.58)
    _footer(slide, ["Source: the store's daily order records.", _definitions(report)] + _per_day_definition(report))


def _sales_slide(prs, report: MonthlyReport) -> None:
    cur, cc = report.current, report.currency
    slide = _new_slide(prs, "Net sales by day in %s" % cur.short)
    unit = _unit_name(cc)
    prev = report.previous
    _daily_chart(
        slide, report, "net_sales", cur.name, "Net sales (%s)" % unit,
        '"$"#,##0' if unit == "$" else "#,##0",
        "Line chart of net sales for each day of %s%s" % (cur.name, ", with %s as a grey line" % prev.name if prev else ""),
        compare=True, top=Inches(1.5), height=Inches(4.9),
    )
    _footer(slide, _chart_notes(cur, "net sales in %s" % ("dollars" if unit == "$" else unit), prev))


def _orders_slide(prs, report: MonthlyReport) -> None:
    cur = report.current
    slide = _new_slide(prs, "Orders by day in %s" % cur.short)
    _daily_chart(
        slide, report, "orders", cur.name, "Orders", "#,##0",
        "Line chart of orders for each day of %s" % cur.name,
        compare=False, top=Inches(1.5), height=Inches(4.9),
    )
    _footer(slide, _chart_notes(cur, "orders"))


def _value_slide(prs, report: MonthlyReport) -> None:
    cur, prev = report.current, report.previous
    slide = _new_slide(prs, "Order value, buyers and best days in %s" % cur.short)
    head = ["Metric", cur.name, prev.name if prev else "Previous month", "Change", "Change %", "Note"]
    rows = [head] + [[k.label, k.current_display, k.previous_display, k.change_display, k.pct_display,
                      _note_text(report, k)] for k in report.kpis if k.key in ("avg_order_value", "buyers_per_day")]
    _table(slide, Inches(1.5), rows, [3.0, 2.0, 2.0, 1.8, 1.3, 3.7], size=14, row_h=0.55)
    if report.top_days:
        _text(slide, MARGIN, Inches(3.35), BODY_W, Inches(0.4), ["Best days by net sales"], 18, NAVY)
        top = [["Rank", "Day", "Net sales"]] + [
            [str(i + 1), fmt_day(day), format_money(value, report.currency or "")]
            for i, (day, value) in enumerate(report.top_days)
        ]
        _table(slide, Inches(3.8), top, [1.0, 2.2, 2.8], size=15, row_h=0.5, left_cols=2, width=Inches(6.0))
    _footer(slide, [_definitions(report)])


def _insights_slide(prs, report: MonthlyReport, insights: Insights) -> None:
    slide = _new_slide(prs, insights_title(report))
    _text(slide, MARGIN, Inches(1.6), BODY_W, Inches(4.8), list(insights.lines), 18, INK, space_after=8)
    _footer(slide, [NARRATIVE_FOOTER])


def _health_title(report: MonthlyReport) -> str:
    cur = report.current
    if cur.missing_days:
        return "Data health: %s is missing %d of %d days" % (cur.short, len(cur.missing_days), cur.days_in_month)
    if not cur.complete:
        return "Data health: %d of %d days of %s are counted so far" % (cur.days_expected, cur.days_in_month, cur.short)
    return "Data health: every day of %s is covered" % cur.short


def _health_slide(prs, report: MonthlyReport) -> None:
    cur, prev = report.current, report.previous
    slide = _new_slide(prs, _health_title(report))

    def span(stats: MonthStats | None) -> str:
        if stats is None:
            return "n/a"
        return "%s to %s" % (fmt_day(stats.data_days[0]), fmt_day(stats.data_days[-1])) if stats.data_days else "none"

    def col(stats: MonthStats | None) -> list[str]:
        if stats is None:
            return ["n/a"] * 6
        return [str(stats.days_in_month), str(len(stats.data_days)), str(len(stats.quiet_days)),
                str(len(stats.missing_days)), str(stats.rows_used), span(stats)]

    labels = ["Days in the month", "Days with orders", "Days with no orders", "Days with no data",
              "Stored daily figures used", "First and last day with orders"]
    a, b = col(cur), col(prev)
    rows = [["Count", cur.name, prev.name if prev else "Previous month"]] + [[l, x, y] for l, x, y in zip(labels, a, b)]
    _table(slide, Inches(1.5), rows, [4.2, 3.2, 3.2], size=15, row_h=0.46)
    notes = ["Days with no data in %s: %s." % (cur.short, days_text(cur.missing_days))]
    if prev is not None:
        notes.append("Days with no data in %s: %s." % (prev.short, days_text(prev.missing_days)))
    else:
        notes.append("No earlier month is stored, so there is nothing to compare against.")
    if not cur.complete:
        notes.append("%s is not over: %d of %d days are counted." % (cur.short, cur.days_expected, cur.days_in_month))
    if cur.missing_days or not cur.complete or (prev is not None and prev.missing_days):
        notes.append("A month with days missing is understated, so its changes are marked partial.")
    _text(slide, MARGIN, Inches(4.95), BODY_W, Inches(1.6), notes, 15, INK, space_after=4)
    _footer(slide, ["Days with orders, days with no orders and days with no data add up to the days in the month.",
                    "A day with no orders is one where a data pull ran and found none. A day with no data is one no data pull covered."])


# ---------------------------------------------------------------- public

def _set_app_properties(prs: Presentation) -> None:
    """Replace the library template's extended properties (a Mac PowerPoint, 0 slides, 4:3)."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>api-analytics-hub</Application><PresentationFormat>Widescreen</PresentationFormat>"
        "<Slides>%d</Slides></Properties>" % len(prs.slides)
    )
    package = prs.part.package
    for part in package.iter_parts():
        if str(part.partname) == "/docProps/app.xml":
            part._blob = xml.encode("utf-8")
    # The template's thumbnail is a picture of a blank slide, not of this deck.
    for rid, rel in list(package._rels.items()):
        if rel.reltype.endswith("/thumbnail"):
            package._rels.pop(rid)


def build_deck(report: MonthlyReport, insights: Insights, *, data_note: str = "") -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    _title_slide(prs, report, data_note)
    _kpi_slide(prs, report)
    _sales_slide(prs, report)
    _orders_slide(prs, report)
    _value_slide(prs, report)
    _insights_slide(prs, report, insights)
    _health_slide(prs, report)
    props = prs.core_properties
    props.title = "Monthly performance report, %s" % report.current.name
    props.author = props.last_modified_by = "api-analytics-hub"
    props.keywords = props.subject = ""
    #: Which writer produced the narrative and why, kept off the slides.
    props.comments = insights.label
    # The library's template carries a 2013 timestamp, which reads as a stale file.
    props.created = props.modified = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    _set_app_properties(prs)
    return prs


def save_deck(report: MonthlyReport, insights: Insights, path: str | Path, *, data_note: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    build_deck(report, insights, data_note=data_note).save(str(path))
    return path


def dump_deck(source: str | Path | Presentation) -> str:
    """Every slide as text: the title, each text box, each table row and each chart series."""
    prs = Presentation(str(source)) if isinstance(source, (str, Path)) else source
    out: list[str] = []
    for n, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text_frame.text if slide.shapes.title is not None else ""
        out.append("--- slide %d: %s" % (n, title))
        for shape in slide.shapes:
            if shape == slide.shapes.title:
                continue
            if shape.has_chart:
                chart = shape.chart
                for s in chart.plots[0].series:
                    values = list(s.values)
                    out.append("chart series %r (%d points): %s" % (s.name, len(values), values))
            elif shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(cell.text for cell in row.cells))
            elif shape.has_text_frame and shape.text_frame.text.strip():
                out.extend(p.text for p in shape.text_frame.paragraphs if p.text.strip())
    provenance = prs.core_properties.comments
    if provenance:
        out.append("--- provenance (file property, not on a slide): %s" % provenance)
    return "\n".join(out)
