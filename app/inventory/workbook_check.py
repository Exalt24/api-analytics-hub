"""Recalculate the plan workbook outside the hub and compare every computed cell with the figures the Python plan gives for the same inputs.

The workbook (app/inventory/workbook.py) carries formulas, and a formula nobody calculated proves nothing. `recalculate` opens the file in LibreOffice headless (`soffice`, found on the
PATH or in its usual install folders) and saves a copy with the values filled in; if LibreOffice is not installed it uses the `formulas` Python package when that is installed, and says
which one it used. `compare` then checks, for each variant, the lead time and the figures looked up from the supplier row, the days of cover, the safety stock, the reorder point, the
stock position, the needs-order flag, the suggested order and the shortage flag against the PlanRow, the Scenario sheet's before and after figures against two plans (without and with the
promotion), and the Classes sheet against counts and sums worked out from the rows. It returns the list of cells that disagree; an empty list is a pass.

The expected figures are computed by `app.inventory.model.plan_variant`, the same function the hub stores its plan with, from the same inputs. A change of one unit in any input cell must
make the list non-empty (tests/test_inventory_workbook.py changes one and checks that).
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import load_workbook

from . import model, workbook
from .alerts import shortage_kind
from .workbook import COL, PLAN_FIRST_ROW, SCENARIO_FIRST_ROW, SHORTAGE_TEXT, WorkbookInputs

# Where LibreOffice installs when it is not on the PATH: under the program folders Windows names in its environment, and the usual Linux and macOS places.
SOFFICE_FOLDERS = tuple(str(Path(base) / "LibreOffice" / "program") for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432") if (base := os.environ.get(var))) + (
    "/usr/bin", "/usr/local/bin", "/opt/libreoffice/program", "/Applications/LibreOffice.app/Contents/MacOS")


class RecalcUnavailable(RuntimeError):
    """Neither LibreOffice nor the `formulas` package is installed, so the workbook cannot be recalculated here."""


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found
    for folder in SOFFICE_FOLDERS:
        for name in ("soffice.exe", "soffice"):
            candidate = Path(folder) / name
            if candidate.exists():
                return str(candidate)
    return None


def engine_name() -> str | None:
    """Which recalculation engine this machine has: 'libreoffice', 'formulas' or None."""
    if find_soffice():
        return "libreoffice"
    try:
        import formulas  # noqa: F401
    except ImportError:
        return None
    return "formulas"


def recalculate(xlsx: bytes, *, timeout: int = 180) -> tuple[Any, str]:
    """Open `xlsx` in a spreadsheet engine, let it calculate every formula, and return (the workbook read with values, the engine's name)."""
    engine = engine_name()
    if engine is None:
        raise RecalcUnavailable("neither LibreOffice (soffice) nor the formulas package is installed")
    with tempfile.TemporaryDirectory(prefix="inventory-workbook-") as tmp:
        source = Path(tmp) / "plan.xlsx"
        source.write_bytes(xlsx)
        out = Path(tmp) / "out"
        out.mkdir()
        if engine == "libreoffice":
            profile = Path(tmp) / "profile"
            cmd = [find_soffice() or "soffice", "--headless", "--norestore", "--nolockcheck", "-env:UserInstallation=%s" % profile.as_uri(),
                   "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(out), str(source)]
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"})
            result = out / "plan.xlsx"
            if done.returncode != 0 or not result.exists():
                raise RuntimeError("LibreOffice did not convert the workbook: %s %s" % (done.returncode, (done.stderr or done.stdout)[:300]))
        else:
            import formulas
            model_ = formulas.ExcelModel().loads(str(source)).finish()
            model_.calculate()
            model_.write(dirpath=str(out))
            written = sorted(out.rglob("*.xlsx"))
            if not written:
                raise RuntimeError("the formulas package wrote no file")
            result = written[0]
        wb = load_workbook(result, data_only=True)
        if engine == "formulas":                  # it writes sheet names in capitals, and a title that differs only in case is refused, so go through a temporary one
            wanted = {ws.title: next((n for n in workbook.SHEETS if n.upper() == ws.title.upper()), ws.title) for ws in wb.worksheets}
            for i, ws in enumerate(wb.worksheets):
                ws.title = "tmp%d" % i
            for (old, new), ws in zip(wanted.items(), wb.worksheets):
                ws.title = new
        return wb, engine


# ------------------------------------------------------------------------------------------------------- comparing

def _norm(value: Any) -> Any:
    if value == "" or value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _same(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def expected_rows(items: Sequence[model.VariantInput], as_of: date, params: model.PlanParams, promos: Sequence[model.Promo]) -> tuple[list[model.PlanRow], list[model.PlanRow]]:
    """The Python plan for the same inputs twice: with the promotion (what the Plan sheet reads) and without it (the Scenario sheet's before)."""
    def run(promo_list: Sequence[model.Promo]) -> list[model.PlanRow]:
        return model.plan_all([model.VariantInput(**{**item.__dict__, "promos": tuple(promo_list)}) for item in items], as_of, params)
    return run(promos), run(())


def _shortage(row: model.PlanRow) -> str | None:
    kind = shortage_kind(row.status, row.velocity.mean, row.available, row.lead_time_days, row.alert_cover_days, row.forecast_cover_days)
    return SHORTAGE_TEXT[kind] if kind else None


def compare(data: WorkbookInputs, items: Sequence[model.VariantInput], after: Sequence[model.PlanRow], before: Sequence[model.PlanRow], calc: Any, *, promos: Sequence[model.Promo]) -> list[str]:
    """Every computed cell that differs from the Python plan, as 'Sheet!Cell: workbook X, python Y'. Empty when the workbook reproduces the plan."""
    bad: list[str] = []
    plan, scenario, classes = calc["Plan"], calc["Scenario"], calc["Classes"]

    def check(sheet: Any, name: str, cell: str, got: Any, want: Any) -> None:
        if not _same(got, want):
            bad.append("%s!%s: workbook %r, python %r" % (name, cell, _norm(got), _norm(want)))

    supplier_rows = {s.name: s for s in data.suppliers}
    for n, (item, a, b, v) in enumerate(zip(items, after, before, data.variants)):
        r = PLAN_FIRST_ROW + n
        got = lambda key: plan["%s%d" % (COL[key], r)].value    # noqa: E731
        supplied = a.lead_time_days is not None
        sup = supplier_rows.get(v.supplier) if supplied and v.supplier else None
        lead_demand = model.demand_over(a.velocity.mean, data.as_of, a.lead_time_days, promos) if supplied else None
        want = {
            "lead": a.lead_time_days, "lead_sd": a.lead_time_sd_days if supplied else None,
            "moq": item.moq if supplied else None, "pack": item.pack_size if supplied else None,
            "cover": a.cover_days, "factor": a.promo_factor if supplied else None, "lead_demand": lead_demand,
            "safety": a.safety_stock, "rop": a.reorder_point, "position": a.available + a.incoming, "needs": a.needs_reorder if supplied else None,
            "order": a.suggested_qty if supplied else None, "shortage": _shortage(a),
        }
        if sup is not None:
            want["lead"], want["lead_sd"] = sup.lead_time_days, sup.lead_time_sd_days
        for key, value in want.items():
            check(plan, "Plan", "%s%d" % (COL[key], r), got(key), value)
        s = SCENARIO_FIRST_ROW + n
        for col, value in (("D", b.safety_stock), ("E", a.safety_stock), ("F", b.reorder_point), ("G", a.reorder_point), ("H", b.suggested_qty if supplied else None),
                           ("I", a.suggested_qty if supplied else None),
                           ("J", a.reorder_point - b.reorder_point if supplied else None), ("K", a.suggested_qty - b.suggested_qty if supplied else None)):
            check(scenario, "Scenario", "%s%d" % (col, s), scenario["%s%d" % (col, s)].value, value)
    total = SCENARIO_FIRST_ROW + len(items)
    check(scenario, "Scenario", "H%d" % total, scenario["H%d" % total].value, sum(b.suggested_qty for b in before))
    check(scenario, "Scenario", "I%d" % total, scenario["I%d" % total].value, sum(a.suggested_qty for a in after))

    revenue_total = sum(v.revenue for v in data.variants)
    for n, group in enumerate(workbook.CLASS_ROWS):
        r = 4 + n
        members = [i for i, v in enumerate(data.variants) if (v.abc if group in ("A", "B", "C") else v.label) == group]
        check(classes, "Classes", "B%d" % r, classes["B%d" % r].value, len(members))
        check(classes, "Classes", "C%d" % r, classes["C%d" % r].value, sum(data.variants[i].revenue for i in members))
        check(classes, "Classes", "D%d" % r, classes["D%d" % r].value, (sum(data.variants[i].revenue for i in members) / revenue_total) if revenue_total else 0)
        check(classes, "Classes", "E%d" % r, classes["E%d" % r].value, sum(data.variants[i].onhand for i in members))
        check(classes, "Classes", "F%d" % r, classes["F%d" % r].value, sum(1 for i in members if after[i].needs_reorder and after[i].lead_time_days is not None))
        check(classes, "Classes", "G%d" % r, classes["G%d" % r].value, sum(after[i].suggested_qty for i in members))
    return bad


def compared_cells(variants: int) -> int:
    """How many computed cells `compare` looks at: 13 Plan and 8 Scenario cells for each variant, the two Scenario totals and 6 cells for each row of Classes."""
    return variants * 21 + 2 + len(workbook.CLASS_ROWS) * 6


def check_inputs(data: WorkbookInputs, items: Sequence[model.VariantInput], params: model.PlanParams, promos: Sequence[model.Promo], xlsx: bytes | None = None) -> tuple[list[str], str]:
    """Build the workbook (or take `xlsx`), recalculate it, and compare it with the plan worked out from `items`. Returns (the disagreeing cells, the engine used)."""
    after, before = expected_rows(items, data.as_of, params, promos)
    calc, engine = recalculate(xlsx if xlsx is not None else workbook.build_workbook(data))
    return compare(data, items, after, before, calc, promos=promos), engine
