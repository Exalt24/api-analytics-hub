-- Bank debit (ACH) payments, invoices with line items, their refunds, and sales rep commissions, on top of the billing ledger of 012 and the access matrix of 022.
--
-- WHY EACH CHANGE IS HERE.
--   billing_payment (new columns)  payment_method_type: card or us_bank_account (ACH); customer_ref and mandate_ref, which a bank debit needs; purpose: a wallet top-up or the payment
--                                  of one invoice; invoice_id and clinic_id. A bank debit is in US dollars only (a constraint). A payment made for an invoice never credits the wallet
--                                  (a constraint), so the two kinds of payment cannot be mixed up by a later edit. An ACH payment is `processing` until the provider says it succeeded.
--   billing_transaction            two more kinds, invoice_payment and invoice_refund.
--   billing_invoice                an invoice of one clinic: draft, open (finalized), paid or void. The total is the sum of the lines at the moment of finalizing, and cannot change after.
--                                  Moves are a table (billing_invoice_transition) enforced by a trigger, and the trigger asks the access matrix for the right action: finalize and void
--                                  are `update`, paying is `settle`, refunding is `refund`. A paid invoice's only later change is its refunded total, which only grows and never passes the total.
--   billing_invoice_line           one line: description, quantity, unit amount, and the amount the table itself checks as their product. Lines move only while the invoice is a draft.
--   billing_invoice_refund         the request log of refunds of an invoice, linked to the provider's refund id (the refunds of 012's payments code) or, for a wallet payment, to the wallet.
--   commission                     append only: an accrual when an invoice is paid (percentage of the total, integer minor units, rounded down) and a reversal when it is refunded. The net
--                                  of one invoice is never negative (a deferred trigger) and the accrual is unique per invoice.
--
-- MONEY is an integer in minor units with its currency beside it. Row level security is enabled and forced on every new table; the policies ask the access matrix (022).
-- Safe to run twice. Needs 012 and 022.

-- ---------------------------------------------------------------- payments: bank debit and the invoice they may pay

alter table billing_payment add column if not exists payment_method_type text not null default 'card';
alter table billing_payment add column if not exists customer_ref text;
alter table billing_payment add column if not exists mandate_ref text;
alter table billing_payment add column if not exists purpose text not null default 'topup';
alter table billing_payment add column if not exists invoice_id uuid;
alter table billing_payment add column if not exists clinic_id uuid;

alter table billing_payment drop constraint if exists payment_method_type_known;
alter table billing_payment add constraint payment_method_type_known check (payment_method_type in ('card', 'us_bank_account'));
alter table billing_payment drop constraint if exists ach_is_usd_with_customer_and_mandate;
alter table billing_payment add constraint ach_is_usd_with_customer_and_mandate
    check (payment_method_type <> 'us_bank_account' or (currency = 'USD' and customer_ref is not null and mandate_ref is not null));
alter table billing_payment drop constraint if exists payment_purpose_known;
alter table billing_payment add constraint payment_purpose_known check (purpose in ('topup', 'invoice'));
alter table billing_payment drop constraint if exists invoice_payment_names_its_invoice;
alter table billing_payment add constraint invoice_payment_names_its_invoice check ((purpose = 'invoice') = (invoice_id is not null));
alter table billing_payment drop constraint if exists invoice_payment_never_credits_wallet;
alter table billing_payment add constraint invoice_payment_never_credits_wallet check (purpose <> 'invoice' or (credited_minor = 0 and refunded_minor = 0));

-- ---------------------------------------------------------------- the ledger knows two more kinds of event

alter table billing_transaction drop constraint if exists billing_transaction_kind_check;
alter table billing_transaction add constraint billing_transaction_kind_check
    check (kind in ('topup', 'usage', 'refund', 'usage_refund', 'adjustment', 'subscription_payment', 'invoice_payment', 'invoice_refund'));

-- ---------------------------------------------------------------- invoices

create sequence if not exists billing_invoice_number_seq;

