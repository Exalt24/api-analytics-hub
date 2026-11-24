#!/usr/bin/env python
# LIFESPAN: permanent
"""Recalculate the plan workbook of a stored plan in a spreadsheet engine and compare every computed cell with the Python plan for the same inputs.

    DATABASE_URL=postgresql://... INVENTORY_TENANT=<tenant uuid> python scripts/inventory_workbook_check.py [--as-of D] [--promo-factor F --promo-from D --promo-to D] [--self-test]

It builds the workbook exactly as the route and the command do (`store.workbook_inputs`), opens it in LibreOffice headless (or the `formulas` package when LibreOffice is not installed) and
compares the lead time and pack looked up from the supplier row, days of cover, safety stock, reorder point, stock position, the needs-order flag, the order, the shortage flag, the Scenario
columns and the Classes totals with what `plan_variant` gives for the same variants and the same promotion. It prints the engine it used and how many cells it compared.

`--self-test` then changes one input cell by one unit (the first supplier's lead time, else the first variant's stock) and checks that the comparison FAILS, so a pass is known to be able to
fail. Exit 0: the workbook reproduces the plan (and, with --self-test, the changed one was caught). Exit 1: a cell differs. Exit 2: refused, or no engine is installed.

A variant with several open orders on different dates is shown in the workbook as one inbound quantity at the earliest date, so it can differ here by design (docs/inventory-planning.md).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook  # noqa: E402

from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.inventory import model, store, workbook, workbook_check  # noqa: E402


def _date(text: str) -> date:
    return date.fromisoformat(text)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=os.environ.get("INVENTORY_TENANT"))
    p.add_argument("--as-of", type=_date)
    p.add_argument("--promo-factor", type=float)
    p.add_argument("--promo-from", type=_date)
    p.add_argument("--promo-to", type=_date)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--keep", type=Path, help="also write the workbook here")
    return p


async def load(args):
    db = Database(max_size=2)
    await db.connect()
    try:
        async with db.tenant(args.tenant) as conn:
            data = await store.workbook_inputs(conn, args.as_of, promo_factor=args.promo_factor, promo_from=args.promo_from, promo_to=args.promo_to)
            params = model.PlanParams(service_level=data.service_level, review_days=data.review_days)
            inputs, _by = await store.load_inputs(conn, data.as_of, params)
    finally:
        await db.close()
    by_id = {i.key: i for i in inputs}
    return data, params, [by_id[v] for v in data.variant_ids]


def main() -> int:
    args = parser().parse_args()
    if not args.tenant:
        print("give --tenant or set INVENTORY_TENANT", file=sys.stderr)
        return 2
    try:
        data, params, items = asyncio.run(load(args))
    except (UnsafeDatabaseRole, LookupError, ValueError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    promos = (model.Promo(data.promo_start, data.promo_end, data.promo_factor),)
    xlsx = workbook.build_workbook(data)
    if args.keep:
        args.keep.write_bytes(xlsx)
    try:
        mismatches, engine = workbook_check.check_inputs(data, items, params, promos, xlsx=xlsx)
    except workbook_check.RecalcUnavailable as exc:
        print("no spreadsheet engine: %s" % exc, file=sys.stderr)
        return 2
    cells = workbook_check.compared_cells(len(items))
    print("plan of %s, %d variants, %d suppliers, promotion factor %.2f from %s to %s" % (data.as_of, len(items), len(data.suppliers), data.promo_factor, data.promo_start, data.promo_end))
    print("recalculated in %s: %d computed cells compared with the Python plan, %d differ" % (engine, cells, len(mismatches)))
    for line in mismatches[:20]:
        print("  " + line)
    if mismatches:
        return 1
    if args.self_test:
        wb = load_workbook(io.BytesIO(xlsx))
        if data.suppliers:
            cell, what = wb["Inputs"]["B%d" % workbook.SUPPLIER_FIRST_ROW], "the first supplier's lead time"
        else:
            cell, what = wb["Plan"]["%s%d" % (workbook.COL["onhand"], workbook.PLAN_FIRST_ROW)], "the first variant's stock on hand"
        was = cell.value
        cell.value = was + 1
        out = io.BytesIO()
        wb.save(out)
        changed, _ = workbook_check.check_inputs(data, items, params, promos, xlsx=out.getvalue())
        print("self-test: %s changed from %s to %s by one: %d cell(s) differ%s" % (what, was, was + 1, len(changed), "" if changed else " (NOT CAUGHT)"))
        for line in changed[:5]:
            print("  " + line)
        return 0 if changed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
