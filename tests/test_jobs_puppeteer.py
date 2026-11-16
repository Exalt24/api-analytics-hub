"""Puppeteer and Playwright read the same fields from the same pages, because they run the same inventory file (app/jobs/inventory.js).

Needs `npm install` in n8n/jobs (puppeteer-core, no browser download) and the Chromium Playwright installs; the suite skips with that reason when either is missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from app.jobs import formfill  # noqa: E402
from app.jobs.browser import open_page  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from jobs_fixtures import run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HAS_PUPPETEER = (ROOT / "n8n" / "jobs" / "node_modules" / "puppeteer-core").exists()
pytestmark = pytest.mark.skipif(not (shutil.which("node") and HAS_PUPPETEER), reason="needs node and `npm install` in n8n/jobs (puppeteer-core)")


def chromium_path() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return p.chromium.executable_path


def via_puppeteer(url: str) -> list[dict]:
    p = subprocess.run(["node", str(ROOT / "n8n" / "jobs" / "puppeteer_inventory.js"), url, "--chrome", chromium_path()], capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def via_playwright(url: str) -> list[dict]:
    async def go():
        async with open_page("local") as op:
            await op.page.goto(url)
            return await op.page.evaluate(formfill.INVENTORY_JS)

    return run(go())


@pytest.mark.parametrize("path", ["/gh/jobs/1", "/gh/jobs/2", "/gh/jobs/5", "/wd/jobs/1"])
def test_both_drivers_read_the_same_fields(path):
    with FakeATS() as ats:
        a, b = via_playwright(ats.url + path), via_puppeteer(ats.url + path)
    assert len(a) >= 3 and a == b


def test_the_inventory_names_the_labels_selectors_and_required_flags_it_is_used_for():
    with FakeATS() as ats:
        fields = {f["label"]: f for f in via_puppeteer(ats.url + "/gh/jobs/1")}
    email = fields["Email *"]
    assert email["selector"] == "#email" and email["required"] and email["type"] == "email"
    assert fields["Resume/CV *"]["kind"] == "file" and fields["Gender"]["options"] == ["Female", "Male", "Decline to self identify"]
    assert not fields["LinkedIn Profile"]["required"]
