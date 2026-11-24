"""An Excel workbook of the inventory plan whose figures are LIVE FORMULAS, so a planner can change an assumption and watch the plan move without the hub.

FOUR SHEETS.
  Inputs     the plan date, the service level, the review period, a promotion factor with its start and end date, and one row for each supplier (lead time, its deviation, minimum order,
             pack size). Every one of these is an editable cell.
  Plan       one row for each variant. The velocity, its deviation, the stock on hand, the inbound quantity and its date are values the hub measured. Days of cover, the supplier's figures
             (looked up by name with INDEX and MATCH), safety stock, reorder point, stock position, the suggested order and the shortage flag are formulas that read the Inputs cells.
  Classes    the A, B and C classes and the best seller, rising, slow mover and steady labels, counted and summed with COUNTIFS and SUMIFS over the Plan sheet.
  Scenario   the reorder point and the order before and after the promotion factor, so changing the factor on Inputs shows its cost in units.

THE FORMULAS ARE THE PLAN'S RULES WRITTEN IN EXCEL (app/inventory/model.py), and they must give the same figures: safety stock = z * sqrt(L * sd^2 + d^2 * sdL^2) rounded up, reorder point =
demand over the lead time + safety stock rounded up, order = (demand over lead time plus review period + safety stock) less the stock position, raised to the minimum and to whole packs, with
the promotion factor applied to the days it covers. The shortage flag is the closed form of the projected cover: the stock lasts less than the lead time when the demand over the lead
time is more than the stock, plus an arrival that lands before the stock would run out. scripts/inventory_workbook_check.py recalculates the file with LibreOffice and compares every
computed cell with the figures the Python plan gives for the same inputs (tests/test_inventory_workbook.py runs it, and a change of one unit in an input fails it).

WHAT IT DOES NOT CARRY. One promotion for every variant: a promotion entered in the hub for a single variant is not in the file. One inbound quantity and its earliest expected date for
a variant: a variant with several open orders on different dates shows the total at the earliest date. No seasonality, as in the plan. Variants the shop does not track, and variants whose
stock was never read, are left out. The values the hub measured are as of the plan date; the file does not refresh itself.
"""
from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

SHEETS = ("Inputs", "Plan", "Classes", "Scenario")
LABEL_TEXT = {"best_seller": "best seller", "rising": "rising", "slow_mover": "slow mover", "steady": "steady"}
SHORTAGE_TEXT = {"out_of_stock": "out of stock", "cover_below_lead_time": "cover under lead time", "stockout_projected": "stockout projected with the promotion"}
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP"}

# the cells of the Inputs sheet the formulas read
ASOF, SERVICE, REVIEW, PROMO_FACTOR, PROMO_START, PROMO_END, ZSCORE = ("Inputs!$B$%d" % n for n in (3, 4, 5, 6, 7, 8, 9))
SUPPLIER_HEADER_ROW = 12
SUPPLIER_FIRST_ROW = 13

PLAN_COLUMNS = (
    ("sku", "SKU"), ("name", "Product"), ("supplier", "Supplier"), ("abc", "Class"), ("label", "Label"), ("revenue", "Revenue"),
    ("velocity", "Daily velocity"), ("sd", "Demand deviation"), ("onhand", "On hand"), ("inbound", "Inbound"), ("arrives", "Inbound expected"),
    ("moq_own", "Min order (this variant)"), ("pack_own", "Pack (this variant)"),
    ("lead", "Lead time"), ("lead_sd", "Lead time deviation"), ("moq", "Min order"), ("pack", "Pack size"), ("cover", "Days of cover"),
    ("pdays_lead", "Promotion days in lead time"), ("factor", "Promotion factor over lead time"), ("lead_demand", "Demand over lead time"), ("safety", "Safety stock"),
    ("rop", "Reorder point"), ("position", "Stock position"), ("needs", "Needs order"), ("pdays_horizon", "Promotion days to cover"), ("target", "Target stock"),
    ("raw", "Quantity before pack rounding"), ("order", "Suggested order"), ("arr_qty", "Inbound counted for cover"), ("arr_days", "Days to arrival"),
    ("arr_demand", "Demand until arrival"), ("shortage", "Shortage"),
)
COL = {key: get_column_letter(i + 1) for i, (key, _h) in enumerate(PLAN_COLUMNS)}
VALUE_KEYS = ("sku", "name", "supplier", "abc", "label", "revenue", "velocity", "sd", "onhand", "inbound", "arrives", "moq_own", "pack_own")
PLAN_FIRST_ROW = 2

