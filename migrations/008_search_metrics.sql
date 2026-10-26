-- The Search Console metric keys, and the table for the figures that have a dimension.
--
-- `search_clicks` and `search_impressions` are counts of things that happened on the day and can be summed.
-- `search_position` is the day's average position in THOUSANDTHS (7123 is 7.123), an integer for the same
-- reason money is, and a gauge: positions are averages, and an average of averages is wrong, so the report
-- weights them by impressions and never adds them.
--
-- The snapshot table's key is (connection, metric, day), which has no room for "which query" or "which page".
-- The top queries and pages of each day go in their own table instead of being forced in as fake metrics. A
-- row is one day's clicks, impressions and average position for one query or one page. The figures are the
-- TOP rows Search Console returns for the day, never a total of the day.
--
-- Row level security is on and forced, as on every tenant table: the request role sees its tenant's rows, the
-- sync worker crosses tenants by design (it also needs DELETE, so a day's rows are replaced as a set when a
-- later sync returns a different top list, instead of the old ones living on beside the new).
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('search_clicks',      'Clicks',           'count', 'delta'),
    ('search_impressions', 'Impressions',      'count', 'delta'),
    ('search_position',    'Average position', 'ratio', 'gauge')
on conflict (key) do nothing;

create table if not exists search_top_row (
    id             bigserial primary key,
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    connection_id  uuid        not null references platform_connection (id) on delete cascade,
    observed_on    date        not null,
    dimension      text        not null check (dimension in ('query', 'page')),
    key            text        not null check (length(key) between 1 and 2048),
    clicks         bigint      not null check (clicks >= 0),
    impressions    bigint      not null check (impressions >= 0),
    position_milli bigint      not null check (position_milli >= 0),
    synced_at      timestamptz not null default now(),
    -- A click is an impression that was clicked, so clicks above impressions is a broken row.
    constraint clicks_within_impressions check (clicks <= impressions),
    unique (connection_id, observed_on, dimension, key)
);

create index if not exists idx_search_top_read on search_top_row (tenant_id, connection_id, observed_on);

alter table search_top_row enable row level security;
alter table search_top_row force row level security;

drop policy if exists search_top_by_tenant on search_top_row;
create policy search_top_by_tenant on search_top_row
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists worker_all_search_top on search_top_row;
create policy worker_all_search_top on search_top_row
    to sync_worker using (true) with check (true);

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new table owes its own.
grant select, insert, update, delete on search_top_row to app_request;
grant select, insert, update, delete on search_top_row to sync_worker;
grant usage, select on all sequences in schema public to app_request;
grant usage, select on all sequences in schema public to sync_worker;

-- Replace each (connection, day) in the payload as a set. Two rows with the same key in one payload collapse to
-- the last, as upsert_snapshots does, so a retried page corrects the day instead of aborting the run.
create or replace function upsert_search_rows(p_rows jsonb) returns integer as $$
declare
    n integer;
begin
    delete from search_top_row t
     using (select distinct r.connection_id, r.observed_on
              from jsonb_to_recordset(p_rows) as r(connection_id uuid, observed_on date)) d
     where t.connection_id = d.connection_id and t.observed_on = d.observed_on;
    insert into search_top_row (tenant_id, connection_id, observed_on, dimension, key,
                                clicks, impressions, position_milli, synced_at)
    select distinct on (r.connection_id, r.observed_on, r.dimension, r.key)
           r.tenant_id, r.connection_id, r.observed_on, r.dimension, r.key,
           r.clicks, r.impressions, r.position_milli, now()
      from jsonb_to_recordset(p_rows) as r(
             tenant_id uuid, connection_id uuid, observed_on date, dimension text, key text,
             clicks bigint, impressions bigint, position_milli bigint)
     order by r.connection_id, r.observed_on, r.dimension, r.key;
    get diagnostics n = row_count;
    return n;
end;
$$ language plpgsql;

grant execute on function upsert_search_rows(jsonb) to sync_worker;
