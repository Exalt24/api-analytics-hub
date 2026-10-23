"""Cron entry point: find stuck or stale syncs and alert a Slack incoming webhook.

    DATABASE_URL=postgresql://... SLACK_WEBHOOK_URL=https://hooks.slack.com/... python scripts/check_sync_health.py --state /var/lib/hub/alert_state.json

Exit 0: nothing wrong (or a repeat inside the quiet window). Exit 1: problems exist. Exit 2: the check itself could not run or the alert could not be delivered.
A failed check is never reported as healthy. The webhook URL is a secret: it is read from the environment and never printed, logged or put in an exception message.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.db import Database  # noqa: E402
from app.health import DEFAULT_STALE_SECONDS, decide, describe, load_state, read_problems, save_state  # noqa: E402
from app.obs import configure_logging  # noqa: E402

import logging  # noqa: E402

log = logging.getLogger("hub")


def post_slack(url: str, text: str, timeout: float = 10.0) -> None:
    try:
        resp = httpx.post(url, json={"text": text}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError("slack post failed: %s" % type(exc).__name__) from None
    if resp.status_code >= 300:
        raise RuntimeError("slack post failed: HTTP %d" % resp.status_code)


async def run(args) -> int:
    db = Database(role=os.environ.get("DATABASE_WORKER_ROLE", "sync_worker"), max_size=2)
    try:
        await db.connect()
        problems = await read_problems(db, args.stale_seconds)
    except Exception as exc:  # noqa: BLE001
        log.error("health check could not run", extra={"event": "health_check_failed"}, exc_info=(type(exc), exc, None))
        return 2
    finally:
        await db.close()

    for p in problems:
        log.warning("sync problem", extra={"event": "sync_problem", "connection_id": p.connection_id, "findings": "%s %ds" % (p.kind, p.age_seconds)})
    log.info(describe(problems), extra={"event": "health_check", "findings": len(problems)})

    state = load_state(args.state) if args.state else {}
    message, new_state = decide(state, problems, time.time(), args.repeat_seconds)
    if message and os.environ.get("SLACK_WEBHOOK_URL"):
        try:
            post_slack(os.environ["SLACK_WEBHOOK_URL"], message)
        except RuntimeError as exc:
            log.error(str(exc), extra={"event": "alert_failed"})
            return 2                       # state is NOT saved, so the next run tries again
    if args.state:
        save_state(args.state, new_state)
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", help="JSON file remembering what was already alerted (recommended under cron)")
    ap.add_argument("--repeat-seconds", type=int, default=6 * 3600, help="re-alert an unchanged problem set after this long")
    ap.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS, help="stale threshold for a connection with no schedule")
    args = ap.parse_args()
    configure_logging()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
