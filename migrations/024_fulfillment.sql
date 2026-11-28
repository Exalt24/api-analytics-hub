-- Partner fulfilment: an order sent to a fulfilment partner (a pharmacy that packs and ships), its state machine, the send with its dead letter, the partner's status events,
-- and a hash chained audit log. Needs 022 (the access matrix).
--
-- WHAT IS NOT HERE ON PURPOSE. No name, address, date of birth, diagnosis, drug or any other personal or health detail. An order is an OPAQUE REFERENCE (`ord_` and 24 hex digits, a check
-- constraint) and the clinic it belongs to. The partner is given the reference and matches it to the prescription through the clinic's own secure channel, which is outside this system.
-- The only free text any table below can hold is a carrier name and a tracking number (both with a shape check), a short error code, and the audit detail, which holds hashes, codes and counts.
--
-- WHY EACH TABLE IS HERE.
--   fulfillment_order        the order: reference, clinic, status, and (once shipped) carrier and tracking number. Status moves along fulfillment_transition only (a trigger), and the
--                            access matrix decides who moves it (`update`). The identity columns never change.
--   fulfillment_transition   the legal moves, as rows: created to submitted or cancelled; submitted to accepted, rejected or cancelled; accepted to packed or cancelled; packed to shipped; shipped
--                            to delivered. rejected, cancelled and delivered end the order.
--   fulfillment_submission   the sending of one order: the Idempotency-Key it is sent with (made once, so every retry carries the same one), the attempts so far, sent or dead.
--   fulfillment_dead_letter  an order that could not be sent: why (a code), the HTTP status, the attempts. Requeued by a person; the row stays as the record.
--   fulfillment_event        the partner's events that were applied or ignored, one row for each partner event id: the second delivery of an id meets this and changes nothing.
--   fulfillment_audit        every step, per order, append only and hash chained (app/fulfillment/audit.py): an edited, deleted or reordered row shows when the chain is recomputed.
--
-- Row level security is enabled and forced on all of them. A role that may read orders reads their rows, a role that may create or update orders writes the children, and a partner
-- event is routed to its tenant through the narrow fulfillment_router role (it reads three columns of the order table and nothing else).
-- Safe to run twice.

create table if not exists fulfillment_order (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    clinic_id        uuid        not null,
    order_ref        text        not null unique check (order_ref ~ '^ord_[0-9a-f]{24}$'),
    status           text        not null default 'created' check (status in ('created', 'submitted', 'accepted', 'packed', 'shipped', 'delivered', 'rejected', 'cancelled')),
    carrier          text        check (carrier ~ '^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$'),
    tracking_number  text        check (tracking_number ~ '^[A-Za-z0-9-]{6,40}$'),
    partner_order_id text        check (partner_order_id ~ '^[A-Za-z0-9_-]{1,80}$'),
    -- the portal user who made it: an opaque uuid
    created_by       text        not null check (length(created_by) between 1 and 80),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (id, tenant_id),
    unique (id, clinic_id),
    foreign key (clinic_id, tenant_id) references clinic (id, tenant_id),
    -- a shipped or delivered order has a carrier and a tracking number, and no other order has either
    constraint shipped_has_carrier_and_tracking check ((status in ('shipped', 'delivered')) = (carrier is not null and tracking_number is not null))
);
create index if not exists idx_fulfillment_order_clinic on fulfillment_order (tenant_id, clinic_id, created_at);

create table if not exists fulfillment_transition (
    from_status text not null,
    to_status   text not null,
    primary key (from_status, to_status)
);
insert into fulfillment_transition (from_status, to_status) values
    ('created', 'submitted'), ('created', 'cancelled'),
    ('submitted', 'accepted'), ('submitted', 'rejected'), ('submitted', 'cancelled'),
    ('accepted', 'packed'), ('accepted', 'cancelled'),
    ('packed', 'shipped'),
    ('shipped', 'delivered')
on conflict do nothing;

