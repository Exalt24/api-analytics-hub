"""The plan workbook (app/inventory/workbook.py): its structure, and that its formulas reproduce the Python plan.

The recalculation tests build the workbook, open it in LibreOffice headless (or the `formulas` package when LibreOffice is not installed) and compare EVERY computed cell with what
`model.plan_variant` gives for the same inputs: the lead time and pack looked up from the supplier row, days of cover, safety stock, reorder point, stock position, the order, the
shortage flag, the Scenario before and after columns and the Classes totals. Worlds are seeded and random (suppliers, minimums, packs, promotion window, open orders on and past their dates).
They skip when no engine is installed, unless WORKBOOK_REQUIRE_RECALC=1 (CI sets it), which makes a missing engine a failure.
"""
from __future__ import annotations

import io
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.inventory import model, workbook, workbook_check  # noqa: E402
from app.inventory.model import PlanParams, Promo, VariantInput, date_range  # noqa: E402
from app.inventory.orders import Arrival  # noqa: E402
from app.inventory.workbook import COL, PLAN_FIRST_ROW, SCENARIO_FIRST_ROW, SupplierRow, VariantRow, WorkbookInputs  # noqa: E402

AS_OF = date(2026, 11, 24)
WINDOW = date_range(AS_OF - timedelta(days=28), AS_OF)
ENGINE = workbook_check.engine_name()

if os.environ.get("WORKBOOK_REQUIRE_RECALC") == "1" and ENGINE is None:
    raise RuntimeError("WORKBOOK_REQUIRE_RECALC=1 but neither LibreOffice nor the formulas package is installed")

needs_engine = pytest.mark.skipif(ENGINE is None, reason="no spreadsheet engine: install LibreOffice (soffice) or the formulas package")


def world(seed: int, count: int = 12):
    """A random plan input: variants with sales and stock history, three suppliers with their own lead times, minimums and packs, one promotion for all variants, open orders."""
    rng = random.Random(seed)
    suppliers = {"Acme Goods": (rng.randint(5, 40), rng.choice((0.0, 2.0, 6.0))), "Borealis Textiles": (rng.randint(10, 60), rng.choice((0.0, 3.0))), "Cobalt Co": (rng.randint(1, 20), 1.5)}
    items, names, makers = [], [], []
    for i in range(count):
        sales = {d: rng.randint(1, 8) for d in WINDOW if rng.random() < rng.choice((0.2, 0.5, 0.9))}
        prior = {AS_OF - timedelta(days=28 + k): rng.randint(1, 5) for k in range(1, 28) if rng.random() < 0.4}
        snaps = {d: rng.choice((0, 5, 30)) for d in WINDOW if rng.random() < rng.choice((0.0, 0.6, 1.0))}
        supplier = rng.choice((*suppliers, None, "Acme Goods", "Acme Goods"))
        arrival = ()
        if rng.random() < 0.4:
            arrival = (Arrival(rng.randint(1, 90), rng.choice((None, AS_OF - timedelta(days=rng.randint(1, 8)), AS_OF + timedelta(days=rng.randint(0, 70)), AS_OF + timedelta(days=rng.randint(0, 12))))),)
        incoming = rng.choice((0, 0, 25)) if not arrival else 0
        items.append(VariantInput(
            key="SKU-%02d" % i, available=rng.choice((0, rng.randint(1, 12), rng.randint(5, 120))), incoming=incoming, snapshots=snaps, sales={**sales, **prior},
            units_all=sum(sales.values()) + sum(prior.values()), revenue_minor=rng.randint(0, 900000), lead_time_days=suppliers[supplier][0] if supplier else None,
            lead_time_sd_days=suppliers[supplier][1] if supplier else 0.0, moq=rng.choice((1, 1, 24, 50)), pack_size=rng.choice((1, 6, 12)), arrivals=arrival))
        names.append("Product %02d" % i)
        makers.append(supplier)
    params = PlanParams(service_level=rng.choice((0.9, 0.95, 0.99)), review_days=rng.choice((7, 14, 30)))
    start = AS_OF + timedelta(days=rng.randint(-5, 20))
    promos = (Promo(start, start + timedelta(days=rng.randint(0, 25)), rng.choice((1.0, 1.5, 2.0, 0.8))),)
    return items, names, makers, params, promos


