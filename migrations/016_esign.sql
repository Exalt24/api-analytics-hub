-- E-signature slice: a signing request for one document, the signature a client gives it, and a hash chained audit log of everything that happened.
--
-- WHY EACH TABLE IS HERE.
--   esign_request    one document a firm put in a client's portal: the exact bytes, their SHA-256, who is to sign, until when, and the version of the
--                    consent text shown. The identity columns never change after the insert (a trigger), and the status only moves forward from
--                    pending to signed, declined or expired.
--   esign_signature  the signature record. One for each request (a unique key), and append only. It carries the hash of the document at signing
--                    time, the name as typed, the consent version, a hash of the user agent and of the client address (never the raw values),
--                    and the chain link that ties it to the audit log.
--   esign_event      the audit log of a request: created, viewed, consented, signed, expired, declined, verified. Each row holds the hash of the
--                    row before it and its own hash, so an edited, deleted or reordered row shows when the chain is recomputed (app/esign/chain.py).
--                    Append only.
--
-- ROW LEVEL SECURITY is enabled and forced on all three with the same policy as every other tenant table, so a key for one tenant sees none of
-- another tenant's requests, signatures or events, and a request id that belongs to someone else answers exactly like one that does not exist.
-- The request role can insert and read. It can update one column of esign_request (status) and nothing else; it has no UPDATE or DELETE on
-- the other two. The triggers below hold for the table's owner as well.
--
-- Safe to run twice.

create table if not exists esign_request (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    document_name    text        not null check (length(btrim(document_name)) between 1 and 200),
    document         bytea       not null check (octet_length(document) between 1 and 5242880),
    document_sha256  char(64)    not null check (document_sha256 ~ '^[0-9a-f]{64}$'),
    signer_name      text        not null check (length(btrim(signer_name)) between 1 and 200),
    signer_email     text        not null check (length(signer_email) <= 254 and signer_email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),
    consent_version  text        not null check (length(consent_version) between 1 and 60),
    status           text        not null default 'pending' check (status in ('pending', 'signed', 'declined', 'expired')),
    -- the id of the API key that made the request: an opaque uuid, never the key
    created_by       text        not null check (length(created_by) between 1 and 80),
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    unique (id, tenant_id),
    constraint expiry_after_creation check (expires_at > created_at)
);

create index if not exists idx_esign_request_tenant on esign_request (tenant_id, created_at);

create table if not exists esign_signature (
    id                   uuid        primary key default gen_random_uuid(),
    tenant_id            uuid        not null references tenant (id) on delete cascade,
    request_id           uuid        not null,
    document_sha256      char(64)    not null check (document_sha256 ~ '^[0-9a-f]{64}$'),
    -- the name exactly as the signer typed it
    signer_name_typed    text        not null check (length(btrim(signer_name_typed)) between 1 and 200),
    consent_version      text        not null,
    consent_text_sha256  char(64)    not null check (consent_text_sha256 ~ '^[0-9a-f]{64}$'),
    signed_at            timestamptz not null,
    -- keyed hashes (HMAC-SHA-256), so the raw user agent and the raw address are never stored
    user_agent_hash      char(64)    not null check (user_agent_hash ~ '^[0-9a-f]{64}$'),
    client_addr_hash     char(64)    not null check (client_addr_hash ~ '^[0-9a-f]{64}$'),
    -- the hash of the audit row just before the signed row (the consent), and the hash of this record with it
    prev_hash            char(64)    not null check (prev_hash ~ '^[0-9a-f]{64}$'),
    signature_hash       char(64)    not null check (signature_hash ~ '^[0-9a-f]{64}$'),
    -- a second signature for one request is refused by the database as well as by the code
    unique (request_id),
    foreign key (request_id, tenant_id) references esign_request (id, tenant_id) on delete cascade
);

create table if not exists esign_event (
    id          bigserial   primary key,
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    request_id  uuid        not null,
    seq         integer     not null check (seq >= 1),
    kind        text        not null check (kind in ('created', 'viewed', 'consented', 'signed', 'expired', 'declined', 'verified')),
    at          timestamptz not null,
    actor       text        not null check (length(actor) between 1 and 80),
    -- hashes, counts and flags only: never a raw address, user agent or document byte
    detail      jsonb       not null default '{}'::jsonb,
    prev_hash   char(64)    not null check (prev_hash ~ '^[0-9a-f]{64}$'),
    entry_hash  char(64)    not null check (entry_hash ~ '^[0-9a-f]{64}$'),
    unique (request_id, seq),
    foreign key (request_id, tenant_id) references esign_request (id, tenant_id) on delete cascade
);

create index if not exists idx_esign_event_request on esign_event (request_id, seq);

-- APPEND ONLY: a signature or an audit row is never changed, and is deleted only when the whole tenant is (the foreign keys cascade). The request
-- role also has no UPDATE or DELETE grant (below); this trigger holds for the table's owner too.
create or replace function esign_append_only() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' or exists (select 1 from tenant where id = old.tenant_id) then
        raise exception '% rows are append only; a % of % was refused', tg_table_name, lower(tg_op), old.id using errcode = 'restrict_violation';
    end if;
    return old;
end;
$$;

drop trigger if exists esign_signature_append_only on esign_signature;
create trigger esign_signature_append_only before update or delete on esign_signature for each row execute function esign_append_only();
drop trigger if exists esign_event_append_only on esign_event;
create trigger esign_event_append_only before update or delete on esign_event for each row execute function esign_append_only();
drop trigger if exists esign_request_no_delete on esign_request;
create trigger esign_request_no_delete before delete on esign_request for each row execute function esign_append_only();

-- A request is changed in one way only: its status moves forward from pending. The document, its hash, the signer and the times never change.
create or replace function esign_request_frozen() returns trigger language plpgsql as $$
begin
    if (new.id, new.tenant_id, new.document_name, new.document, new.document_sha256, new.signer_name, new.signer_email, new.consent_version,
        new.created_by, new.created_at, new.expires_at)
       is distinct from
       (old.id, old.tenant_id, old.document_name, old.document, old.document_sha256, old.signer_name, old.signer_email, old.consent_version,
        old.created_by, old.created_at, old.expires_at) then
        raise exception 'esign_request % is frozen; only its status may move forward', old.id using errcode = 'restrict_violation';
    end if;
    if new.status <> old.status and old.status <> 'pending' then
        raise exception 'esign_request % is already %, so it cannot become %', old.id, old.status, new.status using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists esign_request_frozen on esign_request;
create trigger esign_request_frozen before update on esign_request for each row execute function esign_request_frozen();

-- ---------------------------------------------------------------- row level security, enabled and forced

alter table esign_request   enable row level security;
alter table esign_signature enable row level security;
alter table esign_event     enable row level security;

alter table esign_request   force row level security;
alter table esign_signature force row level security;
alter table esign_event     force row level security;

drop policy if exists esign_request_by_tenant on esign_request;
create policy esign_request_by_tenant on esign_request
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists esign_signature_by_tenant on esign_signature;
create policy esign_signature_by_tenant on esign_signature
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists esign_event_by_tenant on esign_event;
create policy esign_event_by_tenant on esign_event
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- ---------------------------------------------------------------- grants

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new tables owe their own.
grant select, insert on esign_request, esign_signature, esign_event to app_request;
grant update (status) on esign_request to app_request;
grant usage, select on all sequences in schema public to app_request;
