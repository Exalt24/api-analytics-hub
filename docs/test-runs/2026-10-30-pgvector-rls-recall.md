# pgvector 0.8.7 on Postgres 17.11: row level security with an HNSW index, measured 2026-10-30

Setup: pgvector/pgvector:0.8.7-pg17, table items(id, tenant_id, embedding vector(8)), 20000 rows, 20 tenants of 1000 rows each, HNSW vector_cosine_ops m=16 ef_construction=64, FORCE ROW LEVEL SECURITY with policy tenant_id = current_setting('app.tenant')::int, role app (NOSUPERUSER), tenant 3, 30 query vectors, LIMIT 20, recall measured against the exact answer with enable_indexscan = off.

| Setting | Avg rows returned (of 20) | Min rows | Avg recall | Queries under 20 |
|---|---|---|---|---|
| default (iterative_scan off, ef_search 40) | 1.87 | 0 | 0.093 | 30 of 30 |
| hnsw.iterative_scan = relaxed_order | 20.00 | 20 | 1.000 | 0 of 30 |
| hnsw.ef_search = 200 | 10.13 | 5 | 0.507 | 30 of 30 |
| superuser, default, no tenant filter | 20.00 | 20 | 1.000 | 0 of 30 |
| app, strict_order | 20.00 | 20 | 1.000 | 0 of 30 |
| app, relaxed_order and ef_search 200 | 20.00 | 20 | 1.000 | 0 of 30 |
| superuser with explicit WHERE tenant_id = 3, default | 1.87 | 0 | 0.093 | 30 of 30 |

EXPLAIN under RLS, default settings: "Index Scan using items_embedding_idx on items ... Order By: (embedding <=> ...) Filter: (tenant_id = (current_setting('app.tenant'::text))::integer)".

Notes from the run: `SET hnsw.iterative_scan` fails with "unrecognized configuration parameter" until a vector function has loaded the library in the session (a `SELECT '[1]'::vector(1)` fixes it). Port 55432 was taken by an unrelated container, so the experiment ran without a published port through docker exec.

Limits: one synthetic dataset (uniform random 8 dimensions), one tenant, 30 queries, no latency measured, tenants of 5 percent of rows each. A much smaller tenant could hit hnsw.max_scan_tuples (default 20,000).
