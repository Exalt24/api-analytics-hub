-- Proves the three functions in 002 actually work, against real Postgres.
-- Kept as a file rather than a heredoc because psql meta-commands and shell
-- escaping do not mix.

insert into tenant (id, slug, name) values
  ('11111111-1111-1111-1111-111111111111','t1','T1');
insert into platform_connection (id, tenant_id, platform, external_account, credentials_enc)
values ('22222222-2222-2222-2222-222222222222','11111111-1111-1111-1111-111111111111',
        'shopify','dac-dev-store.myshopify.com','x');

\echo '=== claim_sync: a worker claims the connection ==='
select run_id is not null as claimed from claim_sync('22222222-2222-2222-2222-222222222222','worker-a');

\echo '=== SAFE upsert: TWO rows for the SAME day in ONE call (raw ON CONFLICT aborts here) ==='
select upsert_snapshots($$[
  {"tenant_id":"11111111-1111-1111-1111-111111111111",
   "connection_id":"22222222-2222-2222-2222-222222222222",
   "metric_key":"orders","observed_on":"2026-08-01","value_numeric":10,"currency":null,"raw":null},
  {"tenant_id":"11111111-1111-1111-1111-111111111111",
   "connection_id":"22222222-2222-2222-2222-222222222222",
   "metric_key":"orders","observed_on":"2026-08-01","value_numeric":20,"currency":null,"raw":null}
]$$::jsonb) as rows_written;

\echo '=== corrected, not duplicated ==='
select count(*) as rows, max(value_numeric) as val from metric_snapshot where metric_key='orders';

\echo '=== re-running the same window corrects again rather than erroring ==='
select upsert_snapshots($$[
  {"tenant_id":"11111111-1111-1111-1111-111111111111",
   "connection_id":"22222222-2222-2222-2222-222222222222",
   "metric_key":"orders","observed_on":"2026-08-01","value_numeric":77,"currency":null,"raw":null}
]$$::jsonb) as rows_written;
select count(*) as rows, max(value_numeric) as val from metric_snapshot where metric_key='orders';

\echo '=== money without a currency is still refused by the CHECK ==='
select upsert_snapshots($$[
  {"tenant_id":"11111111-1111-1111-1111-111111111111",
   "connection_id":"22222222-2222-2222-2222-222222222222",
   "metric_key":"gross_sales","observed_on":"2026-08-01","value_numeric":500,"currency":null,"raw":null}
]$$::jsonb);

\echo '=== reaper turns a silently dead run into a visible failure ==='
update sync_run set lease_expires_at = now() - interval '1 hour';
insert into sync_schedule (connection_id, tenant_id, cadence_seconds)
values ('22222222-2222-2222-2222-222222222222','11111111-1111-1111-1111-111111111111', 900);
select reap_expired_runs() as reaped;
select status, error_code from sync_run;
select consecutive_failures from sync_schedule;