SCENARIO_FIRST_ROW = 6
SCENARIO_COLUMNS = ("SKU", "Product", "Supplier", "Safety stock before", "Safety stock after", "Reorder point before", "Reorder point after", "Order before", "Order after",
                    "Reorder point change", "Order change")

CLASS_ROWS = ("A", "B", "C", "best seller", "rising", "slow mover", "steady")


@dataclass(frozen=True)
class SupplierRow:
    name: str
    lead_time_days: int
    lead_time_sd_days: float
    moq: int
    pack_size: int


@dataclass(frozen=True)
class VariantRow:
    sku: str
    name: str
    supplier: str | None
    abc: str
    label: str                       # best seller, rising, slow mover or steady
    revenue: float
    velocity: float
    sd: float
    onhand: int
    inbound: int = 0
    arrives: date | None = None
    #: A minimum order or pack that differs from the supplier's row; None means the variant uses the supplier's.
    moq_own: int | None = None
    pack_own: int | None = None


@dataclass(frozen=True)
class WorkbookInputs:
    as_of: date
    service_level: float
    review_days: int
    promo_factor: float
    promo_start: date
    promo_end: date
    suppliers: Sequence[SupplierRow]
    variants: Sequence[VariantRow]
    currency: str | None = None
    notes: Sequence[str] = field(default_factory=tuple)
    #: The hub's variant id of each row of `variants`, when the rows came from the store (so a check can pair each row with the same variant's inputs).
    variant_ids: Sequence[str] = field(default_factory=tuple)

    def validate(self) -> None:
        if not 0.5 <= self.service_level < 1:
            raise ValueError("service level must be at least 0.5 and under 1")
        if self.review_days < 0 or self.promo_factor <= 0:
            raise ValueError("the review period cannot be negative and the promotion factor must be above zero")
        names = [s.name for s in self.suppliers]
        if len(set(names)) != len(names):
            raise ValueError("two suppliers share a name")
        for v in self.variants:
            if v.supplier is not None and v.supplier not in names:
                raise ValueError("variant %s names a supplier that has no row" % v.sku)
        if any(s.moq < 1 or s.pack_size < 1 for s in self.suppliers):
            raise ValueError("a minimum order and a pack size are at least 1")


# ------------------------------------------------------------------------------------------------------- the formulas

def _ref(key: str, row: int) -> str:
    """An absolute-column reference to a Plan cell, as written in a formula on the same sheet."""
    return "$%s%d" % (COL[key], row)


def _plan_ref(key: str, row: int) -> str:
    return "Plan!$%s%d" % (COL[key], row)


def _lookup(source_col: str, key_cell: str, last_row: int) -> str:
    return "INDEX(Inputs!$%s$%d:$%s$%d,MATCH(%s,Inputs!$A$%d:$A$%d,0))" % (source_col, SUPPLIER_FIRST_ROW, source_col, last_row, key_cell, SUPPLIER_FIRST_ROW, last_row)


