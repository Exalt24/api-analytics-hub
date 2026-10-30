#!/usr/bin/env python
# LIFESPAN: permanent (the way anyone sees the wallet and billing ledger run without a Stripe account or any money)
"""Load the plans and two INVENTED tenants into the local Postgres, ready for `python scripts/billing.py demo`.

Applies every migration to a scratch `public` schema (it DROPS the schema first, like seed_demo.py and the database tests, so aim it at a scratch
database), loads the two plans, and makes two tenants. Nothing here is real and nothing leaves the machine.

    python seed_billing_demo.py      prints the two tenant ids and the commands to run next (docs/billing.md)
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg

from app.billing.demo import TENANT_NAMES, load_plans

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
ROOT = Path(__file__).resolve().parent


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("drop schema if exists public cascade; create schema public")
        has_vector = await conn.fetchval("select count(*) from pg_available_extensions where name = 'vector'")
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name.startswith("010_") and not has_vector:
                print("skipped", path.name, "(this Postgres has no pgvector extension; billing does not use it)")
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
        await load_plans(conn)
        ids = {}
        for label, name in TENANT_NAMES.items():
            ids[label] = uuid.uuid4()
            await conn.execute("select set_config('app.tenant_id', $1, false)", str(ids[label]))
            await conn.execute("insert into tenant (id, slug, name) values ($1, $2, $3)", ids[label], "billing-demo-%s" % label.lower(), name)
    finally:
        await conn.close()
    print("loaded 2 plans (Starter, Growth) and 2 tenants (all invented)")
    for label, name in TENANT_NAMES.items():
        print("  tenant %s: %s  %s" % (label, ids[label], name))
    print()
    print("next, in this order (docs/billing.md has the same list):")
    print("  export DATABASE_URL=%s" % DSN)
    print("  python scripts/billing.py demo --tenant-a %s --tenant-b %s --out-dir out" % (ids["A"], ids["B"]))
    print("  python scripts/billing.py --tenant %s reconcile" % ids["A"])
    print("  python scripts/billing.py --tenant %s statement --currency AUD" % ids["A"])
    print("  python scripts/billing.py --tenant %s debit --event-type sync_run --quantity 3 --key try-1      # run it twice: the second changes nothing" % ids["A"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
