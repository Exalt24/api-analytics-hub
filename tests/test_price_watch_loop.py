"""Price watch, the whole loop on the invented demo data: scan real pages in Chromium, match, report, approve, dry run, sync to the fake vendors.

Needs Postgres and Chromium; skips cleanly without either. The competitor pages are fake_site.py on a local port, the vendors are fakes.py in
process. This is the same sequence docs/price-watch.md gives a stranger, driven through scripts/price_watch.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price_watch_fixtures import DAY, DSN, add_tenant, needs_db, reset_schema, run  # noqa: E402

from app.db import Database  # noqa: E402
from app.price_watch import demo, proposals, store  # noqa: E402
from app.price_watch.fake_site import FakeSite  # noqa: E402
from app.price_watch.scraper import PageScraper, browser_available  # noqa: E402

pytestmark = [needs_db, pytest.mark.skipif(not (os.environ.get("PRICE_WATCH_REQUIRE_BROWSER") or browser_available()),
                                           reason="no Chromium for Playwright here (python -m playwright install chromium)")]


def load_cli():
    spec = importlib.util.spec_from_file_location("price_watch_cli", ROOT / "scripts" / "price_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def loop(monkeypatch):
    """The demo catalogue and its eight listings, pointed at a FakeSite on a free port."""
    reset_schema()
    tenant = add_tenant("price-watch-demo")
    with FakeSite() as site:
        async def seed():
            db = Database(DSN, max_size=3)
            await db.connect()
            try:
                async with db.tenant(str(tenant)) as conn:
                    await demo.load_demo(conn, str(tenant), site.base)
            finally:
                await db.close()
        run(seed())
        monkeypatch.setenv("DATABASE_URL", DSN)
        monkeypatch.setenv("PRICE_WATCH_TENANT", str(tenant))
        yield tenant, site


def cli(*argv):
    return load_cli().main(list(argv))


def test_scan_matches_each_page_the_way_the_demo_says(loop, capsys):
    tenant, site = loop
    assert cli("scan") == 0
    out = capsys.readouterr().out
    assert "match: matched (same GTIN)" in out and "match: needs_review" in out and "match: no_match" in out

    async def rows():
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return {r["url"].rsplit("/", 1)[-1]: r for r in await conn.fetch(
                    "select l.url, l.match_status, l.match_method, l.match_score, p.sku from competitor_listing l left join catalogue_product p on p.id = l.catalogue_product_id")}
        finally:
            await db.close()
    got = {slug: (r["match_status"], r["match_method"], r["sku"]) for slug, r in run(rows()).items()}
    assert got == {
        "jsonld-bottle": ("matched", "gtin", "KTB-750"),
        "span-lamp": ("matched", "sku", "LMP-210"),
        "thousands-battery": ("matched", "title", "MER-48V"),
        "sale-jumper": ("matched", "sku", "HMJ-NVY-M"),
        "no-price": ("matched", "title", "TWF-2"),
        "jsonld-stove": ("matched", "gtin", "PCS-1"),
        "euro-gloves": ("needs_review", "title", None),      # 0.80, under the 0.85 auto line: left for a person
        "bottle-500": ("no_match", None, None),               # same brand, 500ml against our 750ml: not the same product
    }


def test_the_whole_loop_scan_report_approve_dry_run_sync(loop, capsys):
    tenant, site = loop
    assert cli("scan") == 0
    capsys.readouterr()

    assert cli("report") == 0
    report = capsys.readouterr().out
    assert "4 product lines where a competitor's landed price is lower than ours, out of 5 compared. 1 excluded, 1 matches need a person." in report
    assert "8.50 ESTIMATED" in report                                         # the lamp page states no shipping
    assert "floor-limited: matching would be 49.50" in report                  # the jumper is held at our 55.00 floor
    assert "no price on the page" in report
    assert "Alpen Store (invented)" in report and "title score" in report      # the gloves wait for a person
    assert "4 new proposal(s) queued" in report

    assert cli("proposals") == 0
    listing = capsys.readouterr().out
    assert listing.count("proposed") == 4 and "ESTIMATED" in listing

    # an unapproved proposal is refused, in a dry run and for real
    first = listing.split()[0]
    assert cli("sync", "--fakes") == 0 and "nothing approved to sync" in capsys.readouterr().out
    assert cli("sync", "--fakes", "--id", first) == 2
    assert "only an approved proposal can be synced" in capsys.readouterr().err
    assert cli("sync", "--fakes", "--apply", "--id", first) == 2
    assert "only an approved proposal can be synced" in capsys.readouterr().err

    ids = {line.split()[2]: line.split()[0] for line in listing.splitlines() if "proposed" in line}     # sku -> short id
    assert set(ids) == {"KTB-750", "LMP-210", "MER-48V", "HMJ-NVY-M"}
    assert cli("approve", "--id", ids["KTB-750"], "--actor", "dana@example.com") == 0
    assert cli("approve", "--id", ids["LMP-210"], "--actor", "dana@example.com") == 0
    assert cli("approve", "--id", ids["LMP-210"], "--actor", "dana@example.com") == 2           # a second approval is refused
    assert "cannot go to approved" in capsys.readouterr().err
    assert cli("reject", "--id", ids["MER-48V"], "--actor", "dana@example.com", "--note", "battery price looks like a typo") == 0
    capsys.readouterr()

    assert cli("sync", "--fakes") == 0                  # dry run by default
    dry = capsys.readouterr().out
    assert "dry run" in dry and "would send:" in dry and "FAKE vendors" in dry

    assert cli("proposals", "--state", "approved") == 0
    assert capsys.readouterr().out.count("approved") == 2          # the dry run changed nothing

    assert cli("sync", "--fakes", "--apply") == 0
    applied = capsys.readouterr().out
    assert applied.count("applied") >= 2 and "FAILED" not in applied

    async def final():
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return ({r["sku"]: r["price_minor"] for r in await conn.fetch("select sku, price_minor from catalogue_product")},
                        {r["sku"]: r["state"] for r in await proposals.list_proposals(conn)})
        finally:
            await db.close()
    prices, states = run(final())
    assert prices["KTB-750"] == 4795 and prices["LMP-210"] == 3340 and prices["MER-48V"] == 134900      # the rejected one kept its price
    assert states == {"KTB-750": "applied", "LMP-210": "applied", "MER-48V": "rejected", "HMJ-NVY-M": "proposed"}


def test_confirming_the_gloves_then_the_euro_price_is_excluded_not_compared(loop, capsys):
    tenant, site = loop
    assert cli("scan") == 0
    capsys.readouterr()
    assert cli("listings", "--status", "needs_review") == 0
    waiting = capsys.readouterr().out
    assert "euro-gloves" in waiting and waiting.count("needs_review") == 1
    listing = waiting.split()[0]
    assert cli("confirm-match", "--listing", listing, "--product", "NO-SUCH-SKU", "--actor", "dana@example.com") == 2
    assert cli("confirm-match", "--listing", listing, "--product", "ASG-L", "--actor", "dana@example.com") == 0
    assert cli("scan") == 0                  # a later scan leaves the person's decision alone
    capsys.readouterr()
    assert cli("listings", "--status", "confirmed") == 0
    assert "-> ASG-L" in capsys.readouterr().out
    assert cli("report", "--no-proposals") == 0
    out = capsys.readouterr().out
    assert "ASG-L at Alpen Store (invented): currency EUR against our AUD" in out and "## Matches that need a person" not in out


def test_report_files_are_written_and_a_missing_tenant_is_refused(loop, capsys, tmp_path, monkeypatch):
    assert cli("scan") == 0
    assert cli("report", "--out-dir", str(tmp_path), "--no-proposals") == 0
    md = next(tmp_path.glob("price-report-*.md")).read_text(encoding="utf-8")
    csv_text = next(tmp_path.glob("price-report-*.csv")).read_text(encoding="utf-8")
    assert md.startswith("# Competitor price report") and csv_text.startswith("date,sku,title")
    monkeypatch.delenv("PRICE_WATCH_TENANT")
    capsys.readouterr()
    assert cli("proposals") == 2 and "no tenant" in capsys.readouterr().err