def plan_formulas(r: int, last_supplier_row: int) -> dict[str, str]:
    """The formula of every computed Plan column for row `r`. A variant with no supplier has no lead time, so its supplier-dependent cells are blank."""
    c = lambda key: _ref(key, r)               # noqa: E731
    blank = '%s=""' % c("lead")
    f: dict[str, str] = {}
    f["lead"] = '=IF(%s="","",%s)' % (c("supplier"), _lookup("B", c("supplier"), last_supplier_row))
    f["lead_sd"] = '=IF(%s="","",%s)' % (c("supplier"), _lookup("C", c("supplier"), last_supplier_row))
    f["moq"] = '=IF(%s="","",IF(%s="",%s,%s))' % (c("supplier"), c("moq_own"), _lookup("D", c("supplier"), last_supplier_row), c("moq_own"))
    f["pack"] = '=IF(%s="","",IF(%s="",%s,%s))' % (c("supplier"), c("pack_own"), _lookup("E", c("supplier"), last_supplier_row), c("pack_own"))
    f["cover"] = '=IF(%s<=0,"",MAX(%s,0)/%s)' % (c("velocity"), c("onhand"), c("velocity"))
    f["pdays_lead"] = "=IF(%s,\"\",MAX(0,MIN(%s+%s-1,%s)-MAX(%s,%s)+1))" % (blank, ASOF, c("lead"), PROMO_END, ASOF, PROMO_START)
    f["factor"] = "=IF(%s,\"\",IF(%s*%s>0,%s/(%s*%s),1))" % (blank, c("velocity"), c("lead"), c("lead_demand"), c("velocity"), c("lead"))
    f["lead_demand"] = "=IF(%s,\"\",%s*(%s+%s*(%s-1)))" % (blank, c("velocity"), c("lead"), c("pdays_lead"), PROMO_FACTOR)
    f["safety"] = "=IF(%s,\"\",ROUNDUP(ROUND(%s*SQRT(%s*(%s*%s)^2+(%s*%s)^2*%s^2),9),0))" % (
        blank, ZSCORE, c("lead"), c("sd"), c("factor"), c("velocity"), c("factor"), c("lead_sd"))
    f["rop"] = "=IF(%s,\"\",ROUNDUP(ROUND(%s+%s,9),0))" % (blank, c("lead_demand"), c("safety"))
    f["position"] = "=%s+%s" % (c("onhand"), c("inbound"))
    f["needs"] = "=IF(%s,\"\",AND(%s>0,%s<=%s))" % (blank, c("velocity"), c("position"), c("rop"))
    f["pdays_horizon"] = "=IF(%s,\"\",MAX(0,MIN(%s+%s+%s-1,%s)-MAX(%s,%s)+1))" % (blank, ASOF, c("lead"), REVIEW, PROMO_END, ASOF, PROMO_START)
    f["target"] = "=IF(%s,\"\",%s*(%s+%s+%s*(%s-1))+%s)" % (blank, c("velocity"), c("lead"), REVIEW, c("pdays_horizon"), PROMO_FACTOR, c("safety"))
    f["raw"] = "=IF(%s,\"\",ROUNDUP(ROUND(%s-%s,9),0))" % (blank, c("target"), c("position"))
    f["order"] = "=IF(%s,\"\",IF(%s,IF(%s<=0,0,CEILING(MAX(%s,%s),%s)),0))" % (blank, c("needs"), c("raw"), c("raw"), c("moq"), c("pack"))
    f["arr_qty"] = '=IF(AND(%s<>"",%s>=%s),%s,0)' % (c("arrives"), c("arrives"), ASOF, c("inbound"))
    f["arr_days"] = '=IF(%s>0,%s-%s,"")' % (c("arr_qty"), c("arrives"), ASOF)
    f["arr_demand"] = '=IF(%s>0,%s*(%s+(%s-1)*MAX(0,MIN(%s-1,%s-%s)-MAX(0,%s-%s)+1)),"")' % (
        c("arr_qty"), c("velocity"), c("arr_days"), PROMO_FACTOR, c("arr_days"), PROMO_END, ASOF, PROMO_START, ASOF)
    # cover under the lead time: with an arrival, the lead time's demand exceeds the stock plus the arrival if the shelf lasts until it lands; without one, the cover is under the lead time
    plain = "IF(%s>0,%s*%s>%s+IF(%s*%s<%s,%s,0),%s/%s<%s)" % (c("arr_qty"), c("velocity"), c("lead"), c("onhand"), c("velocity"), c("arr_days"), c("onhand"), c("arr_qty"),
                                                              c("onhand"), c("velocity"), c("lead"))
    promoted = "%s>%s+IF(%s>0,IF(%s<%s,%s,0),0)" % (c("lead_demand"), c("onhand"), c("arr_qty"), c("arr_demand"), c("onhand"), c("arr_qty"))
    f["shortage"] = '=IF(%s<=0,"",IF(%s<=0,"%s",IF(%s,"",IF(%s,"%s",IF(%s,"%s","")))))' % (
        c("velocity"), c("onhand"), SHORTAGE_TEXT["out_of_stock"], blank, plain, SHORTAGE_TEXT["cover_below_lead_time"], promoted, SHORTAGE_TEXT["stockout_projected"])
    return f


