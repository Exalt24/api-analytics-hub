# Roadmap

The list, in the order I would do it.

1. **Hosting.** Caddy and systemd ([deploy](deploy.md)) are written, not run. A backup with a proven restore ([backups](backups.md)), and a DigitalOcean Terraform setup applied once on a real account and destroyed ([digitalocean](digitalocean.md)). A Railway config is schema-checked, not deployed ([railway](railway.md)).
2. **A clock for the syncs.** The monthly report has one (n8n); next is a cadence for `sync/runner.py`.
3. **Key management.** `CREDENTIAL_KEYS` is an environment variable ([docs/secrets.md](secrets.md)); a KMS with audited rotation and a re-seal command is next.
4. **Auth.** API keys with three roles fit a service integration; users, sessions and SSO are next for a team.
5. **Distinct counts.** `customers` and `users` are per-day distinct counts, so the report averages users over the days that have data.
6. **Connectors.** All eight are built against stand-ins and take an issued token; refresh is next, then a first run against a real account of each.
7. **Alerting.** A stuck or stale sync posts to a webhook ([docs/observability.md](observability.md)); paging is next.
8. **Quarantine.** Rows that will not normalise are captured in a table; review and replay is next.
9. **Traces.** JSON logs, a request id and per process Prometheus metrics are in; traces are next.
10. **Backfill.** A real one needs chunking, restart-safe resumption and a rate-limit budget shared with the live syncs.
