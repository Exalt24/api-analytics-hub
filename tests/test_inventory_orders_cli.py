"""The purchase order and workbook commands of scripts/inventory_plan.py, run as a real process against Postgres as the request role: make a draft from the plan, place it, receive part
of it, list it, raise its late alert, write the workbook, and see the refusals as a message and exit code 2.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from inventory_fixtures import DSN, TODAY, add_tenant, needs_db, reset_schema, run  # noqa: E402

pytestmark = needs_db

from app.db import Database  # noqa: E402
from app.inventory import store  # noqa: E402
from test_inventory_db import World, seed  # noqa: E402

SCRIPT = ROOT / "scripts" / "inventory_plan.py"


@pytest.fixture(scope="module")
def tenant():
    reset_schema()
    w = World()
    w.a = str(add_tenant("cli-a"))

    async def build():
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            await seed(db, w.a)
            async with db.tenant(w.a) as conn:
                await store.make_plan(conn, w.a, TODAY)
        finally:
            await db.close()

    run(build())
    return w.a


def cli(tenant: str, *args: str):
    env = {**os.environ, "DATABASE_URL": DSN, "INVENTORY_TENANT": tenant, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120, encoding="utf-8")


def test_the_order_commands_take_an_order_from_the_plan_to_received(tenant, tmp_path):
    made = cli(tenant, "order-create", "--supplier", "Acme Goods", "--by", "cli test")
    assert made.returncode == 0, made.stderr
    assert "60 x TEE-1" in made.stdout
    oid = made.stdout.split()[1]
    assert len(oid) == 36

    again = cli(tenant, "order-create", "--supplier", "Acme Goods")
    assert again.returncode == 2 and "already open" in again.stderr                    # a refusal is a message and exit code 2

    now = datetime.now(timezone.utc).date()                                            # the command reads the real clock for "late", so the dates here move with it
    ordered_on = now - timedelta(days=4)
    placed = cli(tenant, "order-move", "--order", oid[:8], "--to", "ordered", "--on", ordered_on.isoformat())
    assert placed.returncode == 0 and "is now ordered, expected %s" % (ordered_on + timedelta(days=9)) in placed.stdout           # the order date plus the nine days
    illegal = cli(tenant, "order-move", "--order", oid[:8], "--to", "draft")
    assert illegal.returncode == 2 and "cannot move from ordered to draft" in illegal.stderr

    listed = cli(tenant, "orders", "--open")
    assert listed.returncode == 0 and oid[:8] in listed.stdout and "LATE" not in listed.stdout and "TEE-1 0/60" in listed.stdout.replace("  ", " ")
    moved = cli(tenant, "order-expect", "--order", oid[:8], "--date", (now + timedelta(days=11)).isoformat())
    assert moved.returncode == 0 and "expected %s" % (now + timedelta(days=11)) in moved.stdout

    part = cli(tenant, "order-receive", "--order", oid[:8], "--line", "TEE-1", "--qty", "25")
    assert part.returncode == 0 and "is now partly_received" in part.stdout
    over = cli(tenant, "order-receive", "--order", oid[:8], "--line", "TEE-1", "--qty", "36")
    assert over.returncode == 2 and "more than was ordered" in over.stderr
    rest = cli(tenant, "order-receive", "--order", oid[:8], "--line", "TEE-1", "--qty", "35")
    assert rest.returncode == 0 and "is now received" in rest.stdout
    assert cli(tenant, "order-receive", "--order", "no-such", "--line", "TEE-1", "--qty", "1").returncode == 2


def test_a_late_order_is_raised_by_the_alerts_command_and_listed_by_the_digest(tenant):
    made = cli(tenant, "order-create", "--supplier", "Acme Goods")
    oid = made.stdout.split()[1]
    assert cli(tenant, "order-move", "--order", oid[:8], "--to", "ordered", "--on", "2026-10-01", "--expected", "2026-10-15").returncode == 0
    assert "LATE" in cli(tenant, "orders", "--open").stdout
    first = cli(tenant, "alerts", "--as-of", "2026-11-24")
    second = cli(tenant, "alerts", "--as-of", "2026-11-24")
    assert first.returncode == 0 and "late orders raised 1, already raised 0" in first.stdout
    assert "late orders raised 0, already raised 1" in second.stdout
    digest = cli(tenant, "digest", "--as-of", "2026-11-24")
    assert digest.returncode == 1 and "was expected 2026-10-15" in digest.stdout and "days late" in digest.stdout.replace("day(s) late", "days late")


def test_the_workbook_command_writes_a_file_with_formulas(tenant, tmp_path):
    from openpyxl import load_workbook
    out = tmp_path / "plan.xlsx"
    done = cli(tenant, "workbook", "--out", str(out), "--promo-factor", "1.5", "--promo-from", "2026-12-01", "--promo-to", "2026-12-07")
    assert done.returncode == 0, done.stderr
    assert "promotion factor 1.50 from 2026-12-01 to 2026-12-07" in done.stdout
    wb = load_workbook(out)
    assert wb.sheetnames == ["Inputs", "Plan", "Classes", "Scenario"] and wb["Inputs"]["B6"].value == 1.5
    assert str(wb["Plan"]["T2"].value).startswith("=")
    bad = cli(tenant, "workbook", "--out", str(out), "--service-level", "1.5")
    assert bad.returncode == 2 and "service level" in bad.stderr
