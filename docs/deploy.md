# Running it on a server: Caddy and systemd

`deploy/` holds what a Linux host needs in front of and around the hub: a Caddyfile and four systemd units. None of it is wired into the app code, and none of it has run on a rented server; what was run is listed under "What was run".

## The proxy

`deploy/Caddyfile` terminates TLS for one site and sends `/api/*`, `/health` and `/ready` to the FastAPI service and everything else to the dashboard. `/metrics` is answered 404 at the proxy and is scraped on the loopback port instead. The site name and both upstreams come from the environment (`HUB_SITE`, `HUB_API_UPSTREAM`, `HUB_WEB_UPSTREAM`) with defaults for a single host.

```
caddy validate --config deploy/Caddyfile
HUB_SITE=hub.example.com caddy run --config deploy/Caddyfile
```

Caddy itself runs from its own official systemd unit (`caddy.service` from the Caddy packages, which already sets `User=caddy` and hardening); `deploy/` ships only the Caddyfile. That unit reads `/etc/caddy/Caddyfile` and knows nothing about `/etc/hub/hub.env`, so the three placeholders need their own override or Caddy serves `hub.localhost` with a self-signed certificate instead of your domain:

From the repository root:

```
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl edit caddy
```

In the editor that opens, add these lines and save (they are file content, not shell commands):

```
[Service]
Environment=HUB_SITE=hub.example.com
```

The override sets only the site name; the two upstreams keep their single-host defaults (`127.0.0.1:8000` and `127.0.0.1:3100`), and add `Environment=HUB_API_UPSTREAM=...` lines if yours differ. Then check with the same variable the service will see, and reload: `HUB_SITE=hub.example.com caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`.

After an edit, `caddy validate` then `systemctl reload caddy`, not a restart, and read the log with `journalctl -u caddy`.

## The API and the dashboard