def build(seed: int, count: int = 12):
    items, names, makers, params, promos = world(seed, count)
    after, before = workbook_check.expected_rows(items, AS_OF, params, promos)
    data = workbook.inputs_from_plan(items, after, names, makers, as_of=AS_OF, service_level=params.service_level, review_days=params.review_days,
                                     promo_factor=promos[0].factor, promo_start=promos[0].starts_on, promo_end=promos[0].ends_on)
    return items, params, promos, data


def hand_case():
    """The case in docs/inventory-planning.md: sells 2 a day (sd 1), 10 on the shelf, a 9 day lead time, minimum 50, packs of 12, 95 percent, review 14 days."""
    sales = {d: (4 if i % 2 == 0 else 0) for i, d in enumerate(WINDOW)}
    item = VariantInput(key="TEE-1", available=10, snapshots={d: 9 for d in WINDOW}, sales={d: q for d, q in sales.items() if q}, units_all=56, revenue_minor=56000,
                        lead_time_days=9, moq=50, pack_size=12)
    params = PlanParams()
    promos = (Promo(AS_OF + timedelta(days=2), AS_OF + timedelta(days=30), 1.5),)
    after, before = workbook_check.expected_rows([item], AS_OF, params, promos)
    data = workbook.inputs_from_plan([item], after, ["Tee"], ["Acme Goods"], as_of=AS_OF, service_level=0.95, review_days=14, promo_factor=1.5, promo_start=promos[0].starts_on,
                                     promo_end=promos[0].ends_on)
    return item, params, promos, data, after[0], before[0]


# ------------------------------------------------------------------------------------------------ structure (no recalculation)

def test_the_workbook_has_four_sheets_with_live_formulas_and_editable_inputs():
    _items, _params, _promos, data = build(1)
    wb = load_workbook(io.BytesIO(workbook.build_workbook(data)))
    assert wb.sheetnames == ["Inputs", "Plan", "Classes", "Scenario"]
    inputs, plan, classes, scenario = (wb[n] for n in wb.sheetnames)
    assert [inputs["A%d" % r].value for r in range(3, 10)] == ["As of", "Service level", "Review period (days)", "Promotion factor", "Promotion starts", "Promotion ends", "Safety factor z"]
    assert inputs["B4"].value == data.service_level and inputs["B5"].value == data.review_days and inputs["B6"].value == data.promo_factor
    assert inputs["B9"].value == "=NORMSINV(B4)"
    assert [inputs.cell(row=12, column=c).value for c in range(1, 6)] == ["Supplier", "Lead time (days)", "Lead time deviation (days)", "Min order", "Pack size"]
    assert {inputs["A%d" % (13 + i)].value for i in range(len(data.suppliers))} == {s.name for s in data.suppliers}
    computed = ("lead", "lead_sd", "moq", "pack", "cover", "pdays_lead", "factor", "lead_demand", "safety", "rop", "position", "needs", "pdays_horizon", "target", "raw", "order",
                "arr_qty", "arr_days", "arr_demand", "shortage")
    for key in computed:
        assert str(plan["%s2" % COL[key]].value).startswith("="), key                      # a formula, not a pasted value
    for key in workbook.VALUE_KEYS:
        assert not str(plan["%s2" % COL[key]].value or "").startswith("="), key             # what the hub measured is a value
    assert "INDEX(" in plan["%s2" % COL["lead"]].value and "MATCH(" in plan["%s2" % COL["lead"]].value
    assert "Inputs!$B$9" in plan["%s2" % COL["safety"]].value and "SQRT(" in plan["%s2" % COL["safety"]].value
    assert "SUMIFS(" in classes["C4"].value and "COUNTIFS(" in classes["B4"].value
    assert [classes["A%d" % r].value for r in range(4, 11)] == ["A", "B", "C", "best seller", "rising", "slow mover", "steady"]
    assert scenario["B2"].value == "=Inputs!$B$6"
    assert scenario["D%d" % SCENARIO_FIRST_ROW].value.startswith("=") and scenario["I%d" % SCENARIO_FIRST_ROW].value == "=IF(Plan!$N2=\"\",\"\",Plan!$AC2)"


