#!/usr/bin/env python
"""Load the LIVE Shopify figures into the local Postgres, and mint an API key.

This is the end-to-end wiring proof: real store to connector to database to API to
dashboard. It writes only to the LOCAL docker Postgres on 5433, never to Shopify.

Prints an admin and a viewer key so the RBAC split can be seen in the browser
rather than only in a test.
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

DSN = os.environ.get(
    "ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics"
)
ROOT = Path(__file__).resolve().parent
SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")


def shopify_creds() -> tuple[str, str]:
    text = io.open(SECRETS, encoding="utf-8").read()
    return (
        re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip(),
        re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip(),
    )


async def main() -> int:
    shop, token = shopify_creds()

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql"):
            await conn.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))

        tenant_id = uuid.uuid4()
        await conn.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
        await conn.execute(
            "insert into tenant (id, slug, name) values ($1,$2,$3)",
            tenant_id, "demo", "Demo Agency Client",
        )

        # Encrypt the real token with a real key, so the stored row is what the
        # production path would hold rather than a placeholder.
        os.environ.setdefault("CREDENTIAL_KEYS", "1:" + gen_crypto_key())
        cipher = CredentialCipher()
        connection_id = uuid.uuid4()
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

        # Pull the real window through the connector.
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=14)
        connector = ShopifyConnector(shop_domain=shop, access_token=token)
        points = []
        calls = waits = 0
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

        run_id = await conn.fetchval(
            """insert into sync_run
                   (tenant_id, connection_id, trigger, window_start, window_end,
                    status, rows_written, api_calls, throttle_waits, finished_at)
               values ($1,$2,'manual',$3,$4,'succeeded',$5,$6,$7, now())
               returning id""",
            tenant_id, connection_id, start, end, written, calls, waits,
        )

        keys = {"admin": generate_key(), "viewer": generate_key()}
        await conn.execute("select set_config('app.tenant_id', '', false)")
        for role, raw in (("admin", keys["admin"]), ("viewer", keys["viewer"])):
            await conn.execute(
                "insert into api_key (tenant_id, key_hash, label, role) "
                "values ($1,$2,$3,$4)",
                tenant_id, hash_key(raw), "demo-" + role, role,
            )

        print("tenant     :", tenant_id)
        print("connection :", connection_id)
        print("points     :", len(points), "written:", written)
        print("run        :", run_id)
        print("api_calls  :", calls, " throttle_waits:", waits)
        print()
        print("ADMIN KEY  :", keys["admin"])
        print("VIEWER KEY :", keys["viewer"])
        return 0 if written else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
