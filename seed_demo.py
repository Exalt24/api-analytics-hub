#!/usr/bin/env python
# LIFESPAN: permanent (the way anyone without the owner's Shopify store sees the dashboard run)
"""Load INVENTED store figures into the local Postgres and mint an admin and a viewer API key.

seed_local.py needs the owner's Shopify development store, so nobody else can run it. This one needs nothing: it writes 120 days of made-up
orders, gross sales, refunds and net sales for a store called demo-store.invalid, after applying every migration, through the same tables, the same upsert
function and the same key hashing as the real path. The figures are a fixed formula (no randomness), so two runs give the same dashboard. It drops and recreates the `public`
schema of the database it points at, like the other seeds and the database test suites, and writes only to the local Postgres (5433 by default).

    python seed_demo.py          then start the API and the dashboard as the README says and paste the printed key into the box
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg

from app.api.auth import generate_key, hash_key
from app.crypto import CredentialCipher, generate_key as gen_crypto_key

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
ROOT = Path(__file__).resolve().parent
DAYS = 120


def invented_points(end: date, days: int = DAYS) -> list[dict]:
    """A fixed formula for each day: a weekly rhythm, a slow rise and a few refunds. Money is in minor units (cents)."""
    out = []
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        orders = 8 + (i % 7) * 2 + i // 20
        gross = orders * (4200 + (i % 5) * 150)
        refunds = (i % 9 == 0) * 3500 + (i % 17 == 0) * 6000
        for key, value, cur in (("orders", orders, None), ("gross_sales", gross, "USD"), ("refunds", refunds, "USD"), ("net_sales", gross - refunds, "USD")):
            out.append({"metric_key": key, "observed_on": day.isoformat(), "value_numeric": value, "currency": cur})
    return out


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("drop schema if exists public cascade; create schema public")
        # every migration, in order: a stranger gets a database the whole API can use without a second step. 010 needs the pgvector extension, so it is
        # skipped with a message on a server that does not have it (the rest of the hub does not use it).
        has_vector = await conn.fetchval("select count(*) from pg_available_extensions where name = 'vector'")
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name.startswith("010_") and not has_vector:
                print("skipped", path.name, "(this Postgres has no pgvector extension)")
                continue
            await conn.execute(path.read_text(encoding="utf-8"))

        tenant_id = uuid.uuid4()
        await conn.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
        await conn.execute("insert into tenant (id, slug, name) values ($1,$2,$3)", tenant_id, "demo", "Demo Agency Client (invented data)")

        os.environ.setdefault("CREDENTIAL_KEYS", "1:" + gen_crypto_key())
        cipher = CredentialCipher()
        connection_id = uuid.uuid4()
        blob = cipher.encrypt(json.dumps({"access_token": "invented-demo-token"}), CredentialCipher.aad(str(tenant_id), str(connection_id), "shopify"))
        await conn.execute(
            """insert into platform_connection (id, tenant_id, platform, external_account, display_name, credentials_enc, scopes)
               values ($1,$2,'shopify',$3,$4,$5,$6)""",
            connection_id, tenant_id, "demo-store.invalid", "Invented demo store", blob, ["read_orders"])

        end = date.today()
        start = end - timedelta(days=DAYS - 1)
        points = invented_points(end)
        payload = [{"tenant_id": str(tenant_id), "connection_id": str(connection_id), "raw": "{}", **p} for p in points]
        written = await conn.fetchval("select upsert_snapshots($1::jsonb)", json.dumps(payload))
        run_id = await conn.fetchval(
            """insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, rows_written, api_calls, throttle_waits, finished_at)
               values ($1,$2,'manual',$3,$4,'succeeded',$5,0,0, now()) returning id""",
            tenant_id, connection_id, start, end + timedelta(days=1), written)

        keys = {"admin": generate_key(), "viewer": generate_key()}
        await conn.execute("select set_config('app.tenant_id', '', false)")
        for role, raw in keys.items():
            await conn.execute("insert into api_key (tenant_id, key_hash, label, role) values ($1,$2,$3,$4)", tenant_id, hash_key(raw), "demo-" + role, role)

        print("tenant     :", tenant_id)
        print("connection :", connection_id, "(invented store, no Shopify involved)")
        print("points     :", len(points), "written:", written)
        print("run        :", run_id)
        print()
        print("ADMIN KEY  :", keys["admin"])
        print("VIEWER KEY :", keys["viewer"])
        return 0 if written else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