def test_the_supplier_row_holds_the_most_common_minimum_and_a_variant_that_differs_keeps_its_own():
    entries = [("S", 10, 1.0, 24, 6), ("S", 10, 1.0, 24, 6), ("S", 10, 1.0, 48, 12), ("T", 5, 0.0, 10, 1)]
    rows, defaults = workbook._supplier_rows(entries)
    assert rows == [SupplierRow("S", 10, 1.0, 24, 6), SupplierRow("T", 5, 0.0, 10, 1)] and defaults == {"S": (24, 6), "T": (10, 1)}
    assert workbook._supplier_rows([("S", 1, 0.0, 5, 1), ("S", 1, 0.0, 9, 1)])[1]["S"] == (5, 1)            # a tie takes the smaller


def test_inputs_from_stored_plan_lines_maps_names_labels_money_and_overrides():
    lines_made: list = []

    def line(**kw):
        lines_made.append(1)
        base = {"variant_id": "v-" + str(len(lines_made)), "status": "ok", "supplier_name": "Harbor Textiles", "lead_time_days": 21, "lead_time_sd_days": 4.0, "moq": 24, "pack_size": 6, "product_title": "IPT Studio Tee",
                "variant_title": "M", "sku": "IPT-TEE-M", "abc_class": "A", "label": "best_seller", "revenue_minor": 123456, "currency": "USD", "velocity": 0.57, "velocity_sd": 0.92,
                "available": 10, "incoming": 30, "inbound_expected_on": date(2026, 12, 1)}
        return base | kw
    lines = [line(), line(sku="IPT-TEE-S", variant_title="S", moq=24), line(sku="IPT-SOCK", product_title="IPT Wool Sock", variant_title="Default Title", moq=48, pack_size=12, label="steady"),
             line(sku="NO-SUP", supplier_name=None, lead_time_days=None, status="no_supplier", moq=1, pack_size=1, currency="JPY", revenue_minor=5000), line(sku="GIFT", status="untracked")]
    data = workbook.inputs_from_lines(lines, as_of=AS_OF, service_level=0.95, review_days=14, promo_factor=1.0, promo_start=AS_OF, promo_end=AS_OF)
    assert [v.sku for v in data.variants] == ["IPT-TEE-M", "IPT-TEE-S", "IPT-SOCK", "NO-SUP"]              # the untracked variant is left out
    assert data.suppliers == [SupplierRow("Harbor Textiles", 21, 4.0, 24, 6)]
    assert list(data.variant_ids) == ["v-1", "v-2", "v-3", "v-4"]                                          # the ids of the rows kept, in order
    tee, _, sock, nosup = data.variants
    assert (tee.name, tee.label, tee.revenue, tee.moq_own, tee.arrives, tee.inbound) == ("IPT Studio Tee - M", "best seller", 1234.56, None, date(2026, 12, 1), 30)
    assert (sock.name, sock.moq_own, sock.pack_own) == ("IPT Wool Sock", 48, 12)                          # differs from the supplier's 24 and 6
    assert nosup.supplier is None and nosup.revenue == 5000.0 and nosup.moq_own is None                   # no supplier, and yen has no minor unit