create or replace function fulfillment_order_guard() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'an order is never deleted' using errcode = 'restrict_violation';
    end if;
    if (new.id, new.tenant_id, new.clinic_id, new.order_ref, new.created_by, new.created_at)
       is distinct from (old.id, old.tenant_id, old.clinic_id, old.order_ref, old.created_by, old.created_at) then
        raise exception 'order % is frozen except for its status, carrier, tracking number and partner id', old.id using errcode = 'restrict_violation';
    end if;
    if new.status <> old.status then
        if not exists (select 1 from fulfillment_transition t where t.from_status = old.status and t.to_status = new.status) then
            raise exception 'order % cannot move from % to %', old.id, old.status, new.status using errcode = 'restrict_violation';
        end if;
        if not portal_allows('orders', 'update', new.clinic_id) then
            raise exception 'the asking role may not update order %', old.id using errcode = 'insufficient_privilege';
        end if;
    elsif (new.carrier, new.tracking_number, new.partner_order_id) is distinct from (old.carrier, old.tracking_number, old.partner_order_id) then
        if not portal_allows('orders', 'update', new.clinic_id) then
            raise exception 'the asking role may not update order %', old.id using errcode = 'insufficient_privilege';
        end if;
    end if;
    return new;
end;
$$;
drop trigger if exists fulfillment_order_guard on fulfillment_order;
create trigger fulfillment_order_guard before update or delete on fulfillment_order for each row execute function fulfillment_order_guard();

create table if not exists fulfillment_submission (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    order_id         uuid        not null unique,
    clinic_id        uuid        not null,
    -- made once, before the first attempt, and sent as the Idempotency-Key on every attempt, so a retry can never create a second order at the partner
    idempotency_key  text        not null check (length(idempotency_key) between 8 and 120),
    state            text        not null default 'pending' check (state in ('pending', 'sent', 'dead')),
    attempts         integer     not null default 0 check (attempts >= 0),
    last_error_code  text        check (last_error_code ~ '^[a-z0-9_]{1,40}$'),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    foreign key (order_id, tenant_id) references fulfillment_order (id, tenant_id) on delete cascade,
    foreign key (order_id, clinic_id) references fulfillment_order (id, clinic_id) on delete cascade
);

create table if not exists fulfillment_dead_letter (
    id             uuid        primary key default gen_random_uuid(),
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    order_id       uuid        not null,
    clinic_id      uuid        not null,
    reason_code    text        not null check (reason_code in ('permanent_rejection', 'retries_exhausted', 'unreachable')),
    http_status    integer     check (http_status between 100 and 599),
    attempts       integer     not null check (attempts >= 1),
    created_at     timestamptz not null default now(),
    requeued_at    timestamptz,
    foreign key (order_id, tenant_id) references fulfillment_order (id, tenant_id) on delete cascade,
    foreign key (order_id, clinic_id) references fulfillment_order (id, clinic_id) on delete cascade
);
create index if not exists idx_fulfillment_dead_letter_open on fulfillment_dead_letter (tenant_id, created_at) where requeued_at is null;

create table if not exists fulfillment_event (
    id                bigserial   primary key,
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    order_id          uuid        not null,
    clinic_id         uuid        not null,
    partner_event_id  text        not null check (partner_event_id ~ '^[A-Za-z0-9_.:-]{1,120}$'),
    -- the status the event carried, and what was done with it
    event_status      text        not null check (event_status in ('submitted', 'accepted', 'packed', 'shipped', 'delivered', 'rejected', 'cancelled')),
    result            text        not null check (result in ('applied', 'ignored')),
    received_at       timestamptz not null default now(),
    unique (tenant_id, partner_event_id),
    foreign key (order_id, tenant_id) references fulfillment_order (id, tenant_id) on delete cascade,
    foreign key (order_id, clinic_id) references fulfillment_order (id, clinic_id) on delete cascade
);

create table if not exists fulfillment_audit (
    id           bigserial   primary key,
    tenant_id    uuid        not null references tenant (id) on delete cascade,
    order_id     uuid        not null,
    clinic_id    uuid        not null,
    seq          integer     not null check (seq >= 1),
    kind         text        not null check (kind in ('created', 'submit_attempt', 'submitted', 'dead_lettered', 'requeued', 'transition', 'ignored', 'refused', 'verified')),
    from_status  text,
    to_status    text,
    event_id     text        check (event_id ~ '^[A-Za-z0-9_.:-]{1,120}$'),
    actor        text        not null check (length(actor) between 1 and 80),
    at           timestamptz not null,
    -- codes, counts and the whitelisted partner fields only: never a name, an address or a health detail
    detail       jsonb       not null default '{}'::jsonb check (jsonb_typeof(detail) = 'object' and octet_length(detail::text) <= 2000),
    prev_hash    char(64)    not null check (prev_hash ~ '^[0-9a-f]{64}$'),
    entry_hash   char(64)    not null check (entry_hash ~ '^[0-9a-f]{64}$'),
    unique (order_id, seq),
    foreign key (order_id, tenant_id) references fulfillment_order (id, tenant_id) on delete cascade,
    foreign key (order_id, clinic_id) references fulfillment_order (id, clinic_id) on delete cascade
);
create index if not exists idx_fulfillment_audit_order on fulfillment_audit (order_id, seq);

