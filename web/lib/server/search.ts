/**
 * "Queries like this one" for one tenant, through Kysely: the TypeScript side of
 * app/similar_queries.py and migrations/010_query_embeddings.sql.
 *
 * `embed` is a port of the Python function and must give the same vector for the same
 * text (tests/fixtures/embed_golden.json is written by the Python one and checked here).
 * `similarQueries` runs inside `withTenant`, so row level security does the filtering,
 * and it turns on `hnsw.iterative_scan = strict_order` for the lookup for the reason the
 * migration's header gives: without it the HNSW index hands back about 40 candidates
 * from every tenant and the policy discards most of them.
 */
import { blake2b } from "@noble/hashes/blake2.js";
import { sql } from "kysely";
import type { TenantTx } from "@/lib/server/db";

export const DIMENSIONS = 64;
export const MAX_QUERY_LENGTH = 300;

const encoder = new TextEncoder();

export function normalise(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, " ");
}

/** 64 numbers, unit length. Each character trigram adds +1 or -1 to one slot picked by its hash. */
export function embed(text: string): number[] {
  const t = normalise(text);
  if (!t) throw new Error("an empty query has no embedding");
  const chars = Array.from(`  ${t} `); // code points, like Python's str
  const vec: number[] = new Array<number>(DIMENSIONS).fill(0);
  for (let i = 0; i + 2 < chars.length; i += 1) {
    const trigram = chars.slice(i, i + 3).join("");
    const digest = blake2b(encoder.encode(trigram), { dkLen: 8 });
    let n = 0n;
    for (const byte of digest) n = (n << 8n) | BigInt(byte);
    const slot = Number(n % BigInt(DIMENSIONS));
    vec[slot] = (vec[slot] ?? 0) + (((n >> 32n) & 1n) === 1n ? 1 : -1);
  }
  let norm = Math.sqrt(vec.reduce((sum, x) => sum + x * x, 0));
  if (norm === 0) {
    vec[0] = 1;
    norm = 1;
  }
  return vec.map((x) => x / norm);
}

/** pgvector's text form, cast in SQL because the driver has no codec for the vector type. */
export function literal(vec: number[]): string {
  return `[${vec.map((x) => x.toFixed(7)).join(",")}]`;
}

/** Insert or refresh the vectors for these queries in the tenant of `trx`; returns how many distinct queries were written. */
export async function storeQueries(trx: TenantTx, tenantId: string, queries: string[]): Promise<number> {
  const seen = new Map<string, string>();
  for (const q of queries) {
    const norm = normalise(q);
    if (norm && norm.length <= MAX_QUERY_LENGTH) seen.set(norm, literal(embed(norm)));
  }
  for (const [text, vec] of seen) {
    await trx
      .insertInto("query_embedding")
      .values({ tenant_id: tenantId, query_text: text, embedding: sql<string>`${vec}::text::vector` })
      .onConflict((oc) => oc.columns(["tenant_id", "query_text"]).doUpdateSet({ embedding: sql<string>`excluded.embedding` }))
      .execute();
  }
  return seen.size;
}

export type SimilarQuery = { query: string; similarity: number };

/** The tenant's own stored queries nearest to `text`, nearest first, with cosine similarity (1.0 is identical). */
export async function similarQueries(trx: TenantTx, text: string, limit = 10): Promise<SimilarQuery[]> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error("limit must be between 1 and 200");
  // the cast loads the pgvector library first: hnsw.* settings do not exist until it is loaded in the session
  await sql`select '[1]'::text::vector(1)`.execute(trx);
  await sql`select set_config('hnsw.iterative_scan', 'strict_order', true)`.execute(trx);
  const lit = literal(embed(text));
  const rows = await trx
    .selectFrom("query_embedding")
    .select(["query_text", sql<number>`1 - (embedding <=> ${lit}::text::vector)`.as("similarity")])
    .orderBy(sql`embedding <=> ${lit}::text::vector`)
    .limit(limit)
    .execute();
  return rows.map((r) => ({ query: r.query_text, similarity: Number(r.similarity) }));
}
