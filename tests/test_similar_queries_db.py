"""Search query vectors against real Postgres with pgvector, as the request role.

Three things: the embedding is stable and puts similar spellings close; a tenant only ever sees its own queries; and the
HNSW index plus row level security under-returns unless hnsw.iterative_scan is on. That last one was measured on
2026-10-30 (pgvector 0.8.7): `limit 20` for a tenant owning a tenth of the rows came back with a handful of rows, or
none. `similar(..., iterative=False)` reproduces it here, and `similar(...)` must not.

Skips cleanly when Postgres is not running or has no pgvector (the compose file and CI use the pgvector image). Like the
other database suites it drops and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import similar_queries as sq  # noqa: E402

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")


def _has_pgvector() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            return conn.execute("select count(*) from pg_available_extensions where name = 'vector'").fetchone()[0] == 1
    except Exception:
        return False


# ---- the embedding needs no database ------------------------------------------------------------------------------

def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_the_embedding_is_deterministic_unit_length_and_64_wide():
    v = sq.embed("Red running shoes")
    assert v == sq.embed("red   running shoes ")  # case and spacing do not change it
    assert len(v) == sq.DIMENSIONS == 64
    assert abs(cosine(v, v) - 1.0) < 1e-9


def test_similar_spellings_are_closer_than_unrelated_queries():
    base = sq.embed("red running shoes")
    near = cosine(base, sq.embed("running shoes red"))
    near2 = cosine(base, sq.embed("red running shoe"))
    far = cosine(base, sq.embed("wedding cake delivery"))
    assert near > far + 0.3 and near2 > far + 0.3


def test_an_empty_query_has_no_embedding():
    with pytest.raises(ValueError):
        sq.embed("   ")


# ---- the database side --------------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(not _has_pgvector(), reason="Postgres with pgvector not reachable, run docker compose up -d")

if _has_pgvector():
    from app.db import Database  # noqa: E402


def run(coro):
    return asyncio.run(coro)


TENANTS = [uuid.uuid4() for _ in range(10)]   # each owns a tenth of the rows, like the measured case
ROWS_PER_TENANT = 600


@pytest.fixture(scope="module")
def world():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "010_query_embeddings.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        for i, t in enumerate(TENANTS):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(t),))
            cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (t, f"t{i}", f"t{i}"))
        cur.execute("select setseed(0.42)")
        # random 64 wide unit-ish vectors, ids as text; written by the owner role, which bypasses nothing it should not (FORCE)
        for t in TENANTS:
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(t),))
            cur.execute(
                "insert into query_embedding (tenant_id, query_text, embedding) "
                "select %s, 'q' || g, (select array_agg(random() - 0.5)::real[]::vector from generate_series(1, 64) s where g is not null) "
                "from generate_series(1, %s) g", (t, ROWS_PER_TENANT))
        cur.execute("analyze query_embedding")
    return TENANTS


async def _lookup(tenant, text, limit, iterative):
    db = Database(DSN, max_size=2)
    await db.connect()
    try:
        async with db.tenant(str(tenant)) as conn:
            return await sq.similar(conn, text, limit, iterative=iterative)
    finally:
        await db.close()


@pytestmark_db
def test_a_tenant_gets_a_full_page_back_under_row_level_security(world):
    for text in ("red running shoes", "wedding cake delivery", "cheap flights to cebu", "q17", "q420"):
        got = run(_lookup(world[3], text, 20, True))
        assert len(got) == 20, (text, len(got))
        sims = [g["similarity"] for g in got]
        assert sims == sorted(sims, reverse=True)


@pytest.mark.skipif(not _has_pgvector(), reason="needs pgvector")
def test_without_iterative_scan_the_same_lookup_comes_back_short(world):
    short = [len(run(_lookup(world[3], t, 20, False))) for t in ("red running shoes", "wedding cake delivery", "cheap flights to cebu", "q17", "q420")]
    # the failure the setting exists for: with a tenth of the rows per tenant and ef_search 40, about 4 survive the policy
    assert max(short) < 20, short


@pytest.mark.skipif(not _has_pgvector(), reason="needs pgvector")
def test_a_tenant_only_sees_its_own_queries(world):
    async def go():
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(world[0])) as conn:
                await sq.store(conn, str(world[0]), ["red running shoes", "running shoes red"])
            async with db.tenant(str(world[1])) as conn:
                await sq.store(conn, str(world[1]), ["red running shoes for kids"])
            async with db.tenant(str(world[0])) as conn:
                a = await sq.similar(conn, "red running shoes", 3)
            async with db.tenant(str(world[1])) as conn:
                b = await sq.similar(conn, "red running shoes", 3)
            async with db.tenant(str(world[0])) as conn:
                hit = await conn.fetchval("select count(*) from query_embedding where query_text = 'red running shoes for kids'")
            return a, b, hit
        finally:
            await db.close()

    a, b, hit = run(go())
    assert a[0]["query"] == "red running shoes" and abs(a[0]["similarity"] - 1.0) < 1e-6
    assert "red running shoes for kids" not in [x["query"] for x in a]
    assert "running shoes red" not in [x["query"] for x in b]
    assert hit == 0
