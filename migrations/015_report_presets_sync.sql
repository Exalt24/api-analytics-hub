-- Saved report presets that a browser keeps offline and syncs through Replicache's push and pull.
--
-- WHAT A PRESET IS. A name plus the export settings a person reuses (format and the AI summary switch). The
-- browser writes presets into its own IndexedDB first and sends them later, so a preset made with no network
-- is not lost; the server stores what the pushes say and hands every other browser of the same tenant the
-- changes it has not seen.
--
-- THE THREE TABLES (the "global version" strategy from the Replicache docs, one counter per tenant):
--   replicache_space   one row per tenant, `version` goes up by one for each push that changed anything.
--                      The pull cookie is that number. The row is locked for the length of a push, so two
--                      pushes for one tenant take turns and versions never collide.
--   replicache_client  one row per browser profile that has pushed (Replicache calls it a client): the last
--                      mutation id applied. A push that replays an id already applied is skipped, so a retry
--                      after a timeout cannot apply a change twice, and an id that skips ahead is refused.
--   report_preset      the data. A delete is a row with `deleted = true`, so a pull can tell another browser
--                      to remove it. The key is (tenant_id, id): two tenants may choose the same id and
--                      never meet.
--
-- Row level security is on and forced on all three, like every tenant table; the policy is the same one as
-- 009. Nothing here can be hard deleted by the request role (no delete grant).
--
-- Safe to run twice.

create table if not exists replicache_space (
    tenant_id uuid   primary key references tenant (id) on delete cascade,
    version   bigint not null default 0 check (version >= 0)
);

create table if not exists replicache_client (
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    client_id         text        not null check (client_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    client_group_id   text        not null check (client_group_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    last_mutation_id  bigint      not null default 0 check (last_mutation_id >= 0),
    version           bigint      not null default 0,
    updated_at        timestamptz not null default now(),
    primary key (tenant_id, client_id)
);

create index if not exists idx_replicache_client_group on replicache_client (tenant_id, client_group_id, version);

create table if not exists report_preset (
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    id               text        not null check (id ~ '^[A-Za-z0-9_-]{8,64}$'),
    name             text        not null check (char_length(name) between 1 and 60),
    format           text        not null check (format in ('csv', 'xlsx', 'pdf')),
    include_summary  boolean     not null default false,
    deleted          boolean     not null default false,
    version          bigint      not null,
    updated_at       timestamptz not null default now(),
    primary key (tenant_id, id)
);

create index if not exists idx_report_preset_version on report_preset (tenant_id, version);

do $$
declare
    t text;
begin
    foreach t in array array['replicache_space', 'replicache_client', 'report_preset'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('drop policy if exists %I on %I', t || '_by_tenant', t);
        execute format(
            'create policy %I on %I using (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'with check (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)',
            t || '_by_tenant', t);
        -- 001 granted every table that existed then; a grant is not retroactive (see 003), so each new table owes its own.
        execute format('grant select, insert, update on %I to app_request', t);
    end loop;
end
$$;