def test_a_bad_input_is_refused_before_a_file_is_written():
    ok = dict(as_of=AS_OF, service_level=0.95, review_days=14, promo_factor=1.0, promo_start=AS_OF, promo_end=AS_OF, suppliers=[SupplierRow("A", 5, 0.0, 1, 1)], variants=[])
    workbook.build_workbook(WorkbookInputs(**ok))
    for change, why in (({"service_level": 1.0}, "service level"), ({"promo_factor": 0.0}, "promotion factor"), ({"review_days": -1}, "review period"),
                        ({"suppliers": [SupplierRow("A", 5, 0.0, 1, 1), SupplierRow("A", 6, 0.0, 1, 1)]}, "share a name"),
                        ({"variants": [VariantRow("S", "n", "Nobody", "A", "steady", 1.0, 1.0, 0.0, 1)]}, "no row"), ({"suppliers": [SupplierRow("A", 5, 0.0, 0, 1)]}, "at least 1")):
        with pytest.raises(ValueError, match=why):
            workbook.build_workbook(WorkbookInputs(**{**ok, **change}))


def test_the_file_name_carries_the_plan_date():
    assert workbook.filename(date(2026, 11, 24)) == "inventory-plan-2026-11-24.xlsx"


# ------------------------------------------------------------------------------------------------ recalculated, against the Python plan

@needs_engine
def test_the_hand_worked_case_recalculates_to_the_plans_figures():
    item, params, promos, data, after, before = hand_case()
    mismatches, engine = workbook_check.check_inputs(data, [item], params, promos)
    assert mismatches == [] and engine == ENGINE
    calc, _ = workbook_check.recalculate(workbook.build_workbook(data))
    plan, scenario = calc["Plan"], calc["Scenario"]
    cell = lambda key: plan["%s2" % COL[key]].value        # noqa: E731
    assert cell("lead") == 9 and cell("moq") == 50 and cell("pack") == 12
    assert cell("cover") == pytest.approx(5.0) and cell("position") == 10
    assert cell("safety") == after.safety_stock and cell("rop") == after.reorder_point and cell("order") == after.suggested_qty
    assert scenario["F6"].value == before.reorder_point and scenario["G6"].value == after.reorder_point        # the promotion raises the reorder point
    assert scenario["H6"].value == before.suggested_qty and scenario["I6"].value == after.suggested_qty
    assert after.reorder_point > before.reorder_point
    assert cell("shortage") == "cover under lead time"


def test_the_formulas_package_recalculates_to_the_same_figures_when_libreoffice_is_not_installed(monkeypatch):
    """The fallback engine: with `soffice` hidden, the `formulas` package is used and says so. Skips when that package is not installed."""
    pytest.importorskip("formulas")
    monkeypatch.setattr(workbook_check, "find_soffice", lambda: None)
    item, params, promos, data, _after, _before = hand_case()
    mismatches, engine = workbook_check.check_inputs(data, [item], params, promos)
    assert engine == "formulas" and mismatches == []
    items, params, promos, data = build(2, count=6)
    assert workbook_check.check_inputs(data, items, params, promos)[0] == []


@needs_engine
def test_property_the_workbook_reproduces_the_plan_over_seeded_worlds():
    cells = 0
    for seed in range(6):
        items, params, promos, data = build(seed, count=14)
        mismatches, _engine = workbook_check.check_inputs(data, items, params, promos)
        assert mismatches == [], (seed, mismatches[:8])
        cells += len(items) * 22
    assert cells > 1500


@needs_engine
def test_the_worlds_exercise_the_branches_they_are_meant_to():
    """The seeded worlds must contain open orders on time and late, shortage flags of each kind, a variant with no supplier, and an order needing more than the minimum."""
    seen = {"on_time": 0, "late": 0, "undated": 0, "nosup": 0, "needs": 0, "kinds": set()}
    for seed in range(6):
        items, params, promos, _data = build(seed, count=14)
        after, _before = workbook_check.expected_rows(items, AS_OF, params, promos)
        for item, row in zip(items, after):
            for a in item.arrivals:
                seen["undated" if a.expected_on is None else "late" if a.expected_on < AS_OF else "on_time"] += 1
            seen["nosup"] += row.status == "no_supplier"
            seen["needs"] += row.needs_reorder
            seen["kinds"].add(workbook_check._shortage(row))
    assert min(seen["on_time"], seen["late"], seen["undated"], seen["nosup"], seen["needs"]) > 0, seen
    assert {"out of stock", "cover under lead time", None} <= seen["kinds"], seen


