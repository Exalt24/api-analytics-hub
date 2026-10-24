"""Snapshot change detection: load one day's file of carriers, find what moved, raise each alert once and push it.

The schema is migrations/005_change_detection.sql. This file is the daily job around it, in four parts.

APPLY A DAY IN ONE TRANSACTION. `apply_snapshot` stages the file with COPY, compares it with the current table in one set based statement, writes history, raises events, matches them
to companies, queues the alerts and updates the current table. Every step is inside one `conn.transaction()`, so a crash, a bad row or a lost connection at any point leaves the
database as it was before the file, and the same file can simply be loaded again.

REFUSE A BAD FILE BEFORE IT COMMITS. A truncated file would look like a day of mass disappearance and a wrong baseline would turn every later day into a flood, so a load raises
LoadRefused (and changes nothing) for a file with no data rows, a file that stages under half of the carriers we hold, a file where over a quarter of the rows moved, and a date more than a
day ahead of the database clock. `force=True` skips the three judgement calls, never the empty file. Carriers the file does not mention raise no event: they are counted as rows_missing.

ONLY THE ROWS WHOSE HASH MOVED ARE EXAMINED. The staged file and the current table both carry a hash of the tracked columns only, computed after the cells are cleaned (trimmed, blank
as NULL, codes upper-cased). The compare keeps a row when its hash differs or it is new, so 400,000 rows come down to a few thousand before any per-kind logic runs.

NEVER ALERT TWICE. The events table has a UNIQUE natural key, and the insert is `on conflict do nothing`. A rerun of a file, an overlapping file, or a carrier that flaps back to a
state it already had cannot raise a second alert for the same filing.

THE OUTBOX PUSHES FROM THE STORED EVENT. An alert's payload is written once, when the event is raised. `deliver_pending` claims rows with a lease, calls the sink, and only then stores
the outcome. A row is delivered only when the sink answered ok; a raise, an ambiguous answer or a timeout leaves it pending for a retry that sends the same payload and the same
idempotency key. That is at least once toward the sink and once in effect only if the sink honours the key. No real CRM was used to build this.
"""
from __future__ import annotations

import csv
import io
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

__all__ = [
    "FILE_COLUMNS", "EVENT_KINDS", "KNOWN_POLICY_STATUS", "LoadBusy", "LoadRefused", "StaleSnapshot", "LoadResult", "apply_snapshot", "load_companies", "match_late_company",
    "SinkAnswer", "Claimed", "DeliveryResult", "claim_outbox", "record_outcome", "deliver_pending", "requeue_dead", "daily_report", "format_report", "rows_to_csv",
]

#: The columns of the daily file, in order. The first line of a file must name exactly these.
FILE_COLUMNS = (
    "usdot", "mc", "legal_name", "insurer", "policy_status", "authority_status",
    "authority_date", "filing_id", "filing_date", "cancel_date",
)

#: Most urgent first. A report lists matched changes in this order.
EVENT_KINDS = ("cancellation_filed", "dropped_from_file", "replacement_filed", "insurer_changed", "authority_status_changed", "new_entity")

#: The policy statuses the event rules know. Any other value is counted on every load and shown in the report, so a vocabulary change in the feed is a number, not silence.
KNOWN_POLICY_STATUS = ("ACTIVE", "CANCELLED", "REPLACED")

#: One load at a time. The staging table is shared, so two loads would interleave their rows.
LOAD_LOCK_KEY = 5_005_001

CHUNK_BYTES = 1 << 20


class LoadBusy(RuntimeError):
    """Another load holds the lock."""


class LoadRefused(ValueError):
    """The file was refused before anything committed: empty, too small against what we hold, too much changed, or dated in the future."""


class StaleSnapshot(LoadRefused):
    """The file is older than a load already applied. Applying it would roll the carriers back and raise events for changes that are already history."""


@dataclass
class LoadResult:
    load_id: int
    snapshot_date: date
    baseline: bool
    rows_staged: int
    rows_rejected: int
    rows_duplicate: int
    rows_changed: int
    rows_new: int
    rows_missing: int
    events_new: int
    events_suppressed: int
    unknown_policy_status: dict[str, int] = field(default_factory=dict)
    previous_rows_staged: int | None = None
    seconds: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ the daily load

