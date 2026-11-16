"""The resume worker: takes `apply.resume` messages off RabbitMQ and continues the runs they name.

    python -m app.jobs.worker            # runs until stopped, from the same JOBS_* variables as the API
    python -m app.jobs.worker --once     # handles what is queued now and exits (a test, or a cron)

The API publishes a resume message when the last open review task of a run is answered; this process spends the run's one-time token and opens a
fresh browser session to fill the form again, now with the answers. It runs apart from the API so a slow browser never holds a request, and any
number of workers can run: the token makes one of them win and the rest do nothing. A message that fails three times goes to the dead letter queue.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from app.db import Database

from . import hitl
from app.api.jobs import build_jobs_runtime

log = logging.getLogger("hub.jobs.worker")


async def main(once: bool) -> int:
    db = Database(min_size=1, max_size=3)
    await db.connect()
    rt = await build_jobs_runtime(db)
    if rt is None or rt.broker is None:
        log.error("the worker needs JOBS_S3_BUCKET and JOBS_AMQP_URL")
        await db.close()
        return 2
    stop = asyncio.Event()
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows has no loop signal handlers; Ctrl+C then raises KeyboardInterrupt
                pass

    async def handler(msg: dict) -> None:
        out = await hitl.resume_run(db, msg, browser=rt.browser, files=rt.files, submit=rt.submit, broker=rt.broker, board=rt.board)
        log.info("resumed run %s: %s", msg.get("run_id"), out.state if out else "skipped")

    try:
        while not stop.is_set():
            took = await rt.broker.consume_one(hitl.RESUME_QUEUE, handler, timeout=2.0)
            if once and not took:
                break
    finally:
        await rt.broker.close()
        await db.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    raise SystemExit(asyncio.run(main(ap.parse_args().once)))