create table if not exists billing_invoice (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    clinic_id        uuid        not null,
    number           bigint      not null default nextval('billing_invoice_number_seq'),
    currency         char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    status           text        not null default 'draft' check (status in ('draft', 'open', 'paid', 'void')),
    total_minor      bigint      not null default 0 check (total_minor >= 0),
    refunded_minor   bigint      not null default 0 check (refunded_minor >= 0),
    -- how it was paid: the wallet, a card or a bank debit; the payment row of the last two
    pay_method       text        check (pay_method in ('wallet', 'card', 'us_bank_account')),
    payment_id       uuid,
    idempotency_key  text        not null check (length(idempotency_key) between 1 and 255),
    request_hash     text        not null,
    created_by       text        not null check (length(created_by) between 1 and 80),
    created_at       timestamptz not null default now(),
    finalized_at     timestamptz,
    paid_at          timestamptz,
    voided_at        timestamptz,
    updated_at       timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    unique (tenant_id, number),
    unique (id, tenant_id),
    unique (id, clinic_id),
    foreign key (clinic_id, tenant_id) references clinic (id, tenant_id),
    constraint refund_within_total check (refunded_minor <= total_minor),
    constraint open_or_paid_has_a_total check (status = 'draft' or status = 'void' or total_minor > 0),
    constraint paid_has_a_method_and_a_time check (status <> 'paid' or (pay_method is not null and paid_at is not null)),
    constraint only_a_paid_invoice_is_refunded check (status = 'paid' or refunded_minor = 0),
    constraint only_a_paid_invoice_has_a_paid_time check (status = 'paid' or paid_at is null)
);
create index if not exists idx_billing_invoice_clinic on billing_invoice (tenant_id, clinic_id, created_at);

alter table billing_payment drop constraint if exists billing_payment_invoice_fk;
alter table billing_payment add constraint billing_payment_invoice_fk foreign key (invoice_id, tenant_id) references billing_invoice (id, tenant_id);
alter table billing_payment drop constraint if exists billing_payment_clinic_fk;
alter table billing_payment add constraint billing_payment_clinic_fk foreign key (clinic_id, tenant_id) references clinic (id, tenant_id);
create unique index if not exists idx_billing_payment_one_live_per_invoice on billing_payment (invoice_id) where invoice_id is not null and status in ('created', 'processing', 'unknown', 'succeeded');

create table if not exists billing_invoice_line (
    id                 uuid        primary key default gen_random_uuid(),
    tenant_id          uuid        not null references tenant (id) on delete cascade,
    invoice_id         uuid        not null,
    clinic_id          uuid        not null,
    position           integer     not null check (position >= 1),
    description        text        not null check (length(btrim(description)) between 1 and 200),
    quantity           bigint      not null check (quantity > 0),
    unit_amount_minor  bigint      not null check (unit_amount_minor > 0),
    amount_minor       bigint      not null check (amount_minor > 0),
    unique (invoice_id, position),
    foreign key (invoice_id, tenant_id) references billing_invoice (id, tenant_id) on delete cascade,
    foreign key (invoice_id, clinic_id) references billing_invoice (id, clinic_id) on delete cascade,
    -- the amount is exactly the product: the table cannot hold a different sum
    constraint line_is_price_times_quantity check (amount_minor = unit_amount_minor * quantity)
);

create table if not exists billing_invoice_refund (
    id                  uuid        primary key default gen_random_uuid(),
    tenant_id           uuid        not null references tenant (id) on delete cascade,
    invoice_id          uuid        not null,
    clinic_id           uuid        not null,
    amount_minor        bigint      not null check (amount_minor > 0),
    method              text        not null check (method in ('wallet', 'card', 'us_bank_account')),
    idempotency_key     text        not null check (length(idempotency_key) between 1 and 255),
    request_hash        text        not null,
    -- the provider's refund id when the money goes back to a card or a bank account (it links this row to the refund of billing.payments), null for the wallet
    provider_refund_id  text,
    created_by          text        not null check (length(created_by) between 1 and 80),
    created_at          timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    foreign key (invoice_id, tenant_id) references billing_invoice (id, tenant_id),
    foreign key (invoice_id, clinic_id) references billing_invoice (id, clinic_id)
);

-- A line is added, changed or removed only while its invoice is a draft; an invoice moves along the legal moves only, and the access matrix decides who makes each one.
create table if not exists billing_invoice_transition (
    from_status text not null,
    to_status   text not null,
    -- the matrix action that makes this move
    action      text not null check (action in ('update', 'settle')),
    primary key (from_status, to_status)
);
insert into billing_invoice_transition (from_status, to_status, action) values
    ('draft', 'open', 'update'), ('draft', 'void', 'update'), ('open', 'void', 'update'), ('open', 'paid', 'settle')
on conflict (from_status, to_status) do update set action = excluded.action;

create or replace function billing_invoice_line_only_in_draft() returns trigger language plpgsql as $$
declare
    v_invoice uuid;
    v_status  text;
