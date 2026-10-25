# LIFESPAN: permanent
"""Rebuild the sample monthly report from invented data, through the real code path.

    python scripts/build_sample_report.py [--out docs/monthly-report-sample]

It makes a scratch database on the server named by ANALYTICS_TEST_DSN (default: the compose
Postgres on port 5433), applies migrations 001 and 002, loads the invented snapshots and sync
runs from app/report/demo_data.py, runs the same `generate` the command line uses (as the
app_request role, so row level security applies), writes the deck and a text read-back, and
drops the scratch database. It never touches the database the DSN names. The password in the
DSN is never printed.

If LibreOffice (soffice) and pdftoppm are found, it also renders every slide to a PNG and a
contact sheet. If either is missing it says so and leaves the text read-back as the proof.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.report.deck import dump_deck  # noqa: E402
from app.report.demo_data import DEMO_RUNS, demo_points  # noqa: E402
from app.report.service import generate  # noqa: E402

DEFAULT_DSN = "postgresql://analytics:analytics@localhost:5433/analytics"
YEAR, MONTH = 2026, 10
#: The date the sample treats as today, so the deck is the same on every machine and day.
AS_OF = date(2026, 11, 2)
DATA_NOTE = "Sample report: the orders are invented, not a real store."
RENDER_TIMEOUT_SECONDS = 180


def _swap_db(dsn: str, name: str) -> str:
    head, _, _ = dsn.rpartition("/")
    return head + "/" + name


def find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        candidate = Path(base) / "LibreOffice" / "program" / "soffice.exe" if base else None
        if candidate and candidate.exists():
            return str(candidate)
    return None


def seed(dsn: str) -> tuple[str, str]:
    """Load the invented tenant, connection, snapshots and runs. Returns (tenant, connection)."""
    tenant, connection = str(uuid.uuid4()), str(uuid.uuid4())
    payload = [
        {"tenant_id": tenant, "connection_id": connection, "metric_key": p.metric_key,
         "observed_on": p.observed_on.isoformat(), "value_numeric": p.value_numeric,
         "currency": p.currency, "raw": "{}"}
        for p in demo_points()
    ]
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for name in ("001_init.sql", "002_control_plane.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
        cur.execute("insert into tenant (id, slug, name) values (%s, 'demo', 'Demo')", (tenant,))
        cur.execute(
            "insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc) "
            "values (%s, %s, 'shopify', 'invented-demo.myshopify.com', 'Example Outfitters', %s)",
            (connection, tenant, b"no credential, invented data"),
        )
        cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
        for run in DEMO_RUNS:
            cur.execute(
                "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                "values (%s, %s, 'schedule', %s, %s, %s, now())",
                (tenant, connection, run.window_start, run.window_end, run.status),
            )
    return tenant, connection


def render(deck: Path, outdir: Path) -> list[Path]:
    """Slide PNGs and a contact sheet, or [] with the reason printed when a tool is missing."""
    soffice, pdftoppm = find_soffice(), shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        print("render skipped: %s not found; the text read-back is the proof" %
              " and ".join(n for n, t in (("soffice", soffice), ("pdftoppm", pdftoppm)) if not t))
        return []
    from PIL import Image

    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        profile = "file:///" + Path(tmp, "profile").as_posix()
        subprocess.run(
            [soffice, "-env:UserInstallation=" + profile, "--headless", "--convert-to", "pdf",
             "--outdir", tmp, str(deck)],
            check=True, capture_output=True, timeout=RENDER_TIMEOUT_SECONDS,
        )
        pdf = Path(tmp, deck.stem + ".pdf")
        subprocess.run([pdftoppm, "-r", "80", "-png", str(pdf), str(Path(tmp, "slide"))],
                       check=True, capture_output=True, timeout=RENDER_TIMEOUT_SECONDS)
        shots = sorted(Path(tmp).glob("slide-*.png"))
        for old in outdir.glob("slide-*.png"):
            old.unlink()
        kept = []
        for i, shot in enumerate(shots, 1):
            target = outdir / ("slide-%d.png" % i)
            shutil.copyfile(shot, target)
            kept.append(target)
    images = [Image.open(p).convert("RGB") for p in kept]
    w, h = images[0].size
    cols = 2
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w + (cols + 1) * 12, rows * h + (rows + 1) * 12), (225, 229, 234))
    for i, img in enumerate(images):
        sheet.paste(img, (12 + (i % cols) * (w + 12), 12 + (i // cols) * (h + 12)))
    sheet_path = outdir / "contact-sheet.png"
    sheet.save(sheet_path, optimize=True)
    return kept + [sheet_path]


async def build(dsn: str, outdir: Path) -> int:
    scratch = "report_sample_" + uuid.uuid4().hex[:8]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute('create database "%s"' % scratch)
    scratch_dsn = _swap_db(dsn, scratch)
    db = Database(scratch_dsn, max_size=2)
    try:
        tenant, connection = seed(scratch_dsn)
        await db.connect()
        outdir.mkdir(parents=True, exist_ok=True)
        deck = outdir / ("report_%04d-%02d.pptx" % (YEAR, MONTH))
        path, report, insights = await generate(
            db, tenant, YEAR, MONTH, deck, connection_id=connection, data_note=DATA_NOTE, as_of=AS_OF,
        )
        text = dump_deck(path)
        (outdir / ("report_%04d-%02d.txt" % (YEAR, MONTH))).write_text(text + "\n", encoding="utf-8", newline="\n")
        print("deck %s: %d slides, insights from %s" % (path.name, len(text.split("--- slide ")) - 1, insights.source))
        for item in render(path, outdir):
            print("rendered", item.name)
        return 0
    finally:
        await db.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute('drop database if exists "%s" with (force)' % scratch)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(ROOT / "docs" / "monthly-report-sample"))
    args = p.parse_args(argv)
    return asyncio.run(build(os.environ.get("ANALYTICS_TEST_DSN", DEFAULT_DSN), Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())