def scenario_formulas(plan_row: int, row: int) -> dict[str, str]:
    """The Scenario formulas for one variant: before is the promotion factor 1 (no promotion), after is the Plan sheet, which reads the factor on Inputs."""
    p = lambda key: _plan_ref(key, plan_row)    # noqa: E731
    no_lead = '%s=""' % p("lead")
    ss_before = "ROUNDUP(ROUND(%s*SQRT(%s*%s^2+%s^2*%s^2),9),0)" % (ZSCORE, p("lead"), p("sd"), p("velocity"), p("lead_sd"))
    f = {
        "ss_before": '=IF(%s,"",%s)' % (no_lead, ss_before),
        "ss_after": '=IF(%s,"",%s)' % (no_lead, p("safety")),
        "rop_before": '=IF(%s,"",ROUNDUP(ROUND(%s*%s+$D%d,9),0))' % (no_lead, p("velocity"), p("lead"), row),
        "rop_after": '=IF(%s,"",%s)' % (no_lead, p("rop")),
    }
    raw_before = "ROUNDUP(ROUND(%s*(%s+%s)+$D%d-%s,9),0)" % (p("velocity"), p("lead"), REVIEW, row, p("position"))
    f["order_before"] = '=IF(%s,"",IF(AND(%s>0,%s<=$F%d),IF(%s<=0,0,CEILING(MAX(%s,%s),%s)),0))' % (no_lead, p("velocity"), p("position"), row, raw_before, raw_before, p("moq"), p("pack"))
    f["order_after"] = '=IF(%s,"",%s)' % (no_lead, p("order"))
    f["rop_change"] = '=IF(%s,"",$G%d-$F%d)' % (no_lead, row, row)
    f["order_change"] = '=IF(%s,"",$I%d-$H%d)' % (no_lead, row, row)
    return f


# ------------------------------------------------------------------------------------------------------- the file

HEADER_FONT = Font(bold=True)
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
NOTE_FONT = Font(italic=True, color="666666")


