#!/usr/bin/env python
"""Load live Shopify figures into the DEPLOYED database and mint demo API keys.

Separate from `seed_local.py` for one reason that matters: the local seeder starts
with `drop schema public cascade`, which is right for a scratch container and would
destroy a live database. This script contains no DROP and no schema reset. It is
idempotent through `upsert_snapshots`, so re-running refreshes rather than
duplicates.

Reads the target from ANALYTICS_DEPLOY_DSN so the connection string never appears
in the repo or in a shell history.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg

from app.api.auth import generate_key, hash_key
from app.connectors.shopify import ShopifyConnector
from app.crypto import CredentialCipher, generate_key as gen_crypto_key

SHOPIFY_SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")


async def main() -> int:
    dsn = os.environ.get("ANALYTICS_DEPLOY_DSN")
    if not dsn:
        raise SystemExit("ANALYTICS_DEPLOY_DSN is not set")

    text = io.open(SHOPIFY_SECRETS, encoding="utf-8").read()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()

    conn = await asyncpg.connect(dsn)
    try:
        where = await conn.fetchval("select current_schema()")
        print("schema:", where)
        if where == "public":
            # The deployed database shares an instance with another app that owns
            # public. Landing here means the role's search_path is wrong, and
            # writing would put these tables in someone else's schema.
            raise SystemExit("refusing: current_schema() is public, expected analytics")

        tenant_id = await conn.fetchval("select id from tenant where slug = 'demo'")
        if tenant_id is None:
            tenant_id = uuid.uuid4()
            await conn.execute("select set_config('app.tenant_id', $1, false)",
                               str(tenant_id))
            await conn.execute(
                "insert into tenant (id, slug, name) values ($1,'demo',$2)",
                tenant_id, "Demo Agency Client",
            )
            print("tenant created")
        else:
            await conn.execute("select set_config('app.tenant_id', $1, false)",
                               str(tenant_id))
            print("tenant reused")

        connection_id = await conn.fetchval(
            "select id from platform_connection where platform='shopify' "
            "and external_account=$1", shop,
        )
        if connection_id is None:
            connection_id = uuid.uuid4()
            keys_env = os.environ.get("CREDENTIAL_KEYS") or ("1:" + gen_crypto_key())
            os.environ["CREDENTIAL_KEYS"] = keys_env
            cipher = CredentialCipher()
            blob = cipher.encrypt(
                json.dumps({"access_token": token}),
                CredentialCipher.aad(str(tenant_id), str(connection_id), "shopify"),
            )
            await conn.execute(
                """insert into platform_connection
                       (id, tenant_id, platform, external_account, display_name,
                        credentials_enc, scopes)
                   values ($1,$2,'shopify',$3,$4,$5,$6)""",
                connection_id, tenant_id, shop, "Dev store", blob, ["read_orders"],
            )
            print("connection created; CREDENTIAL_KEYS must match on the server")
        else:
            print("connection reused")

        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=14)
        connector = ShopifyConnector(shop_domain=shop, access_token=token)
        points, calls, waits = [], 0, 0
        async for page in connector.fetch(window_start=start, window_end=end):
            points.extend(page.points)
            calls += page.api_calls
            waits += page.throttle_waits

        payload = [
            {
                "tenant_id": str(tenant_id),
                "connection_id": str(connection_id),
                "metric_key": p.metric_key,
                "observed_on": p.observed_on.isoformat(),
                "value_numeric": int(p.value_numeric),
                "currency": p.currency,
                "raw": json.dumps(p.raw or {}),
            }
            for p in points
        ]
        written = await conn.fetchval("select upsert_snapshots($1::jsonb)",
                                      json.dumps(payload))
        await conn.execute(
            """insert into sync_run
                   (tenant_id, connection_id, trigger, window_start, window_end,
                    status, rows_written, api_calls, throttle_waits, finished_at)
               values ($1,$2,'manual',$3,$4,'succeeded',$5,$6,$7, now())""",
            tenant_id, connection_id, start, end, written, calls, waits,
        )

        made = {}
        if not await conn.fetchval("select count(*) from api_key"):
            await conn.execute("select set_config('app.tenant_id', '', false)")
            for role in ("admin", "viewer"):
                raw = generate_key()
                made[role] = raw
                await conn.execute(
                    "insert into api_key (tenant_id, key_hash, label, role) "
                    "values ($1,$2,$3,$4)",
                    tenant_id, hash_key(raw), "demo-" + role, role,
                )

        print("points:", len(points), "written:", written)
        for role, raw in made.items():
            print("%s KEY: %s" % (role.upper(), raw))
        if not made:
            print("api keys already existed, none minted")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
