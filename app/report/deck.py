"""Render a MonthlyReport as a PowerPoint deck, and read a deck back as text.

Every figure on a slide is a string `monthly.py` already formatted (the KPI rows carry
their own display text), so the deck does arithmetic nowhere. Chart series are the daily
values divided by the currency's minor unit for the axis only; the numbers a reader is
meant to quote are in the tables and titles.

Slides, in order: title, KPI summary, sales by day, orders by day, order value and
buyers, insights, data health. A day with no snapshot and no sync run is a gap in a
chart line, never a zero.

`dump_deck` is the other half: it reads a saved file back through python-pptx so a test
(and a person without PowerPoint) can see exactly what a slide says.
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
from app.report.monthly import MonthlyReport, MonthStats, daily_series, days_text, format_money

NAVY = RGBColor(0x1F, 0x3A, 0x5F)
ACCENT = RGBColor(0x1B, 0x7F, 0x8C)
GREY = RGBColor(0x8A, 0x94, 0xA0)
INK = RGBColor(0x2B, 0x2F, 0x36)
MUTED = RGBColor(0x5B, 0x64, 0x70)
BAND = RGBColor(0xF1, 0xF4, 0xF7)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "Calibri"

SLIDE_W, SLIDE_H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.6)
BODY_W = SLIDE_W - 2 * MARGIN


# ---------------------------------------------------------------- helpers

def _style_runs(frame, size: int, color: RGBColor, *, bold: bool = False, italic: bool = False) -> None:
    for para in frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.name = FONT
            run.font.bold = bold
            run.font.italic = italic
            run.font.color.rgb = color


def _title(slide, text: str, *, size: int = 28) -> None:
    box = slide.shapes.title
    box.left, box.top, box.width, box.height = MARGIN, Inches(0.35), BODY_W, Inches(1.0)
    box.text_frame.text = text
    box.text_frame.word_wrap = True
    box.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    for para in box.text_frame.paragraphs:
        para.alignment = PP_ALIGN.LEFT
    _style_runs(box.text_frame, size, NAVY, bold=True)


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


def _footer(slide, text: str) -> None:
    _text(slide, MARGIN, Inches(6.95), BODY_W, Inches(0.35), [text], 11, MUTED)


def _table(slide, top, rows: Sequence[Sequence[str]], widths: Sequence[float], *, size: int = 16,
           row_h: float = 0.5, left_cols: int = 1):
    """A header row in navy, banded body rows, numbers right aligned."""
    shape = slide.shapes.add_table(len(rows), len(rows[0]), MARGIN, top, BODY_W, Inches(row_h * len(rows)))
    table = shape.table
    total = sum(widths)
    for i, w in enumerate(widths):
        table.columns[i].width = int(BODY_W * w / total)
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


def _chart(slide, kind, categories: Sequence[str], series: Sequence[tuple[str, Sequence[float | None], RGBColor]],
           top, height, number_format: str, alt: str):
    data = CategoryChartData()
    data.categories = list(categories)
    for name, values, _ in series:
        data.add_series(name, list(values))
    gframe = slide.shapes.add_chart(kind, MARGIN, top, BODY_W, height, data)
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
    value_axis = chart.value_axis
    value_axis.has_major_gridlines = True
    value_axis.major_gridlines.format.line.color.rgb = RGBColor(0xD9, 0xDE, 0xE4)
    value_axis.tick_labels.number_format = number_format
    value_axis.tick_labels.number_format_is_linked = False
    value_axis.format.line.fill.background()
    chart.category_axis.format.line.color.rgb = GREY
    for plot_series, (_, _, color) in zip(chart.plots[0].series, series):
        if kind == XL_CHART_TYPE.LINE:
            plot_series.format.line.color.rgb = color
            plot_series.format.line.width = Pt(2.5)
            plot_series.smooth = False
            plot_series.marker.style = XL_MARKER_STYLE.NONE
        else:
            plot_series.format.fill.solid()
            plot_series.format.fill.fore_color.rgb = color
    return chart


def _claim(report: MonthlyReport, key: str) -> str:
    """A slide headline made of the table's own strings."""
    row, cur, prev = report.kpi(key), report.current, report.previous
    if row.current is None:
        return "%s is not available for %s" % (row.label, cur.name)
    if prev is None or row.pct is None:
        return "%s came to %s in %s" % (row.label, row.current_display, cur.name)
    if row.pct == 0:
        return "%s did not change on %s and stayed at %s" % (row.label, prev.name, row.current_display)
    verb = "rose" if row.pct > 0 else "fell"
    return "%s %s %s on %s to %s" % (row.label, verb, row.pct_display.lstrip("+-"), prev.name, row.current_display)


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
    slide = _new_slide(prs, _claim(report, "net_sales"))
    head = ["Metric", cur.name, prev.name if prev else "Previous month", "Change", "Change %", "Note"]
    rows = [head] + [[k.label, k.current_display, k.previous_display, k.change_display, k.pct_display, k.note]
                     for k in report.kpis]
    _table(slide, Inches(1.6), rows, [3.2, 2.2, 2.2, 2.0, 1.4, 2.0], size=15, row_h=0.58)
    _footer(slide, "Source: stored daily snapshots. Change % is the change over the previous month's value, "
                   "n/a when that value is zero or absent.")


