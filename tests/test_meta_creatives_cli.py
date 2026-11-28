"""scripts/meta_creatives.py: the CSV and the JSON file, the exit codes, and the spreadsheet formula guard.

The run is against the stand-in server (`--stand-in`, or META_API_BASE_URL pointed at one), never a live ad account.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import meta_creatives as cli  # noqa: E402
from app.connectors.meta_creatives import COLUMNS  # noqa: E402
from app.connectors.meta_creatives_fake import FakeMetaCreatives, Fault, error_body, load_sample  # noqa: E402

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_a_stand_in_run_writes_one_csv_row_and_one_json_object_per_ad(tmp_path, capsys):
    assert cli.main(["--stand-in", "--out-dir", str(tmp_path)], now=NOW) == 0
    rows = read_csv(tmp_path / "meta_creatives_555001.csv")
    doc = json.loads((tmp_path / "meta_creatives_555001.json").read_text(encoding="utf-8"))
    assert list(rows[0].keys()) == list(COLUMNS)
    assert [r["ad_id"] for r in rows] == [c["ad_id"] for c in doc["creatives"]] == ["1001", "1002", "1003", "1004", "1005", "1006", "1008"]
    assert doc["account"] == "act_555001" and doc["source"] == "stand-in" and doc["complete"] is True
    assert doc["generated_at"] == "2026-11-28T12:00:00Z" and doc["count"] == 7 and doc["stats"]["batches"] == 1
    by = {r["ad_id"]: r for r in rows}
    assert by["1001"]["primary_text"] == "Fresh deals for spring." and by["1001"]["ad_preview_link"] == "https://fb.me/preview/1001"
    assert by["1005"]["video_ids"] == "9001 | 9003" and by["1005"]["notes"] == "video 9003: no source returned"
    assert by["1003"]["video_source_urls"] == "https://video.example.test/v/9002.mp4"
    assert by["1002"]["headline"] == "Seeds | Tools | Demo reel"
    carousel = next(c for c in doc["creatives"] if c["ad_id"] == "1002")
    assert len(carousel["cards"]) == 3 and carousel["video_ids"] == ["9001"]
    assert "7 ads, 3 with a note" in capsys.readouterr().out


def test_a_campaign_and_status_filter_and_include_deleted(tmp_path):
    assert cli.main(["--stand-in", "--out-dir", str(tmp_path), "--campaign", "103", "--include-deleted"], now=NOW) == 0
    assert [r["ad_id"] for r in read_csv(tmp_path / "meta_creatives_555001.csv")] == ["1007", "1008"]
    assert cli.main(["--stand-in", "--out-dir", str(tmp_path), "--campaign", "101,102", "--status", "ACTIVE"], now=NOW) == 0
    assert [r["ad_id"] for r in read_csv(tmp_path / "meta_creatives_555001.csv")] == ["1001", "1002", "1003", "1004", "1005", "1006"]


def test_a_cell_that_would_start_a_spreadsheet_formula_is_quoted_in_the_csv_only(tmp_path):
    assert [cli.safe_cell(v) for v in ("=SUM(1)", "+1", "-5 off", "@handle", "\tx", "plain", "", "a=b")] == [
        "'=SUM(1)", "'+1", "'-5 off", "'@handle", "'\tx", "plain", "", "a=b"]
    with FakeMetaCreatives(token="t") as fake:
        fake.set_account("9", campaigns={"101": "=Campaign"}, ads=[
            {"id": "1", "name": "-Ad", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "101",
             "creative": {"id": "5", "body": "=HYPERLINK(\"http://x\")", "title": "a, \"quoted\" | piped\nline"}}])
        env = {"META_AD_ACCOUNT_ID": "9", "META_ACCESS_TOKEN": "t", "META_API_BASE_URL": fake.url}
        import os
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            assert cli.main(["--out-dir", str(tmp_path)], now=NOW) == 0
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    row = read_csv(tmp_path / "meta_creatives_9.csv")[0]
    assert row["primary_text"] == "'=HYPERLINK(\"http://x\")" and row["ad_name"] == "'-Ad" and row["campaign_name"] == "'=Campaign"
    assert row["headline"] == "a, \"quoted\" | piped\nline", "commas, quotes, pipes and newlines survive the CSV"
    doc = json.loads((tmp_path / "meta_creatives_9.json").read_text(encoding="utf-8"))
    assert doc["creatives"][0]["primary_texts"] == ["=HYPERLINK(\"http://x\")"], "the JSON file is not altered"


@pytest.fixture()
def env_server(monkeypatch):
    with FakeMetaCreatives(token="good") as fake:
        load_sample(fake, "555001")
        monkeypatch.setenv("META_AD_ACCOUNT_ID", "555001")
        monkeypatch.setenv("META_ACCESS_TOKEN", "good")
        monkeypatch.setenv("META_API_BASE_URL", fake.url)
        yield fake


def test_exit_codes_for_a_run_that_could_not_start(tmp_path, capsys, monkeypatch, env_server):
    assert cli.main(["--out-dir", str(tmp_path)], now=NOW) == 0
    capsys.readouterr()
    for argv, word in ((["--account", "abc"], "ad_account_id"), (["--status", "LIVE"], "statuses"), (["--campaign", "12x"], "campaign ids")):
        assert cli.main(["--out-dir", str(tmp_path / "x")] + argv, now=NOW) == 2
        assert word in capsys.readouterr().err
    monkeypatch.delenv("META_ACCESS_TOKEN")
    assert cli.main(["--out-dir", str(tmp_path / "x")], now=NOW) == 2
    assert "META_ACCESS_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "x").exists(), "nothing is written for a run that did not start"


def test_a_refused_token_exits_1_and_writes_nothing(tmp_path, capsys, monkeypatch, env_server):
    monkeypatch.setenv("META_ACCESS_TOKEN", "expired")
    assert cli.main(["--out-dir", str(tmp_path / "o")], now=NOW) == 1
    err = capsys.readouterr().err
    assert "the run failed (auth_expired): the access token was rejected" in err and not (tmp_path / "o").exists()


def test_a_rate_limit_exits_3_and_the_files_say_they_are_partial(tmp_path, capsys, env_server):
    env_server.utilisation = 96
    assert cli.main(["--out-dir", str(tmp_path), "--page-size", "2"], now=NOW) == 3
    assert "PARTIAL: usage is at 96 percent" in capsys.readouterr().err
    doc = json.loads((tmp_path / "meta_creatives_555001.json").read_text(encoding="utf-8"))
    assert doc["complete"] is False and doc["count"] == 2 and "96 percent" in doc["stopped_because"]
    assert len(read_csv(tmp_path / "meta_creatives_555001.csv")) == 2


def test_the_secret_is_never_written_to_the_output(tmp_path, env_server):
    assert cli.main(["--out-dir", str(tmp_path)], now=NOW) == 0
    for name in ("meta_creatives_555001.csv", "meta_creatives_555001.json"):
        assert "good" not in (tmp_path / name).read_text(encoding="utf-8").replace("goodness", "")
        assert env_server.url not in (tmp_path / name).read_text(encoding="utf-8")
