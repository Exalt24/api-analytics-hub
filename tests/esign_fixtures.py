"""Shared setup for the e-signature tests: a fresh schema with 001, 002, 003, 011 and 016, two tenants, a request-role Database, and the superuser's tool for tampering.

Like the other database suites this DROPS and recreates the `public` schema of the database it points at (ANALYTICS_TEST_DSN, default the compose Postgres on 5433), so aim it
at a scratch database. `db` connects as the request role through `Database`, which refuses to start as a superuser. TAMPERING is done as the superuser with triggers and
foreign keys switched off for the session (`session_replication_role = replica`), which is what someone with raw access to the storage could do; the application role
cannot.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price_watch_fixtures import DSN, add_tenant, needs_db, psycopg, reachable, reset_schema, run  # noqa: E402,F401

# CI sets ESIGN_REQUIRE_DB so that a Postgres that did not start FAILS the collection of the database suites instead of skipping all of them green.
if os.environ.get("ESIGN_REQUIRE_DB") == "1" and not reachable():
    raise RuntimeError("ESIGN_REQUIRE_DB is set but Postgres is not reachable at %s" % DSN)

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.db import Database  # noqa: E402
from app.esign import service  # noqa: E402

#: a test key for the keyed hashes; not a credential
PII_KEY = b"esign-test-key-not-a-secret"
T0 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
RAW_AGENT = "Mozilla/5.0 (EsignTest) RawAgentMarker/7.7"
RAW_ADDR = "203.0.113.77"
DOCUMENT = b"%PDF-1.4\nEngagement letter for Dana Whitfield, DOCMARKER-4471.\n%%EOF\n"


def reset_esign_schema() -> None:
    reset_schema(("003_api_keys.sql", "016_esign.sql"))


def with_db(fn, *, size: int = 6):
    """Run `await fn(db)` with a connected request-role Database, closing it after."""
    async def go():
        db = Database(DSN, max_size=size)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()
    return run(go())


def new_tenants(*slugs: str) -> list[str]:
    return [str(add_tenant(s)) for s in slugs]


_admin_connection = None


def admin_rows(query: str, params=()) -> list[tuple]:
    """A read as the superuser, which row level security does not apply to: the way to look at what is really stored.

    One connection is kept for the whole run: a new one for each read (the simulation makes thousands) used up Windows' ephemeral ports (WinError 10048, 'Address already in use')."""
    global _admin_connection
    if _admin_connection is None or _admin_connection.closed:
        _admin_connection = psycopg.connect(DSN, autocommit=True)
    with _admin_connection.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else []


def tamper(*statements) -> None:
    """Run SQL as the superuser with triggers and foreign keys off for the session: a change made behind the application's back.

    Each statement is a text, or a pair of text and parameters."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("set session_replication_role = replica")
        for stmt in statements:
            text, params = stmt if isinstance(stmt, tuple) else (stmt, ())
            cur.execute(text, params)


def add_key(tenant: str, role: str) -> str:
    key = generate_key()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", (tenant, hash_key(key), "%s-%s" % (role, uuid.uuid4().hex[:6]), role))
    return key


async def make_request(db, tenant: str, *, document: bytes = DOCUMENT, name: str = "Engagement letter.pdf", signer: str = "Dana Whitfield", email: str = "dana@example.com",
                       hours: float = 72, now: datetime = T0, actor: str = "key-firm-1") -> dict:
    return await service.create_request(db, tenant, actor=actor, document=document, document_name=name, signer_name=signer, signer_email=email,
                                        expires_at=now + timedelta(hours=hours), now=now)


async def sign_as(db, tenant: str, view: dict, *, now: datetime = T0 + timedelta(hours=1), **over):
    """Sign `view` the way the portal does: the name as named, the box ticked, the version and hash from the page. Any argument can be overridden."""
    args = dict(actor="key-client-1", typed_name=view["signer_name"], consent=True, consent_version=view["consent_version"], document_sha256=view["document_sha256"],
                user_agent=RAW_AGENT, client_addr=RAW_ADDR, pii_key=PII_KEY)
    args.update(over)
    return await service.sign(db, tenant, view["id"], now=now, **args)


def counts(request_id: str) -> dict[str, int]:
    return {"events": admin_rows("select count(*) from esign_event where request_id = %s", (request_id,))[0][0],
            "signatures": admin_rows("select count(*) from esign_signature where request_id = %s", (request_id,))[0][0]}
