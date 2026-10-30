# Backups and a proven restore

`scripts/db_backup.py` takes a `pg_dump` of the hub database, then restores the newest dump into a throwaway database and compares every table's row count with what the dump held. A backup only counts once a restore has worked, so the alert watches both clocks: the age of the newest dump and the age of the last good restore.

```bash
python scripts/db_backup.py backup --dir /var/backups/hub --keep 14     # dump + manifest (sha256, row counts before and after), prunes to 14
python scripts/db_backup.py prove  --dir /var/backups/hub               # scratch restore, row counts compared, scratch database dropped, result recorded
python scripts/db_backup.py check  --dir /var/backups/hub --state /var/lib/hub/backup_alert.json   # cron: Slack when a backup is missing or old, or a restore failed
```

It runs the PostgreSQL tools on the machine, or inside the database container when `PG_CMD_PREFIX` is set (`docker compose exec -T db`). The password travels in the child's environment only. The alert reuses the sync alert policy, so a standing problem alerts once and again after six hours, and a recovery sends one message.

Exit codes: 0 fine, 1 a problem exists (for `backup` and `prove`, the step failed), 2 the tool could not run or the alert could not be delivered. A failed check is never reported as healthy.

## What the first real restore found

Migration 005's helper functions called each other without a schema, and `pg_restore` runs with an empty search path while the generated columns on `carrier_stage` recompute as rows are copied back, so the restore died with `function carrier_clean_text(text) does not exist`. The calls are now `public.carrier_clean_text`, and a test applies every migration to a fresh database and restores its own dump, so it goes red on the old file. The run is recorded in [test-runs/2026-10-30-backup-restore-proof.txt](test-runs/2026-10-30-backup-restore-proof.txt).

## How it is tested

`tests/test_db_backup.py` holds the pure rules (the row count window, pruning, the alert problems), a 120 seed simulation of the alert policy with backups and proofs that succeed or fail while the clock jumps, and a real run: dump, restore, a dump with one flipped byte, a manifest that disagrees with the rows, pruning. The real run needs the PostgreSQL client tools or `PG_CMD_PREFIX`, and skips without them or when the client is older than the server. CI runs it through the database service container. Eight deliberate breakages (an off by one in the count window, a skipped checksum, a scratch database left behind, row counts never compared, among them) each turned the tests red.

## Limits

The proof restores into the same server, so it shows the dump is good but not that a different machine could start from it. The row counts are a window taken just before and after the dump, so a table written heavily during the dump passes inside that window, not to the row. There is no point in time recovery.
