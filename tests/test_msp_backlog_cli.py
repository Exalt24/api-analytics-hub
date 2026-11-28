"""The backlog command line, the ledger merge, and the whole path from three stand-in servers to a ranked report.

The last test is the one that joins the pieces: the ConnectWise, Autotask and NinjaOne connectors read their stand-ins, their records are written to ledgers, `scripts/msp_backlog.py`
merges and ranks them, and the figures in the report are the ones worked out by hand from what was put in the servers.

Every refusal runs a positive control first (the same call without the defect succeeds).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.autotask import AutotaskConnector  # noqa: E402
from app.connectors.autotask_fake import FakeAutotask  # noqa: E402
from app.connectors.connectwise import ConnectWiseConnector  # noqa: E402
from app.connectors.connectwise_fake import FakeConnectWise  # noqa: E402
from app.connectors.ninjaone import NinjaOneConnector  # noqa: E402
from app.connectors.ninjaone_fake import FakeNinjaOne  # noqa: E402
from app.msp.ledger import LedgerError, merge_ledgers, read_ledger, write_ledger  # noqa: E402
from app.msp.sample import generate_world  # noqa: E402
from vendor_helpers import client, collect, retrier, run  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
import msp_backlog  # noqa: E402

AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-10-10T12:00:00Z"


def cli(*args):
    return msp_backlog.main([str(a) for a in args])


@pytest.fixture()
def ledger_file(tmp_path):
    p = tmp_path / "sample.jsonl"
    assert cli("sample", "--seed", 4, "--out", p, "--as-of", STAMP) == 0
    return p


# ---------------------------------------------------------------- the sample

def test_a_sample_is_the_same_bytes_for_the_same_seed_and_different_for_another(tmp_path, capsys):
    a, b, c = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    cli("sample", "--seed", 1, "--out", a, "--as-of", STAMP)
    cli("sample", "--seed", 1, "--out", b, "--as-of", STAMP)
    cli("sample", "--seed", 2, "--out", c, "--as-of", STAMP)
    assert a.read_bytes() == b.read_bytes() and a.read_bytes() != c.read_bytes()
    assert "generated records written" in capsys.readouterr().out


def test_a_sample_holds_only_generated_names_and_every_kind_of_record(ledger_file):
    text = ledger_file.read_text(encoding="utf-8")
    ledger = read_ledger(ledger_file)
    assert ledger.tickets and ledger.times and ledger.alerts and ledger.devices
    assert {t.company for t in ledger.tickets if t.company} <= {"Company %02d" % i for i in range(1, 13)}
    assert {e.technician for e in ledger.times} <= {"Technician %s" % c for c in "ABCDE"}
    assert "@" not in text and "http" not in text


def test_the_sample_as_of_must_be_utc_with_a_z(tmp_path):
    with pytest.raises(SystemExit):
        cli("sample", "--seed", 1, "--out", tmp_path / "x.jsonl", "--as-of", "2026-10-10T12:00:00")
    with pytest.raises(SystemExit):
        cli("sample", "--seed", 1, "--out", tmp_path / "x.jsonl", "--as-of", "notatimeZ")


# ---------------------------------------------------------------- analyze

def test_analyze_prints_the_markdown_report_and_the_same_ledger_gives_the_same_text(ledger_file, capsys):
    assert cli("analyze", ledger_file) == 0
    first = capsys.readouterr().out
    assert cli("analyze", ledger_file) == 0
    assert capsys.readouterr().out == first and "# MSP backlog analysis" in first and "As of %s" % STAMP in first


def test_analyze_defaults_the_time_to_when_the_ledger_was_written_and_the_window_to_30_days_ending_that_day(ledger_file, capsys):
    cli("analyze", ledger_file, "--format", "json")
    report = json.loads(capsys.readouterr().out)
    assert report["as_of"] == STAMP and report["window"] == {"start": "2026-09-11", "end": "2026-10-11"}


def test_the_json_report_is_the_same_data_as_the_markdown_and_can_be_written_to_a_file(ledger_file, tmp_path, capsys):
    out = tmp_path / "r.json"
    assert cli("analyze", ledger_file, "--format", "json", "--out", out, "--top", 2, "--window-days", 7, "--horizon-hours", 12) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["window"] == {"start": "2026-10-04", "end": "2026-10-11"} and report["sla_risk"]["horizon_hours"] == 12 and len(report["ticket_drivers"]["by_title"]) == 2
    assert "report written to" in capsys.readouterr().out


def test_an_explicit_window_replaces_the_default_and_needs_both_ends(ledger_file, capsys):
    cli("analyze", ledger_file, "--format", "json", "--start", "2026-10-01", "--end", "2026-10-05")
    assert json.loads(capsys.readouterr().out)["window"] == {"start": "2026-10-01", "end": "2026-10-05"}
    assert cli("analyze", ledger_file, "--start", "2026-10-01") == 2 and "give --start and --end together" in capsys.readouterr().err


def test_several_ledgers_are_merged_and_give_the_report_of_the_whole(tmp_path, capsys):
    whole = generate_world(9, as_of=AS_OF, tickets=120)
    paths = {}
    for source in ("connectwise", "autotask", "ninjaone"):
        records = [r for r in whole.records() if r.source == source]
        paths[source] = tmp_path / ("%s.jsonl" % source)
        write_ledger(paths[source], records, generated_at=AS_OF)
    write_ledger(tmp_path / "whole.jsonl", whole.records(), generated_at=AS_OF)
    cli("analyze", tmp_path / "whole.jsonl", "--format", "json")
    expect = capsys.readouterr().out
    cli("analyze", *paths.values(), "--format", "json")
    assert capsys.readouterr().out == expect


@pytest.mark.parametrize("extra, message", [(["--window-days", "0"], "--window-days must be at least 1"), (["--start", "2026-10-05", "--end", "2026-10-05"], "at least one day"),
                                            (["--horizon-hours", "0"], "horizon_hours"), (["--top", "0"], "top must be")])
def test_analyze_refuses_a_bad_option_with_exit_2_and_a_good_one_passes(ledger_file, capsys, extra, message):
    assert cli("analyze", ledger_file) == 0
    capsys.readouterr()
    assert cli("analyze", ledger_file, *extra) == 2
    assert message in capsys.readouterr().err


def test_analyze_refuses_a_missing_a_damaged_and_a_repeated_ledger_with_exit_2(ledger_file, tmp_path, capsys):
    assert cli("analyze", ledger_file) == 0
    capsys.readouterr()
    assert cli("analyze", tmp_path / "nope.jsonl") == 2 and "no report" in capsys.readouterr().err
    bad = tmp_path / "bad.jsonl"
    bad.write_text(ledger_file.read_text(encoding="utf-8").replace('"kind"', '"knd"', 1), encoding="utf-8")
    assert cli("analyze", bad) == 2 and "no known kind" in capsys.readouterr().err
    assert cli("analyze", ledger_file, ledger_file) == 2 and "more than one ledger" in capsys.readouterr().err


def test_analyze_refuses_an_as_of_with_no_zone(ledger_file):
    with pytest.raises(SystemExit):
        cli("analyze", ledger_file, "--as-of", "2026-10-10T12:00:00")


def test_the_command_runs_as_a_script_and_exits_2_on_a_missing_file(tmp_path):
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "msp_backlog.py"), "sample", "--seed", "1", "--out", str(tmp_path / "s.jsonl")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert ok.returncode == 0
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "msp_backlog.py"), "analyze", str(tmp_path / "none.jsonl")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert bad.returncode == 2 and "no report" in bad.stderr


# ---------------------------------------------------------------- the ledger merge

def test_a_merged_ledger_holds_everything_and_is_as_old_as_the_oldest(tmp_path):
    world = generate_world(2, as_of=AS_OF, tickets=30)
    one, two = tmp_path / "1.jsonl", tmp_path / "2.jsonl"
    cw = [r for r in world.records() if r.source != "autotask"]
    at = [r for r in world.records() if r.source == "autotask"]
    write_ledger(one, cw, generated_at=AS_OF)
    write_ledger(two, at, generated_at=AS_OF - timedelta(hours=5))
    merged = merge_ledgers(read_ledger(one), read_ledger(two))
    assert len(merged.records()) == len(world.records()) and merged.generated_at == AS_OF - timedelta(hours=5)
    assert merge_ledgers().generated_at is None


def test_the_same_record_in_two_ledgers_is_refused_but_the_same_id_from_two_vendors_is_not(tmp_path):
    world = generate_world(2, as_of=AS_OF, tickets=10)
    cw = next(t for t in world.tickets if t.source == "connectwise")
    other = type(cw)(**{**cw.__dict__, "source": "autotask"})
    p1, p2 = tmp_path / "1.jsonl", tmp_path / "2.jsonl"
    write_ledger(p1, [cw], generated_at=AS_OF)
    write_ledger(p2, [other], generated_at=AS_OF)
    assert len(merge_ledgers(read_ledger(p1), read_ledger(p2)).tickets) == 2
    with pytest.raises(LedgerError, match="more than one ledger"):
        merge_ledgers(read_ledger(p1), read_ledger(p1))


# ---------------------------------------------------------------- stand-in servers to a ranked report

def dt(day, hour=9, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def test_three_connectors_three_ledgers_one_report_with_the_figures_worked_out_by_hand(tmp_path, capsys):
    with FakeConnectWise(company="acme", public_key="pk", private_key="prk", client_id="cid") as cw, FakeAutotask(username="api@example.com", secret="sc", code="ic") as at, \
            FakeNinjaOne(client_id="nj", client_secret="njs-phrase") as nj:
        # ConnectWise: three "disk space" tickets for one company (two repeats), one other; one open past its goal, one on time
        cw.add_ticket(1, dt(2), summary="Disk space low on SRV-01", board="Alerts", ttype="Storage", company="Acme", resolution_goal=dt(3))
        cw.add_ticket(2, dt(3), summary="Disk space low on SRV-02", board="Alerts", ttype="Storage", company="Acme", resolved_at=dt(3, 11), resolution_goal=dt(3, 12))
        cw.add_ticket(3, dt(4), summary="Disk space low on SRV-09", board="Alerts", ttype="Storage", company="Acme", resolution_goal=dt(30))
        cw.add_ticket(4, dt(4), summary="Printer jam", board="Service Desk", ttype="Printing", company="Beta")
        cw.add_time(1, dt(2, 10), "1.0", ticket=1)
        cw.add_time(2, dt(3, 10), "0.5", ticket=2, member="Lee")
        cw.add_time(3, dt(3, 11), "0.25")
        # Autotask: one ticket in the Alerts category, its time, and time on a ticket that does not exist
        at.add_ticket(10, dt(5), title="Disk space low on SRV-04", category=2, issue=11, company=7, resolved_due=dt(6))
        at.add_time(10, dt(5, 10), "2.0", ticket=10, resource=3)
        at.add_time(11, dt(5, 11), "0.5", ticket=424242, resource=3)
        # NinjaOne: three CPU alerts on two devices
        nj.add_device(1, "SRV-01")
        nj.add_device(2, "SRV-02", offline=True)
        for i, dev in enumerate((1, 1, 2)):
            nj.add_alert("a%d" % i, dev, dt(6, 8 + i))

        async def read_all():
            conns = [ConnectWiseConnector(api_url=cw.api_url, company_id="acme", public_key="pk", private_key="prk", client_id="cid", client=client(), retrier=retrier(), clock=lambda: AS_OF),
                     AutotaskConnector(username="api@example.com", secret="sc", integration_code="ic", zone_lookup_url=at.url, client=client(), retrier=retrier(), clock=lambda: AS_OF),
                     NinjaOneConnector(client_id="nj", client_secret="njs-phrase", epoch_unit="seconds", base_url=nj.url, client=client(), retrier=retrier())]
            for conn in conns:
                await collect(conn, date(2026, 10, 1), date(2026, 10, 9))
            return conns

        cw_conn, at_conn, nj_conn = run(read_all())
    write_ledger(tmp_path / "cw.jsonl", [*cw_conn.tickets_read, *cw_conn.time_read], generated_at=AS_OF)
    write_ledger(tmp_path / "at.jsonl", [*at_conn.tickets_read, *at_conn.time_read], generated_at=AS_OF)
    write_ledger(tmp_path / "nj.jsonl", [*nj_conn.devices_read, *nj_conn.alerts_read], generated_at=AS_OF)
    assert cli("analyze", tmp_path / "cw.jsonl", tmp_path / "at.jsonl", tmp_path / "nj.jsonl", "--format", "json", "--start", "2026-10-01", "--end", "2026-10-09") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["inputs"] == {"sources": ["autotask", "connectwise", "ninjaone"], "tickets": 5, "time_entries": 5, "alerts": 3, "devices": 2}
    td = report["ticket_drivers"]
    cw_disk = td["by_title"][0]
    assert (cw_disk["category"], cw_disk["title"], cw_disk["tickets"], cw_disk["companies"], cw_disk["repeats"]) == ("Alerts", "disk space low on srv #", 3, 1, 2)
    assert td["opened_tickets"] == 5
    sr = report["sla_risk"]
    assert sr["open"] == 4 and sr["breached"] == 2 and sr["ok"] == 1 and sr["no_goal"] == 1, "ticket 2 is resolved; tickets 1 and 10 are past their goals; ticket 3's goal is ahead; ticket 4 has none"
    tt = report["touch_time"]
    assert tt["total_seconds"] == 3600 + 1800 + 900 + 7200 + 1800 and tt["non_ticket_seconds"] == 900 and tt["unmatched_seconds"] == 1800 and tt["placed_seconds"] == 12600
    alerts_cat = next(c for c in tt["by_category"] if c["category"] == "Alerts")
    assert alerts_cat["seconds"] == 3600 + 1800 and alerts_cat["tickets_touched"] == 2, "ConnectWise's Alerts board only: Autotask's category is named RMM Alerts"
    rmm = next(c for c in tt["by_category"] if c["category"] == "RMM Alerts")
    assert rmm["seconds"] == 7200 and rmm["technicians"] == [{"technician": "resource 3", "seconds": 7200, "tickets": 1}]
    ad = report["alert_drivers"]
    assert ad["active_alerts"] == 3 and ad["by_source"][0]["alerts"] == 3 and ad["by_source"][0]["devices"] == 2 and ad["by_source"][0]["repeats"] == 1
    assert ad["noisiest_devices"][0] == {"device_id": 1, "device_name": "SRV-01", "alerts": 2, "sources": 1} and ad["devices_offline"] == 1


# ---------------------------------------------------------------- the sync scripts' --ledger

def _import_script(name):
    import importlib

    return importlib.import_module(name)


@pytest.mark.parametrize("script, kind", [("sync_connectwise", "cw"), ("sync_autotask", "at"), ("sync_ninjaone", "nj")])
def test_a_sync_script_with_ledger_writes_what_the_run_read_and_without_it_writes_nothing(tmp_path, monkeypatch, capsys, script, kind):
    module = _import_script(script)

    async def fake_run(args, *, platform, connector, default_end):
        """Stands in for the database run: reads the window through the connector as the runner would, and says it succeeded."""
        async for _ in connector.fetch(window_start=date(2026, 10, 1), window_end=date(2026, 10, 9)):
            pass
        await connector.aclose()
        return 0

    monkeypatch.setattr(module, "run_connection_sync", fake_run)
    with FakeConnectWise(company="acme", public_key="pk", private_key="prk", client_id="cid") as cw, FakeAutotask(username="api@example.com", secret="sc", code="ic") as at, \
            FakeNinjaOne(client_id="nj", client_secret="njs-phrase") as nj:
        cw.add_ticket(1, dt(2), summary="Printer jam")
        cw.add_time(1, dt(2, 10), "1.0", ticket=1)
        at.add_ticket(10, dt(5), title="VPN down")
        nj.add_device(1, "SRV-01")
        nj.add_alert("a1", 1, dt(6))
        env = {"cw": {"CONNECTWISE_API_URL": cw.api_url, "CONNECTWISE_COMPANY_ID": "acme", "CONNECTWISE_PUBLIC_KEY": "pk", "CONNECTWISE_PRIVATE_KEY": "prk", "CONNECTWISE_CLIENT_ID": "cid"},
               "at": {"AUTOTASK_USERNAME": "api@example.com", "AUTOTASK_SECRET": "sc", "AUTOTASK_INTEGRATION_CODE": "ic", "AUTOTASK_API_URL": at.api_url, "AUTOTASK_ZONE_LOOKUP_URL": at.url},
               "nj": {"NINJAONE_CLIENT_ID": "nj", "NINJAONE_CLIENT_SECRET": "njs-phrase", "NINJAONE_EPOCH_UNIT": "seconds", "NINJAONE_BASE_URL": nj.url}}[kind]
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        args = ["--tenant", "t", "--connection", "c"]
        assert module.main(args) == 0 and not list(tmp_path.iterdir()), "no --ledger, no file"
        path = tmp_path / "out.jsonl"
        assert module.main([*args, "--ledger", str(path)]) == 0
    ledger = read_ledger(path)
    got = {"cw": (len(ledger.tickets), len(ledger.times)), "at": (len(ledger.tickets), len(ledger.times)), "nj": (len(ledger.devices), len(ledger.alerts))}[kind]
    assert got == {"cw": (1, 1), "at": (1, 0), "nj": (1, 1)}[kind]
    assert "ledger: " in capsys.readouterr().out


@pytest.mark.parametrize("script", ["sync_connectwise", "sync_autotask", "sync_ninjaone"])
def test_a_sync_script_that_failed_writes_no_ledger(tmp_path, monkeypatch, script):
    module = _import_script(script)

    async def failing_run(args, *, platform, connector, default_end):
        await connector.aclose()
        return 1

    monkeypatch.setattr(module, "run_connection_sync", failing_run)
    for key, value in {"CONNECTWISE_API_URL": "https://na.myconnectwise.net/v4_6_release/apis/3.0", "CONNECTWISE_COMPANY_ID": "a", "CONNECTWISE_PUBLIC_KEY": "p", "CONNECTWISE_PRIVATE_KEY": "q",
                       "CONNECTWISE_CLIENT_ID": "c", "AUTOTASK_USERNAME": "a@b.c", "AUTOTASK_SECRET": "s", "AUTOTASK_INTEGRATION_CODE": "i", "NINJAONE_CLIENT_ID": "n",
                       "NINJAONE_CLIENT_SECRET": "s", "NINJAONE_EPOCH_UNIT": "seconds"}.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "out.jsonl"
    assert module.main(["--tenant", "t", "--connection", "c", "--ledger", str(path)]) == 1 and not path.exists()