begin
    if tg_op = 'DELETE' then v_invoice := old.invoice_id; else v_invoice := new.invoice_id; end if;
    select status into v_status from billing_invoice where id = v_invoice;
    if v_status is distinct from 'draft' then
        raise exception 'invoice % is %, so its lines cannot change', v_invoice, v_status using errcode = 'restrict_violation';
    end if;
    if tg_op = 'DELETE' then return old; end if;
    return new;
end;
$$;
drop trigger if exists billing_invoice_line_only_in_draft on billing_invoice_line;
create trigger billing_invoice_line_only_in_draft before insert or update or delete on billing_invoice_line for each row execute function billing_invoice_line_only_in_draft();

create or replace function billing_invoice_guard() returns trigger language plpgsql as $$
declare
    v_action text;
    v_sum    bigint;
begin
    if tg_op = 'DELETE' then
        raise exception 'an invoice is never deleted, it is voided' using errcode = 'restrict_violation';
    end if;
    if (new.id, new.tenant_id, new.clinic_id, new.number, new.currency, new.idempotency_key, new.request_hash, new.created_by, new.created_at)
       is distinct from (old.id, old.tenant_id, old.clinic_id, old.number, old.currency, old.idempotency_key, old.request_hash, old.created_by, old.created_at) then
        raise exception 'invoice % is frozen except for its status, payment and refunded total', old.id using errcode = 'restrict_violation';
    end if;
    if new.status <> old.status then
        select t.action into v_action from billing_invoice_transition t where t.from_status = old.status and t.to_status = new.status;
        if v_action is null then
            raise exception 'invoice % cannot move from % to %', old.id, old.status, new.status using errcode = 'restrict_violation';
        end if;
        if not portal_allows('invoices', v_action, new.clinic_id) then
            raise exception 'the asking role may not % invoice %', v_action, old.id using errcode = 'insufficient_privilege';
        end if;
        if old.status = 'draft' and new.status = 'open' then
            select coalesce(sum(amount_minor), 0) into v_sum from billing_invoice_line where invoice_id = old.id;
            if v_sum <= 0 or new.total_minor <> v_sum then
                raise exception 'invoice % has lines worth %, so its total cannot be %', old.id, v_sum, new.total_minor using errcode = 'check_violation';
            end if;
        elsif new.total_minor <> old.total_minor then
            raise exception 'the total of invoice % cannot change after it is finalized', old.id using errcode = 'restrict_violation';
        end if;
    else
        if new.total_minor <> old.total_minor then
            raise exception 'the total of invoice % cannot change after it is finalized', old.id using errcode = 'restrict_violation';
        end if;
        if new.refunded_minor <> old.refunded_minor then
            if old.status <> 'paid' or new.refunded_minor < old.refunded_minor then
                raise exception 'invoice %: a refunded total only grows, and only on a paid invoice', old.id using errcode = 'restrict_violation';
            end if;
            if not portal_allows('invoices', 'refund', new.clinic_id) then
                raise exception 'the asking role may not refund invoice %', old.id using errcode = 'insufficient_privilege';
            end if;
        elsif (new.payment_id, new.pay_method) is distinct from (old.payment_id, old.pay_method) then
            if old.status <> 'open' or not portal_allows('invoices', 'settle', new.clinic_id) then
                raise exception 'a payment is attached to an open invoice by a role that may settle it' using errcode = 'insufficient_privilege';
            end if;
        end if;
    end if;
    return new;
end;
$$;
drop trigger if exists billing_invoice_guard on billing_invoice;
create trigger billing_invoice_guard before update or delete on billing_invoice for each row execute function billing_invoice_guard();

create or replace function billing_invoice_refund_guard() returns trigger language plpgsql as $$
begin
    if not exists (select 1 from billing_invoice i where i.id = new.invoice_id and i.status = 'paid') then
        raise exception 'only a paid invoice is refunded' using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
drop trigger if exists billing_invoice_refund_guard on billing_invoice_refund;
create trigger billing_invoice_refund_guard before insert on billing_invoice_refund for each row execute function billing_invoice_refund_guard();

-- ---------------------------------------------------------------- commissions

