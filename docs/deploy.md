# Running it on a server: Caddy and systemd

`deploy/` holds what a Linux host needs in front of and around the hub: a Caddyfile and four systemd units. None of it is wired into the app code, and none of it has run on a rented server; what was run is listed under "What was run".

## The proxy

`deploy/Caddyfile` terminates TLS for one site and sends `/api/*`, `/health` and `/ready` to the FastAPI service and everything else to the dashboard. `/metrics` is answered 404 at the proxy and is scraped on the loopback port instead. The site name and both upstreams come from the environment (`HUB_SITE`, `HUB_API_UPSTREAM`, `HUB_WEB_UPSTREAM`) with defaults for a single host.

```
caddy validate --config deploy/Caddyfile
HUB_SITE=hub.example.com caddy run --config deploy/Caddyfile
```

Caddy itself runs from its own official systemd unit (`caddy.service` from the Caddy packages, which already sets `User=caddy` and hardening); `deploy/` ships only the Caddyfile. That unit reads `/etc/caddy/Caddyfile` and knows nothing about `/etc/hub/hub.env`, so the three placeholders need their own override or Caddy serves `hub.localhost` with a self-signed certificate instead of your domain:

```
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl edit caddy        # add the lines below, save, then: caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
[Service]
Environment=HUB_SITE=hub.example.com
```

After an edit, `caddy validate` then `systemctl reload caddy`, not a restart, and read the log with `journalctl -u caddy`.

## The units

| Unit | Does |
|---|---|
| `hub-backup.service` and `.timer` | 02:30 daily, then `db_backup.py backup` followed by `db_backup.py prove` (scratch restore and row count comparison). A missed run happens at the next boot (`Persistent=true`, which systemd honours only for calendar timers). A failed run starts the alert check at once (`OnFailure=`). |
| `hub-backup-check.service` and `.timer` | every 30 minutes after boot (an interval timer, so no `Persistent=`), `db_backup.py check`: sends the alert once when a backup or a restore proof is stale or failed, then repeats it every six hours. |

Both services run as the `hub` user with `ProtectSystem=strict`, `NoNewPrivileges` and a short `ReadWritePaths`. The check unit sees the backup directory read-only. The user and the three directories must exist first, because a `ReadWritePaths` entry that is missing fails the unit:

```
useradd --system hub
install -d -o hub -g hub /var/backups/hub /var/lib/hub && install -d /etc/hub
```

Install:

```
cp deploy/hub-backup*.service deploy/hub-backup*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hub-backup.timer hub-backup-check.timer
```

`/etc/hub/hub.env` carries `DATABASE_URL` and the Slack alert settings and is read by `EnvironmentFile=`.

## What was run

- `caddy validate` with the `caddy:2` image: valid.
- A request through a running Caddy container to a stub upstream, over TLS with Caddy's own local certificate: `/api/kpis`, `/health` and `/` returned 200 from the upstream, `/metrics` and `/metrics/x` returned 404. The first version used a bare `respond` for `/metrics` and the catch-all `handle` answered it first, so `/metrics` leaked through; the live request is what showed it, and `tests/test_deploy_files.py` pins the fix.
- `deploy/verify_units.sh` in a `debian:bookworm-slim` container: it installs systemd and creates stand-in files for `/opt/hub/.venv/bin/python`, `db_backup.py` and `/etc/hub/hub.env`, so `systemd-analyze verify` checks that the four units parse and that the paths they name exist, not that your real virtualenv does. It passes on all four units. With one `ExecStart` pointed at a missing binary the same script exits 1 with `Command ... is not executable`, so the check does fail on a bad unit.
- `tests/test_deploy_files.py`: the properties above (the `hub` user, the sandbox, `Persistent=true`, backup before prove, every flag exists in `db_backup.py`, every written directory is allowed). Two of its checks were turned red on purpose by breaking the Caddyfile and a timer.

## Not run

No unit was started by a real systemd (a container without an init cannot), no alert was delivered from a timer, and nothing ran on a rented server. `systemd-analyze verify` proves the files parse and name real binaries, not that a nightly run succeeds on a host.
