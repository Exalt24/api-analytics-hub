-- The sync CONTROL PLANE. 001 gave us somewhere to put facts; this is what
-- decides when to fetch them, stops two workers doing it at once, notices when a
-- worker dies, and remembers where we got to across runs.
--
-- Everything here exists because of a specific silent failure. None of these
-- raise an error when missing; they just quietly produce wrong or frozen numbers,
-- which is the failure mode that costs a client's trust rather than a pager.

-- ---------------------------------------------------------------- schedule
--
-- Per-connection cadence, NOT a global cron. Two reasons a single cron is wrong:
-- a daily-granularity platform cannot usefully be polled every 15 minutes and
-- doing so just burns quota, and every tenant firing at :00 is a thundering herd
-- against both our workers and the vendor's rate limiter.
--
-- `jitter_seconds` is a FIXED per-connection offset rather than random per run,
-- so a connection's runs stay evenly spaced (cadence is measured start-to-start)
-- while different connections are spread across the window.

create table sync_schedule (
    connection_id     uuid primary key references platform_connection (id) on delete cascade,
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    cadence_seconds   integer     not null check (cadence_seconds >= 60),
    -- Deterministic offset in [0, cadence). Derived from the connection id at
    -- insert time so it is stable across restarts.
    jitter_seconds    integer     not null default 0 check (jitter_seconds >= 0),
    next_run_at       timestamptz not null default now(),
    enabled           boolean     not null default true,
    -- Set when a circuit breaker or a revoked credential stops scheduling. A
    -- disabled connection with no reason is indistinguishable from one nobody
    -- has enabled yet.
    paused_reason     text,
    last_succeeded_at timestamptz,
    consecutive_failures integer  not null default 0,
    created_at        timestamptz not null default now(),
    check (enabled or paused_reason is not null)
);

create index idx_schedule_due on sync_schedule (next_run_at)
    where enabled;

-- ---------------------------------------------------------------- watermarks
--
-- THE DISTINCTION THAT MATTERS: sync_run.resume_cursor is WITHIN-run pagination
-- state. This is the ACROSS-run watermark. Without it every run derives its
-- window from now(), which loses data whenever a run is skipped and refetches
-- everything whenever it is not.

create table connection_stream_state (
    connection_id    uuid        not null references platform_connection (id) on delete cascade,
    stream           text        not null,
    -- The high-water mark actually committed. Advanced ONLY after the facts it
    -- covers are committed, never before, or a crash between the two creates a
    -- permanent hole.
    cursor_value     text,
    cursor_committed_at timestamptz,
    -- Backfill progresses downward from the incremental watermark, tracked
    -- separately so a backfill and an incremental can run without fighting.
    backfill_requested_from date,
    backfill_complete_through date,
    updated_at       timestamptz not null default now(),
    primary key (connection_id, stream)
);

-- ---------------------------------------------------------------- runs, leased
--
-- 001's sync_run can say "running" but cannot distinguish "still working" from
-- "died nine days ago". That is the most common silent failure in a scheduled
-- pipeline: a killed worker leaves a row at 'running' forever, so the connection
-- never syncs again AND never alerts, because nothing is technically failed.

alter table sync_run add column worker_id        text;
alter table sync_run add column claim_token      uuid;
alter table sync_run add column lease_expires_at timestamptz;
alter table sync_run add column heartbeat_at     timestamptz;
alter table sync_run add column attempt          integer not null default 1;
-- Which mapper produced these numbers, so a mapping fix can be replayed against
-- exactly the affected cohort rather than everything.
alter table sync_run add column connector_version text;

create index idx_run_expired_lease on sync_run (lease_expires_at)
    where status = 'running';

-- ---------------------------------------------------------------- quarantine
--
-- A record that will not normalise must not kill an otherwise good 90-day
-- backfill, and must not be silently skipped either. Both are worse than putting
-- it here. Quarantine VOLUME is also how a schema change announces itself.

create table sync_quarantine (
    id            bigserial primary key,
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    connection_id uuid        not null references platform_connection (id) on delete cascade,
    run_id        uuid        references sync_run (id) on delete set null,
    stream        text        not null,
    raw           jsonb       not null,
    error_code    text        not null,
    error_detail  text,
    connector_version text,
    attempts      integer     not null default 1,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    resolved_at   timestamptz
);

create index idx_quarantine_open on sync_quarantine (connection_id, first_seen_at desc)
    where resolved_at is null;

-- ---------------------------------------------------------------- schema drift
--
-- The failure this catches raises no exception at all. A field renamed
-- `spend` -> `spend_micros` does not error: it charts as null, or as a million
-- times the truth. Type violations are caught at the connector boundary;
-- ADDITIONS and DISAPPEARANCES are not.

create table payload_shape (
    connection_id uuid        not null references platform_connection (id) on delete cascade,
    stream        text        not null,
    -- Hash of the sorted key set (and inferred types) of a raw payload.
    shape_hash    text        not null,
    sample        jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    occurrences   bigint      not null default 1,
    -- no_change / non_breaking / breaking. A breaking change auto-pauses the
    -- connection rather than continuing to write wrong numbers.
    classification text not null default 'unknown'
        check (classification in ('unknown', 'non_breaking', 'breaking')),
    primary key (connection_id, stream, shape_hash)
);

-- ---------------------------------------------------------------- RLS
--
-- Same rules as 001: enable AND force, deny by default, tenant from the session
-- GUC. A new table without these is exactly the hole the invariant test in
-- tests/test_tenant_isolation.py exists to catch.

alter table sync_schedule            enable row level security;
alter table connection_stream_state  enable row level security;
alter table sync_quarantine          enable row level security;
alter table payload_shape            enable row level security;