create table if not exists commission (
    id            uuid        primary key default gen_random_uuid(),
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    invoice_id    uuid        not null,
    clinic_id     uuid        not null,
    rep_user_id   uuid        not null,
    kind          text        not null check (kind in ('accrual', 'reversal')),
    -- an accrual is positive, a reversal negative; both in minor units of the invoice's currency
    amount_minor  bigint      not null,
    -- what the percentage was applied to (the invoice total for an accrual, the refunded total so far for a reversal) and the rate, in basis points
    basis_minor   bigint      not null check (basis_minor > 0),
    rate_bp       integer     not null check (rate_bp between 0 and 10000),
    currency      char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    -- accrual for the accrual; refund:<total refunded so far> for a reversal, so a repeat of the same refund writes nothing
    ref           text        not null check (length(ref) between 1 and 80),
    created_at    timestamptz not null default now(),
    unique (tenant_id, invoice_id, kind, ref),
    foreign key (invoice_id, tenant_id) references billing_invoice (id, tenant_id),
    foreign key (invoice_id, clinic_id) references billing_invoice (id, clinic_id),
    foreign key (rep_user_id, tenant_id) references portal_user (id, tenant_id),
    constraint accrual_is_positive_reversal_is_negative check ((kind = 'accrual' and amount_minor >= 0) or (kind = 'reversal' and amount_minor < 0)),
    constraint ref_matches_kind check ((kind = 'accrual' and ref = 'accrual') or (kind = 'reversal' and ref like 'refund:%'))
);
create unique index if not exists idx_commission_one_accrual on commission (invoice_id) where kind = 'accrual';
create index if not exists idx_commission_rep on commission (tenant_id, rep_user_id, created_at);

create or replace function commission_append_only() returns trigger language plpgsql as $$
begin
    raise exception 'commission rows are append only; a % was refused', lower(tg_op) using errcode = 'restrict_violation';
end;
$$;
drop trigger if exists commission_append_only on commission;
create trigger commission_append_only before update or delete on commission for each row execute function commission_append_only();

-- the net commission of one invoice (accrual plus reversals) is never below zero, checked as each row goes in (the accrual always goes in first)
create or replace function commission_net_never_negative() returns trigger language plpgsql as $$
declare
    v_net bigint;
begin
    select coalesce(sum(amount_minor), 0) into v_net from commission where invoice_id = new.invoice_id;
    if v_net < 0 then
        raise exception 'the commission on invoice % would go below zero (%)', new.invoice_id, v_net using errcode = 'check_violation';
    end if;
    return null;
end;
$$;
drop trigger if exists commission_net_never_negative on commission;
create trigger commission_net_never_negative after insert on commission for each row execute function commission_net_never_negative();

-- ---------------------------------------------------------------- the proof for invoices: what is stored against the ledger, for one tenant. Empty is the proof.

