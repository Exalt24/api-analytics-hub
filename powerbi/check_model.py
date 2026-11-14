#!/usr/bin/env python
# LIFESPAN: permanent (the saved check for the Power BI file: opens it in Desktop, runs the measures and compares them with figures worked out from the CSV files)
"""Open powerbi/AnalyticsHub.pbix in Power BI Desktop, run the model's measures through its Analysis Services port and compare each one with a figure computed
here, straight from the four CSV files, with no DAX involved.

    python powerbi/check_model.py                  opens a temporary copy of the .pbix, checks, closes it
    python powerbi/check_model.py --port 54296     checks a model that is already open
    python powerbi/check_model.py --expected       prints the expected figures and exits (no Desktop needed)

Windows only, Microsoft Store build of Power BI Desktop. Exit code 0 only when every comparison matches. The comparison itself (`compare`) and the expected
figures (`expected_figures`) are plain Python and are tested without Desktop in tests/test_powerbi_project.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
TOL = 1e-9


def _rows(name: str) -> list[dict]:
    with open(DATA / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def expected_figures(data_dir: Path | None = None) -> dict:
    """Every figure the Power BI pages show, worked out from the CSV files alone."""
    global DATA
    keep = DATA
    if data_dir:
        DATA = Path(data_dir)
    try:
        fact, conns, runs, metrics = _rows("fact_snapshot.csv"), _rows("dim_connection.csv"), _rows("sync_run.csv"), _rows("dim_metric.csv")
    finally:
        DATA = keep
    platform_of = {c["connection_id"]: c["platform"] for c in conns}
    by_day: dict[tuple[str, str], int] = {}
    present: dict[str, set[str]] = defaultdict(set)
    for r in fact:
        by_day[(r["observed_on"], r["metric_key"])] = int(r["value"])
        present[platform_of[r["connection_id"]]].add(r["observed_on"])
    days = sorted({r["observed_on"] for r in fact})
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    calendar = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]

    def total(key, within=None):
        return sum(v for (d, k), v in by_day.items() if k == key and (within is None or d in within))

    money = lambda key, within=None: total(key, within) / 100
    net, gross, refunds = money("net_sales"), money("gross_sales"), money("refunds")
    orders, spend, claimed = total("orders"), money("ad_spend"), money("ad_purchase_value")
    sessions = total("sessions")
    with_sessions = {d for d in calendar if by_day.get((d, "sessions"), 0) > 0}

    months: dict[str, list[str]] = defaultdict(list)
    for d in calendar:
        months[d[:7]].append(d)
    month_keys = sorted(months)
    monthly = {}
    for i, m in enumerate(month_keys):
        n = money("net_sales", set(months[m]))
        prev = money("net_sales", set(months[month_keys[i - 1]])) if i else None
        monthly[m] = {"net": n, "orders": total("orders", set(months[m])), "ad": money("ad_spend", set(months[m])),
                      "mom": None if not prev else (n - prev) / prev}

    last7 = set(calendar[-7:])
    overclaim = {d: (by_day.get((d, "ad_purchase_value"), 0) - by_day.get((d, "net_sales"), 0)) / 100
                 for d in calendar if by_day.get((d, "ad_purchase_value"), 0) > by_day.get((d, "net_sales"), 0) > 0}
    missing = {p: [d for d in calendar if d not in present[p]] for p in platform_of.values()}
    partial = {p: sum(1 for r in runs if platform_of[r["connection_id"]] == p and r["status"] != "succeeded") for p in platform_of.values()}
    return {
        "rows": {"Snapshot": len(fact), "Metric": len(metrics), "Connection": len(conns), "SyncRun": len(runs), "Date": len(calendar)},
        "totals": {
            "net": net, "gross": gross, "refunds": refunds, "orders": orders, "aov": gross / orders, "refund_rate": refunds / gross,
            "ad_spend": spend, "purchase_value": claimed, "roas": claimed / spend,
            "conversion": sum(by_day.get((d, "orders"), 0) for d in with_sessions) / sessions,
            "gap": round(gross - refunds - net, 6),
            "not_reconcile": sum(1 for d in calendar if by_day.get((d, "gross_sales"), 0) - by_day.get((d, "refunds"), 0) != by_day.get((d, "net_sales"), 0)),
            "overclaim_days": len(overclaim), "days_without": sum(len(v) for v in missing.values()), "partial": sum(partial.values()),
        },
        "monthly": monthly,
        "last_day": {"date": calendar[-1], "avg7": money("net_sales", last7) / 7, "mtd": money("net_sales", set(months[month_keys[-1]]))},
        "overclaim": overclaim,
        "missing": missing,
        "partial": partial,
    }


def compare(expected: dict, measured: dict) -> list[tuple[str, bool, str]]:
    """One (name, passed, detail) per figure. `measured` has the same shape as `expected_figures()`."""
    out: list[tuple[str, bool, str]] = []

    def num(name, want, got):
        ok = got is not None and want is not None and abs(float(got) - float(want)) <= max(TOL, 1e-9 * abs(float(want)))
        if want is None and got is None:
            ok = True
        out.append((name, ok, f"expected {want}, model says {got}"))

    for k, v in expected["rows"].items():
        num(f"rows in {k}", v, measured["rows"].get(k))
    for k, v in expected["totals"].items():
        num(f"total {k}", v, measured["totals"].get(k))
    for m, row in expected["monthly"].items():
        got = measured["monthly"].get(m, {})
        for k, v in row.items():
            num(f"{m} {k}", v, got.get(k))
    for k in ("avg7", "mtd"):
        num(f"last day {k}", expected["last_day"][k], measured["last_day"].get(k))
    same = set(expected["overclaim"]) == set(measured["overclaim"])
    out.append(("overclaim days listed", same, f"expected {sorted(expected['overclaim'])}, model says {sorted(measured['overclaim'])}"))
    for d, v in expected["overclaim"].items():
        num(f"overclaim amount {d}", v, measured["overclaim"].get(d))
    for p, days in expected["missing"].items():
        out.append((f"missing days for {p}", sorted(days) == sorted(measured["missing"].get(p, [])), f"expected {days}, model says {measured['missing'].get(p, [])}"))
    for p, v in expected["partial"].items():
        num(f"failed or partial syncs for {p}", v, measured["partial"].get(p))
    return out


DAX = {
    "totals": 'EVALUATE ROW("rows_snapshot", COUNTROWS(Snapshot), "rows_metric", COUNTROWS(Metric), "rows_connection", COUNTROWS(Connection), "rows_syncrun", COUNTROWS(SyncRun), '
              '"rows_date", COUNTROWS(\'Date\'), "net", [Net Sales], "gross", [Gross Sales], "refunds", [Refunds], "orders", [Orders], "aov", [Average Order Value], '
              '"refund_rate", [Refund Rate], "ad_spend", [Ad Spend], "purchase_value", [Ad Purchase Value], "roas", [ROAS], "conversion", [Conversion Rate], "gap", [Net Sales Gap], '
              '"not_reconcile", [Days Net Sales Does Not Reconcile], "overclaim_days", [Overclaim Days], "days_without", [Days Without Data], "partial", [Partial Or Failed Syncs])',
    "monthly": 'EVALUATE SUMMARIZECOLUMNS(\'Date\'[Month], "net", [Net Sales], "orders", [Orders], "ad", [Ad Spend], "mom", [Net Sales MoM %]) ORDER BY \'Date\'[Month]',
    "last_day": 'EVALUATE ROW("avg7", CALCULATE([Net Sales 7 Day Avg], \'Date\'[Date] = MAX(\'Date\'[Date])), "mtd", CALCULATE([Net Sales MTD], \'Date\'[Date] = MAX(\'Date\'[Date])), '
                '"day", MAX(\'Date\'[Date]))',
    "overclaim": 'EVALUATE SUMMARIZECOLUMNS(\'Date\'[Date], "amount", [Overclaim Amount])',
    "missing": 'EVALUATE SUMMARIZECOLUMNS(\'Date\'[Date], Connection[Platform], "flag", [Missing Day Flag])',
    "partial": 'EVALUATE SUMMARIZECOLUMNS(Connection[Platform], "partial", [Partial Or Failed Syncs])',
}


def _strip(row: dict) -> dict:
    return {k.strip("[]").split("[")[-1]: v for k, v in row.items()}


def shape_measured(raw: dict) -> dict:
    t = _strip(raw["totals"][0])
    monthly = {}
    for r in raw["monthly"]:
        r = _strip(r)
        monthly[r["Month"][:7]] = {"net": r["net"], "orders": r["orders"], "ad": r["ad"], "mom": r["mom"]}
    ld = _strip(raw["last_day"][0])
    missing: dict[str, list[str]] = defaultdict(list)
    for r in raw["missing"]:
        r = _strip(r)
        missing[r["Platform"]].append(r["Date"])
    return {
        "rows": {"Snapshot": t["rows_snapshot"], "Metric": t["rows_metric"], "Connection": t["rows_connection"], "SyncRun": t["rows_syncrun"], "Date": t["rows_date"]},
        "totals": {k: t[k] for k in ("net", "gross", "refunds", "orders", "aov", "refund_rate", "ad_spend", "purchase_value", "roas", "conversion", "gap",
                                     "not_reconcile", "overclaim_days", "days_without", "partial")},
        "monthly": monthly,
        "last_day": {"avg7": ld["avg7"], "mtd": ld["mtd"]},
        "overclaim": {_strip(r)["Date"]: _strip(r)["amount"] for r in raw["overclaim"]},
        "missing": dict(missing),
        "partial": {_strip(r)["Platform"]: _strip(r)["partial"] for r in raw["partial"]},
    }


def _powershell(*args: str, timeout: int = 120) -> str:
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip())
    return done.stdout


def run_dax(port: int | None) -> dict:
    args = ["-File", str(HERE / "run_dax.ps1"), "-Queries", json.dumps(DAX)]
    if port:
        args += ["-Port", str(port)]
    return json.loads(_powershell(*args))


def desktop_exe() -> Path:
    out = _powershell("-Command", "(Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop).InstallLocation").strip()
    if not out:
        raise SystemExit("Power BI Desktop (Microsoft Store build) is not installed")
    return Path(out) / "bin" / "PBIDesktop.exe"


def open_in_desktop(pbix: Path) -> tuple[Path, int, int]:
    work = Path(tempfile.mkdtemp(prefix="pbcheck_"))
    copy = work / pbix.name
    shutil.copy2(pbix, copy)
    before = {m["Pid"] for m in _listed()}
    subprocess.Popen([str(desktop_exe()), str(copy)])
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(2)
        fresh = [m for m in _listed() if m["Pid"] not in before]
        if fresh:
            port = fresh[0]["Port"]
            try:
                probe = json.loads(_powershell("-File", str(HERE / "run_dax.ps1"), "-Port", str(port), "-Queries",
                                               json.dumps({"n": 'EVALUATE ROW("n", COUNTROWS(Snapshot))'})))
                if list(_strip(probe["n"][0]).values())[0]:
                    return work, port, fresh[0]["Pid"]
            except Exception:
                pass
    raise SystemExit("Power BI Desktop did not open a model with data within three minutes")


def _listed() -> list[dict]:
    text = _powershell("-File", str(HERE / "run_dax.ps1"), "-List").strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def close_desktop(work: Path, model_pid: int) -> None:
    """Stop the Desktop window that holds the temporary copy and the Analysis Services process it started, then delete the copy."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -eq 'PBIDesktop.exe' -and $_.CommandLine -like '*" + work.name + "*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Stop-Process -Id " + str(model_pid) + " -Force -ErrorAction SilentlyContinue")
    _powershell("-Command", script)
    shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pbix", default=str(HERE / "AnalyticsHub.pbix"))
    ap.add_argument("--port", type=int, help="check a model that is already open on this Analysis Services port")
    ap.add_argument("--expected", action="store_true", help="print the expected figures and exit")
    args = ap.parse_args()
    want = expected_figures()
    if args.expected:
        print(json.dumps(want, indent=2, default=str))
        return 0
    work, model_pid = None, 0
    port = args.port
    try:
        if not port:
            work, port, model_pid = open_in_desktop(Path(args.pbix))
        results = compare(want, shape_measured(run_dax(port)))
    finally:
        if work:
            close_desktop(work, model_pid)
    bad = 0
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  " + detail))
        bad += not ok
    print(f"{len(results) - bad} of {len(results)} comparisons match")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
