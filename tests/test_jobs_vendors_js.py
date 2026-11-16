"""n8n/jobs/pull.js and normalize.js against the stand-in vendor server, over real HTTP, run by Node.

What is shown: each of the nine pulls gets its vendor's documented request accepted (the stand-in refuses a missing header, a wrong key, an
unaccepted filter), pages and cursors are followed to the end, the normaliser turns each vendor's record shape into the same canonical job,
the overlap between vendors collapses to one dedupe key, and a throttle (429 with Retry-After) is waited out while a wrong key is an error that
names the status and does not retry.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.connectors.fake_http import Fault
from app.jobs.fake_vendors import JOBS, KEYS, SLICES, FakeVendors, expected_distinct

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def cfg_for(vendor: str, **extra) -> dict:
    k = KEYS[vendor]
    c = {"apiKey": k[0] if isinstance(k, tuple) else k}
    if isinstance(k, tuple):
        c["apiToken"] = k[1]
    if vendor == "theirstack":
        c.update(pageSize=5)
    if vendor == "hirebase":
        c.update(pageSize=6, since="2026-11-01T00:00:00Z")
    if vendor == "crustdata":
        c.update(pageSize=5, since="2026-11-01")
    if vendor == "jobdataapi":
        c.update(pageSize=5)
    if vendor == "predictleads":
        c.update(domains=["acme.com", "globex.com", "initech.com", "umbrella.com", "hooli.com", "stark.com"])
    if vendor == "techmap":
        c.update(host="daily-international-job-postings.p.rapidapi.com", countryCode="us")
    if vendor == "apify":
        c.update(actorId="someone~job-scraper")
    c.update(extra)
    return c


def run_node(server: FakeVendors, vendors: dict) -> dict:
    p = subprocess.run(["node", str(ROOT / "n8n" / "jobs" / "pull_cli.js")], input=json.dumps({"baseUrl": server.url, "vendors": vendors}), capture_output=True, text=True,
                       encoding="utf-8", timeout=120, check=False)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.parametrize("vendor", sorted(SLICES))
def test_each_vendor_is_pulled_to_the_end_and_every_job_normalises(vendor):
    with FakeVendors() as srv:
        out = run_node(srv, {vendor: cfg_for(vendor)})[vendor]
    assert "error" not in out, out
    want = len(SLICES[vendor])
    assert out["received"] == want and out["invalid"] == 0 and len(out["jobs"]) == want
    first = out["jobs"][0]
    assert first["vendor"] == vendor and first["title"] and first["apply_url"] and first["dedupe_key"]
    assert all(j["ats"] in ("greenhouse", "lever", "workday", "ashby", None) for j in out["jobs"])


def test_the_same_job_from_different_vendors_has_the_same_dedupe_key_and_the_union_is_the_expected_count():
    with FakeVendors() as srv:
        out = run_node(srv, {v: cfg_for(v) for v in SLICES})
    keys = {j["dedupe_key"] for v in out.values() for j in v["jobs"]}
    assert len(keys) == expected_distinct()
    total = sum(len(v["jobs"]) for v in out.values())
    assert total > len(keys), "the slices overlap, so there are more records than distinct jobs"
    ats = {j["ats"] for v in out.values() for j in v["jobs"]}
    assert {"greenhouse", "lever", "workday", "ashby"} <= ats


def test_paging_really_happened_for_the_paged_vendors():
    with FakeVendors() as srv:
        run_node(srv, {"theirstack": cfg_for("theirstack"), "hirebase": cfg_for("hirebase"), "crustdata": cfg_for("crustdata"), "jobdataapi": cfg_for("jobdataapi")})
        assert srv.pulls["theirstack"] == 3 and srv.pulls["hirebase"] == 3 and srv.pulls["crustdata"] == 3 and srv.pulls["jobdataapi"] == 3


def test_a_wrong_key_is_an_error_that_says_so_and_is_not_retried():
    with FakeVendors() as srv:
        out = run_node(srv, {"hirebase": cfg_for("hirebase", apiKey="nope")})["hirebase"]
        assert "answered 401" in out["error"]
        assert len([r for r in srv.requests if r.path == "/v2/jobs/search"]) == 1


def test_a_429_with_retry_after_is_waited_out_and_the_pull_still_completes():
    with FakeVendors() as srv:
        srv.faults.append(Fault(429, {"error": "slow down"}, {"Retry-After": "0"}))
        out = run_node(srv, {"theirstack": cfg_for("theirstack")})["theirstack"]
        assert "error" not in out and out["received"] == len(SLICES["theirstack"])
        assert len([r for r in srv.requests if r.path == "/v1/jobs/search"]) == 4


def test_techmap_sends_the_rapidapi_headers_and_follows_next_cursor_to_the_end():
    with FakeVendors() as srv:
        out = run_node(srv, {"techmap": cfg_for("techmap")})["techmap"]
        calls = [r for r in srv.requests if r.path == "/api/v2/jobs/search"]
        assert out["received"] == len(SLICES["techmap"]) == 8 and len(calls) == 3
        assert calls[0].query["cursor"] == "true" and calls[1].query["cursor"] == "cur-3"
        assert all(r.headers["x-rapidapi-host"] for r in calls)
        assert out["jobs"][0]["description"] and out["jobs"][0]["ats"] in ("greenhouse", "lever", "workday", "ashby", None)
    with FakeVendors() as srv:
        assert "401" in run_node(srv, {"techmap": cfg_for("techmap", apiKey="wrong")})["techmap"]["error"]


def test_the_bright_data_snapshot_is_polled_until_ready_and_a_missing_snapshot_id_is_an_error():
    with FakeVendors(progress_polls=3) as srv:
        out = run_node(srv, {"brightdata": cfg_for("brightdata")})["brightdata"]
        assert out["received"] == len(SLICES["brightdata"])
        assert len([r for r in srv.requests if r.path.startswith("/datasets/v3/progress/")]) == 4
    with FakeVendors() as srv:
        srv.faults.append(Fault(200, {"nothing": True}))
        assert "no snapshot_id" in run_node(srv, {"brightdata": cfg_for("brightdata")})["brightdata"]["error"]


def test_coresignal_follows_the_next_page_header_and_collects_each_id():
    with FakeVendors() as srv:
        out = run_node(srv, {"coresignal": cfg_for("coresignal")})["coresignal"]
        searches = [r for r in srv.requests if r.path.endswith("/search/es_dsl")]
        collects = [r for r in srv.requests if "/collect/" in r.path]
        assert len(searches) == 2 and len(collects) == len(SLICES["coresignal"]) == out["received"]
        assert "after" in searches[1].query


def test_predictleads_sends_both_headers_and_merges_the_included_company():
    with FakeVendors() as srv:
        out = run_node(srv, {"predictleads": cfg_for("predictleads")})["predictleads"]
        assert out["received"] == len(SLICES["predictleads"]) and all(j["company_name"] for j in out["jobs"])
        assert all(r.headers.get("x-api-token") == "pl-token" for r in srv.requests)
    with FakeVendors() as srv:
        out = run_node(srv, {"predictleads": cfg_for("predictleads", apiToken="wrong")})["predictleads"]
        assert "401" in out["error"]


def test_the_data_set_has_the_variety_the_dedupe_needs():
    assert len(JOBS) == 30 and expected_distinct() == 30 and len(SLICES) == 10
    assert sum(len(s) for s in SLICES.values()) > 30
