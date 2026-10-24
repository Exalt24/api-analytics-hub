"""Snapshot change detection against real Postgres, on generated tables.

Every table here is built by code: one before and after pair per event kind, every key shape for matching, the idempotence cases (same file twice, overlapping files, a crash at every stage,
a flap), the outbox cases (ok, raise, ambiguous, rejected, a worker that dies mid push, a stale worker), a seeded property test over random worlds checked against the plain Python rules in
scripts/make_carrier_snapshot.py, and a 2,000 carrier run of the whole flow. The 400,000 carrier run is scripts/measure_change_detection.py, run by hand.

Skips cleanly when Postgres is not running (`docker compose up -d`). Like the other database suites it drops and recreates the `public` schema of the database it is pointed at.
"""
from __future__ import annotations

import itertools
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

from app import changes  # noqa: E402
from app.changes import (  # noqa: E402
    SinkAnswer, apply_snapshot, claim_outbox, daily_report, deliver_pending, format_report, load_companies, match_late_company, record_outcome, requeue_dead, rows_to_csv,
)
from app.changes_fake_sink import AppendSink, FakeSink  # noqa: E402
from make_carrier_snapshot import Row, expected_events, generate, normalise, norm_dot, norm_mc  # noqa: E402

MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "004_observability.sql", "005_change_detection.sql")
STAGES = ("setup", "copy", "clean", "compare", "history", "events", "apply")
TABLES = "carrier_stage, carrier_current, carrier_history, change_event, change_event_match, change_outbox, carrier_load, carrier_company"


@pytest.fixture(scope="module")
def _schema():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            c.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


@pytest.fixture()
def conn(_schema):
    c = psycopg.connect(DSN, autocommit=True)
    # one round trip; the append only triggers would refuse a plain reset, and a test database has no use for a synchronous flush
    c.execute("set synchronous_commit = off; set session_replication_role = replica; truncate %s restart identity cascade; reset session_replication_role" % TABLES)
    yield c
    c.close()


def D(n: int) -> date:
    return date(2026, 10, 1) + timedelta(days=n)


def row(usdot="90000001", mc="MC-1000001", name="Lab One Freight", insurer="Zorlan Casualty", policy="ACTIVE", authority="ACTIVE", adate="2024-01-01", filing="F1", fdate="2025-01-01",
        cancel="") -> Row:
    return Row(usdot, mc, name, insurer, policy, authority, adate, filing, fdate, cancel)


def load(conn, rows, day, *, quote_all=False, **kw):
    """Apply a generated file. `force` defaults to True because most tests change every row of a one or two carrier table, which the refusal guards rightly call a bad file; the guard
    tests pass force=False."""
    kw.setdefault("force", True)
    return apply_snapshot(conn, rows_to_csv(rows, quote_all=quote_all).encode("utf-8"), D(day), **kw)


def events(conn):
    return sorted(conn.execute("select usdot_norm, kind, event_ref from change_event").fetchall())


def fingerprint(conn):
    return conn.execute(
        "select (select count(*) from carrier_current), (select count(*) from carrier_history), (select count(*) from change_event), (select count(*) from change_outbox), "
        "(select count(*) from carrier_load), (select coalesce(md5(string_agg(usdot_norm || track_hash::text, ',' order by usdot_norm)), '') from carrier_current)"
    ).fetchone()


def add_company(conn, name, usdot=None, mc=None):
    return conn.execute("insert into carrier_company (name, usdot_no, mc_no) values (%s, %s, %s) returning id", (name, usdot, mc)).fetchone()[0]


# ----------------------------------------------------------------------------------------- the key normalisers

DOT_CASES = [(zeros + "90000001", "90000001") for zeros in ("", "0", "000")] + [("DOT 90000001", "90000001"), (" 90000001 ", "90000001"), ("9000-0001", "90000001"),
            ("", None), (None, None), ("0", None), ("000", None), ("abc", None)]
MC_CASES = [(prefix + zeros + "1234567", "MC1234567") for prefix in ("", "MC", "mc", "Mc-", "MC-", "MC ", "MC#", " MC-") for zeros in ("", "0", "000")]
MC_CASES += [(prefix + zeros + "1234567", prefix.upper() + "1234567") for prefix in ("FF", "FF-", "MX-", "mx ") for zeros in ("", "00")]
MC_CASES = [(raw, "FF1234567" if raw.upper().lstrip().startswith("FF") else "MX1234567" if raw.upper().lstrip().startswith("MX") else want) for raw, want in MC_CASES]
MC_CASES += [("MC-", None), ("MC-0", None), ("", None), (None, None)]


@pytest.mark.parametrize("raw,want", DOT_CASES)
def test_the_usdot_normaliser_drops_zeros_and_decoration_and_a_blank_matches_nothing(conn, raw, want):
    assert conn.execute("select carrier_norm_dot(%s)", (raw,)).fetchone()[0] == want
    assert norm_dot(raw) == want                                                    # the plain Python oracle agrees


@pytest.mark.parametrize("raw,want", MC_CASES)
def test_the_mc_normaliser_handles_prefix_zeros_and_keeps_other_dockets_apart(conn, raw, want):
    assert conn.execute("select carrier_norm_mc(%s)", (raw,)).fetchone()[0] == want
    assert norm_mc(raw) == want


def test_an_ff_docket_never_equals_an_mc_docket_with_the_same_digits(conn):
    assert conn.execute("select carrier_norm_mc('FF-1234567') = carrier_norm_mc('MC-1234567')").fetchone()[0] is False


# ----------------------------------------------------------------------------------------- the hash and the event kinds

def _hist(conn):
    return conn.execute("select count(*) from carrier_history").fetchone()[0]


@pytest.mark.parametrize("field,value,kinds", [
    ("insurer", "Quillon Mutual", {"insurer_changed"}),
    ("policy", "CANCELLED", {"cancellation_filed"}),
    ("authority", "REVOKED", {"authority_status_changed"}),
    ("adate", "2025-05-05", set()),
    ("filing", "F2", set()),
    ("fdate", "2025-06-06", set()),
    ("cancel", "2025-07-07", set()),
])
def test_each_tracked_column_alone_moves_the_hash_and_writes_history(conn, field, value, kinds):
    load(conn, [row()], 0)
    assert _hist(conn) == 1
    load(conn, [row(**{field: value})], 1)
    assert _hist(conn) == 2, "a change in %s was not seen" % field
    assert {k for _d, k, _r in events(conn)} == kinds


def test_a_change_in_an_untracked_column_alone_is_not_examined(conn):
    load(conn, [row()], 0)
    r = load(conn, [row(name="A Very Different Name", mc="MC-9999999")], 1)
    assert r.rows_changed == 0 and r.events_new == 0 and _hist(conn) == 1


def test_a_null_and_an_empty_value_are_the_same_in_the_file_and_distinct_from_a_value(conn):
    load(conn, [row(cancel="")], 0)
    assert load(conn, [row(cancel="")], 1).rows_changed == 0
    assert load(conn, [row(cancel="2026-01-01")], 2).rows_changed == 1


def _pairs():
    base = row()
    out = []
    out.append(("new carrier", None, base))
    out.append(("insurer", base, row(insurer="Quillon Mutual", filing="F2", fdate="2026-09-01")))
    out.append(("insurer to blank", base, row(insurer="")))
    out.append(("cancellation", base, row(policy="CANCELLED", filing="F2", fdate="2026-09-01", cancel="2026-10-01")))
    out.append(("replacement same insurer", base, row(policy="REPLACED", filing="F2", fdate="2026-09-01")))
    out.append(("replacement new insurer", base, row(policy="REPLACED", insurer="Quillon Mutual", filing="F2", fdate="2026-09-01")))
    out.append(("authority", base, row(authority="REVOKED", adate="2026-09-15")))
    out.append(("authority back to active", row(authority="REVOKED"), row(authority="ACTIVE", adate="2026-09-20")))
    out.append(("cancellation and authority together", base, row(policy="CANCELLED", cancel="2026-10-01", authority="INACTIVE", adate="2026-09-15", filing="F9")))
    out.append(("reinstated, no kind", row(policy="CANCELLED", cancel="2026-08-01"), row(policy="ACTIVE", filing="F7")))
    out.append(("already cancelled, new filing", row(policy="CANCELLED"), row(policy="CANCELLED", filing="F8")))
    out.append(("already replaced, new insurer", row(policy="REPLACED"), row(policy="REPLACED", insurer="Quillon Mutual")))
    return out


@pytest.mark.parametrize("label,old,new", _pairs(), ids=[p[0] for p in _pairs()])
def test_every_event_kind_from_a_before_and_after_pair_matches_the_plain_rules(conn, label, old, new):
    other = row(usdot="90000999", name="Another", mc="MC-2000002")
    load(conn, [old, other] if old else [other], 0)
    load(conn, [new, other], 1)
    got = sorted((k, r) for d, k, r in events(conn) if d == "90000001")
    assert got == sorted(expected_events(old, new))