def _style_header(ws: Worksheet, row: int, count: int) -> None:
    for col in range(1, count + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _inputs_sheet(wb: Workbook, data: WorkbookInputs) -> int:
    ws = wb.active
    ws.title = "Inputs"
    ws["A1"] = "Inventory plan inputs"
    ws["A1"].font = Font(bold=True, size=14)
    rows = [
        (3, "As of", data.as_of, "yyyy-mm-dd", "The plan date. The stock and velocity values on the Plan sheet were measured for it."),
        (4, "Service level", data.service_level, "0.0%", "The chance of not stocking out between an order and its arrival. Safety stock uses its normal quantile."),
        (5, "Review period (days)", data.review_days, "0", "The cover an order buys beyond the lead time."),
        (6, "Promotion factor", data.promo_factor, "0.00", "1.5 means half as much again as a normal day. 1 is no promotion. Applies to every variant."),
        (7, "Promotion starts", data.promo_start, "yyyy-mm-dd", "The first day the factor applies."),
        (8, "Promotion ends", data.promo_end, "yyyy-mm-dd", "The last day the factor applies."),
    ]
    for r, label, value, fmt, note in rows:
        ws.cell(row=r, column=1, value=label)
        cell = ws.cell(row=r, column=2, value=value)
        cell.number_format = fmt
        cell.fill = INPUT_FILL
        ws.cell(row=r, column=3, value=note).font = NOTE_FONT
    ws["A9"] = "Safety factor z"
    ws["B9"] = "=NORMSINV(B4)"
    ws["B9"].number_format = "0.0000"
    ws["C9"] = "The normal quantile of the service level, worked out from it."
    ws["C9"].font = NOTE_FONT
    ws["A11"] = "Suppliers"
    ws["A11"].font = HEADER_FONT
    for i, h in enumerate(("Supplier", "Lead time (days)", "Lead time deviation (days)", "Min order", "Pack size")):
        ws.cell(row=SUPPLIER_HEADER_ROW, column=i + 1, value=h)
    _style_header(ws, SUPPLIER_HEADER_ROW, 5)
    for n, s in enumerate(data.suppliers):
        r = SUPPLIER_FIRST_ROW + n
        for col, value in enumerate((s.name, s.lead_time_days, s.lead_time_sd_days, s.moq, s.pack_size), start=1):
            cell = ws.cell(row=r, column=col, value=value)
            cell.fill = INPUT_FILL
    last = SUPPLIER_FIRST_ROW + max(len(data.suppliers), 1) - 1
    note_row = last + 2
    for n, text in enumerate(data.notes):
        ws.cell(row=note_row + n, column=1, value=text).font = NOTE_FONT
    ws.column_dimensions["A"].width = 26
    for col in "BCDE":
        ws.column_dimensions[col].width = 18
    ws.column_dimensions["C"].width = 26
    return last


def _plan_sheet(wb: Workbook, data: WorkbookInputs, last_supplier_row: int) -> int:
    ws = wb.create_sheet("Plan")
    for i, (_key, header) in enumerate(PLAN_COLUMNS):
        ws.cell(row=1, column=i + 1, value=header)
    _style_header(ws, 1, len(PLAN_COLUMNS))
    for n, v in enumerate(data.variants):
        r = PLAN_FIRST_ROW + n
        values = {"sku": v.sku, "name": v.name, "supplier": v.supplier, "abc": v.abc, "label": v.label, "revenue": v.revenue, "velocity": v.velocity, "sd": v.sd,
                  "onhand": v.onhand, "inbound": v.inbound, "arrives": v.arrives, "moq_own": v.moq_own, "pack_own": v.pack_own}
        for key in VALUE_KEYS:
            ws["%s%d" % (COL[key], r)] = values[key]
        for key, formula in plan_formulas(r, last_supplier_row).items():
            ws["%s%d" % (COL[key], r)] = formula
        ws["%s%d" % (COL["arrives"], r)].number_format = "yyyy-mm-dd"
        for key in ("velocity", "sd", "cover", "lead_demand", "target", "factor"):
            ws["%s%d" % (COL[key], r)].number_format = "0.00"
        ws["%s%d" % (COL["revenue"], r)].number_format = "#,##0.00"
        for key in ("moq_own", "pack_own"):
            ws["%s%d" % (COL[key], r)].fill = INPUT_FILL
    ws.freeze_panes = "C2"
    for i, (key, _h) in enumerate(PLAN_COLUMNS):
        ws.column_dimensions[get_column_letter(i + 1)].width = 34 if key == "name" else 20 if key in ("shortage", "supplier") else 14
    return PLAN_FIRST_ROW + len(data.variants) - 1


def _classes_sheet(wb: Workbook, last_plan_row: int) -> None:
    ws = wb.create_sheet("Classes")
    ws["A1"] = "Classes and labels"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Counted and summed from the Plan sheet with COUNTIFS and SUMIFS."
    ws["A2"].font = NOTE_FONT
    headers = ("Group", "Variants", "Revenue", "Sales share", "On hand", "Variants to order", "Units to order")
    for i, h in enumerate(headers):
        ws.cell(row=3, column=i + 1, value=h)
    _style_header(ws, 3, len(headers))
    last = max(last_plan_row, PLAN_FIRST_ROW)
    rng = lambda key: "Plan!$%s$%d:$%s$%d" % (COL[key], PLAN_FIRST_ROW, COL[key], last)    # noqa: E731
    for n, group in enumerate(CLASS_ROWS):
        r = 4 + n
        by = rng("abc") if group in ("A", "B", "C") else rng("label")
        ws.cell(row=r, column=1, value=group)
        ws.cell(row=r, column=2, value="=COUNTIFS(%s,$A%d)" % (by, r))
        ws.cell(row=r, column=3, value="=SUMIFS(%s,%s,$A%d)" % (rng("revenue"), by, r))
        ws.cell(row=r, column=4, value="=IF(SUM(%s)=0,0,$C%d/SUM(%s))" % (rng("revenue"), r, rng("revenue")))
        ws.cell(row=r, column=5, value="=SUMIFS(%s,%s,$A%d)" % (rng("onhand"), by, r))
        ws.cell(row=r, column=6, value="=COUNTIFS(%s,$A%d,%s,TRUE)" % (by, r, rng("needs")))
        ws.cell(row=r, column=7, value="=SUMIFS(%s,%s,$A%d)" % (rng("order"), by, r))
        ws.cell(row=r, column=3).number_format = "#,##0.00"
        ws.cell(row=r, column=4).number_format = "0.0%"
    ws.column_dimensions["A"].width = 18
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 18


def _scenario_sheet(wb: Workbook, data: WorkbookInputs, last_plan_row: int) -> None:
    ws = wb.create_sheet("Scenario")
    ws["A1"] = "Promotion scenario"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Promotion factor on Inputs"
    ws["B2"] = "=%s" % PROMO_FACTOR
    ws["A3"] = "Before is a factor of 1 (no promotion). After reads the Plan sheet. Change the factor, start or end on Inputs and the After columns move."
    ws["A3"].font = NOTE_FONT
    for i, h in enumerate(SCENARIO_COLUMNS):
        ws.cell(row=5, column=i + 1, value=h)
    _style_header(ws, 5, len(SCENARIO_COLUMNS))
    keys = ("ss_before", "ss_after", "rop_before", "rop_after", "order_before", "order_after", "rop_change", "order_change")
    for n, _v in enumerate(data.variants):
        plan_row = PLAN_FIRST_ROW + n
        r = SCENARIO_FIRST_ROW + n
        ws.cell(row=r, column=1, value="=Plan!$%s%d" % (COL["sku"], plan_row))
        ws.cell(row=r, column=2, value="=Plan!$%s%d" % (COL["name"], plan_row))
        ws.cell(row=r, column=3, value='=IF(Plan!$%s%d="","",Plan!$%s%d)' % (COL["supplier"], plan_row, COL["supplier"], plan_row))
        forms = scenario_formulas(plan_row, r)
        for i, key in enumerate(keys):
            ws.cell(row=r, column=4 + i, value=forms[key])
    total = SCENARIO_FIRST_ROW + len(data.variants)
    ws.cell(row=total, column=1, value="Total units").font = HEADER_FONT
    for col in ("H", "I", "K"):
        ws["%s%d" % (col, total)] = "=SUM(%s%d:%s%d)" % (col, SCENARIO_FIRST_ROW, col, max(total - 1, SCENARIO_FIRST_ROW))
        ws["%s%d" % (col, total)].font = HEADER_FONT
    ws.freeze_panes = "D6"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 20
    for col in "DEFGHIJK":
        ws.column_dimensions[col].width = 16


def build_workbook(data: WorkbookInputs) -> bytes:
    """The workbook as .xlsx bytes. The figures on Plan, Classes and Scenario are formulas; a spreadsheet program calculates them when it opens the file."""
    data.validate()
    wb = Workbook()
    last_supplier_row = _inputs_sheet(wb, data)
    last_plan_row = _plan_sheet(wb, data, last_supplier_row)
    _classes_sheet(wb, last_plan_row)
    _scenario_sheet(wb, data, last_plan_row)
    for ws in wb.worksheets:                      # printed or saved as a PDF, every sheet is one page wide
        ws.page_setup.orientation = "landscape"
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def filename(as_of: date) -> str:
    return "inventory-plan-%s.xlsx" % as_of.isoformat()


# ------------------------------------------------------------------------------------------------------- the inputs, from the model and from the store

def _major(minor: int, currency: str | None) -> float:
    exponent = 0 if (currency or "").upper() in ZERO_DECIMAL else 2
    return float(Decimal(minor) / (10 ** exponent))


def _supplier_rows(entries: Sequence[tuple[str, int, float, int, int]]) -> tuple[list[SupplierRow], dict[str, tuple[int, int]]]:
    """One row for each supplier from (supplier, lead time, deviation, moq, pack) of its variants. The minimum order and pack are the most common among its variants (the smaller on a tie),
    and a variant that differs keeps its own."""
    by: dict[str, list[tuple[int, float, int, int]]] = {}
    for name, lead, sd, moq, pack in entries:
        by.setdefault(name, []).append((lead, sd, moq, pack))
    rows: list[SupplierRow] = []
    defaults: dict[str, tuple[int, int]] = {}
    for name in sorted(by):
        group = by[name]
        moq = sorted(Counter(g[2] for g in group).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        pack = sorted(Counter(g[3] for g in group).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rows.append(SupplierRow(name, group[0][0], group[0][1], moq, pack))
        defaults[name] = (moq, pack)
    return rows, defaults


def inputs_from_lines(lines: Sequence[Any], *, as_of: date, service_level: float, review_days: int, promo_factor: float, promo_start: date, promo_end: date,
                      notes: Sequence[str] = ()) -> WorkbookInputs:
    """The workbook inputs from stored plan lines joined with their variant's minimum order and pack (`moq`, `pack_size`). Only a variant the plan could read is included."""
    use = [l for l in lines if l["status"] in ("ok", "no_supplier")]
    entries = [(l["supplier_name"], l["lead_time_days"], float(l["lead_time_sd_days"] or 0.0), l["moq"], l["pack_size"]) for l in use if l["supplier_name"] and l["lead_time_days"] is not None]
    suppliers, defaults = _supplier_rows(entries)
    variants: list[VariantRow] = []
    for l in use:
        name = l["product_title"] + ("" if l["variant_title"] in ("", "Default Title") else " - " + l["variant_title"])
        supplied = bool(l["supplier_name"]) and l["lead_time_days"] is not None
        d_moq, d_pack = defaults.get(l["supplier_name"], (None, None)) if supplied else (None, None)
        currency = l["currency"].strip() if l["currency"] else None
        variants.append(VariantRow(
            sku=l["sku"] or name, name=name, supplier=l["supplier_name"] if supplied else None, abc=l["abc_class"], label=LABEL_TEXT[l["label"]],
            revenue=_major(int(l["revenue_minor"]), currency), velocity=float(l["velocity"]), sd=float(l["velocity_sd"]), onhand=int(l["available"]), inbound=int(l["incoming"]),
            arrives=l["inbound_expected_on"], moq_own=l["moq"] if supplied and l["moq"] != d_moq else None, pack_own=l["pack_size"] if supplied and l["pack_size"] != d_pack else None))
    currency = next((l["currency"].strip() for l in use if l["currency"]), None)
    return WorkbookInputs(as_of, service_level, review_days, promo_factor, promo_start, promo_end, suppliers, variants, currency, tuple(notes), tuple(str(l["variant_id"]) for l in use))


def inputs_from_plan(items: Sequence[Any], rows: Sequence[Any], names: Sequence[str], suppliers: Sequence[str | None], *, as_of: date, service_level: float, review_days: int,
                     promo_factor: float, promo_start: date, promo_end: date) -> WorkbookInputs:
    """The workbook inputs from the model's own objects: each VariantInput beside the PlanRow it gave, its display name and its supplier's name. Used by the tests and the check, so
    the figures the workbook is compared with come from `plan_variant` itself."""
    entries = [(supplier, item.lead_time_days, float(item.lead_time_sd_days), item.moq, item.pack_size)
               for item, supplier in zip(items, suppliers) if supplier and item.lead_time_days is not None]
    suppliers_rows, defaults = _supplier_rows(entries)
    variants = []
    for item, row, name, supplier in zip(items, rows, names, suppliers):
        supplied = bool(supplier) and item.lead_time_days is not None
        d_moq, d_pack = defaults.get(supplier, (None, None)) if supplied else (None, None)
        variants.append(VariantRow(
            sku=item.key, name=name, supplier=supplier if supplied else None, abc=row.abc_class, label=LABEL_TEXT[row.label], revenue=item.revenue_minor / 100,
            velocity=row.velocity.mean, sd=row.velocity.sd, onhand=item.available, inbound=row.incoming, arrives=row.inbound_expected_on,
            moq_own=item.moq if supplied and item.moq != d_moq else None, pack_own=item.pack_size if supplied and item.pack_size != d_pack else None))
    return WorkbookInputs(as_of, service_level, review_days, promo_factor, promo_start, promo_end, suppliers_rows, variants)
