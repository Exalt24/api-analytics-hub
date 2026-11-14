"""The Power BI project under powerbi/: the data files, the model files, the report files and the .pbix, checked without Power BI Desktop.

What Desktop itself proves (the measures return the right numbers) is `powerbi/check_model.py`, which needs a Windows machine with Desktop; its comparison logic
is tested here against tampered figures so a check that could never fail is caught. The Postgres test runs the real export through the hub's migrations.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
sys.path.insert(0, str(PBI))

import check_model  # noqa: E402
import export_data  # noqa: E402

MODEL = PBI / "AnalyticsHub.SemanticModel" / "definition"
REPORT = PBI / "AnalyticsHub.Report" / "definition"


def read_csv(name):
    with open(PBI / "data" / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------------------ the data


def test_the_csv_files_are_what_the_formulas_write():
    want = export_data.expected_files()
    for name, text in want.items():
        assert (PBI / "data" / name).read_bytes() == text.encode("utf-8"), name + " differs from export_data.py; run it again"


def test_the_formulas_are_a_pure_function():
    assert export_data.expected_files() == export_data.expected_files()


def test_every_day_reconciles_gross_minus_refunds_equals_net():
    by = {}
    for r in read_csv("fact_snapshot.csv"):
        by[(r["observed_on"], r["metric_key"])] = int(r["value"])
    days = {d for d, _ in by}
    bad = [d for d in days if ((d, "gross_sales") in by) and by[(d, "gross_sales")] - by[(d, "refunds")] != by[(d, "net_sales")]]
    assert bad == []


def test_the_planted_problems_are_there_and_nothing_else_is_missing():
    want = check_model.expected_figures()
    assert sorted(want["overclaim"]) == ["2026-06-10", "2026-08-04", "2026-09-28"]
    assert want["missing"]["ga4"] == ["2026-08-29", "2026-08-30"]
    assert want["missing"]["shopify"] == [] and want["missing"]["meta"] == []
    assert want["partial"] == {"ga4": 1, "meta": 0, "shopify": 0}
    assert want["totals"]["not_reconcile"] == 0


def test_the_money_columns_are_whole_cents_and_carry_a_currency():
    money = {k for k, v in export_data.METRIC_META.items() if v[1] == "minor_currency"}
    for r in read_csv("fact_snapshot.csv"):
        assert int(r["value"]) == float(r["value"])
        assert (r["currency"] != "") == (r["metric_key"] in money), r


def test_metric_labels_units_and_kinds_match_the_migrations():
    sql = "\n".join((ROOT / "migrations" / n).read_text(encoding="utf-8") for n in ("001_init.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql"))
    inserts = "\n".join(re.findall(r"insert into metric \(key, label, unit, kind\) values(.*?)(?:on conflict|;)", sql, re.S))
    seen = {m.group(1): (m.group(2), m.group(3), m.group(4)) for m in re.finditer(r"\('(\w+)',\s*'([^']+)',\s*'(\w+)',\s*'(\w+)'\)", inserts)}
    for key, meta in export_data.METRIC_META.items():
        assert seen[key] == meta, key


def test_the_sync_runs_cover_each_source_and_the_failed_one_is_the_website():
    runs = read_csv("sync_run.csv")
    conns = {r["connection_id"]: r["platform"] for r in read_csv("dim_connection.csv")}
    assert {conns[r["connection_id"]] for r in runs} == {"shopify", "meta", "ga4"}
    assert [conns[r["connection_id"]] for r in runs if r["status"] != "succeeded"] == ["ga4"]


# ------------------------------------------------------------------ the model files


def tmdl(name):
    return (MODEL / "tables" / (name + ".tmdl")).read_text(encoding="utf-8")


def columns_of(table):
    return set(m.group(1).strip("'") for m in re.finditer(r"^\tcolumn ('[^']+'|\w+)", tmdl(table), re.M))


def measures():
    return set(m.group(1) for m in re.finditer(r"^\tmeasure '([^']+)' =", tmdl("Snapshot"), re.M))


def test_every_table_the_model_lists_has_a_file_and_every_file_is_listed():
    listed = set(re.findall(r"^ref table (\w+)", (MODEL / "model.tmdl").read_text(encoding="utf-8"), re.M))
    files = {p.stem for p in (MODEL / "tables").glob("*.tmdl")}
    assert listed == files == {"Date", "Snapshot", "Metric", "Connection", "SyncRun"}


def test_relationships_join_real_columns_on_the_many_side_to_a_key():
    text = (MODEL / "relationships.tmdl").read_text(encoding="utf-8")
    pairs = re.findall(r"fromColumn: (\w+)\.(\w+)\n\ttoColumn: (\w+)\.(\w+)", text)
    assert len(pairs) == 4
    for ft, fc, tt, tc in pairs:
        assert fc in columns_of(ft) and tc in columns_of(tt), (ft, fc, tt, tc)
    assert {(a, c) for a, _, c, _ in pairs} == {("Snapshot", "Date"), ("Snapshot", "Metric"), ("Snapshot", "Connection"), ("SyncRun", "Connection")}


def test_the_power_query_steps_name_the_columns_the_csv_files_have():
    for table, csv_name in (("Snapshot", "fact_snapshot.csv"), ("Metric", "dim_metric.csv"), ("Connection", "dim_connection.csv"), ("SyncRun", "sync_run.csv")):
        text = tmdl(table)
        assert csv_name in text
        header = read_csv(csv_name)[0].keys()
        typed = re.search(r"Typed = Table\.TransformColumnTypes\(Promoted, \{(.*?)\}, \"en-US\"\)", text).group(1)
        named = re.findall(r'\{"(\w+)", ', typed)
        assert named == list(header), table
        renamed = dict(re.findall(r'\{"(\w+)", "(\w+)"\}', re.search(r"Renamed = Table\.RenameColumns\(.*\)", text).group(0)))
        assert set(renamed) == set(header)
        assert set(renamed.values()) == columns_of(table), table


def test_every_measure_is_formatted_foldered_and_uses_only_known_names():
    names = measures()
    assert len(names) >= 25
    body = tmdl("Snapshot")
    for m in re.finditer(r"^\tmeasure '([^']+)' = (.*)\n\t\tformatString: (.*)\n\t\tdisplayFolder: (.*)", body, re.M):
        assert m.group(3) and m.group(4)
        for ref in re.findall(r"(?<![\w'])\[([^\]]+)\]", m.group(2)):
            assert ref in names or ref in columns_of("Date"), (m.group(1), ref)


def test_the_data_folder_parameter_defaults_to_the_public_copy_of_these_files():
    text = (MODEL / "expressions.tmdl").read_text(encoding="utf-8")
    assert "https://raw.githubusercontent.com/Exalt24/api-analytics-hub/main/powerbi/data/" in text
    assert "IsParameterQuery=true" in text


# ------------------------------------------------------------------ the report files


def visuals():
    for p in sorted(REPORT.glob("pages/*/visuals/*/visual.json")):
        yield p, json.loads(p.read_text(encoding="utf-8"))


def fields(node):
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            if kind in node and "Property" in node[kind]:
                yield kind, node[kind]["Expression"]["SourceRef"]["Entity"], node[kind]["Property"]
        for v in node.values():
            yield from fields(v)
    elif isinstance(node, list):
        for v in node:
            yield from fields(v)


def test_every_field_a_visual_uses_exists_in_the_model():
    names = measures()
    used = 0
    for path, doc in visuals():
        for kind, entity, prop in fields(doc):
            used += 1
            if kind == "Measure":
                assert entity == "Snapshot" and prop in names, (path.parent.name, prop)
            else:
                assert prop in columns_of(entity), (path.parent.name, entity, prop)
    assert used > 30


def test_the_report_has_two_pages_and_the_pages_file_lists_them_in_order():
    pages = json.loads((REPORT / "pages" / "pages.json").read_text(encoding="utf-8"))
    assert pages["pageOrder"] == ["sales", "checks"]
    for name, title in (("sales", "Sales and ads"), ("checks", "Data checks")):
        assert json.loads((REPORT / "pages" / name / "page.json").read_text(encoding="utf-8"))["displayName"] == title
    assert len(list(visuals())) == 18


def test_no_visual_sits_off_the_page():
    for path, doc in visuals():
        pos = doc["position"]
        assert pos["x"] >= 0 and pos["y"] >= 0 and pos["x"] + pos["width"] <= 1280 and pos["y"] + pos["height"] <= 720, path.parent.name


def test_the_project_file_points_at_the_report_and_the_report_at_the_model():
    pbip = json.loads((PBI / "AnalyticsHub.pbip").read_text(encoding="utf-8"))
    assert (PBI / pbip["artifacts"][0]["report"]["path"] / "definition.pbir").exists()
    pbir = json.loads((PBI / "AnalyticsHub.Report" / "definition.pbir").read_text(encoding="utf-8"))
    assert (PBI / "AnalyticsHub.Report" / pbir["datasetReference"]["byPath"]["path"] / "definition.pbism").exists()


def test_the_pbix_is_a_package_with_a_model_and_a_report_and_the_screenshots_exist():
    with zipfile.ZipFile(PBI / "AnalyticsHub.pbix") as z:
        names = set(z.namelist())
    assert {"DataModel", "Report/definition/report.json"} <= names
    for shot in ("sales-and-ads.png", "data-checks.png"):
        assert (ROOT / "docs" / "powerbi" / shot).stat().st_size > 20000


# ------------------------------------------------------------------ the check itself


def measured_like(expected):
    """What the model would report if every measure were right."""
    return copy.deepcopy({k: expected[k] for k in ("rows", "totals", "monthly", "overclaim", "missing", "partial")}) | {
        "last_day": {"avg7": expected["last_day"]["avg7"], "mtd": expected["last_day"]["mtd"]}}


def test_the_comparison_passes_when_the_model_agrees_and_fails_on_each_kind_of_difference():
    want = check_model.expected_figures()
    assert all(ok for _, ok, _ in check_model.compare(want, measured_like(want)))
    cases = {
        "one dollar of net sales": lambda m: m["totals"].__setitem__("net", m["totals"]["net"] + 1),
        "a month's order count": lambda m: m["monthly"]["2026-07"].__setitem__("orders", m["monthly"]["2026-07"]["orders"] + 1),
        "a missing day dropped": lambda m: m["missing"]["ga4"].pop(),
        "an overclaim day dropped": lambda m: m["overclaim"].pop("2026-08-04"),
        "a failed sync forgotten": lambda m: m["partial"].__setitem__("ga4", 0),
        "the trailing average": lambda m: m["last_day"].__setitem__("avg7", 0),
        "a row count": lambda m: m["rows"].__setitem__("Snapshot", 1),
    }
    for label, break_it in cases.items():
        broken = measured_like(want)
        break_it(broken)
        failed = [n for n, ok, _ in check_model.compare(want, broken) if not ok]
        assert failed, label + " went unnoticed"


def test_expected_figures_follow_the_files_not_a_constant(tmp_path):
    for p in (PBI / "data").glob("*.csv"):
        (tmp_path / p.name).write_bytes(p.read_bytes())
    before = check_model.expected_figures(tmp_path)["totals"]["net"]
    rows = (tmp_path / "fact_snapshot.csv").read_text(encoding="utf-8").splitlines()
    i = next(i for i, r in enumerate(rows) if ",net_sales," in r)
    parts = rows[i].split(",")
    parts[3] = str(int(parts[3]) + 500)
    rows[i] = ",".join(parts)
    (tmp_path / "fact_snapshot.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert check_model.expected_figures(tmp_path)["totals"]["net"] == before + 5


# ------------------------------------------------------------------ the real database


def _db_reachable(dsn):
    import asyncpg

    async def go():
        conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres", timeout=3)
        await conn.close()

    try:
        asyncio.run(go())
        return True
    except Exception:
        return False


DSN = os.environ.get("POWERBI_DEMO_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/powerbi_demo")


@pytest.mark.skipif(not _db_reachable(DSN) and not os.environ.get("POWERBI_REQUIRE_DB"), reason="no Postgres on 5433 (docker compose up -d --wait db)")
def test_the_export_through_the_hubs_own_tables_matches_the_formulas_byte_for_byte():
    assert _db_reachable(DSN), "POWERBI_REQUIRE_DB is set and Postgres is not answering"
    got = asyncio.run(export_data.load_and_export(DSN))
    assert got == export_data.expected_files()