Nothing in `deploy/` starts them. Run the API as in the README (`uvicorn app.api.main:app --port 8000`) and build and start the dashboard on port 3100 as the README does, with two values set for the public site: `NEXT_PUBLIC_API_BASE=https://hub.example.com` when you run `npm run build` (it is baked into the page at build time and defaults to `http://127.0.0.1:8000`, so without it every visitor's browser calls port 8000 on their own machine and the dashboard shows nothing) and `DASHBOARD_ORIGIN=https://hub.example.com` in the API's environment (the origins it allows). Caddy then sends `/api/*` to the API on the same host name. Units for these two are not written.

## The units

| Unit | Does |
|---|---|
| `hub-backup.service` and `.timer` | 02:30 daily, then `db_backup.py backup` followed by `db_backup.py prove` (scratch restore and row count comparison). A missed run happens at the next boot (`Persistent=true`, which systemd honours only for calendar timers). A failed run starts the alert check at once (`OnFailure=`), and the check sees it because every `backup` and `prove` run records how it ended in `last_run.json` beside the dumps (a failed run newer than the newest dump is the problem `run_failed`; under a real systemd the check used to say healthy after a failed night because it judged only the age of the newest dump). |
| `hub-backup-check.service` and `.timer` | every 30 minutes after boot (an interval timer, so no `Persistent=`), `db_backup.py check`: sends the alert once when a backup or a restore proof is stale or failed, then repeats it every six hours. |

Both services run as the `hub` user with `ProtectSystem=strict`, `NoNewPrivileges` and a short `ReadWritePaths`. The check unit sees the backup directory read-only. The backup units expect the repository at `/opt/hub` with its virtualenv at `/opt/hub/.venv` (`apt-get install -y python3-venv && python3 -m venv /opt/hub/.venv && /opt/hub/.venv/bin/pip install -r /opt/hub/requirements.txt`; Debian 12 ships Python 3.11 and the README asks for 3.13, so install 3.13 first if the requirements will not install) and the Postgres client tools `pg_dump`, `pg_restore` and `psql`, version 17 or newer, on the `PATH`. The default repositories of Debian 12 and Ubuntu 24.04 carry only 15 and 16 (`apt-cache show postgresql-client-17` finds nothing there, checked in containers 2026-10-30), and `pg_dump` refuses a server newer than itself, so add the PostgreSQL project's own repository first: `apt-get update && apt-get install -y postgresql-common ca-certificates && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y`, then `apt-get install -y postgresql-client-17` (a fresh image has no package lists, so the first `apt-get update` is needed; the script asks for Enter unless given `-y`) (the `apt.postgresql.org.sh` step is from the PostgreSQL wiki page Apt, saved under the research evidence). Enabling `hub-backup.timer` does not run its service (it fires at 02:30), so a missing piece in the backup path shows up as a failed unit the next night; enabling `hub-backup-check.timer` on a running host runs the check at once, because a boot-relative timer that is already past fires immediately. Run `systemctl start hub-backup.service` once by hand and read `journalctl -u hub-backup` before trusting the timer. The user and the three directories must exist first, because a `ReadWritePaths` entry that is missing fails the unit:

```
useradd --system hub
install -d -o hub -g hub /var/backups/hub /var/lib/hub && install -d /etc/hub
install -m 600 /dev/null /etc/hub/hub.env
```

The last line creates `/etc/hub/hub.env` empty and readable by root only; fill it in (below) before the next step, because enabling the check timer on a running host runs the check at once and a missing or empty environment file fails that first run. The file holds the database password and the Slack webhook, which is why it is mode 600.

Install:

```
cp deploy/hub-backup*.service deploy/hub-backup*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hub-backup.timer hub-backup-check.timer
```

`/etc/hub/hub.env` is read by `EnvironmentFile=` and needs exactly two names: `DATABASE_URL` (a role that can read every table and can also `CREATE DATABASE` and connect to the `postgres` database, because `prove` restores into a throwaway database it creates and drops) and `SLACK_WEBHOOK_URL`, the only variable the alert reads. With `SLACK_WEBHOOK_URL` unset, nobody is paged: the check logs `alert_not_configured` as an error, exits 1 (the unit shows up in `systemctl --failed`) and remembers nothing, so the alert goes out on the first run after you set it. Set it before relying on the timers.

## What was run

- `caddy validate` with the `caddy:2` image: valid. From the repository root: `docker run --rm -v "$PWD/deploy:/etc/caddy:ro" caddy:2 caddy validate --config /etc/caddy/Caddyfile` (on Git Bash for Windows set `MSYS_NO_PATHCONV=1` and use `$(pwd -W)` for the path).
- A request through a running Caddy container to a stub upstream, over TLS with Caddy's own local certificate: `/api/kpis`, `/health` and `/` returned 200 from the upstream, `/metrics` returned 404 (a later widening of the matcher to `/metrics /metrics/*` covers its children, and an adversary pass over 30 spellings saw 404 for each). The first version used a bare `respond` for `/metrics` and the catch-all `handle` answered it first, so `/metrics` leaked through; the live request is what showed it, and `tests/test_deploy_files.py` pins the fix.
- `deploy/verify_units.sh` in a `debian:bookworm-slim` container (`docker run --rm -v "$PWD/deploy:/deploy:ro" debian:bookworm-slim sh /deploy/verify_units.sh`; add `-e BREAK=1` to see it fail on a broken unit): it installs systemd and creates stand-in files for `/opt/hub/.venv/bin/python`, `db_backup.py` and `/etc/hub/hub.env`, so `systemd-analyze verify` checks that the four units parse and that the `ExecStart` binaries they name exist, not that your real virtualenv does; it does not check `EnvironmentFile=` (it passes with `/etc/hub/hub.env` missing) or the `ReadWritePaths` directories. It passes on all four units. With one `ExecStart` pointed at a missing binary the same script exits 1 with `Command ... is not executable`, so the check does fail on a bad unit.
- `tests/test_deploy_files.py`: the properties above (the `hub` user, the sandbox, `Persistent=true`, backup before prove, every flag exists in `db_backup.py`, every written directory is allowed). Three of its checks were turned red on purpose (the Caddyfile once, the timers twice) and green again after restoring each file.

## Not run

No unit was started by a real systemd (a container without an init cannot), no alert was delivered from a timer, and nothing ran on a rented server. `systemd-analyze verify` proves the files parse and name real binaries, not that a nightly run succeeds on a host.
