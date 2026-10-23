-- Observability: one function that names every connection whose sync is not healthy, and one that counts runs.
--
-- WHY A FUNCTION AND NOT A DASHBOARD QUERY. "Stuck" has three different shapes and each one fails silently:
--   expired_lease    a run still says 'running' but its worker stopped heartbeating. The reaper turns it into a failure, but only when something calls it;
--                    until then the connection looks busy and nothing alerts.
--   never_succeeded  an active connection with no successful run at all (a bad first token, a schedule that was never enabled).
--   stale            the last success is older than the connection's own cadence allows. This is the one nobody notices: every run since then may
--                    have failed, been skipped by the lock or never started, and the dashboard still shows the last good numbers.
-- A plain function, not SECURITY DEFINER: row level security stays in force for the caller, so the sync_worker role (which crosses tenants by design)
-- sees every connection and a request role sees none. That is checked by a test, not assumed.

create or replace function sync_problems(p_default_stale_seconds integer default 21600)
returns table (connection_id uuid, platform text, kind text, age_seconds bigint) as $$
    with last_ok as (
        select r.connection_id, max(r.finished_at) as at
          from sync_run r
         where r.status in ('succeeded', 'partial')
         group by r.connection_id
    ),
    expired as (
        select r.connection_id, extract(epoch from (now() - r.lease_expires_at))::bigint as age
          from sync_run r
         where r.status = 'running' and r.lease_expires_at < now()
    )
    select c.id, c.platform, 'expired_lease', e.age
      from platform_connection c join expired e on e.connection_id = c.id
     where c.status = 'active'
    union all
    select c.id, c.platform, 'never_succeeded', extract(epoch from (now() - c.created_at))::bigint
      from platform_connection c
      left join last_ok l on l.connection_id = c.id
      left join sync_schedule s on s.connection_id = c.id
     where c.status = 'active' and l.at is null
       and now() - c.created_at > make_interval(secs => greatest(coalesce(s.cadence_seconds * 3, p_default_stale_seconds), 900))
    union all
    select c.id, c.platform, 'stale', extract(epoch from (now() - l.at))::bigint
      from platform_connection c
      join last_ok l on l.connection_id = c.id
      left join sync_schedule s on s.connection_id = c.id
     where c.status = 'active'
       and now() - l.at > make_interval(secs => greatest(coalesce(s.cadence_seconds * 3, p_default_stale_seconds), 900))
$$ language sql stable;

-- Counts by platform and status over a window, for the /metrics page.
create or replace function sync_run_counts(p_window_seconds integer default 86400)
returns table (platform text, status text, runs bigint) as $$
    select c.platform, r.status, count(*)
      from sync_run r join platform_connection c on c.id = r.connection_id
     where r.started_at > now() - make_interval(secs => p_window_seconds)
     group by c.platform, r.status
$$ language sql stable;

-- 001 granted schema USAGE to app_request only. On a stock database the PUBLIC default hides that; on one where public was recreated or locked down, sync_worker could not
-- even resolve a table name, and a worker-role pool failed with "function does not exist" rather than "permission denied". Explicit beats inherited.
grant usage on schema public to sync_worker;
grant execute on function sync_problems(integer), sync_run_counts(integer) to sync_worker;
