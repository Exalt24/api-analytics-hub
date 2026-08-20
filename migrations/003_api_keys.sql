-- API keys and roles.
--
-- Keys are stored as SHA-256 hex, never in the clear, so a database dump does not
-- hand over working credentials. No salt: the key is 32 bytes of urandom, so there
-- is nothing to precompute, and a deterministic hash keeps the lookup an index hit.
--
-- ROLE LIVES ON THE KEY, not on a user, because there is no user model here yet and
-- inventing one to hold a single column would be the wrong kind of thorough. When a
-- user model arrives the key points at it and this column moves; the permission map
-- in app/api/auth.py does not change.

create table api_key (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    -- 64 hex characters. Fixed length so a truncated write cannot masquerade as a
    -- valid hash of something shorter.
    key_hash    char(64)    not null unique,
    label       text        not null,
    role        text        not null
                check (role in ('viewer', 'operator', 'admin')),
    created_at  timestamptz not null default now(),
    last_used_at timestamptz,
    -- Revocation is a timestamp rather than a delete, so an incident review can
    -- still see the key existed and when it stopped working.
    revoked_at  timestamptz
);

create index idx_api_key_tenant on api_key (tenant_id) where revoked_at is null;

-- NO ROW LEVEL SECURITY ON THIS TABLE, and that is deliberate rather than an
-- oversight. Authentication has to read the key row BEFORE it knows which tenant
-- the caller is, so a tenant-scoped policy here would be a chicken-and-egg lock:
-- the policy needs app.tenant_id, which is only known after this lookup succeeds.
--
-- What makes that safe is that the lookup is BY HASH of a 256-bit secret. Without
-- the key there is nothing to look up, and the row exposes no other tenant's data.
-- The tenant boundary is still enforced everywhere it matters, on every table this
-- lookup then leads to.
--
-- Recorded explicitly because the invariant test asserts every tenant-scoped table
-- has RLS, and an unexplained exemption in that list is indistinguishable from the
-- hole it is designed to catch.
comment on table api_key is
  'Auth lookup table. Intentionally not RLS-scoped: the lookup happens before a '
  'tenant is known, and it is keyed by the hash of a 256-bit secret.';

-- 001 granted app_request access to every table that existed AT THAT MOMENT, and
-- a grant is not retroactive, so this table was unreachable by the application
-- role until now. Measured: the API returned "permission denied for table api_key"
-- on every authenticated request while every policy and every role check was
-- correct. Any future migration that adds a table owes it the same two lines.
grant select, insert, update, delete on api_key to app_request;
grant usage, select on all sequences in schema public to app_request;
