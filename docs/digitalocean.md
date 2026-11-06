# A DigitalOcean droplet for the hub

`deploy/digitalocean/` is a Terraform setup for one Linux VPS that runs the hub behind Caddy: a droplet, a firewall, a block volume for the database dumps and three monitoring alerts. It is the server-side partner of [docs/deploy.md](deploy.md) (the Caddyfile and the systemd units) and [docs/backups.md](backups.md) (the dumps and the proven restore).

**What was run, and what was not.** On 2026-11-06 it was validated with a dummy-value plan ([the output](test-runs/2026-11-06-digitalocean-validate.txt)) and then applied once on a real DigitalOcean account, checked and destroyed, twice ([the run](test-runs/2026-11-06-digitalocean-apply.txt)). Seven resources came up in about 35 seconds, the first boot (cloud-init) finished in about four minutes, and Docker, Caddy, fail2ban and the monitoring agent were active, SSH password login was off, `/etc/hub/hub.env` was mode 600, and from outside only SSH (from one address) and port 80 answered. Not run: the application itself on the droplet, DNS and TLS, `scripts/db_backup.py prove` on the droplet, a triggered alert and a taken weekly backup. The droplet is not left running; it was a proof, and `deploy/digitalocean/live_check.sh` repeats it.

**What the real run changed.** Two things a plan with dummy values cannot show. With only `backups = true` DigitalOcean applied its own policy (daily, 7 days kept), not weekly, so the file now sets `backup_policy` to weekly on Sundays at 04:00 UTC (28 days kept). And the provider reads `backups` back as false, so every later plan wanted a change whose apply errored (backups were already on); `ignore_changes = [backups]` fixes that and the next plan reported no changes. Both are pinned by tests with controls.

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

`tests/test_deploy_digitalocean.py` reads the files as text, so it needs no account or network: SSH is never open to the world, only 80 and 443 are public, monitoring and backups are on, the three alerts watch the droplet, cloud-init is valid YAML with no secret in it and creates `/etc/hub/hub.env` with no key in it and readable by root alone, and the backup policy is weekly. Each check has a control that shows it can fail (open the SSH rule, swap a port, turn monitoring off, detach an alert, plant a secret).

## Limits

- Applied and destroyed, not kept running. Caddy has no site address until `HUB_SITE` is set, so only port 80 listens and 443 does not, which is why the real run saw 443 closed even though the firewall allows it.
- To repeat the proof: `export DIGITALOCEAN_TOKEN=... ALERT_EMAIL=...` then `bash deploy/digitalocean/live_check.sh` (a few cents, destroys what it makes, confirms the account is empty); `--plan-only` creates nothing. The script's full path was done by hand twice; only its `--plan-only` mode has run as the script.
- The droplet's weekly backups are DigitalOcean's whole-disk snapshots. The database's own backup is the dump on the volume, and its restore is proven by `scripts/db_backup.py prove`, not by the snapshot.
- Postgres runs on the droplet. A managed database, a second node and a load balancer are a different shape and are not here.
- Terraform state is local. A team would put it in a remote backend; the state file is ignored by git because it holds every value Terraform read.
