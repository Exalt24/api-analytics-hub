"""The report's database reads against real Postgres, as the request role.

Proves three things the pure tests cannot: the report reads only its own tenant's rows
through row level security, a failed sync run is not coverage (its days stay missing), and
the whole path from stored rows to a saved deck works. The world is the hand-made rows from
report_fixtures.py plus a second tenant whose figures must never appear.

Skips cleanly when Postgres is not running (`docker compose up -d`). Like the other
database suites it drops and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

from app.db import Database  # noqa: E402
from app.report.monthly import ReportError  # noqa: E402
from app.report.service import generate, load_inputs  # noqa: E402
from report_fixtures import AS_OF, both_months, day_rows  # noqa: E402

RUNS = (  # (start, end, status): the failed one covers the hole of October 10 and 11
    (date(2026, 9, 1), date(2026, 10, 10), "succeeded"),
    (date(2026, 10, 10), date(2026, 10, 12), "failed"),
    (date(2026, 10, 12), date(2026, 11, 1), "partial"),
)


def run(coro):
    return asyncio.run(coro)


def _seed(cur, tenant, name, rows, *, runs=RUNS):
    cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
    cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tenant, name, name))
    cur.execute(
        "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
        "values (%s, 'shopify', %s, %s, %s) returning id",
        (tenant, name + ".myshopify.com", name + " store", b"ciphertext"),
    )
    cid = cur.fetchone()[0]
    payload = [{"tenant_id": str(tenant), "connection_id": str(cid), "metric_key": r["metric_key"],
                "observed_on": r["observed_on"].isoformat(), "value_numeric": r["value_numeric"],
                "currency": r["currency"], "raw": "{}"} for r in rows]
    cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
    for start, end, status in runs:
        cur.execute(
            "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
            "values (%s, %s, 'schedule', %s, %s, %s, now())", (tenant, cid, start, end, status))
    return cid


@pytest.fixture(scope="module")
def world():
    a, b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cid_a = _seed(cur, a, "tenant-a", both_months())
        # the other tenant sells far more on the same days; none of it may reach tenant A's report
        _seed(cur, b, "tenant-b", day_rows(date(2026, 10, 1), 999, 9_990_000, 0, 500))
    return a, b, cid_a


def _db():
    return Database(DSN, max_size=2)


async def _inputs(tenant, **kw):
    db = _db()
    await db.connect()
    try:
        return await load_inputs(db, str(tenant), 2026, 10, **kw)
    finally:
        await db.close()


def test_the_report_reads_only_its_own_tenants_rows(world):
    a, _, _ = world
    inputs = run(_inputs(a))
    assert inputs.label == "tenant-a store"
    assert {r["value_numeric"] for r in inputs.rows if r["metric_key"] == "orders"} == {10, 5, 12, 8}
    assert not any(r["value_numeric"] == 9_990_000 for r in inputs.rows)


def test_a_failed_run_is_not_coverage_but_a_partial_one_is(world):
    a, _, _ = world
    covered = run(_inputs(a)).covered
    assert covered == [(date(2026, 9, 1), date(2026, 10, 10)), (date(2026, 10, 12), date(2026, 11, 1))]


def test_a_connection_that_belongs_to_another_tenant_is_not_found(world):
    a, b, cid_a = world
    with pytest.raises(ReportError, match="no active connection"):
        run(_inputs(b, connection_id=str(cid_a)))


def test_from_stored_rows_to_a_saved_deck(world, tmp_path):
    a, _, cid_a = world

    async def go():
        db = _db()
        await db.connect()
        try:
            return await generate(db, str(a), 2026, 10, tmp_path / "out.pptx", connection_id=str(cid_a), as_of=AS_OF)
        finally:
            await db.close()

    path, report, insights = run(go())
    assert path.exists() and insights.source == "template"
    assert report.kpi("net_sales").current_display == "USD 210.00"
    assert report.current.missing_days == [date(2026, 10, 10), date(2026, 10, 11)]


# ------------------------------------------------------------ the command line

def _cli(args, *, extra_env=None):
    import subprocess

    env = dict(os.environ, DATABASE_URL=DSN, **(extra_env or {}))
    for name in ("GROQ_API_KEY", "LLM_API_KEY"):
        if not extra_env or name not in extra_env:
            env.pop(name, None)
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "monthly_report.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, cwd=ROOT)


def test_the_command_writes_a_deck_and_prints_it_back(world, tmp_path):
    a, _, cid_a = world
    out = tmp_path / "cli.pptx"
    done = _cli(["--month", "2026-10", "--tenant", str(a), "--connection", str(cid_a), "--out", str(out),
                 "--as-of", AS_OF.isoformat(), "--dump"])
    assert done.returncode == 0, done.stderr
    assert out.exists()
    assert "insights: template" in done.stdout and "Net sales rose 50.0% on September 2026 to USD 210.00" in done.stdout


def test_the_command_exits_2_with_the_reason_when_it_cannot_report(world, tmp_path):
    a, b, cid_a = world
    done = _cli(["--month", "2026-10", "--tenant", str(b), "--connection", str(cid_a), "--out", str(tmp_path / "x.pptx"),
                 "--as-of", AS_OF.isoformat()])
    assert done.returncode == 2
    assert "no report: no active connection" in done.stderr
    assert not (tmp_path / "x.pptx").exists()


def test_a_model_key_is_never_printed_and_no_model_forces_the_template(world, tmp_path):
    a, _, cid_a = world
    done = _cli(["--month", "2026-10", "--tenant", str(a), "--connection", str(cid_a), "--out", str(tmp_path / "k.pptx"),
                 "--as-of", AS_OF.isoformat(), "--no-model", "--dump"], extra_env={"GROQ_API_KEY": "sentinel-key-123"})
    assert done.returncode == 0
    assert "sentinel-key-123" not in done.stdout + done.stderr
    assert "insights: template" in done.stdout


def test_a_bad_tenant_id_exits_2_without_a_traceback(world, tmp_path):
    done = _cli(["--month", "2026-10", "--tenant", "not-a-uuid", "--out", str(tmp_path / "y.pptx")])
    assert done.returncode == 2
    assert "Traceback" not in done.stderr
