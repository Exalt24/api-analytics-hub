-- One row for each monthly report a tenant has asked for, so asking again for the same month answers with the same deck.
--
-- WHY A TABLE. A scheduler that runs on the first of the month will, sooner or later, run twice: a retry after a
-- timeout, a person pressing Execute again, a second n8n instance. If each call built a new deck and a new message,
-- a client would get two reports with two different "created" times and possibly different figures, since the
-- stored rows may have moved between the calls. The caller sends an idempotency key (for example
-- "monthly-report:<tenant name>:2026-10"); the first call with a key builds the deck and keeps it here, and every later
-- call with that key gets the stored deck back and is told whether it was already delivered. `unique (tenant_id,
-- idempotency_key)` is what makes two calls at the same moment safe: one inserts the row, the other sees it.
--
-- A run is `running` while its deck is being built, `done` with the deck and its hash, or `failed` with the reason. A
-- failed run, and a `running` one nobody has finished for a quarter of an hour, may be taken over by a later call
-- with the same key. `delivered_at` is set by a call that says the message really went out, and is what stops a second
-- send; it is never set by building the deck.
--
-- Row level security is on and forced, like every tenant table. Nothing here can be deleted by the request role.
--
-- Safe to run twice.

create table if not exists report_run (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    idempotency_key  text        not null check (idempotency_key ~ '^[A-Za-z0-9:_.@-]{8,128}$'),
    month            text        not null check (month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    status           text        not null check (status in ('running', 'done', 'failed')),
    deck             bytea,
    deck_sha256      char(64),
    slides           integer,
    sources          text[],
    narrative        text[],
    insights_source  text,
    error            text,
    created_at       timestamptz not null default now(),
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    delivered_at     timestamptz,
    unique (tenant_id, idempotency_key),
    -- a finished deck has its hash and its size, a failed one its reason, and nothing is delivered that was not built
    constraint done_has_a_deck check (status <> 'done' or (deck is not null and deck_sha256 is not null and slides is not null)),
    constraint failed_has_a_reason check (status <> 'failed' or error is not null),
    constraint delivered_only_when_done check (delivered_at is null or status = 'done')
);

create index if not exists idx_report_run_month on report_run (tenant_id, month, finished_at desc);

alter table report_run enable row level security;
alter table report_run force row level security;

drop policy if exists report_run_by_tenant on report_run;
create policy report_run_by_tenant on report_run
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new table owes its own.
grant select, insert, update on report_run to app_request;
