# A DigitalOcean droplet for the hub

`deploy/digitalocean/` is a Terraform setup for one Linux VPS that runs the hub behind Caddy: a droplet, a firewall, a block volume for the database dumps and three monitoring alerts. It is the server-side partner of [docs/deploy.md](deploy.md) (the Caddyfile and the systemd units) and [docs/backups.md](backups.md) (the dumps and the proven restore).

**What was run, and what was not.** `terraform fmt`, `init`, `validate` and a `plan` with dummy values were run on 2026-11-06 with Terraform 1.16.6 and a fake token, so the configuration is valid and renders seven resources ([the output](test-runs/2026-11-06-digitalocean-validate.txt)). It has never been applied: no droplet exists, nothing was created in any account, and the first boot script (cloud-init) has not run on a real droplet. Treat the apply as the next step, not as something proven.

## What it creates

| Resource | What it does |
|---|---|
| `digitalocean_droplet.hub` | Ubuntu 24.04, `s-2vcpu-4gb` by default, monitoring agent on, weekly whole-droplet backups on, no password login |
| `digitalocean_volume.backups` | A 50 GB volume the dumps from `scripts/db_backup.py` are written to, so a lost droplet does not take its backups with it |
| `digitalocean_firewall.hub` | World-open on 80 and 443 only. SSH only from `admin_cidrs`. Postgres and the API are not listed, so they are unreachable from outside |
| `digitalocean_monitor_alert` (three) | CPU above 80 percent, memory above 90 percent and disk above 85 percent for 5 minutes, emailed to `alert_emails` |

The variable checks refuse the unsafe inputs: an empty SSH key list (the droplet would be unreachable), an `admin_cidrs` that includes `0.0.0.0/0` or `::/0`, and an empty alert list.

## Running it

```bash
cd deploy/digitalocean
export DIGITALOCEAN_TOKEN=...            # in the shell only; see docs/secrets.md
terraform init
terraform plan  -var 'ssh_key_fingerprints=["..."]' -var 'admin_cidrs=["203.0.113.7/32"]' -var 'alert_emails=["ops@example.com"]'
terraform apply  # same -var flags
```

After the apply: point the DNS record for `HUB_SITE` at `droplet_ipv4`, put the application's secrets in `/etc/hub/hub.env` ([docs/secrets.md](secrets.md)), copy the Caddyfile and the units as [docs/deploy.md](deploy.md) says, then run `python scripts/db_backup.py prove` once on the server and read its result before calling the host done.

## What the tests pin

`tests/test_deploy_digitalocean.py` reads the files as text, so it needs no account or network: SSH is never open to the world, only 80 and 443 are public, monitoring and backups are on, the three alerts watch the droplet, cloud-init is valid YAML with no secret in it and creates `/etc/hub/hub.env` empty and readable by root alone. Each check has a control that shows it can fail (open the SSH rule, swap a port, turn monitoring off, detach an alert, plant a secret).

## Limits

- Never applied to an account. The first boot (Docker, Caddy from its own apt repository, the volume link) is written from the documentation and not yet run.
- The droplet's weekly backups are DigitalOcean's whole-disk snapshots. The database's own backup is the dump on the volume, and its restore is proven by `scripts/db_backup.py prove`, not by the snapshot.
- Postgres runs on the droplet. A managed database, a second node and a load balancer are a different shape and are not here.
- Terraform state is local. A team would put it in a remote backend; the state file is ignored by git because it holds every value Terraform read.