create or replace function billing_invoice_discrepancies(p_tenant uuid)
returns table (problem text, ref text, stored_minor bigint, derived_minor bigint) language sql stable as $$
    -- a paid invoice has exactly one invoice_payment transaction worth its total
    select 'paid invoice not in the ledger'::text, i.id::text, i.total_minor,
           coalesce((select -sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'
                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_payment' and t.provider_ref = 'invoice:' || i.id::text), 0)::bigint
      from billing_invoice i
     where i.tenant_id = p_tenant and i.status = 'paid'
       and i.total_minor <> coalesce((select -sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'
                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_payment' and t.provider_ref = 'invoice:' || i.id::text), 0)
    union all
    -- an invoice that is not paid has no invoice_payment transaction
    select 'unpaid invoice has a payment in the ledger'::text, i.id::text, 0::bigint, 1::bigint
      from billing_invoice i
     where i.tenant_id = p_tenant and i.status <> 'paid'
       and exists (select 1 from billing_transaction t where t.tenant_id = i.tenant_id and t.kind = 'invoice_payment' and t.provider_ref = 'invoice:' || i.id::text)
    union all
    -- the refunded total is what the invoice_refund transactions moved out of revenue
    select 'invoice refunded differs from ledger'::text, i.id::text, i.refunded_minor,
           coalesce((select sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'
                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_refund' and split_part(t.provider_ref, ':', 2) = i.id::text), 0)::bigint
      from billing_invoice i
     where i.tenant_id = p_tenant
       and i.refunded_minor <> coalesce((select sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'
                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_refund' and split_part(t.provider_ref, ':', 2) = i.id::text), 0)
    union all
    -- the commission net of an invoice is the rate times (total minus refunded), rounded down, or nothing when the clinic had no rep
    select 'commission differs from the rate'::text, c.invoice_id::text, sum(c.amount_minor)::bigint,
           ((i.total_minor - i.refunded_minor) * max(c.rate_bp) / 10000)::bigint
      from commission c join billing_invoice i on i.id = c.invoice_id
     where c.tenant_id = p_tenant
     group by c.invoice_id, i.total_minor, i.refunded_minor
    having sum(c.amount_minor) <> ((i.total_minor - i.refunded_minor) * max(c.rate_bp) / 10000)
$$;

-- ---------------------------------------------------------------- row level security

alter table billing_invoice           enable row level security;
alter table billing_invoice_line      enable row level security;
alter table billing_invoice_refund    enable row level security;
alter table commission                enable row level security;
alter table billing_invoice           force row level security;
alter table billing_invoice_line      force row level security;
alter table billing_invoice_refund    force row level security;
alter table commission                force row level security;

drop policy if exists billing_invoice_select on billing_invoice;
create policy billing_invoice_select on billing_invoice for select using (tenant_id = portal_tenant() and portal_allows('invoices', 'read', clinic_id));
drop policy if exists billing_invoice_insert on billing_invoice;
create policy billing_invoice_insert on billing_invoice for insert with check (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id));
-- any of the three moves may update a row; billing_invoice_guard then asks for the action that the particular change needs
drop policy if exists billing_invoice_update on billing_invoice;
create policy billing_invoice_update on billing_invoice for update
    using (tenant_id = portal_tenant() and (portal_allows('invoices', 'update', clinic_id) or portal_allows('invoices', 'settle', clinic_id) or portal_allows('invoices', 'refund', clinic_id)))
    with check (tenant_id = portal_tenant() and (portal_allows('invoices', 'update', clinic_id) or portal_allows('invoices', 'settle', clinic_id) or portal_allows('invoices', 'refund', clinic_id)));

drop policy if exists billing_invoice_line_select on billing_invoice_line;
create policy billing_invoice_line_select on billing_invoice_line for select using (tenant_id = portal_tenant() and portal_allows('invoices', 'read', clinic_id));
drop policy if exists billing_invoice_line_write on billing_invoice_line;
create policy billing_invoice_line_write on billing_invoice_line for all using (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id))
    with check (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id));

drop policy if exists billing_invoice_refund_select on billing_invoice_refund;
create policy billing_invoice_refund_select on billing_invoice_refund for select using (tenant_id = portal_tenant() and portal_allows('invoices', 'read', clinic_id));
drop policy if exists billing_invoice_refund_insert on billing_invoice_refund;
create policy billing_invoice_refund_insert on billing_invoice_refund for insert with check (tenant_id = portal_tenant() and portal_allows('invoices', 'refund', clinic_id));

drop policy if exists commission_select on commission;
create policy commission_select on commission for select using (tenant_id = portal_tenant() and portal_allows('commissions', 'read', clinic_id, rep_user_id));
drop policy if exists commission_insert on commission;
create policy commission_insert on commission for insert
    with check (tenant_id = portal_tenant() and portal_allows('commissions', case when kind = 'reversal' then 'refund' else 'create' end, clinic_id, rep_user_id));

-- billing_payment already has its tenant policy (012). These are RESTRICTIVE: a row must pass them as well. With a role set the matrix decides; with NO role set the older
-- tenant wide behaviour stays, because the wallet engine, its webhook receiver and their tests use this table without a portal user. Every portal request sets a role.
drop policy if exists billing_payment_matrix_select on billing_payment;
create policy billing_payment_matrix_select on billing_payment as restrictive for select using (portal_role() is null or portal_allows('payments', 'read', clinic_id));
drop policy if exists billing_payment_matrix_insert on billing_payment;
create policy billing_payment_matrix_insert on billing_payment as restrictive for insert with check (portal_role() is null or portal_allows('payments', 'create', clinic_id));
drop policy if exists billing_payment_matrix_update on billing_payment;
create policy billing_payment_matrix_update on billing_payment as restrictive for update
    using (portal_role() is null or portal_allows('payments', 'update', clinic_id)) with check (portal_role() is null or portal_allows('payments', 'update', clinic_id));

-- ---------------------------------------------------------------- grants

grant select on billing_invoice_transition to app_request;
grant select, insert, update on billing_invoice to app_request;
grant select, insert, update, delete on billing_invoice_line to app_request;
grant select, insert on billing_invoice_refund, commission to app_request;
grant usage, select on all sequences in schema public to app_request;
grant execute on function billing_invoice_discrepancies(uuid) to app_request;