def _series_note(stats: MonthStats) -> str:
    return ("Gaps in the line are days with no snapshot and no sync run: %s." % days_text(stats.missing_days)
            if stats.missing_days else "Every day in the period has a snapshot or a sync run that found no orders.")


def _sales_slide(prs, report: MonthlyReport) -> None:
    cur, prev, cc = report.current, report.previous, report.currency
    slide = _new_slide(prs, "Net sales by day, %s" % cur.name)
    days = max(cur.days_in_month, prev.days_in_month if prev else 0)
    cats = [str(d) for d in range(1, days + 1)]

    def values(stats: MonthStats) -> list[float | None]:
        got = [_major(v, cc) for _, v in daily_series(stats, "net_sales")]
        return got + [None] * (days - len(got))

    series = [(cur.name, values(cur), ACCENT)]
    if prev is not None:
        series.append((prev.name, values(prev), GREY))
    _chart(slide, XL_CHART_TYPE.LINE, cats, series, Inches(1.5), Inches(4.9), "#,##0",
           "Line chart of net sales for each day of %s%s" % (cur.name, ", with %s as a grey line" % prev.name if prev else ""))
    _footer(slide, "Day of the month on the horizontal axis, net sales in %s. %s" % (cc or "", _series_note(cur)))


def _orders_slide(prs, report: MonthlyReport) -> None:
    cur = report.current
    slide = _new_slide(prs, "Orders by day, %s" % cur.name)
    points = daily_series(cur, "orders")
    _chart(slide, XL_CHART_TYPE.COLUMN_CLUSTERED, [str(d.day) for d, _ in points],
           [("Orders", [v for _, v in points], ACCENT)], Inches(1.5), Inches(4.9), "#,##0",
           "Column chart of orders for each day of %s" % cur.name)
    gap = ("Days with no snapshot and no sync run: %s." % days_text(cur.missing_days) if cur.missing_days
           else "Every day in the period has a snapshot or a sync run that found no orders.")
    _footer(slide, "Day of the month on the horizontal axis. A day with no column had no orders or no data. %s" % gap)


def _value_slide(prs, report: MonthlyReport) -> None:
    cur, prev = report.current, report.previous
    slide = _new_slide(prs, _claim(report, "avg_order_value"))
    head = ["Metric", cur.name, prev.name if prev else "Previous month", "Change", "Change %"]
    rows = [head] + [[k.label, k.current_display, k.previous_display, k.change_display, k.pct_display]
                     for k in report.kpis if k.key in ("avg_order_value", "buyers_per_day")]
    _table(slide, Inches(1.6), rows, [3.4, 2.4, 2.4, 2.0, 1.4], size=16, row_h=0.55)
    _text(slide, MARGIN, Inches(3.55), BODY_W, Inches(0.4), ["Best days by net sales"], 18, NAVY)
    top = [["Rank", "Day", "Net sales"]] + [
        [str(i + 1), day.isoformat(), format_money(value, report.currency or "")]
        for i, (day, value) in enumerate(report.top_days)
    ]
    _table(slide, Inches(4.0), top, [1.0, 3.0, 3.0], size=16, row_h=0.5, left_cols=2)
    _footer(slide, "Average order value is gross sales over orders for the month. Buyers per active day is the "
                   "mean of the daily distinct buyer counts, which cannot be summed across days.")


