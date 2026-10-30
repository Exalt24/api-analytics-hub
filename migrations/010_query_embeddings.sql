-- Search queries stored as vectors, so "queries like this one" is a nearest neighbour lookup inside one tenant.
--
-- WHY THE QUERY CODE SETS hnsw.iterative_scan. An HNSW index returns about hnsw.ef_search (40) nearest rows from
-- ALL tenants, and row level security then throws away the ones that belong to someone else. On 20000 random vectors
-- spread over 20 tenants, `limit 20` for one tenant returned 1.87 rows on average and sometimes none, with no error
-- (pgvector 0.8.7, Postgres 17.11, measured 2026-10-30; the run is in docs/test-runs/). `hnsw.iterative_scan =
-- strict_order` (exact distance order; relaxed_order measured the same but pgvector says it may return rows slightly out of
-- order) keeps scanning the index until the filter has produced enough rows and returned all 20 every time;
-- raising ef_search to 200 only got to about 10. app/similar_queries.py sets it for every lookup and
-- tests/test_similar_queries_db.py fails if it does not.
--
-- The column is 64 wide because app/similar_queries.py builds it from hashed character trigrams, which needs no model
-- and gives the same vector for the same text on every machine. A real embedding model would change the width here,
-- in embed(), and nowhere else.
--
-- The operator class is vector_cosine_ops, so the index serves `<=>` (cosine distance) and only that. Row level
-- security is on and forced, like every tenant table.
--
-- Needs the pgvector extension (the compose file and CI use the pgvector/pgvector image). Safe to run twice.

create extension if not exists vector;

create table if not exists query_embedding (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    query_text  text        not null check (length(query_text) between 1 and 300),
    embedding   vector(64)  not null,
    created_at  timestamptz not null default now(),
    unique (tenant_id, query_text)
);

create index if not exists idx_query_embedding_hnsw
    on query_embedding using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);

alter table query_embedding enable row level security;
alter table query_embedding force row level security;

drop policy if exists query_embedding_by_tenant on query_embedding;
create policy query_embedding_by_tenant on query_embedding
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- a grant is not retroactive (see 003), so the new table owes its own
grant select, insert, update on query_embedding to app_request;