@needs_engine
def test_a_change_of_one_unit_in_an_input_fails_the_check():
    item, params, promos, data, _after, _before = hand_case()
    good = workbook.build_workbook(data)
    assert workbook_check.check_inputs(data, [item], params, promos, xlsx=good)[0] == []
    probes = {"the supplier's lead time": ("Inputs", "B13", 1), "the stock on hand": ("Plan", "%s2" % COL["onhand"], 1), "the daily velocity": ("Plan", "%s2" % COL["velocity"], 1),
              "the minimum order": ("Inputs", "D13", 1), "the pack size": ("Inputs", "E13", 1), "the review period": ("Inputs", "B5", 1), "the promotion factor": ("Inputs", "B6", 1)}
    for name, (sheet, ref, delta) in probes.items():
        wb = load_workbook(io.BytesIO(good))
        wb[sheet][ref].value = wb[sheet][ref].value + delta
        out = io.BytesIO()
        wb.save(out)
        mismatches, _ = workbook_check.check_inputs(data, [item], params, promos, xlsx=out.getvalue())
        assert mismatches, "changing %s by one unit was not caught" % name


@needs_engine
def test_changing_the_promotion_factor_moves_the_after_columns_and_not_the_before_ones():
    item, params, promos, data, _after, _before = hand_case()
    good = workbook.build_workbook(data)
    got = {}
    for factor in (1.0, 2.0):
        wb = load_workbook(io.BytesIO(good))
        wb["Inputs"]["B6"].value = factor
        out = io.BytesIO()
        wb.save(out)
        calc, _ = workbook_check.recalculate(out.getvalue())
        s = calc["Scenario"]
        got[factor] = (s["F6"].value, s["G6"].value, s["H6"].value, s["I6"].value, s["J6"].value, s["K6"].value)
    assert got[1.0][0] == got[2.0][0] and got[1.0][2] == got[2.0][2]               # before: unchanged by the factor
    assert got[1.0][1] == got[1.0][0] and got[1.0][3] == got[1.0][2]               # a factor of 1 is no promotion: after equals before
    assert got[2.0][1] > got[2.0][0]                                                # a factor of 2 raises the reorder point
    assert got[2.0][3] >= got[2.0][2] and got[2.0][4] == got[2.0][1] - got[2.0][0]


@needs_engine
def test_a_variant_with_no_supplier_is_blank_where_the_lead_time_is_needed_and_still_counted():
    items = [VariantInput(key="A", available=0, snapshots={d: 0 for d in WINDOW}, sales={WINDOW[0]: 3}, units_all=3, revenue_minor=900, lead_time_days=None)]
    params, promos = PlanParams(), (Promo(AS_OF, AS_OF, 1.0),)
    after, _before = workbook_check.expected_rows(items, AS_OF, params, promos)
    data = workbook.inputs_from_plan(items, after, ["Orphan"], [None], as_of=AS_OF, service_level=0.95, review_days=14, promo_factor=1.0, promo_start=AS_OF, promo_end=AS_OF)
    mismatches, _ = workbook_check.check_inputs(data, items, params, promos)
    assert mismatches == []
    calc, _ = workbook_check.recalculate(workbook.build_workbook(data))
    assert calc["Plan"]["%s2" % COL["lead"]].value in (None, "") and calc["Plan"]["%s2" % COL["safety"]].value in (None, "")
    assert calc["Classes"]["B4"].value + calc["Classes"]["B5"].value + calc["Classes"]["B6"].value == 1


def test_the_comparison_names_the_cell_that_differs():
    item, params, promos, data, after, before = hand_case()

    class Cell:
        def __init__(self, value):
            self.value = value

    class Sheet(dict):
        def __missing__(self, key):
            return Cell(None)

    sheets = {"Plan": Sheet(), "Scenario": Sheet(), "Classes": Sheet()}
    bad = workbook_check.compare(data, [item], [after], [before], sheets, promos=promos)
    assert bad and bad[0].startswith("Plan!%s2: workbook None" % COL["lead"])