def _insights_slide(prs, insights: Insights) -> None:
    slide = _new_slide(prs, "What the numbers say")
    _text(slide, MARGIN, Inches(1.6), BODY_W, Inches(4.4), list(insights.lines), 20, INK, space_after=12)
    _text(slide, MARGIN, Inches(6.3), BODY_W, Inches(0.6), [insights.label], 13, MUTED, italic=True)


def _health_slide(prs, report: MonthlyReport) -> None:
    cur, prev = report.current, report.previous
    slide = _new_slide(prs, "Data health: %d of %d days have data in %s" % (len(cur.data_days), cur.days_expected, cur.name))

    def span(stats: MonthStats | None) -> str:
        if stats is None:
            return "n/a"
        return "%s to %s" % (stats.data_days[0].isoformat(), stats.data_days[-1].isoformat()) if stats.data_days else "none"

    def col(stats: MonthStats | None) -> list[str]:
        if stats is None:
            return ["n/a"] * 6
        return [str(stats.days_expected), str(len(stats.data_days)), str(len(stats.quiet_days)),
                str(len(stats.missing_days)), str(stats.rows_used), span(stats)]

    labels = ["Days expected", "Days with snapshots", "Days with no orders (a sync ran)",
              "Days missing (no snapshot, no sync)", "Snapshot rows used", "First and last day with data"]
    a, b = col(cur), col(prev)
    rows = [["", cur.name, prev.name if prev else "Previous month"]] + [[l, x, y] for l, x, y in zip(labels, a, b)]
    _table(slide, Inches(1.5), rows, [4.2, 3.2, 3.2], size=15, row_h=0.46)
    notes = ["Missing days in %s: %s." % (cur.name, days_text(cur.missing_days))]
    if prev is not None:
        notes.append("Missing days in %s: %s." % (prev.name, days_text(prev.missing_days)))
    else:
        notes.append("No earlier month is stored, so there is nothing to compare against.")
    if not cur.complete:
        notes.append("The month is not over: %d of %d days are included." % (cur.days_expected, cur.days_in_month))
    if cur.missing_days or not cur.complete or (prev is not None and prev.missing_days):
        notes.append("Totals for a month with missing days are understated, so its changes are marked partial data.")
    _text(slide, MARGIN, Inches(5.0), BODY_W, Inches(1.8), notes, 15, INK, space_after=4)
    _footer(slide, "A day counts as a quiet day only when a succeeded or partial sync run's window covers it.")


# ---------------------------------------------------------------- public

def build_deck(report: MonthlyReport, insights: Insights, *, data_note: str = "") -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    _title_slide(prs, report, data_note)
    _kpi_slide(prs, report)
    _sales_slide(prs, report)
    _orders_slide(prs, report)
    _value_slide(prs, report)
    _insights_slide(prs, insights)
    _health_slide(prs, report)
    props = prs.core_properties
    props.title = "Monthly performance report, %s" % report.current.name
    props.author = props.last_modified_by = "api-analytics-hub"
    props.comments = props.keywords = props.subject = ""
    # The library's template carries a 2013 timestamp, which reads as a stale file.
    props.created = props.modified = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
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
                cats = list(chart.plots[0].categories)
                for s in chart.plots[0].series:
                    out.append("chart series %r (%d points): %s" % (s.name, len(cats), list(s.values)))
            elif shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(cell.text for cell in row.cells))
            elif shape.has_text_frame and shape.text_frame.text.strip():
                out.extend(p.text for p in shape.text_frame.paragraphs if p.text.strip())
    return "\n".join(out)
