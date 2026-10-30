#!/usr/bin/env python
# LIFESPAN: permanent (the way anyone sees the price watch loop run without a real shop or a real Shopify or Zoho account)
"""Load INVENTED products, competitor pages and shipping rates into the local Postgres, ready for scripts/price_watch.py.

Applies every migration to a scratch `public` schema (it DROPS the schema first, like seed_demo.py and the database tests, so aim it at a
scratch database), makes a tenant called price-watch-demo, and loads the catalogue, the eight listings that scripts/price_watch.py demo-server
serves on 127.0.0.1:8791, and a shipping rate table. Nothing here is real and nothing leaves the machine.

    python seed_price_watch_demo.py      prints the tenant id and the commands to run next (docs/price-watch.md)
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg

from app.price_watch.demo import load_demo

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
ROOT = Path(__file__).resolve().parent


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("drop schema if exists public cascade; create schema public")
        has_vector = await conn.fetchval("select count(*) from pg_available_extensions where name = 'vector'")
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name.startswith("010_") and not has_vector:
                print("skipped", path.name, "(this Postgres has no pgvector extension; price watch does not use it)")
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
        tenant_id = uuid.uuid4()
        await conn.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
        await conn.execute("insert into tenant (id, slug, name) values ($1, $2, $3)", tenant_id, "price-watch-demo", "Price watch demo (invented data)")
        counts = await load_demo(conn, str(tenant_id))
    finally:
        await conn.close()
    print("loaded %(products)d products, %(listings)d competitor listings and %(rates)d shipping bands (all invented)" % counts)
    print("tenant id:", tenant_id)
    print()
    print("next, in this order (docs/price-watch.md has the same list):")
    print("  export DATABASE_URL=%s" % DSN)
    print("  export PRICE_WATCH_TENANT=%s" % tenant_id)
    print("  python scripts/price_watch.py demo-server &        # the invented competitor shop on 127.0.0.1:8791")
    print("  python scripts/price_watch.py scan")
    print("  python scripts/price_watch.py report")
    print("  python scripts/price_watch.py proposals")
    print("  python scripts/price_watch.py approve --id <first 8 characters of an id> --actor you@example.com")
    print("  python scripts/price_watch.py sync --fakes             # dry run: shows what would be sent")
    print("  python scripts/price_watch.py sync --fakes --apply     # against the in-process fake Shopify and Zoho")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
