"""Search queries as vectors, and "queries like this one" for one tenant.

The table is migrations/010_query_embeddings.sql. `embed` turns a query into 64 numbers from hashed character
trigrams: no model, the same vector for the same text on every machine, and similar spellings land close together
(it finds "red running shoes" near "running shoes red", not near "wedding cake"). It is a lab embedding, good for
this lookup and for showing the database side honestly; a real model would replace `embed` and the column width.

`similar` is the part that matters for correctness. It runs inside `Database.tenant(...)`, so row level security
filters the rows, and it turns on `hnsw.iterative_scan = strict_order` for the lookup. Without it the HNSW index hands
back about 40 candidates from every tenant and the policy discards most of them, so a tenant asking for 20 gets a
handful or none, silently (measured in the migration's header comment). `iterative=False` exists so the test can show
that failure; nothing else should pass it.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import asyncpg

DIMENSIONS = 64
MAX_QUERY_LENGTH = 300


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def embed(text: str) -> list[float]:
    """64 numbers, unit length. Each character trigram adds +1 or -1 to one slot picked by its hash."""
    t = _normalise(text)
    if not t:
        raise ValueError("an empty query has no embedding")
    padded = f"  {t} "
    vec = [0.0] * DIMENSIONS
    for i in range(len(padded) - 2):
        digest = hashlib.blake2b(padded[i:i + 3].encode("utf-8"), digest_size=8).digest()
        n = int.from_bytes(digest, "big")
        vec[n % DIMENSIONS] += 1.0 if (n >> 32) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:  # every slot cancelled out; vanishingly rare, and a zero vector cannot be compared by cosine
        vec[0], norm = 1.0, 1.0
    return [x / norm for x in vec]


def literal(vec: list[float]) -> str:
    """pgvector's text form. Sent as text and cast in SQL, because asyncpg has no codec for the vector type."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def store(conn: asyncpg.Connection, tenant_id: str, queries: list[str]) -> int:
    """Insert or refresh the vectors for these queries; returns how many distinct queries were written."""
    seen: dict[str, str] = {}
    for q in queries:
        norm = _normalise(q)
        if norm and len(norm) <= MAX_QUERY_LENGTH:
            seen[norm] = literal(embed(norm))
    for text, vec in seen.items():
        await conn.execute(
            "insert into query_embedding (tenant_id, query_text, embedding) values ($1::uuid, $2, $3::text::vector) "
            "on conflict (tenant_id, query_text) do update set embedding = excluded.embedding",
            str(tenant_id), text, vec)
    return len(seen)


async def similar(conn: asyncpg.Connection, text: str, limit: int = 10, *, iterative: bool = True) -> list[dict[str, Any]]:
    """The tenant's own stored queries nearest to `text`, nearest first, with cosine similarity (1.0 is identical).

    `conn` must come from Database.tenant(...), which opens a transaction with app.tenant_id set. The setting is
    transaction-local (`set_config(..., true)`), so it cannot leak to the next borrower of the pooled connection.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if iterative:
        # the cast loads the pgvector library first: hnsw.* settings do not exist until it is loaded in the session
        await conn.execute("select '[1]'::text::vector(1)")
        await conn.execute("select set_config('hnsw.iterative_scan', 'strict_order', true)")
    rows = await conn.fetch(
        "select query_text, 1 - (embedding <=> $1::text::vector) as similarity "
        "from query_embedding order by embedding <=> $1::text::vector limit $2",
        literal(embed(text)), limit)
    return [{"query": r["query_text"], "similarity": float(r["similarity"])} for r in rows]