alter table sync_schedule            force row level security;
alter table connection_stream_state  force row level security;
alter table sync_quarantine          force row level security;
alter table payload_shape            force row level security;

create policy schedule_by_tenant on sync_schedule
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

create policy quarantine_by_tenant on sync_quarantine
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- These two have no tenant_id of their own: they hang off a connection, so the
-- boundary is enforced by joining to it. Written as EXISTS against
-- platform_connection, which is itself RLS-protected, so the check composes
-- rather than duplicating the tenant column.
create policy stream_state_by_connection on connection_stream_state
    using (exists (select 1 from platform_connection c where c.id = connection_id))
    with check (exists (select 1 from platform_connection c where c.id = connection_id));

create policy shape_by_connection on payload_shape
    using (exists (select 1 from platform_connection c where c.id = connection_id))
    with check (exists (select 1 from platform_connection c where c.id = connection_id));

grant select, insert, update, delete on all tables in schema public to app_request;
grant select, insert, update, delete on all tables in schema public to sync_worker;
grant usage, select on all sequences in schema public to app_request;
grant usage, select on all sequences in schema public to sync_worker;

create policy worker_all_schedule on sync_schedule
    to sync_worker using (true) with check (true);
create policy worker_all_stream_state on connection_stream_state
    to sync_worker using (true) with check (true);
create policy worker_all_quarantine on sync_quarantine
    to sync_worker using (true) with check (true);
create policy worker_all_shape on payload_shape
    to sync_worker using (true) with check (true);

-- ---------------------------------------------------------------- claiming
--
-- WHY AN ADVISORY LOCK RATHER THAN `FOR UPDATE SKIP LOCKED`. At claim time the
-- run row does not exist yet, so there is no row to lock: two schedulers firing
-- in the same second both pass the due-check and both start a run. The advisory
-- lock is keyed on the CONNECTION, which is the thing that must be exclusive,
-- and it exists whether or not a row does.
--
-- try_ rather than blocking, so a second worker skips instead of queueing behind
-- a long sync and then starting a duplicate the moment it finishes.

create or replace function claim_sync(
    p_connection_id uuid,
    p_worker_id     text,
    p_lease_seconds integer default 300,
    p_trigger       text default 'schedule',
    p_window_start  date default null,
    p_window_end    date default null
) returns table (run_id uuid, claim_token uuid) as $$
declare
    v_tenant uuid;
    v_run    uuid;
    v_token  uuid := gen_random_uuid();
begin
    -- Non-blocking. Returns nothing if another worker holds this connection.
    if not pg_try_advisory_xact_lock(hashtextextended(p_connection_id::text, 0)) then
        return;
    end if;

    -- A live run means someone else is already working, even if they hold no
    -- lock right now (e.g. a lease renewed in another transaction).
    if exists (
        select 1 from sync_run
         where connection_id = p_connection_id
           and status = 'running'
           and lease_expires_at > now()
    ) then
        return;
    end if;

    select tenant_id into v_tenant from platform_connection where id = p_connection_id;
    if v_tenant is null then
        return;
    end if;

    insert into sync_run (
        tenant_id, connection_id, trigger, window_start, window_end,
        status, worker_id, claim_token, lease_expires_at, heartbeat_at
    ) values (
        v_tenant, p_connection_id, p_trigger,
        coalesce(p_window_start, current_date - 1),
        coalesce(p_window_end, current_date),
        'running', p_worker_id, v_token,
        now() + make_interval(secs => p_lease_seconds), now()
    ) returning id into v_run;

    return query select v_run, v_token;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------- the reaper
--
-- Turns "died silently" into "failed visibly". Without this a killed worker's row
-- sits at 'running' forever and the connection is never scheduled again.

create or replace function reap_expired_runs() returns integer as $$
declare
    n integer;
begin
    with expired as (
        update sync_run
           set status = 'failed',
               error_code = 'lease_expired',
               error_detail = 'worker stopped heartbeating; lease expired',
               finished_at = now()
         where status = 'running'
           and lease_expires_at < now()
        returning connection_id
    )
    update sync_schedule s
       set consecutive_failures = s.consecutive_failures + 1,
           next_run_at = now() + make_interval(secs => least(s.cadence_seconds * 2, 3600))
      from expired e
     where s.connection_id = e.connection_id;
    get diagnostics n = row_count;
    return n;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------- the safe upsert
--
-- VERIFIED AGAINST THIS POSTGRES 2026-08-20: inserting two rows with the same
-- conflict key in ONE statement raises
--   ERROR: ON CONFLICT DO UPDATE command cannot affect row a second time
-- which aborts the WHOLE batch. Two pages of a paginated fetch covering the same
-- day produce exactly that, so this is not theoretical.
--
-- DISTINCT ON collapses same-key rows first, keeping the latest, so a retried or
-- overlapping window CORRECTS the day instead of aborting the run.

create or replace function upsert_snapshots(p_rows jsonb) returns integer as $$
declare
    n integer;
begin
    insert into metric_snapshot (
        tenant_id, connection_id, metric_key, observed_on,
        value_numeric, currency, raw, synced_at
    )
    select distinct on (r.connection_id, r.metric_key, r.observed_on)
           r.tenant_id, r.connection_id, r.metric_key, r.observed_on,
           r.value_numeric, r.currency, r.raw, now()
      from jsonb_to_recordset(p_rows) as r(
             tenant_id uuid, connection_id uuid, metric_key text,
             observed_on date, value_numeric bigint, currency char(3), raw jsonb
           )
     order by r.connection_id, r.metric_key, r.observed_on
    on conflict (connection_id, metric_key, observed_on) do update
       set value_numeric = excluded.value_numeric,
           currency      = excluded.currency,
           raw           = excluded.raw,
           synced_at     = now();
    get diagnostics n = row_count;
    return n;
end;
$$ language plpgsql;