def test_the_literal_events_for_the_main_kinds(conn):
    load(conn, [row(), row(usdot="90000002")], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2"), row(usdot="90000002", authority="REVOKED", adate="2026-09-15"), row(usdot="90000003")], 1)
    days = (date(2026, 9, 15) - date(1970, 1, 1)).days
    assert events(conn) == sorted([("90000001", "insurer_changed", "F2"), ("90000002", "authority_status_changed", "REVOKED:%d" % days), ("90000003", "new_entity", "")])


def test_the_first_load_is_a_baseline_and_raises_nothing(conn):
    r = load(conn, [row(usdot=str(90000000 + i)) for i in range(50)], 0)
    assert r.baseline and r.events_new == 0 and r.rows_new == 50 and events(conn) == []
    assert load(conn, [row(usdot="90000500")], 1).events_new == 1                  # the same file after the baseline raises new_entity


def test_a_carrier_that_is_new_raises_new_entity_and_an_existing_one_does_not(conn):
    load(conn, [row()], 0)
    load(conn, [row(), row(usdot="90000002")], 1)
    assert events(conn) == [("90000002", "new_entity", "")]


def test_a_carrier_absent_from_a_later_file_is_not_a_change(conn):
    load(conn, [row(), row(usdot="90000002")], 0)
    r = load(conn, [row()], 1)
    assert r.events_new == 0 and conn.execute("select count(*) from carrier_current").fetchone()[0] == 2


def test_history_keeps_every_state_in_order_with_the_previous_hash(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    load(conn, [row(insurer="Zorlan Casualty", filing="F3")], 2)
    rows = conn.execute("select insurer, prev_hash is null, prev_hash = lag(track_hash) over (order by id) from carrier_history order by id").fetchall()
    assert [r[0] for r in rows] == ["ZORLAN CASUALTY", "QUILLON MUTUAL", "ZORLAN CASUALTY"]          # codes are upper-cased on the way in
    assert [r[1] for r in rows] == [True, False, False] and rows[1][2] and rows[2][2]


# ----------------------------------------------------------------------------------------- never alert twice

def test_the_same_file_twice_raises_nothing_the_second_time(conn):
    load(conn, [row()], 0)
    first = load(conn, [row(policy="CANCELLED", filing="F2", cancel="2026-10-01"), row(usdot="90000002")], 1)
    before = events(conn)
    second = load(conn, [row(policy="CANCELLED", filing="F2", cancel="2026-10-01"), row(usdot="90000002")], 1)
    assert first.events_new == 2 and second.events_new == 0 and second.rows_changed == 0 and events(conn) == before


def test_overlapping_files_raise_only_what_is_new(conn):
    load(conn, [row(), row(usdot="90000002")], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2"), row(usdot="90000002")], 1)
    r = load(conn, [row(insurer="Quillon Mutual", filing="F2"), row(usdot="90000002", authority="REVOKED", adate="2026-09-15")], 2)
    assert r.events_new == 1 and [k for _d, k, _r in events(conn)].count("insurer_changed") == 1


@pytest.mark.parametrize("stage", STAGES)
def test_a_crash_at_any_stage_leaves_no_half_day_and_a_clean_rerun_raises_the_events_once(conn, stage):
    add_company(conn, "Acct", usdot="90000001")
    load(conn, [row(), row(usdot="90000002")], 0)
    base = fingerprint(conn)
    staged_before = conn.execute("select count(*) from carrier_stage").fetchone()[0]
    day2 = [row(policy="CANCELLED", filing="F2", cancel="2026-10-01"), row(usdot="90000002", insurer="Quillon Mutual", filing="F3"), row(usdot="90000003")]

    def boom(s):
        if s == stage:
            raise RuntimeError("crash at " + s)

    with pytest.raises(RuntimeError):
        load(conn, day2, 1, on_stage=boom)
    assert fingerprint(conn) == base
    assert conn.execute("select count(*) from carrier_stage").fetchone()[0] == staged_before          # the staging table was rolled back with the day
    r = load(conn, day2, 1)
    assert r.events_new == 3 and len(events(conn)) == 3 and conn.execute("select count(*) from change_outbox").fetchone()[0] == 1
    assert load(conn, day2, 1).events_new == 0


def test_a_flap_back_to_a_state_with_the_same_filing_alerts_once_and_a_new_filing_alerts_again(conn):
    load(conn, [row()], 0)
    load(conn, [row(policy="CANCELLED", filing="F2", cancel="2026-10-01")], 1)
    load(conn, [row(policy="ACTIVE", filing="F1")], 2)
    flapped = load(conn, [row(policy="CANCELLED", filing="F2", cancel="2026-10-01")], 3)
    assert flapped.events_new == 0 and flapped.events_suppressed == 1
    load(conn, [row(policy="ACTIVE", filing="F4")], 4)
    again = load(conn, [row(policy="CANCELLED", filing="F5", cancel="2026-11-01")], 5)
    assert again.events_new == 1
    assert [k for _d, k, _r in events(conn)].count("cancellation_filed") == 2
    assert _hist(conn) == 6                                                            # every state is kept even when its alert is suppressed


def test_an_authority_lost_twice_alerts_both_times_because_the_date_is_part_of_the_key(conn):
    load(conn, [row()], 0)
    load(conn, [row(authority="REVOKED", adate="2026-02-01")], 1)
    load(conn, [row(authority="ACTIVE", adate="2026-03-01")], 2)
    load(conn, [row(authority="REVOKED", adate="2026-04-01")], 3)
    assert [k for _d, k, _r in events(conn)].count("authority_status_changed") == 3


def test_an_older_file_than_one_already_loaded_is_refused_and_changes_nothing(conn):
    load(conn, [row()], 5)
    base = fingerprint(conn)
    with pytest.raises(changes.StaleSnapshot):
        load(conn, [row(insurer="Quillon Mutual")], 4)
    assert fingerprint(conn) == base


def test_a_second_load_while_one_holds_the_lock_is_refused(conn):
    load(conn, [row()], 0)
    other = psycopg.connect(DSN, autocommit=True)
    try:
        other.execute("select pg_advisory_lock(%s)", (changes.LOAD_LOCK_KEY,))
        base = fingerprint(conn)
        with pytest.raises(changes.LoadBusy):
            load(conn, [row(insurer="Quillon Mutual")], 1)
        assert fingerprint(conn) == base
    finally:
        other.close()
    assert load(conn, [row(insurer="Quillon Mutual")], 1).events_new == 1


def test_a_duplicate_carrier_row_keeps_the_last_and_a_row_with_no_usdot_is_rejected_and_counted(conn):
    load(conn, [row()], 0)
    r = load(conn, [row(insurer="Old Insurer"), row(insurer="Quillon Mutual", filing="F2"), row(usdot="", name="nobody"), row(usdot="000")], 1)
    assert (r.rows_staged, r.rows_rejected, r.rows_duplicate) == (1, 2, 1)
    assert events(conn) == [("90000001", "insurer_changed", "F2")]


def test_the_same_carrier_written_with_leading_zeros_is_one_carrier(conn):
    load(conn, [row(usdot="90000001")], 0)
    r = load(conn, [row(usdot="0090000001", insurer="Quillon Mutual", filing="F2")], 1)
    assert r.rows_new == 0 and conn.execute("select count(*) from carrier_current").fetchone()[0] == 1 and r.events_new == 1


_GOOD = rows_to_csv([row()]).encode()
_SWAPPED = _GOOD.replace(b"usdot,mc,", b"mc,usdot,", 1)                                 # same column count, so only the header check can see it


@pytest.mark.parametrize("bad,error", [
    (b"usdot,mc\n1,2\n", Exception),                                                     # wrong header
    (_SWAPPED, ValueError),                                                               # right columns, wrong order: would load the numbers into each other
    (b"", ValueError),                                                                    # empty
    (_GOOD.replace(b"2025-01-01", b"not-a-date"), Exception),                             # a malformed value fails the whole day
    (_GOOD.replace(b"2025-01-01", b"01/02/2025"), Exception),                             # a date that is not ISO is refused, never read under some DateStyle
])
def test_a_bad_file_fails_the_whole_day_and_changes_nothing(conn, bad, error):
    load(conn, [row()], 0)
    base = fingerprint(conn)
    with pytest.raises(error):
        apply_snapshot(conn, bad, D(1), force=True)                                       # forced, so only the file itself can be what refuses it
    assert fingerprint(conn) == base


def test_history_and_events_cannot_be_rewritten_or_deleted(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    for sql in ("update carrier_history set insurer = 'x'", "delete from carrier_history", "truncate carrier_history",
                "update change_event set kind = 'new_entity'", "delete from change_event", "truncate change_event cascade"):
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(sql)
    assert _hist(conn) == 2 and len(events(conn)) == 1


def test_the_event_natural_key_is_enforced_by_the_database_itself(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into change_event (load_id, snapshot_date, usdot_norm, kind, event_ref, detail) values (1, %s, '90000001', 'insurer_changed', 'F2', '{}')", (D(1),))


# ----------------------------------------------------------------------------------------- matching to companies

# (the MC the carrier has in the file, the company's USDOT, the company's MC, expected matched_by or None)
MATCH_CASES = [
    ("MC-0001234", "90000001", None, "usdot"),
    ("MC-0001234", "0090000001", None, "usdot"),
    ("MC-0001234", "DOT 90000001", None, "usdot"),
    ("MC-0001234", None, "MC 1234", "mc"),
    ("MC-0001234", None, "1234", "mc"),
    ("MC-0001234", None, "0001234", "mc"),
    ("MC-0001234", None, "mc-001234", "mc"),
    ("1234", None, "MC-0001234", "mc"),
    ("0001234", None, "MC1234", "mc"),
    ("MC1234", "90000001", "MC-1234", "both"),
    ("MC-1234", "90000001", "FF-1234", "usdot"),
    ("FF-1234", None, "MC-1234", None),
    ("MC-1234", None, "MC-12340", None),
    ("MC-1234", "90000002", None, None),
    ("", None, "MC-1234", None),
    ("", "90000001", "MC-1234", "usdot"),
]


@pytest.mark.parametrize("file_mc,co_dot,co_mc,want", MATCH_CASES)
def test_an_event_matches_the_company_by_either_number_after_normalising(conn, file_mc, co_dot, co_mc, want):
    cid = add_company(conn, "Acct", usdot=co_dot, mc=co_mc)
    load(conn, [row(mc=file_mc)], 0)
    load(conn, [row(mc=file_mc, insurer="Quillon Mutual", filing="F2")], 1)
    got = conn.execute("select company_id, matched_by from change_event_match").fetchall()
    assert got == ([(cid, want)] if want else [])
    queued = conn.execute("select payload from change_outbox").fetchall()
    assert len(queued) == (1 if want else 0)
    if want:
        assert queued[0][0]["match"] == "matched" and queued[0][0]["companies"][0]["id"] == cid


def test_a_carrier_that_matches_two_companies_is_a_conflict_with_both_named_and_none_picked(conn):
    a = add_company(conn, "By Dot", usdot="90000001")
    b = add_company(conn, "By MC", mc="MC-1000001")
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    assert sorted(r[0] for r in conn.execute("select company_id from change_event_match").fetchall()) == sorted([a, b])
    payload = conn.execute("select payload from change_outbox").fetchone()[0]
    assert payload["match"] == "conflict" and [c["id"] for c in payload["companies"]] == sorted([a, b])
    assert daily_report(conn, D(1))["conflict_events"] == 1


def test_two_companies_cannot_share_a_normalised_number(conn):
    add_company(conn, "One", usdot="90000001", mc="MC-1000001")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_company(conn, "Two", usdot="0090000001")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_company(conn, "Three", mc="1000001")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_company(conn, "Four", usdot="000", mc="")


def test_an_event_for_a_carrier_that_is_no_company_is_stored_and_reported_but_never_queued(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    assert len(events(conn)) == 1 and conn.execute("select count(*) from change_outbox").fetchone()[0] == 0
    assert daily_report(conn, D(1))["unmatched_events"] == 1


# ----------------------------------------------------------------------------------------- the outbox

def _queue_one(conn, n=1):
    for i in range(n):
        add_company(conn, "Acct %d" % i, usdot=str(90000001 + i))
    load(conn, [row(usdot=str(90000001 + i), mc="") for i in range(n)], 0)
    load(conn, [row(usdot=str(90000001 + i), mc="", insurer="Quillon Mutual", filing="F2") for i in range(n)], 1)


def _ob(conn):
    return conn.execute("select status, attempts, last_outcome, sink_ref, claim_token is not null from change_outbox order by event_id").fetchall()


def test_an_ok_answer_marks_the_row_delivered_with_the_sinks_own_id_and_it_is_never_pushed_again(conn):
    _queue_one(conn)
    sink = FakeSink()
    r = deliver_pending(conn, sink)
    assert r.delivered == 1 and _ob(conn) == [("delivered", 1, "ok", "task-0001", False)]
    assert deliver_pending(conn, sink).delivered == 0 and len(sink.calls) == 1


def test_a_row_is_not_delivered_until_the_sink_has_answered(conn):
    _queue_one(conn)
    seen = []

    def sink(payload, key):
        with psycopg.connect(DSN, autocommit=True) as other:
            seen.append(other.execute("select status, claim_token is not null from change_outbox").fetchone())
        return SinkAnswer("ok", "t1")

    deliver_pending(conn, sink)
    assert seen == [("pending", True)] and _ob(conn)[0][0] == "delivered"


def test_a_sink_that_raises_leaves_the_row_pending_and_stores_the_class_never_the_message(conn):
    _queue_one(conn)
    sink = FakeSink([RuntimeError("https://hooks.example/T000/SECRETTOKEN")])
    r = deliver_pending(conn, sink, backoff_seconds=0)
    assert r.retrying == 1 and r.delivered == 0
    status, attempts, outcome, ref, claimed = _ob(conn)[0]
    assert (status, attempts, ref, claimed) == ("pending", 1, None, False) and outcome == "ambiguous: RuntimeError" and "SECRET" not in outcome


def test_a_retry_sends_the_same_stored_payload_and_key_and_never_recomputes_it(conn):
    _queue_one(conn)
    sink = FakeSink([RuntimeError("boom")])
    deliver_pending(conn, sink, backoff_seconds=0)
    conn.execute("update carrier_company set name = 'Renamed Since'")                  # a recomputed payload would carry this
    conn.execute("update carrier_current set insurer = 'Changed Under It'")
    deliver_pending(conn, sink, backoff_seconds=0)
    assert len(sink.calls) == 2 and sink.calls[0] == sink.calls[1]
    assert sink.calls[0][0]["companies"][0]["name"] == "Acct 0" and sink.calls[0][1] == "90000001:insurer_changed:F2"
    assert _ob(conn)[0][0] == "delivered"


def test_the_backoff_holds_a_failed_row_back_until_it_is_due(conn):
    _queue_one(conn)
    sink = FakeSink(["ambiguous"])
    deliver_pending(conn, sink, backoff_seconds=3600)
    assert deliver_pending(conn, sink, backoff_seconds=3600).retrying == 0 and len(sink.calls) == 1
    conn.execute("update change_outbox set next_attempt_at = now()")
    assert deliver_pending(conn, sink).delivered == 1 and len(sink.calls) == 2


def test_an_ambiguous_answer_after_the_sink_acted_creates_one_task_because_the_key_is_reused(conn):
    _queue_one(conn)
    sink = FakeSink(["ambiguous"], acts_before_failing=True)
    deliver_pending(conn, sink, backoff_seconds=0)
    assert _ob(conn)[0][:2] == ("pending", 1) and len(sink.tasks) == 1
    deliver_pending(conn, sink, backoff_seconds=0)
    assert _ob(conn)[0][:2] == ("delivered", 2) and len(sink.tasks) == 1 and len({k for _p, k in sink.calls}) == 1


@pytest.mark.parametrize("answer", ["weird", None, 7, ("ok", "t")])
def test_an_answer_the_sink_should_not_give_is_never_read_as_delivered(conn, answer):
    _queue_one(conn)
    sink = FakeSink([answer])
    deliver_pending(conn, sink, backoff_seconds=0)
    assert _ob(conn)[0][0] == "pending"


def test_a_rejected_answer_is_permanent_and_is_not_retried(conn):
    _queue_one(conn)
    sink = FakeSink(["rejected"])
    r = deliver_pending(conn, sink, backoff_seconds=0)
    assert r.dead == 1 and _ob(conn)[0][:3] == ("dead", 1, "rejected: the sink refused the content")
    assert deliver_pending(conn, sink, backoff_seconds=0).dead == 0 and len(sink.calls) == 1


def test_a_row_that_keeps_failing_goes_dead_after_the_attempt_limit_and_stops_being_pushed(conn):
    _queue_one(conn)
    sink = FakeSink(lambda n: RuntimeError("down"))
    for _ in range(6):
        deliver_pending(conn, sink, max_attempts=3, backoff_seconds=0)
    assert len(sink.calls) == 3 and _ob(conn)[0][:2] == ("dead", 3)


def test_a_live_claim_blocks_a_second_worker_and_an_expired_one_is_taken_over(conn):
    _queue_one(conn)
    first = claim_outbox(conn, lease_seconds=300)
    assert len(first) == 1 and claim_outbox(conn, lease_seconds=300) == []            # the first worker is mid push
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")   # it died
    second = claim_outbox(conn, lease_seconds=300)
    assert len(second) == 1 and second[0].attempts == 2 and second[0].claim_token != first[0].claim_token
    assert second[0].payload == first[0].payload and second[0].idempotency_key == first[0].idempotency_key


def test_a_restart_after_a_push_that_was_never_recorded_pushes_again_with_the_same_key_and_makes_one_task(conn):
    _queue_one(conn)
    sink = FakeSink()
    died = claim_outbox(conn, lease_seconds=300)[0]
    sink(died.payload, died.idempotency_key)                                           # the worker pushed, then the process died before storing the outcome
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    assert deliver_pending(conn, sink).delivered == 1
    assert len(sink.calls) == 2 and len(sink.tasks) == 1 and _ob(conn)[0][:2] == ("delivered", 2)


def test_a_worker_that_lost_its_claim_cannot_overwrite_the_new_workers_result(conn):
    _queue_one(conn)
    stale = claim_outbox(conn, lease_seconds=300)[0]
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    fresh = claim_outbox(conn, lease_seconds=300)[0]
    assert record_outcome(conn, fresh, SinkAnswer("ok", "t-fresh")) == "delivered"
    assert record_outcome(conn, stale, SinkAnswer("ambiguous", None, "late")) is None
    assert record_outcome(conn, stale, SinkAnswer("rejected", None, "late")) is None
    assert _ob(conn)[0][:4] == ("delivered", 2, "ok", "t-fresh")


def test_a_stale_workers_ok_does_not_count_while_another_worker_holds_the_row(conn):
    _queue_one(conn)
    stale = claim_outbox(conn, lease_seconds=300)[0]
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    fresh = claim_outbox(conn, lease_seconds=300)[0]
    assert record_outcome(conn, stale, SinkAnswer("ok", "t-stale")) is None
    assert _ob(conn)[0][0] == "pending" and fresh


def test_many_rows_are_claimed_in_a_batch_once_each(conn):
    _queue_one(conn, n=7)
    sink = FakeSink()
    assert deliver_pending(conn, sink, limit=3).delivered == 3
    assert deliver_pending(conn, sink, limit=100).delivered == 4
    assert len(sink.calls) == 7 and len({k for _p, k in sink.calls}) == 7 and deliver_pending(conn, sink).delivered == 0


# ----------------------------------------------------------------------------------------- the report

def test_the_report_counts_by_kind_for_its_own_date_lists_the_urgent_first_and_survives_a_rerun(conn):
    for i in range(4):
        add_company(conn, "Acct %d" % i, usdot=str(90000001 + i))
    load(conn, [row(usdot=str(90000001 + i), mc="") for i in range(4)], 0)
    day1 = [row(usdot="90000001", mc="", authority="REVOKED", adate="2026-09-15"), row(usdot="90000002", mc="", insurer="Quillon Mutual", filing="F2"),
            row(usdot="90000003", mc="", policy="CANCELLED", filing="F3", cancel="2026-10-01"), row(usdot="90000004", mc=""), row(usdot="90000050", mc="")]
    load(conn, day1, 1)
    load(conn, day1, 1)                                                                 # a rerun must not change the day's report
    load(conn, day1 + [row(usdot="90000060", mc="")], 2)                               # a later day has its own count
    rep = daily_report(conn, D(1))
    assert rep["events_by_kind"] == {"cancellation_filed": 1, "insurer_changed": 1, "authority_status_changed": 1, "new_entity": 1}
    assert [t["kind"] for t in rep["top_changes"]] == ["cancellation_filed", "insurer_changed", "authority_status_changed"]
    assert (rep["matched_events"], rep["unmatched_events"], rep["conflict_events"], rep["rows_changed"]) == (3, 1, 0, 4)
    assert rep["events_repeated"] == 0 and rep["outbox"] == {"pending": 3, "delivered": 0, "dead": 0}
    assert daily_report(conn, D(2))["events_by_kind"] == {"new_entity": 1}
    assert daily_report(conn)["snapshot_date"] == D(2).isoformat()
    text = format_report(rep)
    assert "cancellation_filed" in text and "Acct 2" in text and "3 matched a company" in text


def test_a_report_for_a_date_with_no_load_is_an_error_not_an_empty_day(conn):
    with pytest.raises(LookupError):
        daily_report(conn, D(3))


# ----------------------------------------------------------------------------------------- a property test over random worlds

def _world_scenarios(rng, current: Row, history: list[Row], day: date, serial: list[int]) -> Row:
    serial[0] += 1
    fid = "F%05d" % serial[0]
    pick = rng.choice(["insurer", "cancel", "replace", "authority", "reinstate", "date_only", "name_only", "flap", "filing_only", "reuse_filing"])
    insurers = ["Zorlan Casualty", "Quillon Mutual", "Brightvale Surety", "Kestrel Bay Indemnity"]
    if pick == "insurer":
        return current._replace(insurer=rng.choice([i for i in insurers if i != current.insurer]), filing_id=fid, filing_date=day.isoformat())
    if pick == "cancel":
        return current._replace(policy_status="CANCELLED", filing_id=fid, cancel_date=(day + timedelta(days=30)).isoformat())
    if pick == "replace":
        return current._replace(policy_status="REPLACED", insurer=rng.choice(insurers), filing_id=fid, filing_date=day.isoformat())
    if pick == "authority":
        return current._replace(authority_status=rng.choice(["ACTIVE", "INACTIVE", "REVOKED", "PENDING", ""]), authority_date=rng.choice([day.isoformat(), current.authority_date]))
    if pick == "reinstate":
        return current._replace(policy_status="ACTIVE", filing_id=fid, cancel_date="")
    if pick == "date_only":
        return current._replace(filing_date=day.isoformat())
    if pick == "name_only":
        return current._replace(legal_name=current.legal_name + "x")
    if pick == "flap":
        return rng.choice(history)
    if pick == "filing_only":
        return current._replace(filing_id=fid)
    return current._replace(policy_status=rng.choice(["CANCELLED", "REPLACED"]), filing_id=rng.choice([h.filing_id for h in history]))


def _shape(rng, i: int) -> tuple[str, str]:
    dot = rng.choice(["%d", "0%d", "00%d"]) % (90000000 + i)
    mc = rng.choice(["MC-%07d", "%d", "MC%d", "FF-%d"]) % (1000000 + i) if rng.random() < 0.9 else ""
    return dot, mc


def _noisy(rng, r: Row) -> Row:
    """The same carrier state written the way a sloppy exporter might: padding, odd case, a blank cell as a space, doubled spaces inside a name."""
    def pad(v):
        return v if rng.random() < 0.5 else rng.choice([" ", "  ", "\t"]) + v + rng.choice([" ", "  "])

    def case(v):
        return v.lower() if rng.random() < 0.3 else v

    def blank(v):
        return v if v else rng.choice(["", " "])

    return r._replace(insurer=pad(case(blank(r.insurer))), policy_status=pad(case(r.policy_status)), authority_status=pad(case(blank(r.authority_status))),
                      legal_name=pad(r.legal_name).replace(" ", "  "), filing_id=pad(blank(r.filing_id)), cancel_date=blank(r.cancel_date), authority_date=blank(r.authority_date))


@pytest.mark.parametrize("seed", range(18))
def test_random_worlds_agree_with_the_plain_rules_across_days_reruns_crashes_and_flaps(conn, seed):
    rng = random.Random(seed)
    for i in range(0, 12, 3):
        add_company(conn, "Co %d" % i, usdot="%d" % (90000000 + i), mc=None)
    shapes: dict[int, tuple[str, str]] = {}
    current: dict[int, Row] = {}
    past: dict[int, list[Row]] = {}
    model_events: set[tuple[str, str, str]] = set()
    model_hist = 0
    serial = [0]
    next_id = 0
    for day in range(7):
        todays: dict[int, Row] = {}
        if day == 0 or rng.random() < 0.6:
            for _ in range(rng.randint(3, 9)):
                shapes[next_id] = _shape(rng, next_id)
                base = row(usdot=shapes[next_id][0], mc=shapes[next_id][1], name="Lab %d" % next_id, filing="F%05d" % (next_id + 50000), insurer=rng.choice(["Zorlan Casualty", "Quillon Mutual"]))
                current_view = base
                todays[next_id] = current_view
                next_id += 1
        for ix, cur_row in list(current.items()):
            if rng.random() < 0.3:
                todays[ix] = _world_scenarios(rng, cur_row, past[ix], D(day), serial)._replace(usdot=shapes[ix][0], mc=shapes[ix][1])
            elif rng.random() < 0.8:
                todays[ix] = cur_row                                                    # unchanged but present; an absent carrier is the other 20 percent
        real = [_noisy(rng, r) if rng.random() < 0.3 else r for r in todays.values()]          # the file writes some cells padded, cased or blank-as-space; the rules must not care
        rng.shuffle(real)
        stale = [r._replace(insurer="Stale Version") for r in real if rng.random() < 0.1]      # an earlier duplicate row: the later one wins
        junk = [row(usdot=rng.choice(["", "000", "n/a"]), name="junk")] if rng.random() < 0.3 else []
        order = stale + real + junk
        qa = rng.random() < 0.3                                                                  # a quoted empty cell versus an unquoted one
        expected_missing = len([ix for ix in current if ix not in todays])

        if rng.random() < 0.4:
            base = fingerprint(conn)
            stage = rng.choice(STAGES)

            def boom(s, stage=stage):
                if s == stage:
                    raise RuntimeError("crash")

            with pytest.raises(RuntimeError):
                load(conn, order, day, on_stage=boom, quote_all=qa)
            assert fingerprint(conn) == base, "a crash at %s left a half day (seed %d day %d)" % (stage, seed, day)
        result = load(conn, order, day, quote_all=qa)
        assert result.rows_missing == expected_missing and result.unknown_policy_status == {}

        # the plain rules
        baseline = day == 0
        for ix, new in todays.items():
            old = current.get(ix)
            if old is not None and all(getattr(old, c) == getattr(new, c) for c in ("insurer", "policy_status", "authority_status", "authority_date", "filing_id", "filing_date", "cancel_date")):
                continue
            model_hist += 1
            if not baseline:
                for kind, ref in expected_events(old, new):
                    model_events.add((norm_dot(new.usdot), kind, ref))
            past.setdefault(ix, []).append(new)
            current[ix] = new
        got = {(d, k, r) for d, k, r in events(conn)}
        assert got == model_events, "events differ (seed %d day %d)" % (seed, day)
        assert _hist(conn) == model_hist
        assert result.events_new == (0 if baseline else result.events_new)

        if rng.random() < 0.5:                                                          # the same file again: nothing new, nothing changed
            before = fingerprint(conn)
            again = load(conn, order, day, quote_all=not qa)
            after = fingerprint(conn)
            assert again.events_new == 0 and again.rows_changed == 0
            assert before[:4] == after[:4] and before[5] == after[5]
    db_current = {d: (i, p, a, ad, f, fd, c) for d, i, p, a, ad, f, fd, c in conn.execute(
        "select usdot_norm, insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date from carrier_current")}
    want = {norm_dot(r.usdot): (normalise(r).insurer or None, normalise(r).policy_status or None, normalise(r).authority_status or None,
                                 date.fromisoformat(r.authority_date) if r.authority_date else None, r.filing_id or None,
                                 date.fromisoformat(r.filing_date) if r.filing_date else None, date.fromisoformat(r.cancel_date) if r.cancel_date else None)
            for r in current.values()}
    assert db_current == want
    # outbox rows only for matched events, one per event, keyed by the natural key
    keys = [k for (k,) in conn.execute("select idempotency_key from change_outbox").fetchall()]
    assert len(keys) == len(set(keys))
    queued = conn.execute("select count(*) from change_outbox").fetchone()[0]
    matched = conn.execute("select count(distinct event_id) from change_event_match").fetchone()[0]
    assert queued == matched


# ----------------------------------------------------------------------------------------- the whole flow at 2,000 carriers

def test_the_whole_flow_on_2000_generated_carriers_finds_exactly_what_the_generator_changed(conn, tmp_path):
    manifest = generate(tmp_path, n=2000, change_rate=0.02, new_rate=0.005, flappers=5, day1=date.today() - timedelta(days=5))
    assert load_companies(conn, tmp_path / "companies.csv") == manifest["companies"]
    d1, d2 = date.fromisoformat(manifest["snapshot_day1"]), date.fromisoformat(manifest["snapshot_day2"])
    base = apply_snapshot(conn, tmp_path / "carriers_day1.csv", d1)
    assert base.baseline and base.rows_staged == 2000 and base.events_new == 0 and base.rows_new == 2000

    day2 = apply_snapshot(conn, tmp_path / "carriers_day2.csv", d2)
    by_kind = dict(conn.execute("select kind, count(*) from change_event group by kind").fetchall())
    assert by_kind == manifest["expected_events_by_kind"] and day2.events_new == manifest["expected_events_total"]
    assert day2.rows_changed == manifest["rows_changed_tracked"] + manifest["rows_new"], "name only edits must not be examined"
    rep = daily_report(conn, d2)
    assert (rep["matched_events"], rep["conflict_events"], rep["unmatched_events"]) == (manifest["expected_matched"], manifest["expected_conflict"], manifest["expected_unmatched"])
    assert manifest["expected_conflict"] >= 1 and manifest["expected_matched"] >= 1

    fp = fingerprint(conn)
    again = apply_snapshot(conn, tmp_path / "carriers_day2.csv", d2)
    assert again.events_new == 0 and again.rows_changed == 0 and fingerprint(conn)[:4] == fp[:4] and fingerprint(conn)[5] == fp[5]

    sink = FakeSink()
    total = 0
    while True:
        n = deliver_pending(conn, sink, limit=500).delivered
        if not n:
            break
        total += n
    assert total == manifest["expected_matched"] + manifest["expected_conflict"] and len(sink.tasks) == total
    assert daily_report(conn, d2)["outbox"]["delivered"] == total
    assert "Carrier changes for %s" % d2 in format_report(rep)

    # Days 3 and 4: cancelled on day 2, reinstated on day 3, cancelled again on day 4 on the same filing id. The manifest says two alerts each, from the scenario.
    assert manifest["flap_carriers"] > 0
    day3 = apply_snapshot(conn, tmp_path / "carriers_day3.csv", d2 + timedelta(days=1))
    day4 = apply_snapshot(conn, tmp_path / "carriers_day4.csv", d2 + timedelta(days=2))
    n_flap = conn.execute("select count(*) from change_event where kind = 'cancellation_filed' and usdot_norm = any(%s)", (manifest["flap_usdots"],)).fetchone()[0]
    assert day3.rows_changed == manifest["flap_carriers"] and day3.events_new == 0 and day4.events_new == manifest["flap_carriers"]
    assert n_flap == manifest["expected_flap_cancellations"]
    assert apply_snapshot(conn, tmp_path / "carriers_day4.csv", d2 + timedelta(days=2)).events_new == 0


# ----------------------------------------------------------------------------------------- refusing a bad file

def _bulk(n, **kw):
    return [row(usdot=str(90000000 + i), mc="", name="Lab %d" % i, **kw) for i in range(n)]


def _moved(n_moved, total=20):
    """`total` carriers of which the first `n_moved` have a new insurer on a new filing."""
    return [row(usdot=str(90000000 + i), mc="", name="Lab %d" % i, insurer="Quillon Mutual" if i < n_moved else "Zorlan Casualty", filing="F2" if i < n_moved else "F1")
            for i in range(total)]


def test_a_file_with_a_header_and_no_rows_is_refused_even_when_forced(conn):
    load(conn, _bulk(5), 0)
    base = fingerprint(conn)
    for force in (False, True):
        with pytest.raises(changes.LoadRefused, match="no data rows"):
            apply_snapshot(conn, rows_to_csv([]).encode(), D(1), force=force)
    assert fingerprint(conn) == base


def test_a_baseline_that_stages_nothing_usable_is_refused_and_leaves_no_load_behind(conn):
    with pytest.raises(changes.LoadRefused):
        load(conn, [row(usdot=""), row(usdot="000")], 0, force=False)
    assert conn.execute("select count(*) from carrier_load").fetchone()[0] == 0
    assert load(conn, _bulk(3), 0, force=False).baseline                                  # the next real file is still the baseline


def test_a_file_that_stages_under_half_of_what_we_hold_is_refused_and_force_lets_it_through(conn):
    load(conn, _bulk(20), 0)
    base = fingerprint(conn)
    with pytest.raises(changes.LoadRefused, match="truncated"):
        load(conn, _bulk(9), 1, force=False)
    assert fingerprint(conn) == base
    half = load(conn, _bulk(10), 1, force=False)                                          # exactly half is allowed
    assert half.events_new == 0 and half.rows_missing == 10 and events(conn) == []         # absent carriers raise nothing
    with pytest.raises(changes.LoadRefused):
        load(conn, _bulk(10), 2, force=False, min_staged_ratio=0.9)                        # the threshold is a parameter
    assert load(conn, _bulk(9), 2, force=True).rows_missing == 11
    assert conn.execute("select count(*) from carrier_current").fetchone()[0] == 20       # nothing is deleted for being absent


def test_a_file_where_over_a_quarter_of_the_rows_moved_is_refused_and_force_lets_it_through(conn):
    load(conn, _bulk(20), 0)
    base = fingerprint(conn)
    with pytest.raises(changes.LoadRefused, match="moved"):
        load(conn, _moved(6), 1, force=False)                                             # 6 of 20 is 30 percent
    assert fingerprint(conn) == base and events(conn) == []
    assert load(conn, _moved(5), 1, force=False).events_new == 5                          # exactly 25 percent is allowed
    wide = _moved(5)
    wide[5:8] = [r._replace(insurer="Brightvale Surety", filing_id="F3") for r in wide[5:8]]
    with pytest.raises(changes.LoadRefused):
        load(conn, wide, 2, force=False, max_changed_ratio=0.1)                            # the threshold is a parameter
    assert load(conn, _moved(12), 2, force=True).events_new == 7                           # forced: a real mass change goes through


def test_a_file_dated_more_than_a_day_ahead_of_the_database_is_refused_and_force_lets_it_through(conn):
    today = conn.execute("select current_date").fetchone()[0]
    load(conn, _bulk(3), 0)
    base = fingerprint(conn)
    with pytest.raises(changes.LoadRefused, match="after the database"):
        apply_snapshot(conn, rows_to_csv(_bulk(3)).encode(), today + timedelta(days=2))
    assert fingerprint(conn) == base
    assert apply_snapshot(conn, rows_to_csv(_bulk(3)).encode(), today + timedelta(days=1)).events_new == 0      # tomorrow is allowed, a timezone slip
    assert apply_snapshot(conn, rows_to_csv(_bulk(3)).encode(), today + timedelta(days=2), force=True).events_new == 0


def test_the_report_shows_the_carriers_a_file_did_not_mention(conn):
    load(conn, _bulk(20), 0)
    load(conn, _bulk(12), 1)
    rep = daily_report(conn, D(1))
    assert rep["rows_missing"] == 8 and "8 held carriers not in the file" in format_report(rep)


# ----------------------------------------------------------------------------------------- cleaning cells before the hash

def test_blank_cells_padding_case_and_quoting_do_not_move_the_hash(conn):
    base = row()
    load(conn, [base], 0)
    variants = [
        base._replace(cancel_date=" ", authority_date=" 2024-01-01 "),
        base._replace(insurer="  zorlan   casualty "),
        base._replace(policy_status="active", authority_status="Active"),
        base._replace(filing_id=" F1 ", filing_date="2025-01-01 "),
        base._replace(legal_name="Lab   One  Freight "),
    ]
    for i, v in enumerate(variants):
        assert load(conn, [v], 1 + i).rows_changed == 0, v
    assert load(conn, [base], 10, quote_all=True).rows_changed == 0                       # quoted empty cells
    assert _hist(conn) == 1
    assert load(conn, [base._replace(insurer="Zorlan Casualty Co")], 11).rows_changed == 1  # a real difference still moves it


def test_unknown_policy_status_values_are_counted_and_shown_in_the_report(conn):
    load(conn, _bulk(3), 0)
    rows = _bulk(4)
    rows[0] = rows[0]._replace(policy_status="suspended")
    rows[1] = rows[1]._replace(policy_status="Suspended ")
    rows[2] = rows[2]._replace(policy_status="PENDING_REVIEW")
    r = load(conn, rows, 1)
    assert r.unknown_policy_status == {"SUSPENDED": 2, "PENDING_REVIEW": 1}
    rep = daily_report(conn, D(1))
    assert rep["unknown_policy_status"] == {"SUSPENDED": 2, "PENDING_REVIEW": 1} and "SUSPENDED x2" in format_report(rep)
    assert daily_report(conn, D(0))["unknown_policy_status"] == {}


# ----------------------------------------------------------------------------------------- what makes two alerts the same alert

def _cancellations(conn):
    return conn.execute("select detail ->> 'effective' from change_event where kind = 'cancellation_filed' order by id").fetchall()


def test_cancelled_reinstated_and_cancelled_again_on_one_filing_alerts_twice_and_a_replay_alerts_once(conn):
    add_company(conn, "Acct", usdot="90000001")
    load(conn, [row(filing="F1")], 0)
    cancelled = [row(policy="CANCELLED", filing="F1", cancel="2026-10-31")]
    reinstated = [row(policy="ACTIVE", filing="F1")]
    cancelled_again = [row(policy="CANCELLED", filing="F1", cancel="2026-12-31")]
    load(conn, cancelled, 1)
    load(conn, cancelled, 1)                                                              # an exact replay
    assert _cancellations(conn) == [("2026-10-31",)]
    load(conn, reinstated, 2)
    assert load(conn, cancelled_again, 3).events_new == 1
    load(conn, cancelled_again, 3)
    assert _cancellations(conn) == [("2026-10-31",), ("2026-12-31",)]                      # two alerts, one per cancellation, written from the scenario
    keys = [k for (k,) in conn.execute("select idempotency_key from change_outbox").fetchall()]
    assert len(keys) == 2 and len(set(keys)) == 2


def test_a_cancellation_with_no_cancel_date_still_has_a_key_made_from_the_filing_date(conn):
    load(conn, [row()], 0)
    load(conn, [row(policy="CANCELLED", filing="F9", fdate="2026-09-09", cancel="")], 1)
    ref = conn.execute("select event_ref from change_event where kind = 'cancellation_filed'").fetchone()[0]
    assert ref and "2026-09-09" in ref and ref.startswith("F9")


# ----------------------------------------------------------------------------------------- a company added after its change

def test_a_company_added_after_the_change_gets_its_alert_once(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    assert conn.execute("select count(*) from change_outbox").fetchone()[0] == 0
    cid = add_company(conn, "Late Acct", usdot="0090000001")
    assert match_late_company(conn, cid) == 1
    assert match_late_company(conn, cid) == 0                                              # already queued
    payload = conn.execute("select payload from change_outbox").fetchone()[0]
    assert payload["match"] == "matched" and payload["companies"][0]["name"] == "Late Acct" and payload["kind"] == "insurer_changed"
    assert deliver_pending(conn, FakeSink()).delivered == 1


def test_the_late_match_window_is_the_number_of_days_since_the_event_was_raised(conn):
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    conn.execute("set session_replication_role = replica; update change_event set created_at = now() - interval '40 days'; reset session_replication_role")
    cid = add_company(conn, "Late Acct", usdot="90000001")
    assert match_late_company(conn, cid, days=30) == 0 and conn.execute("select count(*) from change_event_match").fetchone()[0] == 0
    assert match_late_company(conn, cid, days=60) == 1


def test_a_late_company_never_adds_a_second_queue_row_for_an_event_already_queued(conn):
    add_company(conn, "First", usdot="90000001")
    load(conn, [row()], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2")], 1)
    second = add_company(conn, "Second", mc="MC-1000001")
    assert match_late_company(conn, second) == 0
    assert conn.execute("select count(*) from change_outbox").fetchone()[0] == 1
    assert conn.execute("select count(*) from change_event_match").fetchone()[0] == 2     # the fact is recorded, the queued payload is not rewritten


def test_a_late_company_only_picks_up_events_for_its_own_carrier(conn):
    load(conn, [row(), row(usdot="90000002", mc="MC-1000002")], 0)
    load(conn, [row(insurer="Quillon Mutual", filing="F2"), row(usdot="90000002", mc="MC-1000002", insurer="Quillon Mutual", filing="F3")], 1)
    cid = add_company(conn, "Only Two", usdot="90000002")
    assert match_late_company(conn, cid) == 1
    assert [p["usdot"] for (p,) in conn.execute("select payload from change_outbox").fetchall()] == ["90000002"]


# ----------------------------------------------------------------------------------------- outbox: throttling, requeue, commits

def test_a_throttled_answer_reschedules_without_spending_an_attempt_and_honours_retry_after(conn):
    _queue_one(conn)
    sink = FakeSink([SinkAnswer("throttled", None, "429", 0)] * 6)
    for _ in range(6):
        r = deliver_pending(conn, sink, max_attempts=2, backoff_seconds=3600)
    assert r.throttled == 1 and len(sink.calls) == 6
    assert _ob(conn)[0][:3] == ("pending", 0, "throttled: 429")                            # six pushes, none counted against the limit of two
    assert deliver_pending(conn, sink).delivered == 1 and _ob(conn)[0][:2] == ("delivered", 1)


def test_a_throttled_answer_with_retry_after_holds_the_row_back_for_that_long(conn):
    _queue_one(conn)
    slow = FakeSink([SinkAnswer("throttled", None, "429", 3600)])
    deliver_pending(conn, slow)
    assert deliver_pending(conn, slow).throttled == 0 and len(slow.calls) == 1             # held back for the hour the sink asked for


def test_dead_rows_can_be_requeued_with_the_reason_recorded(conn):
    _queue_one(conn, n=2)
    deliver_pending(conn, FakeSink(["rejected", "rejected"]))
    first, second = [e for (e,) in conn.execute("select event_id from change_outbox order by event_id").fetchall()]
    assert requeue_dead(conn, [first], reason="sink fixed") == 1
    row_first = conn.execute("select status, attempts, requeued_count, last_outcome from change_outbox where event_id = %s", (first,)).fetchone()
    assert row_first[:3] == ("pending", 0, 1) and row_first[3].startswith("requeued: sink fixed") and "rejected" in row_first[3]
    assert conn.execute("select status from change_outbox where event_id = %s", (second,)).fetchone()[0] == "dead"
    assert deliver_pending(conn, FakeSink()).delivered == 1
    assert requeue_dead(conn) == 1 and requeue_dead(conn) == 0


def test_claim_and_outcome_commit_on_their_own_on_a_normal_connection(conn):
    _queue_one(conn)
    normal = psycopg.connect(DSN)                                                          # autocommit off, the default
    try:
        claimed = claim_outbox(normal)
        assert len(claimed) == 1 and _ob(conn)[0][4] is True                               # another session sees the claim before the sink is called
        assert record_outcome(normal, claimed[0], SinkAnswer("ambiguous", None, "timeout"), backoff_seconds=0) == "pending"
    finally:
        normal.close()                                                                     # a lost outcome would roll back here and the push would be repeated blind
    assert _ob(conn)[0][:3] == ("pending", 1, "ambiguous: timeout") and _ob(conn)[0][4] is False
    normal = psycopg.connect(DSN)
    try:
        assert deliver_pending(normal, FakeSink()).delivered == 1
    finally:
        normal.close()
    assert _ob(conn)[0][:2] == ("delivered", 2)


# ----------------------------------------------------------------------------------------- a source where absence is the news (optional mode)

def _absent_world(conn, n=20):
    add_company(conn, "Acct 3", usdot="90000003")
    load(conn, _bulk(n), 0)


def _dropped(conn):
    return conn.execute("select usdot_norm, event_ref from change_event where kind = 'dropped_from_file' order by id").fetchall()


def test_with_the_mode_off_a_carrier_that_vanishes_is_counted_and_raises_nothing(conn):
    _absent_world(conn)
    r = load(conn, _bulk(19), 1, force=False)
    assert r.rows_missing == 1 and events(conn) == []
    assert conn.execute("select count(*) from carrier_current where absent_load is not null").fetchone()[0] == 0


def test_a_truncated_file_is_refused_in_absent_mode_instead_of_raising_an_ending_per_missing_carrier(conn):
    _absent_world(conn)
    base = fingerprint(conn)
    for force in (False, True):                                                           # force cannot skip the floor in this mode
        with pytest.raises(changes.LoadRefused, match="truncated"):
            load(conn, _bulk(5), 1, force=force, absent_is_event=True)
    assert fingerprint(conn) == base and events(conn) == []


def test_a_vanished_carrier_raises_one_event_on_the_first_absent_day_and_none_after_and_a_rerun_raises_nothing(conn):
    _absent_world(conn)
    gone = [r for r in _bulk(20) if r.usdot != "90000003"]
    first = load(conn, gone, 1, force=False, absent_is_event=True)
    assert first.events_new == 1 and [d for d, _r in _dropped(conn)] == ["90000003"]
    assert conn.execute("select count(*) from change_outbox").fetchone()[0] == 1          # matched to the company and queued like any event
    assert load(conn, gone, 1, force=False, absent_is_event=True).events_new == 0         # an exact rerun
    second_day = load(conn, gone, 2, force=False, absent_is_event=True)                    # the second absent day
    assert second_day.events_new == 0 and second_day.events_suppressed == 0              # not even raised and swallowed by the unique key: the mark is the first guard
    assert len(_dropped(conn)) == 1 and daily_report(conn, D(1))["events_by_kind"] == {"dropped_from_file": 1}


def test_a_carrier_that_returns_clears_the_mark_without_an_alert_and_a_second_vanish_alerts_again(conn):
    _absent_world(conn)
    everyone, gone = _bulk(20), [r for r in _bulk(20) if r.usdot != "90000003"]
    load(conn, gone, 1, force=False, absent_is_event=True)
    back = load(conn, everyone, 2, force=False, absent_is_event=True)
    assert back.events_new == 0 and back.rows_changed == 1                                # it goes through the history path
    assert conn.execute("select absent_load from carrier_current where usdot_norm = '90000003'").fetchone()[0] is None
    assert conn.execute("select count(*) from carrier_history where usdot_norm = '90000003'").fetchone()[0] == 2
    load(conn, gone, 3, force=False, absent_is_event=True)
    keys = _dropped(conn)
    assert len(keys) == 2 and keys[0] != keys[1]                                           # a new key, so a new alert
    assert conn.execute("select count(*) from change_outbox").fetchone()[0] == 2


def test_a_carrier_that_stays_absent_and_one_that_returns_each_stay_independent(conn):
    load(conn, _bulk(20), 0)
    load(conn, _bulk(18), 1, force=False, absent_is_event=True)                            # 18 and 19 vanish
    assert sorted(d for d, _r in _dropped(conn)) == ["90000018", "90000019"]
    load(conn, _bulk(19), 2, force=False, absent_is_event=True)                            # 18 returns, 19 stays away
    assert len(_dropped(conn)) == 2
    marked = [d for (d,) in conn.execute("select usdot_norm from carrier_current where absent_load is not null").fetchall()]
    assert marked == ["90000019"]


def test_the_first_load_in_absent_mode_is_still_a_baseline(conn):
    r = load(conn, _bulk(5), 0, absent_is_event=True)
    assert r.baseline and r.events_new == 0


# ----------------------------------------------------------------------------------------- the row count against the previous load

def test_the_report_compares_the_file_with_the_previous_load(conn):
    first = load(conn, _bulk(20), 0)
    second = load(conn, _bulk(18), 1, force=False)
    assert first.previous_rows_staged is None and second.previous_rows_staged == 20
    rep = daily_report(conn, D(1))
    assert rep["previous_rows_staged"] == 20 and "18 rows in the file (previous load 20, -10.0 percent)" in format_report(rep)
    assert "previous load" not in format_report(daily_report(conn, D(0)))


# ----------------------------------------------------------------------------------------- the key reaches the sink as an external key

def test_the_payload_carries_the_same_key_as_an_external_key_for_the_sink_to_upsert_on(conn):
    _queue_one(conn)
    payload, key = conn.execute("select payload, idempotency_key from change_outbox").fetchone()
    assert payload["external_key"] == key == "90000001:insurer_changed:F2"


def test_a_crash_between_the_sink_succeeding_and_the_outcome_being_stored_leaves_one_record_when_the_sink_upserts_on_the_external_key(conn):
    _queue_one(conn)
    sink = FakeSink()
    died = claim_outbox(conn, lease_seconds=300)[0]
    sink(died.payload, died.idempotency_key)                                              # the CRM stored the record, then the worker died before record_outcome
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    assert deliver_pending(conn, sink).delivered == 1
    assert len(sink.calls) == 2 and len(sink.tasks) == 1


def test_the_same_crash_against_a_sink_that_does_not_dedupe_leaves_a_duplicate_record(conn):
    _queue_one(conn)
    sink = AppendSink()
    died = claim_outbox(conn, lease_seconds=300)[0]
    sink(died.payload, died.idempotency_key)
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    assert deliver_pending(conn, sink).delivered == 1
    assert len(sink.records) == 2 and sink.records[0] == sink.records[1]                  # at least once, and twice in effect: the limit, shown


def test_a_stale_workers_failure_report_does_not_release_the_new_workers_claim(conn):
    _queue_one(conn)
    stale = claim_outbox(conn, lease_seconds=300)[0]
    conn.execute("update change_outbox set lease_expires_at = now() - interval '1 second'")
    fresh = claim_outbox(conn, lease_seconds=300)[0]
    assert record_outcome(conn, stale, SinkAnswer("ambiguous", None, "late"), backoff_seconds=0) is None
    assert record_outcome(conn, stale, SinkAnswer("rejected", None, "late")) is None
    status, attempts, outcome, _ref, claimed = _ob(conn)[0]
    assert (status, attempts, outcome, claimed) == ("pending", 2, None, True) and fresh
    assert claim_outbox(conn, lease_seconds=300) == []                                    # still the fresh worker's


# ----------------------------------------------------------------------------------------- the command line

def _cli(tmp_path, rows, day, *flags):
    f = tmp_path / ("file_%s.csv" % day)
    f.write_text(rows_to_csv(rows), encoding="utf-8")
    env = {**os.environ, "DATABASE_URL": DSN}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "load_carrier_snapshot.py"), str(f), "--date", D(day).isoformat(), *flags],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_the_command_line_loads_refuses_and_forces(conn, tmp_path):
    ok = _cli(tmp_path, _bulk(20), 0)
    assert ok.returncode == 0 and "baseline load" in ok.stdout
    refused = _cli(tmp_path, _bulk(5), 1)
    assert refused.returncode == 3 and "truncated" in refused.stderr and "nothing changed" in refused.stderr
    assert conn.execute("select count(*) from carrier_load").fetchone()[0] == 1
    forced = _cli(tmp_path, _bulk(5), 1, "--force")
    assert forced.returncode == 0 and "15 held carriers not in the file" in forced.stdout
    assert "analytics:analytics" not in ok.stdout + ok.stderr + refused.stdout + refused.stderr          # the connection string is never printed


def test_the_command_line_absent_flag_turns_the_mode_on_and_nothing_else_does(conn, tmp_path):
    _cli(tmp_path, _bulk(20), 0)
    off = _cli(tmp_path, _bulk(19), 1)
    assert off.returncode == 0 and conn.execute("select count(*) from change_event").fetchone()[0] == 0
    on = _cli(tmp_path, _bulk(18), 2, "--absent-is-event")
    assert on.returncode == 0 and conn.execute("select count(*) from change_event where kind = 'dropped_from_file'").fetchone()[0] == 2     # 18 and 19 both vanished; the day before, with the mode off, marked nothing
    broken = _cli(tmp_path, _bulk(18), 3, "--date", "not-a-date")
    assert broken.returncode == 2


# ----------------------------------------------------------------------------------------- output round 1 fixes: a connection inside a transaction, a re-filed alert, a spreadsheet number

from psycopg import pq  # noqa: E402


def _in_transaction():
    c = psycopg.connect(DSN)            # the psycopg default: not autocommit
    c.execute("select 1")               # any read leaves it inside a transaction
    return c


CALLS_THAT_COMMIT_ON_THEIR_OWN = {
    "apply_snapshot": lambda c: apply_snapshot(c, rows_to_csv([row()]).encode("utf-8"), D(0)),
    "load_companies": lambda c: load_companies(c, b"name,usdot_no,mc_no\nAcme,90000001,\n"),
    "match_late_company": lambda c: match_late_company(c, 1),
    "claim_outbox": lambda c: claim_outbox(c),
    "record_outcome": lambda c: record_outcome(c, changes.Claimed(1, "k", {}, 1, "00000000-0000-0000-0000-000000000000"), SinkAnswer("ok", "x")),
    "requeue_dead": lambda c: requeue_dead(c),
}


# CLASS-TABLE: every call that commits on its own, against a connection inside a transaction (refused) and an idle normal connection (accepted), generated by product()
@pytest.mark.parametrize("name,state", list(itertools.product(sorted(CALLS_THAT_COMMIT_ON_THEIR_OWN), ["inside a transaction", "idle normal connection"])))
def test_a_call_that_promises_to_commit_refuses_a_connection_inside_a_transaction(conn, name, state):
    c = _in_transaction() if state == "inside a transaction" else psycopg.connect(DSN)
    try:
        if state == "inside a transaction":
            with pytest.raises(changes.ConnectionInTransaction):
                CALLS_THAT_COMMIT_ON_THEIR_OWN[name](c)
            assert c.info.transaction_status == pq.TransactionStatus.INTRANS, "the refused call must not touch the caller's transaction"
        else:
            CALLS_THAT_COMMIT_ON_THEIR_OWN[name](c)            # must not raise
            assert c.info.transaction_status == pq.TransactionStatus.IDLE
    finally:
        c.rollback()
        c.close()


def test_report_then_delivery_on_a_normal_connection_is_seen_by_others_and_pushes_once(conn):
    add_company(conn, "Acme Freight", usdot="90000001")
    other = row(usdot="90000002", mc="MC-1000002", name="Other Freight")
    load(conn, [row(), other], 0)
    load(conn, [row(insurer="Quillon Mutual"), other], 1)
    sink = FakeSink()
    normal = psycopg.connect(DSN)       # the psycopg default, not autocommit
    try:
        daily_report(normal)
        assert normal.info.transaction_status == pq.TransactionStatus.IDLE, "a report must leave a normal connection as it found it"
        result = deliver_pending(normal, sink)
        assert result.delivered == 1 and len(sink.calls) == 1
    finally:
        normal.close()
    assert conn.execute("select count(*) from change_outbox where status = 'delivered'").fetchone()[0] == 1, "the outcome was lost with the connection"
    assert conn.execute("select attempts from change_outbox").fetchone()[0] == 1
    again = psycopg.connect(DSN)
    try:
        assert deliver_pending(again, sink).delivered == 0 and len(sink.calls) == 1, "a second run pushed the same alert again"
    finally:
        again.close()


@pytest.mark.parametrize("status,kind", [("CANCELLED", "cancellation_filed"), ("REPLACED", "replacement_filed")])
def test_a_new_filing_on_a_status_already_held_alerts_again_and_a_replay_alerts_once(conn, status, kind):
    # CLASS-TABLE: both statuses that carry a filing
    other = row(usdot="90000002", mc="MC-1000002", name="Other Freight")
    load(conn, [row(policy=status, filing="F2", cancel="2026-10-01"), other], 0)
    load(conn, [row(policy=status, filing="F3", cancel="2026-10-20"), other], 1)
    assert [(k, r) for d, k, r in events(conn) if d == "90000001"] == [(kind, "F3|2026-10-20")]
    before = events(conn)
    load(conn, [row(policy=status, filing="F3", cancel="2026-10-20"), other], 1)          # the same file again
    assert events(conn) == before, "a replay alerted again"
    load(conn, [row(policy=status, filing="F3", cancel="2026-10-20", name="Renamed Freight"), other], 2)   # an untracked column only
    assert events(conn) == before
    load(conn, [row(policy=status, filing="F4", cancel="2026-11-02"), other], 3)
    assert [k for d, k, r in events(conn) if d == "90000001"] == [kind, kind]


@pytest.mark.parametrize("raw,want", [("90005001.0", "90005001"), ("90005001.00", "90005001"), (" 90005001.0 ", "90005001"), ("1234.05", "123405"), ("12.50", "1250"), ("0.0", None)])
def test_a_spreadsheet_style_trailing_dot_zero_is_not_a_digit_of_the_usdot_number(conn, raw, want):
    assert conn.execute("select carrier_norm_dot(%s)", (raw,)).fetchone()[0] == want
    assert norm_dot(raw) == want


@pytest.mark.parametrize("raw,want", [("MC-713740.0", "MC713740"), ("713740.0", "MC713740"), ("FF-12.0", "FF12"), ("MC-71.30", "MC7130")])
def test_a_spreadsheet_style_trailing_dot_zero_is_not_a_digit_of_the_docket_number(conn, raw, want):
    assert conn.execute("select carrier_norm_mc(%s)", (raw,)).fetchone()[0] == want
    assert norm_mc(raw) == want


def test_the_command_line_says_why_a_load_could_not_run_and_never_prints_the_password(conn, tmp_path):
    # CLASS-TABLE: the ways a load fails before the rules run, each of which used to print only an exception class
    f = tmp_path / "bad_cell.csv"
    f.write_text(rows_to_csv([row()]).replace("2024-01-01", "garbage"), encoding="utf-8")
    run = lambda env_dsn, *args: subprocess.run([sys.executable, str(ROOT / "scripts" / "load_carrier_snapshot.py"), str(f), *args],   # noqa: E731
                                                  env={**os.environ, "DATABASE_URL": env_dsn}, capture_output=True, text=True, encoding="utf-8", timeout=60)
    bad_cell = run(DSN, "--date", D(0).isoformat())
    assert bad_cell.returncode == 2 and "not an ISO date: garbage" in bad_cell.stderr and "line 2" in bad_cell.stderr
    bad_date = run(DSN, "--date", "nonsense")
    assert bad_date.returncode == 2 and "nonsense" in bad_date.stderr
    secret = "S3CR3T-pw-9f2a"
    wrong = run(psycopg.conninfo.make_conninfo(DSN, password=secret), "--date", D(0).isoformat())
    assert wrong.returncode == 2 and secret not in wrong.stderr + wrong.stdout and "could not load: OperationalError" in wrong.stderr
    broken = run("postgresql://u:%s%%zzword@h/db" % secret, "--date", D(0).isoformat())          # a bare % makes the string unparseable, and libpq quotes what it could not parse
    assert broken.returncode == 2 and secret not in broken.stderr + broken.stdout and "could not be parsed" in broken.stderr


def test_the_report_names_rejected_and_repeated_rows_and_a_second_load_of_the_same_date(conn):
    other = row(usdot="90000002", mc="MC-1000002", name="Other Freight")
    load(conn, [row(), other, row(usdot="", name="No key"), row(usdot="90000003", mc="MC-1000003", name="Twice A"), row(usdot="90000003", mc="MC-1000003", name="Twice B")], 0)
    report = daily_report(conn)
    assert report["rows_rejected"] == 1 and report["rows_duplicate"] == 1 and report["loads_on_date"] == 1
    text = format_report(report)
    assert "1 rows rejected (no usable USDOT number) and 1 repeated carriers dropped" in text and "loads on this date" not in text
    load(conn, [row(insurer="Quillon Mutual"), other], 0)                                    # a corrected file published on the same date
    again = format_report(daily_report(conn, D(0)))
    assert "2 loads on this date: the file numbers above are from the first" in again
