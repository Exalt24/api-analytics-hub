# LIFESPAN: permanent
"""Rebuild the sample monthly report from invented data, through the real code path.

    python scripts/build_sample_report.py [--out docs/monthly-report-sample]

It makes a scratch database on the server named by ANALYTICS_TEST_DSN (default: the compose
Postgres on port 5433), applies migrations 001, 002 and 006, loads the invented store snapshots and
sync runs from app/report/demo_data.py, and gets the invented website, ad and search figures the way a real run
would: each connector reads them over HTTP from its stand-in server (app/connectors/ga4_fake.py, meta_fake.py,
gsc_fake.py) and the sync runner stores them (as the sync_worker role). It records one failed run for each
source over the days it leaves without data, runs the same `generate` the command line uses (as the
app_request role, so row level security applies), writes the deck and a text read-back, and drops the scratch
database. It never touches the database the DSN names. The password in the DSN is never printed.

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

from app.connectors.ga4 import GA4Connector  # noqa: E402
from app.connectors.ga4_fake import FakeGA4  # noqa: E402
from app.connectors.gsc import GSCConnector  # noqa: E402
from app.connectors.gsc_fake import FakeGSC  # noqa: E402
from app.connectors.meta import MetaConnector  # noqa: E402
from app.connectors.meta_fake import FakeMeta  # noqa: E402
from app.db import Database  # noqa: E402
from app.report.deck import dump_deck  # noqa: E402
from app.report.demo_data import (  # noqa: E402
    DEMO_RUNS,
    PAID_CURRENCY,
    PAID_FAILED_WINDOW,
    PAID_SYNC_WINDOWS,
    SEARCH_FAILED_WINDOW,
    SEARCH_SYNC_WINDOWS,
    WEB_FAILED_WINDOW,
    WEB_SYNC_WINDOWS,
    demo_paid_days,
    demo_points,
    demo_search_days,
    demo_web_days,
)
from app.report.service import generate  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

DEFAULT_DSN = "postgresql://analytics:analytics@127.0.0.1:5433/analytics"
YEAR, MONTH = 2026, 10
#: The date the sample treats as today, so the deck is the same on every machine and day.
AS_OF = date(2026, 11, 2)
DATA_NOTE = "Sample report: invented figures. The website, ad and search sides ran against stand-in servers, not Google or Meta."
#: The invented property and token the stand-in server answers for. Neither is real.
SAMPLE_PROPERTY = "100000001"
SAMPLE_TOKEN = "sample-token-not-a-credential"
SAMPLE_AD_ACCOUNT = "200000002"
SAMPLE_SITE = "sc-domain:example-outfitters.test"
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


def seed(dsn: str) -> tuple[str, str, str, str, str]:
    """Load the invented tenant, store connection, snapshots and runs, and empty website, ad and search connections.

    Returns (tenant, store, website, ads, search) ids. The other three sources' rows come from the sync functions.
    """
    tenant, connection, website = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    ads, search = str(uuid.uuid4()), str(uuid.uuid4())
    payload = [
        {"tenant_id": tenant, "connection_id": connection, "metric_key": p.metric_key,
         "observed_on": p.observed_on.isoformat(), "value_numeric": p.value_numeric,
         "currency": p.currency, "raw": "{}"}
        for p in demo_points()
    ]
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for name in ("001_init.sql", "002_control_plane.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "008_search_metrics.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
        cur.execute("insert into tenant (id, slug, name) values (%s, 'demo', 'Demo')", (tenant,))
        cur.execute(
            "insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc) "
            "values (%s, %s, 'shopify', 'invented-demo.myshopify.com', 'Example Outfitters', %s)",
            (connection, tenant, b"no credential, invented data"),
        )
        cur.execute(
            "insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc) "
            "values (%s, %s, 'ga4', 'properties/' || %s, 'Example site', %s)",
            (website, tenant, SAMPLE_PROPERTY, b"no credential, invented data"),
        )
        for connection_id, platform, account, name in ((ads, "meta", "act_" + SAMPLE_AD_ACCOUNT, "Example ads"),
                                                       (search, "gsc", SAMPLE_SITE, "Example search")):
            cur.execute(
                "insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc) "
                "values (%s, %s, %s, %s, %s, %s)",
                (connection_id, tenant, platform, account, name, b"no credential, invented data"),
            )
        cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
        for run in DEMO_RUNS:
            cur.execute(
                "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                "values (%s, %s, 'schedule', %s, %s, %s, now())",
                (tenant, connection, run.window_start, run.window_end, run.status),
            )
        # the days the website sync left without data: a run that failed over them, which is not coverage
        cur.execute(
            "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
            "values (%s, %s, 'schedule', %s, %s, %s, now())",
            (tenant, website, WEB_FAILED_WINDOW.window_start, WEB_FAILED_WINDOW.window_end, WEB_FAILED_WINDOW.status),
        )
        for connection_id, failed in ((ads, PAID_FAILED_WINDOW), (search, SEARCH_FAILED_WINDOW)):
            cur.execute(
                "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                "values (%s, %s, 'schedule', %s, %s, %s, now())",
                (tenant, connection_id, failed.window_start, failed.window_end, failed.status),
            )
    return tenant, connection, website, ads, search


async def sync_website(dsn: str, tenant: str, website: str) -> None:
    """Fill the website connection through the GA4 connector, the stand-in server and the sync runner."""
    import httpx

    db = Database(dsn, max_size=2, role="sync_worker")
    await db.connect()
    try:
        with FakeGA4(token=SAMPLE_TOKEN) as server:
            server.set_days(SAMPLE_PROPERTY, demo_web_days())
            for start, end in WEB_SYNC_WINDOWS:
                connector = GA4Connector(
                    property_id=SAMPLE_PROPERTY, access_token=SAMPLE_TOKEN, base_url=server.url,
                    client=httpx.AsyncClient(trust_env=False, verify=False, timeout=10))
                try:
                    outcome = await SyncRunner(db).run_once(
                        tenant_id=tenant, connection_id=website, connector=connector,
                        window_start=start, window_end=end, trigger="backfill")
                finally:
                    await connector.aclose()
                if outcome.status != "succeeded":
                    raise SystemExit("the website sync did not succeed: %s %s" % (outcome.status, outcome.error_code))
                print("website sync %s to %s: %d rows" % (start, end, outcome.rows_written))
    finally:
        await db.close()


async def sync_ads_and_search(dsn: str, tenant: str, ads: str, search: str) -> None:
    """Fill the ad and search connections through their connectors, the stand-in servers and the sync runner."""
    import httpx

    def client():
        return httpx.AsyncClient(trust_env=False, verify=False, timeout=10)

    db = Database(dsn, max_size=2, role="sync_worker")
    await db.connect()
    try:
        with FakeMeta(token=SAMPLE_TOKEN) as meta, FakeGSC(token=SAMPLE_TOKEN, today=AS_OF) as gsc:
            meta.set_account(SAMPLE_AD_ACCOUNT, demo_paid_days(), currency=PAID_CURRENCY)
            days, queries, pages = demo_search_days()
            gsc.set_site(SAMPLE_SITE, days, queries=queries, pages=pages)
            for label, connection, windows, build in (
                ("ads", ads, PAID_SYNC_WINDOWS, lambda: MetaConnector(
                    ad_account_id=SAMPLE_AD_ACCOUNT, access_token=SAMPLE_TOKEN, base_url=meta.url, client=client())),
                ("search", search, SEARCH_SYNC_WINDOWS, lambda: GSCConnector(
                    site_url=SAMPLE_SITE, access_token=SAMPLE_TOKEN, base_url=gsc.url, client=client())),
            ):
                for start, end in windows:
                    connector = build()
                    try:
                        outcome = await SyncRunner(db).run_once(
                            tenant_id=tenant, connection_id=connection, connector=connector,
                            window_start=start, window_end=end, trigger="backfill")
                    finally:
                        await connector.aclose()
                    if outcome.status != "succeeded":
                        raise SystemExit("the %s sync did not succeed: %s %s" % (label, outcome.status, outcome.error_code))
                    print("%s sync %s to %s: %d rows" % (label, start, end, outcome.rows_written))
    finally:
        await db.close()


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
        tenant, connection, website, ads, search = seed(scratch_dsn)
        await sync_website(scratch_dsn, tenant, website)
        await sync_ads_and_search(scratch_dsn, tenant, ads, search)
        await db.connect()
        outdir.mkdir(parents=True, exist_ok=True)
        deck = outdir / ("report_%04d-%02d.pptx" % (YEAR, MONTH))
        path, report, insights = await generate(
            db, tenant, YEAR, MONTH, deck, connection_id=connection, web_connection_id=website,
            paid_connection_id=ads, search_connection_id=search, data_note=DATA_NOTE, as_of=AS_OF,
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
