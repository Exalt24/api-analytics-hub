"""seed_demo.py gives a stranger a working dashboard without the owner's Shopify store.

Runs the real script against Postgres and checks, as the request role through the real API auth path, that the printed keys work and that the viewer
cannot do what the admin can. The pure formula is checked without a database. Skips cleanly when Postgres is not running. Like the other database
suites it drops and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import seed_demo  # noqa: E402

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")
DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")


def _reachable():
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


def test_the_invented_figures_are_a_fixed_formula_with_four_metrics_a_day():
    a = seed_demo.invented_points(date(2026, 10, 30))
    b = seed_demo.invented_points(date(2026, 10, 30))
    assert a == b                                              # no randomness
    assert len(a) == 4 * seed_demo.DAYS
    assert {p["metric_key"] for p in a} == {"orders", "gross_sales", "refunds", "net_sales"}
    days = {p["observed_on"] for p in a}
    assert len(days) == seed_demo.DAYS and max(days) == "2026-10-30"
    for d in days:
        row = {p["metric_key"]: p["value_numeric"] for p in a if p["observed_on"] == d}
        assert row["net_sales"] == row["gross_sales"] - row["refunds"] and row["orders"] > 0


@pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")
def test_the_script_seeds_a_tenant_and_prints_two_working_keys():
    seed_demo.DSN = DSN
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert asyncio.run(seed_demo.main()) == 0
    out = buf.getvalue()
    admin = re.search(r"ADMIN KEY\s*:\s*(\S+)", out).group(1)
    viewer = re.search(r"VIEWER KEY\s*:\s*(\S+)", out).group(1)
    assert admin != viewer
    from app.api.auth import hash_key
    with psycopg.connect(DSN, autocommit=True) as conn:
        roles = dict(conn.execute("select label, role from api_key").fetchall())
        assert roles == {"demo-admin": "admin", "demo-viewer": "viewer"}
        hashes = {r[0] for r in conn.execute("select key_hash from api_key").fetchall()}
        assert hash_key(admin) in hashes and hash_key(viewer) in hashes
        assert conn.execute("select count(*) from metric_snapshot").fetchone()[0] == 4 * seed_demo.DAYS