def _chunks(source):
    """Yield bytes from a path or a file like object, a megabyte at a time."""
    if isinstance(source, (str, Path)):
        with open(source, "rb") as fh:
            yield from _chunks(fh)
        return
    if isinstance(source, (bytes, bytearray)):
        yield bytes(source)
        return
    while True:
        block = source.read(CHUNK_BYTES)
        if not block:
            return
        yield block.encode("utf-8") if isinstance(block, str) else block


def _check_header(first_block: bytes) -> None:
    line = first_block.split(b"\n", 1)[0].decode("utf-8-sig").strip()
    got = tuple(next(csv.reader([line]), ()))
    if got != FILE_COLUMNS:
        raise ValueError("the file header is %r, expected %r" % (got, FILE_COLUMNS))


def apply_snapshot(
    conn: psycopg.Connection,
    source,
    snapshot_date: date,
    *,
    baseline: bool | None = None,
    force: bool = False,
    absent_is_event: bool = False,
    min_staged_ratio: float = 0.5,
    max_changed_ratio: float = 0.25,
    on_stage: Callable[[str], None] | None = None,
) -> LoadResult:
    """Apply one day's file. `baseline` defaults to "no load has happened yet": the first file only fills the tables, it does not raise 400,000 new_entity alerts.

    A normal load is refused (LoadRefused, nothing committed) when it stages under `min_staged_ratio` of the carriers we hold, or when more than `max_changed_ratio` of its rows moved, or
    when its date is over a day ahead of the database's date. `force=True` skips those three; a file with no data rows is refused regardless.

    `absent_is_event` (off by default) is for a source whose active-only file drops a carrier when its policy ends, where absence IS the news: a carrier held and not in the file raises one
    `dropped_from_file` event on the first load it is missing from and none after, and clears when it returns. It makes the under-half refusal non-negotiable: `force` cannot skip it, since a
    truncated file would otherwise read as thousands of endings. A source that writes an ending as a row (a status and a cancel date) leaves it off.

    `on_stage` is called with the name of each stage as it finishes, still inside the transaction. A test raises from it to simulate a crash at that point.
    """
    seconds: dict[str, float] = {}
    mark = [time.perf_counter()]

    def done(stage: str) -> None:
        now = time.perf_counter()
        seconds[stage] = seconds.get(stage, 0.0) + (now - mark[0])
        mark[0] = now
        if on_stage:
            on_stage(stage)
            mark[0] = time.perf_counter()

    started = time.perf_counter()
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("select pg_try_advisory_xact_lock(%s)", (LOAD_LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise LoadBusy("another snapshot load is running")

        cur.execute("select max(snapshot_date), count(*), current_date, (select rows_staged from carrier_load order by id desc limit 1) from carrier_load")
        latest, loads, today, previous_staged = cur.fetchone()
        if latest is not None and snapshot_date < latest:
            raise StaleSnapshot("the file is dated %s but %s is already loaded" % (snapshot_date, latest))
        if not force and (snapshot_date - today).days > 1:
            raise LoadRefused("the file is dated %s, more than a day after the database's date %s: a typo or a clock slip would block every later file" % (snapshot_date, today))
        if baseline is None:
            baseline = loads == 0
        cur.execute("insert into carrier_load (snapshot_date, baseline) values (%s, %s) returning id", (snapshot_date, baseline))
        load_id = cur.fetchone()[0]
        done("setup")

        cur.execute("truncate carrier_stage")
        first = True
        with cur.copy(
            "copy carrier_stage (usdot_raw, mc_raw, legal_name_raw, insurer_raw, policy_status_raw, authority_status_raw, authority_date_raw, filing_id_raw, filing_date_raw, cancel_date_raw) "
            "from stdin with (format csv, header true)"
        ) as copy:
            for block in _chunks(source):
                if first:
                    _check_header(block)
                    first = False
                copy.write(block)
        if first:
            raise LoadRefused("the file is empty")
        cur.execute("select count(*) from carrier_stage")
        if cur.fetchone()[0] == 0:
            raise LoadRefused("the file has a header and no data rows")
        done("copy")

        # A row with no usable USDOT number cannot be tied to a carrier, and two rows for one carrier cannot both be today's state. The first is counted and dropped; of the second the
        # last row in the file wins, which is right for a file with one row per carrier and wrong for one with several rows per carrier (see docs/change-detection.md). Both counts are
        # kept on the load so a bad file is visible, not silently trimmed.
        cur.execute("delete from carrier_stage where usdot_norm is null")
        rejected = cur.rowcount
        cur.execute(
            "delete from carrier_stage s using (select usdot_norm, max(seq) as keep from carrier_stage group by usdot_norm having count(*) > 1) d "
            "where s.usdot_norm = d.usdot_norm and s.seq < d.keep"
        )
        duplicate = cur.rowcount
        cur.execute("analyze carrier_stage")
        cur.execute("select count(*) from carrier_stage")
        staged = cur.fetchone()[0]
        if staged == 0:
            raise LoadRefused("no row in the file has a usable USDOT number")
        cur.execute("select policy_status, count(*) from carrier_stage where policy_status is not null and policy_status <> all(%s) group by 1 order by 2 desc, 1 limit 20",
                    (list(KNOWN_POLICY_STATUS),))
        unknown = dict(cur.fetchall())
        cur.execute("select count(*) from carrier_current")
        held = cur.fetchone()[0]
        if not baseline and (absent_is_event or not force) and held > 0 and staged < min_staged_ratio * held:
            raise LoadRefused("the file stages %d carriers but %d are held, under %d percent: a truncated file would read as a day of disappearances" % (staged, held, round(min_staged_ratio * 100)))
        done("clean")

        # The one set based comparison. Rows whose tracked hash is unchanged never leave this statement.
        cur.execute(
            """
            create temp table carrier_delta on commit drop as
            select s.usdot_norm, s.mc_norm, s.legal_name, s.insurer, s.policy_status, s.authority_status, s.authority_date, s.filing_id, s.filing_date, s.cancel_date, s.track_hash,
                   (c.usdot_norm is null) as is_new,
                   c.track_hash as prev_hash, c.insurer as old_insurer, c.policy_status as old_policy_status,
                   c.authority_status as old_authority_status, c.authority_date as old_authority_date
              from carrier_stage s
              left join carrier_current c on c.usdot_norm = s.usdot_norm
             where c.usdot_norm is null or c.track_hash is distinct from s.track_hash or c.absent_load is not null
            """
        )
        # A temporary table is never analysed by autovacuum, and the joins that follow read it several times.
        cur.execute("analyze carrier_delta")
        cur.execute("select count(*), count(*) filter (where is_new) from carrier_delta")
        changed, new = cur.fetchone()
        if not baseline and not force and changed > max_changed_ratio * staged:
            raise LoadRefused("%d of %d rows moved, over %d percent: a bad file or a feed change, not a day of insurance news" % (changed, staged, round(max_changed_ratio * 100)))
        cur.execute("select count(*) from carrier_current c where not exists (select 1 from carrier_stage s where s.usdot_norm = c.usdot_norm)")
        missing = cur.fetchone()[0]
        done("compare")

        cur.execute(
            """
            insert into carrier_history (load_id, snapshot_date, usdot_norm, mc_norm, insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date, prev_hash, track_hash)
            select %s, %s, usdot_norm, mc_norm, insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date, prev_hash, track_hash
              from carrier_delta
            """,
            (load_id, snapshot_date),
        )
        done("history")

        events_new = suppressed = 0
        if not baseline:
            events_new, suppressed = _raise_events(cur, load_id, snapshot_date, absent_is_event)
        done("events")

        if absent_is_event and not baseline:
            cur.execute(
                "update carrier_current cu set absent_load = %s where cu.absent_load is null and not exists (select 1 from carrier_stage s where s.usdot_norm = cu.usdot_norm)",
                (load_id,),
            )
        cur.execute(
            """
            update carrier_current c
               set mc_norm = d.mc_norm, legal_name = d.legal_name, insurer = d.insurer, policy_status = d.policy_status, authority_status = d.authority_status,
                   authority_date = d.authority_date, filing_id = d.filing_id, filing_date = d.filing_date, cancel_date = d.cancel_date, last_changed_load = %s, absent_load = null
              from carrier_delta d
             where not d.is_new and c.usdot_norm = d.usdot_norm
            """,
            (load_id,),
        )
        cur.execute(
            """
            insert into carrier_current (usdot_norm, mc_norm, legal_name, insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date,
                                         first_seen_load, last_changed_load)
            select usdot_norm, mc_norm, legal_name, insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date, %s, %s
              from carrier_delta where is_new
            """,
            (load_id, load_id),
        )
        cur.execute(
            "update carrier_load set rows_staged=%s, rows_rejected=%s, rows_duplicate=%s, rows_changed=%s, rows_new=%s, rows_missing=%s, events_new=%s, events_suppressed=%s, "
            "unknown_policy_status=%s, previous_rows_staged=%s where id=%s",
            (staged, rejected, duplicate, changed, new, missing, events_new, suppressed, Jsonb(unknown), previous_staged, load_id),
        )
        done("apply")
    seconds["total"] = time.perf_counter() - started
    return LoadResult(load_id, snapshot_date, baseline, staged, rejected, duplicate, changed, new, missing, events_new, suppressed, unknown, previous_staged, seconds)


def _match_events(cur, where: str, params: tuple) -> None:
    """Record which companies an event matched. `where` selects events (alias e) and may restrict companies (alias c); it is internal text, never input.

    Two equal joins, one per number, rather than one join on "either": the planner can hash each, and a company matched by both numbers collapses to one row.
    """
    cur.execute(
        f"""
        insert into change_event_match (event_id, company_id, matched_by)
        select event_id, company_id,
               case when bool_or(by_dot) and bool_or(by_mc) then 'both' when bool_or(by_dot) then 'usdot' else 'mc' end
          from (select e.id as event_id, c.id as company_id, true as by_dot, false as by_mc
                  from change_event e join carrier_company c on c.usdot_norm = e.usdot_norm where {where}
                union all
                select e.id, c.id, false, true
                  from change_event e join carrier_company c on c.mc_norm = e.mc_norm where {where}) x
         group by event_id, company_id
        on conflict do nothing
        """,
        params + params,
    )


def _queue_events(cur, where: str, params: tuple) -> int:
    """Queue an alert for every selected event that has at least one matched company and no queue row yet. Returns how many rows it queued.

    A change at a carrier we have no relationship with is recorded and reported, never pushed. Two matches make a conflict. The payload is written here, once; a retry sends it as stored.
    """
    cur.execute(
        f"""
        insert into change_outbox (event_id, idempotency_key, payload)
        select e.id, e.usdot_norm || ':' || e.kind || ':' || e.event_ref,
               jsonb_build_object('event_id', e.id, 'external_key', e.usdot_norm || ':' || e.kind || ':' || e.event_ref, 'kind', e.kind, 'usdot', e.usdot_norm, 'mc', e.mc_norm, 'snapshot_date', e.snapshot_date, 'detail', e.detail,
                                  'match', case when count(*) > 1 then 'conflict' else 'matched' end,
                                  'companies', jsonb_agg(jsonb_build_object('id', c.id, 'name', c.name, 'matched_by', m.matched_by) order by c.id))
          from change_event e
          join change_event_match m on m.event_id = e.id
          join carrier_company c on c.id = m.company_id
         where {where}
         group by e.id
        on conflict (event_id) do nothing
        """,
        params,
    )
    return cur.rowcount


def _raise_events(cur, load_id: int, snapshot_date: date, absent_is_event: bool = False) -> tuple[int, int]:
    """Turn the delta into events, match them to companies and queue the alerts. Returns (new events, suppressed repeats).

    event_ref is what makes two alerts about one carrier the same alert or two. A filing event carries the filing id and the date it takes effect (the cancel date, or the filing date when
    there is none), so a policy cancelled, reinstated and cancelled again on one filing id with a new date alerts twice, while a replay of either day alerts once.
    """
    # A carrier held and missing from the file, in the optional mode only. The key carries the load it last changed in, so a carrier that vanishes, returns and vanishes again alerts again.
    absent_sql = """
        union all
        select c.usdot_norm, c.mc_norm, 'dropped_from_file', coalesce(c.filing_id, '') || ':' || c.last_changed_load,
               jsonb_build_object('insurer', c.insurer, 'policy_status', c.policy_status, 'authority_status', c.authority_status, 'last_changed_load', c.last_changed_load)
          from carrier_current c
         where c.absent_load is null and not exists (select 1 from carrier_stage s where s.usdot_norm = c.usdot_norm)
    """ if absent_is_event else ""
    cur.execute(
        """
        create temp table carrier_candidate on commit drop as
        select usdot_norm, mc_norm, 'new_entity' as kind, '' as event_ref,
               jsonb_build_object('insurer', insurer, 'policy_status', policy_status, 'authority_status', authority_status) as detail
          from carrier_delta where is_new
        union all
        select usdot_norm, mc_norm, 'insurer_changed', coalesce(filing_id, ''), jsonb_build_object('from', old_insurer, 'to', insurer)
          from carrier_delta where not is_new and insurer is distinct from old_insurer
        union all
        select usdot_norm, mc_norm, 'cancellation_filed', coalesce(filing_id, '') || '|' || coalesce(to_char(coalesce(cancel_date, filing_date), 'YYYY-MM-DD'), ''),
               jsonb_build_object('from', old_policy_status, 'to', policy_status, 'effective', cancel_date)
          from carrier_delta where not is_new and policy_status = 'CANCELLED' and old_policy_status is distinct from 'CANCELLED'
        union all
        select usdot_norm, mc_norm, 'replacement_filed', coalesce(filing_id, '') || '|' || coalesce(to_char(coalesce(cancel_date, filing_date), 'YYYY-MM-DD'), ''),
               jsonb_build_object('from', old_policy_status, 'to', policy_status, 'insurer', insurer)
          from carrier_delta where not is_new and policy_status = 'REPLACED' and old_policy_status is distinct from 'REPLACED'
        union all
        select usdot_norm, mc_norm, 'authority_status_changed', coalesce(authority_status, '') || ':' || coalesce((authority_date - date '1970-01-01')::text, ''),
               jsonb_build_object('from', old_authority_status, 'to', authority_status, 'since', authority_date)
          from carrier_delta where not is_new and authority_status is distinct from old_authority_status
        """ + absent_sql
    )
    cur.execute("select count(*) from carrier_candidate")
    candidates = cur.fetchone()[0]
    cur.execute(
        """
        insert into change_event (load_id, snapshot_date, usdot_norm, mc_norm, kind, event_ref, detail)
        select %s, %s, usdot_norm, mc_norm, kind, event_ref, detail from carrier_candidate
        on conflict on constraint change_event_natural_key do nothing
        """,
        (load_id, snapshot_date),
    )
    inserted = cur.rowcount
    _match_events(cur, "e.load_id = %s", (load_id,))
    _queue_events(cur, "e.load_id = %s", (load_id,))
    return inserted, candidates - inserted


def load_companies(conn: psycopg.Connection, source) -> int:
    """Load a made-up company book (header name,usdot_no,mc_no) into carrier_company and return how many rows went in."""
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy("copy carrier_company (name, usdot_no, mc_no) from stdin with (format csv, header true)") as copy:
            for block in _chunks(source):
                copy.write(block)
        return cur.rowcount


def match_late_company(conn: psycopg.Connection, company_id: int, days: int = 30) -> int:
    """A company added after its carrier's change was raised: match the events raised in the last `days` days to it and queue their alerts. Returns how many alerts it queued.

    It uses the same matching and the same payload as the daily load, and never a second queue row for an event that is already queued (such an event gains its match row only).
    """
    with conn.transaction(), conn.cursor() as cur:
        where = "e.created_at >= now() - make_interval(days => %s) and c.id = %s"
        _match_events(cur, where, (days, company_id))
        return _queue_events(
            cur,
            "e.id in (select m2.event_id from change_event_match m2 join change_event e2 on e2.id = m2.event_id "
            "where m2.company_id = %s and e2.created_at >= now() - make_interval(days => %s))",
            (company_id, days),
        )


# ------------------------------------------------------------------------------------------------ the alert outbox

class SinkAnswer(NamedTuple):
    """What a sink says about one push. `ok` carries the sink's own id for the task it made. `rejected` is permanent (the sink refused the content). `ambiguous` means the sink may or may
    not have acted (a timeout, a 5xx), and the push is retried with the same key. `throttled` means the sink asked us to slow down (a 429): the row is rescheduled after `retry_after`
    seconds without spending one of its attempts."""
    status: str
    ref: str | None = None
    detail: str | None = None
    retry_after: float | None = None


Sink = Callable[[dict[str, Any], str], SinkAnswer]


@dataclass(frozen=True)
class Claimed:
    event_id: int
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    claim_token: str


@dataclass
class DeliveryResult:
    delivered: int = 0
    retrying: int = 0
    throttled: int = 0
    dead: int = 0
    lost_claim: int = 0


def claim_outbox(conn: psycopg.Connection, *, limit: int = 100, lease_seconds: int = 300) -> list[Claimed]:
    """Take up to `limit` due rows. A row whose lease is still running is not due, so two workers never push the same row, and a worker that died frees its rows when the lease ends.

    It commits on its own, on an autocommit connection or a normal one, so the claim is visible to other workers before the sink is called.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update change_outbox o
               set claim_token = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => %s), attempts = o.attempts + 1, last_attempt_at = now()
             where o.event_id in (select event_id from change_outbox
                                   where status = 'pending' and next_attempt_at <= now() and (lease_expires_at is null or lease_expires_at <= now())
                                   order by event_id limit %s for update skip locked)
            returning o.event_id, o.idempotency_key, o.payload, o.attempts, o.claim_token
            """,
            (lease_seconds, limit),
        )
        rows = cur.fetchall()
    return sorted((Claimed(r[0], r[1], r[2], r[3], str(r[4])) for r in rows), key=lambda c: c.event_id)


def record_outcome(
    conn: psycopg.Connection,
    claimed: Claimed,
    answer: SinkAnswer | None,
    *,
    max_attempts: int = 5,
    backoff_seconds: float = 60.0,
    backoff_cap_seconds: float = 3600.0,
) -> str | None:
    """Store what the sink said. Returns the new status, or None when this worker no longer holds the claim (its lease ran out and another worker took the row), in which case nothing is
    written, so a stale worker cannot overwrite a newer one's result. It commits on its own, so a caller on a normal connection cannot lose an outcome and push again."""
    with conn.transaction(), conn.cursor() as cur:
        if answer is not None and answer.status == "ok":
            cur.execute(
                "update change_outbox set status = 'delivered', delivered_at = now(), sink_ref = %s, last_outcome = 'ok', claim_token = null, lease_expires_at = null "
                "where event_id = %s and claim_token = %s and status = 'pending' returning status",
                (answer.ref, claimed.event_id, claimed.claim_token),
            )
        elif answer is not None and answer.status == "rejected":
            cur.execute(
                "update change_outbox set status = 'dead', last_outcome = %s, claim_token = null, lease_expires_at = null "
                "where event_id = %s and claim_token = %s and status = 'pending' returning status",
                ("rejected: " + (answer.detail or "")[:200], claimed.event_id, claimed.claim_token),
            )
        elif answer is not None and answer.status == "throttled":
            wait = backoff_seconds if answer.retry_after is None else answer.retry_after
            cur.execute(
                "update change_outbox set attempts = greatest(attempts - 1, 0), next_attempt_at = now() + make_interval(secs => least(%s, %s)), "
                "       last_outcome = %s, claim_token = null, lease_expires_at = null "
                "where event_id = %s and claim_token = %s and status = 'pending' returning status",
                (max(wait, 0), backoff_cap_seconds, "throttled: " + (answer.detail or "")[:200], claimed.event_id, claimed.claim_token),
            )
        else:
            outcome = "ambiguous: " + (answer.detail or "")[:200] if answer is not None and answer.status == "ambiguous" else "error: " + (
                answer.detail if answer is not None and answer.detail else "no usable answer")[:200]
            cur.execute(
                "update change_outbox "
                "   set status = case when attempts >= %s then 'dead' else 'pending' end, "
                "       next_attempt_at = now() + make_interval(secs => least(%s * power(2, attempts - 1), %s)), "
                "       last_outcome = %s, claim_token = null, lease_expires_at = null "
                " where event_id = %s and claim_token = %s and status = 'pending' returning status",
                (max_attempts, backoff_seconds, backoff_cap_seconds, outcome, claimed.event_id, claimed.claim_token),
            )
        row = cur.fetchone()
    return row[0] if row else None


def deliver_pending(
    conn: psycopg.Connection,
    sink: Sink,
    *,
    limit: int = 100,
    lease_seconds: int = 300,
    max_attempts: int = 5,
    backoff_seconds: float = 60.0,
) -> DeliveryResult:
    """One pass: claim due rows, push each stored payload to the sink, store the outcome. Call it from a schedule until it returns nothing delivered."""
    result = DeliveryResult()
    for claimed in claim_outbox(conn, limit=limit, lease_seconds=lease_seconds):
        try:
            answer = sink(claimed.payload, claimed.idempotency_key)
            if not isinstance(answer, SinkAnswer):
                answer = SinkAnswer("ambiguous", None, "unrecognised answer")
        except Exception as exc:  # noqa: BLE001
            # The class only: a driver message can carry a URL with a token in it.
            answer = SinkAnswer("ambiguous", None, type(exc).__name__)
        status = record_outcome(conn, claimed, answer, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        if status == "delivered":
            result.delivered += 1
        elif status == "dead":
            result.dead += 1
        elif status == "pending" and answer.status == "throttled":
            result.throttled += 1
        elif status == "pending":
            result.retrying += 1
        else:
            result.lost_claim += 1
    return result


def requeue_dead(conn: psycopg.Connection, event_ids: list[int] | None = None, *, reason: str = "operator requeue") -> int:
    """Put dead rows back to pending with their attempts reset, and record why on the row. `event_ids` limits it to those events; None means every dead row. Returns how many moved."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update change_outbox set status = 'pending', attempts = 0, requeued_count = requeued_count + 1, next_attempt_at = now(), claim_token = null, lease_expires_at = null, "
            "       last_outcome = left('requeued: ' || %s || ' (was ' || coalesce(last_outcome, 'no outcome') || ')', 300) "
            "where status = 'dead' and (%s::bigint[] is null or event_id = any(%s::bigint[]))",
            (reason, event_ids, event_ids),
        )
        return cur.rowcount


# ------------------------------------------------------------------------------------------------ the daily report

def daily_report(conn: psycopg.Connection, snapshot_date: date | None = None, *, top: int = 10) -> dict[str, Any]:
    """Counts by kind for one snapshot date (the latest date loaded when none is given) and the most urgent matched changes.

    The date, not the load, is the unit: a file loaded twice raised its events in the first load and nothing in the second, and the report for that day must still show them. The
    file's own numbers come from the first load of the date, and `events_repeated` adds up the repeats every load of that date suppressed.
    """
    with conn.cursor() as cur:
        if snapshot_date is None:
            cur.execute("select max(snapshot_date) from carrier_load")
            snapshot_date = cur.fetchone()[0]
        cur.execute("select id, baseline, rows_staged, rows_changed, rows_new, rows_missing, unknown_policy_status, previous_rows_staged, "
                    "(select coalesce(sum(events_suppressed), 0) from carrier_load where snapshot_date = %s) "
                    "from carrier_load where snapshot_date = %s order by id limit 1", (snapshot_date, snapshot_date))
        load = cur.fetchone()
        if load is None:
            raise LookupError("no load to report on")
        cur.execute("select kind, count(*) from change_event where snapshot_date = %s group by kind", (snapshot_date,))
        by_kind = dict(cur.fetchall())
        cur.execute(
            "select count(*) filter (where n = 0), count(*) filter (where n = 1), count(*) filter (where n > 1) "
            "from (select e.id, count(m.event_id) as n from change_event e left join change_event_match m on m.event_id = e.id where e.snapshot_date = %s group by e.id) t",
            (snapshot_date,),
        )
        unmatched, matched, conflicts = cur.fetchone()
        cur.execute(
            "select o.status, count(*) from change_outbox o join change_event e on e.id = o.event_id where e.snapshot_date = %s group by o.status", (snapshot_date,)
        )
        outbox = {"pending": 0, "delivered": 0, "dead": 0, **dict(cur.fetchall())}
        cur.execute(
            """
            select e.kind, e.usdot_norm, e.mc_norm, o.payload -> 'companies', e.detail
              from change_event e join change_outbox o on o.event_id = e.id
             where e.snapshot_date = %s
             order by array_position(%s::text[], e.kind), e.id
             limit %s
            """,
            (snapshot_date, list(EVENT_KINDS), top),
        )
        top_rows = [{"kind": k, "usdot": d, "mc": m, "companies": c, "detail": det} for k, d, m, c, det in cur.fetchall()]
    return {
        "snapshot_date": snapshot_date.isoformat(), "baseline": load[1], "rows_staged": load[2], "rows_changed": load[3], "rows_new": load[4], "rows_missing": load[5] or 0,
        "unknown_policy_status": load[6] or {}, "previous_rows_staged": load[7], "events_new": sum(by_kind.values()), "events_repeated": int(load[8]),
        "events_by_kind": {k: by_kind[k] for k in EVENT_KINDS if k in by_kind},
        "matched_events": matched, "conflict_events": conflicts, "unmatched_events": unmatched, "outbox": outbox, "top_changes": top_rows,
    }


def _versus_previous(report: dict[str, Any]) -> str:
    """The note after the row count: the previous load and the change. A file that shrinks a little every day is visible here long before it trips the refusal floor."""
    prev = report.get("previous_rows_staged")
    if not prev:
        return ""
    return " (previous load %d, %+.1f percent)" % (prev, 100.0 * (report["rows_staged"] - prev) / prev)


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "Carrier changes for %s%s" % (report["snapshot_date"], " (baseline load, no alerts)" if report["baseline"] else ""),
        "%d rows in the file%s, %d with a tracked change, %d new carriers, %d held carriers not in the file" % (
            report["rows_staged"], _versus_previous(report), report["rows_changed"], report["rows_new"], report["rows_missing"]),
    ]
    if report["unknown_policy_status"]:
        lines.append("unknown policy status values: " + ", ".join("%s x%d" % (k, v) for k, v in report["unknown_policy_status"].items()))
    for kind, n in report["events_by_kind"].items():
        lines.append("  %-26s %d" % (kind, n))
    lines.append("alerts: %d matched a company, %d matched two (needs a person), %d matched none; %d repeats suppressed" % (
        report["matched_events"], report["conflict_events"], report["unmatched_events"], report["events_repeated"]))
    lines.append("outbox: %(pending)d pending, %(delivered)d delivered, %(dead)d dead" % report["outbox"])
    for row in report["top_changes"]:
        names = ", ".join(c["name"] for c in (row["companies"] or []))
        lines.append("  %s  USDOT %s  %s" % (row["kind"], row["usdot"], names))
    return "\n".join(lines)


def rows_to_csv(rows, *, quote_all: bool = False) -> str:
    """Write rows (tuples in FILE_COLUMNS order, None for blank) as a daily file. `quote_all` writes a blank cell as a quoted empty string, the way some exporters do."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL)
    writer.writerow(FILE_COLUMNS)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return out.getvalue()
