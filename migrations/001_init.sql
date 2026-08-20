-- Canonical schema for a multi-tenant, multi-connector analytics warehouse.
--
-- Three decisions drive everything here, and each one exists because the obvious
-- alternative fails in a way that is hard to see later:
--
--   1. TENANT ISOLATION LIVES IN THE DATABASE. Filtering by tenant in application
--      code means one forgotten WHERE clause is a cross-client data leak. Row
--      level security is ENABLED and FORCED on every tenant-scoped table, so even
--      the table owner cannot bypass it, and the policy reads the tenant from a
--      session GUC set by the connection layer rather than from anything a caller
--      can put in a request.
--
--   2. METRICS ARE STORED AS DATED SNAPSHOTS, NEVER OVERWRITTEN. Platforms hand
--      you a current value (today's follower count, today's spend) and no history.
--      If you UPDATE a row you can never answer "growth over 90 days". Appending
--      one row per (account, metric, date) makes history a query instead of a
--      feature you have to add later.
--
--   3. MONEY IS AN INTEGER IN MINOR UNITS, WITH ITS CURRENCY BESIDE IT. Floats
--      accumulate error and two platforms agreeing on the word "cost" while
--      disagreeing on currency is worse than them using different words.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------- tenants

create table tenant (
    id          uuid primary key default gen_random_uuid(),
    slug        text        not null unique,
    name        text        not null,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- connections
--
-- One row per (tenant, platform, external account). Credentials are stored
-- ENCRYPTED as bytea: the application holds the key, so a database dump alone
-- does not hand over a shop's access token. The token is never selected by the
-- read paths, only by the sync worker.

create table platform_connection (
    id                 uuid primary key default gen_random_uuid(),
    tenant_id          uuid        not null references tenant (id) on delete cascade,
    platform           text        not null,
    external_account   text        not null,
    display_name       text,
    -- AES-256-GCM envelope: one version byte, a 96-bit random nonce, then
    -- ciphertext and tag. See app/crypto.py. The additional authenticated data
    -- binds the blob to (tenant_id, id, platform), so a ciphertext copied into
    -- another row stops decrypting. That matters because row level security
    -- stops a tenant READING another row and does nothing about a blob being
    -- moved between rows by anything holding write access.
    --
    -- This comment previously said "Fernet ciphertext" while no encryption code
    -- existed anywhere in the project. Never a plaintext column, and deliberately not JSON, so
    -- nothing can accidentally log it as a readable field.
    credentials_enc    bytea       not null,
    -- Absolute instant, NOT the relative expires_in the provider returns: a
    -- relative value is already wrong the moment it is read back from disk.
    token_expires_at   timestamptz,
    scopes             text[]      not null default '{}',
    status             text        not null default 'active'
                       check (status in ('active', 'paused', 'revoked')),
    created_at         timestamptz not null default now(),
    unique (tenant_id, platform, external_account)
);

create index idx_conn_tenant_platform on platform_connection (tenant_id, platform)
    where status = 'active';

-- ---------------------------------------------------------------- metric registry
--
-- The canonical vocabulary. Connectors translate INTO this; nothing downstream
-- ever sees a vendor's field name. `unit` is what makes "spend" from one platform
-- and "cost" from another comparable, and what stops a value in cents being
-- charted next to a value in dollars.

create table metric (
    key         text primary key,
    label       text not null,
    unit        text not null check (unit in ('minor_currency', 'count', 'ratio', 'seconds')),
    -- Whether a value describes a point in time (follower count) or an interval
    -- (spend on a given day). Summing a point-in-time metric across days is
    -- meaningless, and this is what lets the query layer refuse to.
    kind        text not null check (kind in ('gauge', 'delta'))
);

insert into metric (key, label, unit, kind) values
    ('orders',          'Orders',            'count',          'delta'),
    ('gross_sales',     'Gross sales',       'minor_currency', 'delta'),
    ('refunds',         'Refunds',           'minor_currency', 'delta'),
    ('net_sales',       'Net sales',         'minor_currency', 'delta'),
    ('avg_order_value', 'Average order value','minor_currency', 'gauge'),
    ('customers',       'Customers',         'count',          'gauge'),
    ('followers',       'Followers',         'count',          'gauge');

-- ---------------------------------------------------------------- the fact table
--
-- One row per (connection, metric, day). `observed_on` is the date the PLATFORM
-- reported, not the date we fetched: a late sync must not create a phantom flat
-- day, and a backfill must land on the day it belongs to.

create table metric_snapshot (
    id             bigserial primary key,
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    connection_id  uuid        not null references platform_connection (id) on delete cascade,
    metric_key     text        not null references metric (key),
    observed_on    date        not null,
    value_numeric  bigint      not null,
    currency       char(3),
    -- The untranslated payload this row was derived from, so a mapping mistake is
    -- recoverable without re-hitting the platform. Vendors also silently add
    -- fields, and this is where they are found.
    raw            jsonb,
    synced_at      timestamptz not null default now(),
    -- Re-running a sync for the same day must correct the row, not duplicate it.
    unique (connection_id, metric_key, observed_on),
    -- A currency is required for money and meaningless otherwise. Enforced here
    -- because the alternative is discovering it in a chart.
    constraint currency_iff_money check (
        (currency is not null) = (
            metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value')
        )
    )
);

create index idx_snapshot_read
    on metric_snapshot (tenant_id, metric_key, observed_on desc);
create index idx_snapshot_connection
    on metric_snapshot (connection_id, observed_on desc);

-- ---------------------------------------------------------------- sync runs
--
-- Every sync writes one row, success or failure. "The number looks wrong" is
-- unanswerable without this, and a scheduled job that silently stops looks
-- exactly like one with nothing to do.

create table sync_run (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    connection_id  uuid        not null references platform_connection (id) on delete cascade,
    trigger        text        not null check (trigger in ('schedule', 'manual', 'backfill')),
    window_start   date        not null,
    window_end     date        not null,
    status         text        not null default 'running'
                   check (status in ('running', 'succeeded', 'failed', 'partial')),
    rows_written   integer     not null default 0,
    api_calls      integer     not null default 0,
    throttle_waits integer     not null default 0,
    error_code     text,
    error_detail   text,
    -- Where a paginated run stopped, so a rate limit halfway through resumes
    -- instead of discarding the first half.
    resume_cursor  text,
    started_at     timestamptz not null default now(),
    finished_at    timestamptz
);

create index idx_run_recent on sync_run (tenant_id, connection_id, started_at desc);

-- ---------------------------------------------------------------- RLS
--
-- FORCE matters as much as ENABLE: without it the owning role bypasses every
-- policy, which is precisely the role the application connects as in most
-- deployments. Default deny, then one policy per table keyed on the session GUC.

alter table tenant              enable row level security;
alter table platform_connection enable row level security;
alter table metric_snapshot     enable row level security;
alter table sync_run            enable row level security;

alter table tenant              force row level security;
alter table platform_connection force row level security;
alter table metric_snapshot     force row level security;
alter table sync_run            force row level security;

-- current_setting(..., true) returns NULL rather than raising when the GUC is
-- unset, so a connection that forgot to set the tenant sees NOTHING instead of
-- erroring in a way someone might be tempted to catch and ignore.
create policy tenant_self on tenant
    using (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

create policy conn_by_tenant on platform_connection
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

create policy snapshot_by_tenant on metric_snapshot
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

create policy run_by_tenant on sync_run
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- ---------------------------------------------------------------- roles
--
-- THE REQUEST-SERVING ROLE MUST NOT BE A SUPERUSER OR THE TABLE OWNER, and this
-- is the part that is easy to get catastrophically wrong.
--
-- Measured 2026-08-20: the first run of the isolation suite showed tenant A
-- reading tenant B's rows even though RLS was ENABLED and FORCED on every table
-- and every policy was correct. The cause was the connection, not the schema:
-- the tests connected as the database's bootstrap user, which Postgres creates as
-- a SUPERUSER, and a superuser bypasses row level security unconditionally. FORCE
-- closes the table-owner hole; it does NOT close the superuser hole. Nothing in
-- the catalog looks wrong in that state, which is why a policy-by-policy test
-- suite passes while the database is open.
--
-- So the role is created here, in the migration, rather than left as a deployment
-- note somebody might skip.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'app_request') then
        create role app_request nologin;
    end if;
end
$$;

grant usage on schema public to app_request;
grant select, insert, update, delete on all tables in schema public to app_request;
grant usage, select on all sequences in schema public to app_request;

-- The sync worker needs to cross tenants by design (it services all of them), so
-- it gets its own role rather than an application-level escape hatch. Keeping the
-- bypass in a separate role means the request-serving role can never be talked
-- into it by a crafted request.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'sync_worker') then
        create role sync_worker;
    end if;
end
$$;

grant select, insert, update on all tables in schema public to sync_worker;
grant usage, select on all sequences in schema public to sync_worker;
alter table platform_connection force row level security;

create policy worker_all_connections on platform_connection
    to sync_worker using (true) with check (true);
create policy worker_all_snapshots on metric_snapshot
    to sync_worker using (true) with check (true);
create policy worker_all_runs on sync_run
    to sync_worker using (true) with check (true);