-- the audit log and the event log are append only: never changed, deleted only when the whole tenant is (the foreign keys cascade). The request role also has no UPDATE or DELETE grant.
create or replace function fulfillment_append_only() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' or exists (select 1 from tenant where id = old.tenant_id) then
        raise exception '% rows are append only; a % of % was refused', tg_table_name, lower(tg_op), old.id using errcode = 'restrict_violation';
    end if;
    return old;
end;
$$;
drop trigger if exists fulfillment_audit_append_only on fulfillment_audit;
create trigger fulfillment_audit_append_only before update or delete on fulfillment_audit for each row execute function fulfillment_append_only();
drop trigger if exists fulfillment_event_append_only on fulfillment_event;
create trigger fulfillment_event_append_only before update or delete on fulfillment_event for each row execute function fulfillment_append_only();

-- ---------------------------------------------------------------- who may route a partner event to a tenant

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'fulfillment_router') then
        create role fulfillment_router;
    end if;
end
$$;

-- ---------------------------------------------------------------- row level security

alter table fulfillment_order        enable row level security;
alter table fulfillment_submission   enable row level security;
alter table fulfillment_dead_letter  enable row level security;
alter table fulfillment_event        enable row level security;
alter table fulfillment_audit        enable row level security;
alter table fulfillment_order        force row level security;
alter table fulfillment_submission   force row level security;
alter table fulfillment_dead_letter  force row level security;
alter table fulfillment_event        force row level security;
alter table fulfillment_audit        force row level security;

drop policy if exists fulfillment_order_select on fulfillment_order;
create policy fulfillment_order_select on fulfillment_order for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));
drop policy if exists fulfillment_order_insert on fulfillment_order;
create policy fulfillment_order_insert on fulfillment_order for insert with check (tenant_id = portal_tenant() and status = 'created' and portal_allows('orders', 'create', clinic_id));
drop policy if exists fulfillment_order_update on fulfillment_order;
create policy fulfillment_order_update on fulfillment_order for update using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))
    with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));

-- the narrow router reads the order's id, tenant and reference to find whose a partner event is, and nothing else
drop policy if exists fulfillment_order_router_read on fulfillment_order;
create policy fulfillment_order_router_read on fulfillment_order for select to fulfillment_router using (true);

drop policy if exists fulfillment_submission_select on fulfillment_submission;
create policy fulfillment_submission_select on fulfillment_submission for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));
drop policy if exists fulfillment_submission_write on fulfillment_submission;
create policy fulfillment_submission_write on fulfillment_submission for all
    using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))
    with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));

drop policy if exists fulfillment_dead_letter_select on fulfillment_dead_letter;
create policy fulfillment_dead_letter_select on fulfillment_dead_letter for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));
drop policy if exists fulfillment_dead_letter_write on fulfillment_dead_letter;
create policy fulfillment_dead_letter_write on fulfillment_dead_letter for all
    using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))
    with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));

drop policy if exists fulfillment_event_select on fulfillment_event;
create policy fulfillment_event_select on fulfillment_event for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));
drop policy if exists fulfillment_event_insert on fulfillment_event;
create policy fulfillment_event_insert on fulfillment_event for insert with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));

drop policy if exists fulfillment_audit_select on fulfillment_audit;
create policy fulfillment_audit_select on fulfillment_audit for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));
-- the row that records an order's creation is written by a role that may create orders; every later row by a role that may update them
drop policy if exists fulfillment_audit_insert on fulfillment_audit;
create policy fulfillment_audit_insert on fulfillment_audit for insert
    with check (tenant_id = portal_tenant() and (portal_allows('orders', 'update', clinic_id) or (kind = 'created' and portal_allows('orders', 'create', clinic_id))));

-- ---------------------------------------------------------------- grants

grant select on fulfillment_transition to app_request;
grant select, insert, update on fulfillment_order, fulfillment_submission, fulfillment_dead_letter to app_request;
grant select, insert on fulfillment_event, fulfillment_audit to app_request;
grant usage, select on all sequences in schema public to app_request;

grant usage on schema public to fulfillment_router;
grant select (id, tenant_id, order_ref) on fulfillment_order to fulfillment_router;
