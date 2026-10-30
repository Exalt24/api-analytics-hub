-- Prepaid wallet and billing ledger: a double-entry ledger in integer minor units, a wallet for each tenant, payments taken through a
-- Stripe-style provider, subscriptions, and the tables that make every money-moving step safe to repeat.
--
-- WHY EACH TABLE IS HERE.
--   billing_plan            the plan catalogue (monthly price, included credits). Global, read-only to the request role: it holds no tenant data.
--   billing_usage_price     the tenant's own price table: what one unit of each billable event costs.
--   billing_account         one per tenant, kind and currency: wallet (a liability), platform_revenue, provider_clearing, refunds, adjustments.
--                           Its balance is kept by a trigger from the entries and can be proved by billing_discrepancies().
--   billing_transaction     one row for each business event. The unique idempotency key and the request hash live here, so a retry returns the
--                           first result and a retry with different content is refused.
--   billing_entry           the lines. Debit is positive, credit is negative, and the lines of one transaction add up to zero (a deferred
--                           constraint trigger, so a half written transaction cannot commit). Append only.
--   billing_usage_event     what was metered: one row for each billable event, with the price it was charged at.
--   billing_payment         a payment asked of the provider: our idempotency key, the key sent to the provider, status, failure code, refunded total.
--   billing_subscription    one per tenant: plan, state (active, past_due, canceled), the provider ids, the last event seen.
--   billing_plan_change     a mid cycle plan change with the proration worked out in code.
--   billing_webhook_event   every provider event, once: the unique provider event id is the duplicate guard, and the row is written in the same
--                           transaction as the ledger lines it caused.
--   billing_notification    an outbox of things a person should be told (a failed payment, a past due plan). Nothing here sends them.
--
-- MONEY is an integer in minor units with its currency beside it, like the rest of the hub (see 001). A transaction has one currency and every
-- account and line must match it (composite foreign keys), so there is no line that mixes currencies.
--
-- Row level security is on and forced on every tenant table, with the same policy as every other tenant table. billing_plan has no tenant, so it
-- has none. The provider contract is from Stripe's official pages (docs/vendor-contract/stripe/) and has never been run against a live account.
--
-- Safe to run twice.

-- ---------------------------------------------------------------- plans (global)

create table if not exists billing_plan (
    id                      uuid        primary key default gen_random_uuid(),
    code                    text        not null unique check (length(code) between 1 and 40),
    name                    text        not null,
    currency                char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    monthly_price_minor     bigint      not null check (monthly_price_minor >= 0),
    included_credits_minor  bigint      not null default 0 check (included_credits_minor >= 0),
    -- the provider's id for the same plan, so a subscription event can be matched to a plan
    provider_price_id       text        unique,
    active                  boolean     not null default true,
    created_at              timestamptz not null default now()
);

-- ---------------------------------------------------------------- accounts and the ledger

create table if not exists billing_account (
    id              uuid        primary key default gen_random_uuid(),
    tenant_id       uuid        not null references tenant (id) on delete cascade,
    kind            text        not null check (kind in ('wallet', 'platform_revenue', 'provider_clearing', 'refunds', 'adjustments')),
    currency        char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    -- the sum of this account's entries (debit minus credit), kept by billing_apply_entry. A wallet is a liability, so its balance is zero or negative:
    -- minus the balance is what the customer can spend.
    balance_minor   bigint      not null default 0,
    created_at      timestamptz not null default now(),
    unique (tenant_id, kind, currency),
    unique (id, tenant_id),
    unique (id, currency),
    constraint wallet_never_overdrawn check (kind <> 'wallet' or balance_minor <= 0)
);

create table if not exists billing_transaction (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    kind             text        not null check (kind in ('topup', 'usage', 'refund', 'usage_refund', 'adjustment', 'subscription_payment')),
    currency         char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    -- the caller's key for this command (or one derived from the provider's object id), and a hash of what the command asked for
    idempotency_key  text        not null check (length(idempotency_key) between 1 and 255),
    request_hash     text        not null,
    -- the provider's id for what caused this (a payment intent, or a payment intent and the refunded total), when there is one
    provider_ref     text,
    reason           text,
    actor            text        not null check (length(btrim(actor)) between 1 and 120),
    occurred_at      timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    unique (id, tenant_id),
    unique (id, currency),
    constraint adjustment_needs_a_reason check (kind <> 'adjustment' or length(btrim(coalesce(reason, ''))) >= 3)
);

-- the same provider object moves money once for each kind, whatever key the caller used
create unique index if not exists idx_billing_transaction_provider_ref
    on billing_transaction (tenant_id, kind, provider_ref) where provider_ref is not null;
create index if not exists idx_billing_transaction_time on billing_transaction (tenant_id, occurred_at);

create table if not exists billing_entry (
    id              bigserial   primary key,
    tenant_id       uuid        not null references tenant (id) on delete cascade,
    transaction_id  uuid        not null,
    account_id      uuid        not null,
    currency        char(3)     not null,
    -- debit positive, credit negative
    amount_minor    bigint      not null check (amount_minor <> 0),
    created_at      timestamptz not null default now(),
    foreign key (transaction_id, tenant_id) references billing_transaction (id, tenant_id) on delete cascade,
    foreign key (transaction_id, currency)  references billing_transaction (id, currency)  on delete cascade,
    foreign key (account_id, tenant_id)     references billing_account (id, tenant_id),
    foreign key (account_id, currency)      references billing_account (id, currency)
);

create index if not exists idx_billing_entry_transaction on billing_entry (transaction_id);
create index if not exists idx_billing_entry_account on billing_entry (tenant_id, account_id, id);

-- BALANCED: the lines of a transaction add up to zero, and there are at least two. Deferred, so the lines can be inserted one at a time and the
-- check runs once at commit. A transaction with no lines is caught by the trigger on billing_transaction.
create or replace function billing_assert_balanced(p_transaction uuid) returns void language plpgsql as $$
declare
    v_sum   bigint;
    v_count bigint;
begin
    select coalesce(sum(amount_minor), 0), count(*) into v_sum, v_count from billing_entry where transaction_id = p_transaction;
    if v_count < 2 then
        raise exception 'billing transaction % has % line(s); a transaction needs at least two', p_transaction, v_count
            using errcode = 'check_violation';
    end if;
    if v_sum <> 0 then
        raise exception 'billing transaction % is out of balance by % minor units', p_transaction, v_sum
            using errcode = 'check_violation';
    end if;
end;
$$;

create or replace function billing_entry_balanced() returns trigger language plpgsql as $$
begin
    perform billing_assert_balanced(new.transaction_id);
    return null;
end;
$$;

create or replace function billing_transaction_has_lines() returns trigger language plpgsql as $$
begin
    perform billing_assert_balanced(new.id);
    return null;
end;
$$;

drop trigger if exists billing_entry_balanced on billing_entry;
create constraint trigger billing_entry_balanced after insert on billing_entry
    deferrable initially deferred for each row execute function billing_entry_balanced();

drop trigger if exists billing_transaction_has_lines on billing_transaction;
create constraint trigger billing_transaction_has_lines after insert on billing_transaction
    deferrable initially deferred for each row execute function billing_transaction_has_lines();

-- BALANCES: each entry moves its account's balance in the same statement, so a wallet that would go below zero is refused by wallet_never_overdrawn
-- on that very insert. The update also takes the account row's lock, which is what orders two entries for one wallet. SECURITY DEFINER because the
-- request role has no UPDATE on billing_account, so it cannot write a balance of its own; the owner is still filtered by forced row level security.
create or replace function billing_apply_entry() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
    update billing_account set balance_minor = balance_minor + new.amount_minor where id = new.account_id;
    return null;
end;
$$;

drop trigger if exists billing_apply_entry on billing_entry;
create trigger billing_apply_entry after insert on billing_entry for each row execute function billing_apply_entry();

-- APPEND ONLY: an entry or a transaction is never changed, and is deleted only when the whole tenant is (the foreign keys cascade). The request role
-- also has no UPDATE or DELETE grant (below); this trigger holds for the table's owner too.
create or replace function billing_append_only() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' or exists (select 1 from tenant where id = old.tenant_id) then
        raise exception '% rows are append only; a % of % was refused', tg_table_name, lower(tg_op), old.id using errcode = 'restrict_violation';
    end if;
    return old;
end;
$$;

drop trigger if exists billing_entry_append_only on billing_entry;
create trigger billing_entry_append_only before update or delete on billing_entry for each row execute function billing_append_only();
drop trigger if exists billing_transaction_append_only on billing_transaction;
create trigger billing_transaction_append_only before update or delete on billing_transaction for each row execute function billing_append_only();

-- ---------------------------------------------------------------- metering

create table if not exists billing_usage_price (
    id                uuid        primary key default gen_random_uuid(),
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    event_type        text        not null check (length(event_type) between 1 and 60),
    currency          char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    unit_price_minor  bigint      not null check (unit_price_minor > 0),
    unique (tenant_id, event_type, currency)
);

create table if not exists billing_usage_event (
    id                  bigserial   primary key,
    tenant_id           uuid        not null references tenant (id) on delete cascade,
    transaction_id      uuid        not null,
    event_type          text        not null,
    quantity            bigint      not null check (quantity > 0),
    unit_price_minor    bigint      not null check (unit_price_minor > 0),
    amount_minor        bigint      not null check (amount_minor > 0),
    currency            char(3)     not null,
    occurred_at         timestamptz not null default now(),
    foreign key (transaction_id, tenant_id) references billing_transaction (id, tenant_id) on delete cascade,
    -- the amount charged is exactly the price times the quantity: the table cannot hold a different sum
    constraint usage_is_price_times_quantity check (amount_minor = unit_price_minor * quantity)
);

create index if not exists idx_billing_usage_event_time on billing_usage_event (tenant_id, occurred_at);

-- ---------------------------------------------------------------- payments, subscriptions, provider events

create table if not exists billing_payment (
    id                        uuid        primary key default gen_random_uuid(),
    tenant_id                 uuid        not null references tenant (id) on delete cascade,
    idempotency_key           text        not null check (length(idempotency_key) between 1 and 255),
    request_hash              text        not null,
    -- the key sent to the provider with the create call; stored before the call, so a retry after a crash sends the same one
    provider_idempotency_key  text        not null,
    amount_minor              bigint      not null check (amount_minor > 0),
    currency                  char(3)     not null check (currency ~ '^[A-Z]{3}$'),
    -- a provider token for a payment method, never a card number
    payment_method_ref        text,
    status                    text        not null default 'created'
                              check (status in ('created', 'processing', 'succeeded', 'failed', 'unknown')),
    provider_payment_id       text,
    failure_code              text,
    failure_message           text,
    credited_minor            bigint      not null default 0 check (credited_minor >= 0),
    refunded_minor            bigint      not null default 0 check (refunded_minor >= 0),
    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    -- a failed payment never credits, and a refund can never exceed what was paid
    constraint failed_never_credits check (status <> 'failed' or credited_minor = 0),
    constraint credit_within_payment check (credited_minor <= amount_minor),
    constraint refund_within_credit check (refunded_minor <= credited_minor)
);

create unique index if not exists idx_billing_payment_provider on billing_payment (provider_payment_id) where provider_payment_id is not null;

create table if not exists billing_subscription (
    id                        uuid        primary key default gen_random_uuid(),
    tenant_id                 uuid        not null unique references tenant (id) on delete cascade,
    plan_id                   uuid        not null references billing_plan (id),
    status                    text        not null default 'active' check (status in ('active', 'past_due', 'canceled')),
    provider_customer_id      text        unique,
    provider_subscription_id  text        unique,
    -- what the provider last called it, kept as sent
    provider_status           text,
    period_start              timestamptz not null,
    period_end                timestamptz not null,
    -- the created time of the newest event applied, so an older one that arrives late is not applied over it
    last_event_created        bigint      not null default 0,
    past_due_since            timestamptz,
    canceled_at               timestamptz,
    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now(),
    constraint period_is_forward check (period_end > period_start),
    constraint canceled_has_a_time check (status <> 'canceled' or canceled_at is not null)
);

create table if not exists billing_plan_change (
    id                uuid        primary key default gen_random_uuid(),
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    idempotency_key   text        not null check (length(idempotency_key) between 1 and 255),
    request_hash      text        not null,
    from_plan_id      uuid        not null references billing_plan (id),
    to_plan_id        uuid        not null references billing_plan (id),
    changed_at        timestamptz not null,
    period_start      timestamptz not null,
    period_end        timestamptz not null,
    -- the unused part of the old plan, the rest of the period on the new plan, and the difference (all minor units, see app/billing/proration.py)
    credit_minor      bigint      not null check (credit_minor >= 0),
    charge_minor      bigint      not null check (charge_minor >= 0),
    net_minor         bigint      not null,
    actor             text        not null,
    created_at        timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    constraint net_is_the_difference check (net_minor = charge_minor - credit_minor)
);

create table if not exists billing_webhook_event (
    id                  bigserial   primary key,
    tenant_id           uuid        not null references tenant (id) on delete cascade,
    provider_event_id   text        not null check (length(provider_event_id) between 1 and 255),
    event_type          text        not null,
    -- processed: its effect is in the ledger. ignored: a type we do not act on, or an older state than the one held. held: waiting for something
    -- (a refund before its payment, a refund larger than the wallet) and retried by reprocess_held.
    status              text        not null default 'processed' check (status in ('processed', 'ignored', 'held')),
    detail              text,
    payload             jsonb       not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz,
    -- one row for each provider event: the second delivery of an id meets this and writes nothing
    unique (tenant_id, provider_event_id)
);

create table if not exists billing_notification (
    id              bigserial   primary key,
    tenant_id       uuid        not null references tenant (id) on delete cascade,
    kind            text        not null check (kind in ('payment_failed', 'subscription_past_due', 'subscription_canceled', 'refund_held')),
    subject_ref     text,
    message         text        not null,
    created_at      timestamptz not null default now(),
    delivered_at    timestamptz
);

-- ---------------------------------------------------------------- the proof: stored balances and payments against the entries

-- Every disagreement between what is stored and what the entries say, for one tenant. An empty result is the proof. A plain function, so row
-- level security applies to the caller.
create or replace function billing_discrepancies(p_tenant uuid)
returns table (problem text, ref text, stored_minor bigint, derived_minor bigint) language sql stable as $$
    select 'account balance'::text, a.kind || ' ' || a.currency, a.balance_minor, coalesce(sum(e.amount_minor), 0)::bigint
      from billing_account a left join billing_entry e on e.account_id = a.id
     where a.tenant_id = p_tenant
     group by a.id, a.kind, a.currency, a.balance_minor
    having a.balance_minor <> coalesce(sum(e.amount_minor), 0)
    union all
    select 'unbalanced transaction'::text, t.id::text, 0::bigint, coalesce(sum(e.amount_minor), 0)::bigint
      from billing_transaction t left join billing_entry e on e.transaction_id = t.id
     where t.tenant_id = p_tenant
     group by t.id
    having coalesce(sum(e.amount_minor), 0) <> 0 or count(e.id) < 2
    union all
    select 'payment credited differs from ledger'::text, p.provider_payment_id, p.credited_minor,
           coalesce((select -sum(e.amount_minor) from billing_transaction t
                       join billing_entry e on e.transaction_id = t.id
                       join billing_account a on a.id = e.account_id and a.kind = 'wallet'
                      where t.tenant_id = p.tenant_id and t.kind = 'topup' and t.provider_ref = p.provider_payment_id), 0)::bigint
      from billing_payment p
     where p.tenant_id = p_tenant and p.provider_payment_id is not null
       and p.credited_minor <> coalesce((select -sum(e.amount_minor) from billing_transaction t
                       join billing_entry e on e.transaction_id = t.id
                       join billing_account a on a.id = e.account_id and a.kind = 'wallet'
                      where t.tenant_id = p.tenant_id and t.kind = 'topup' and t.provider_ref = p.provider_payment_id), 0)
    union all
    select 'payment refunded differs from ledger'::text, p.provider_payment_id, p.refunded_minor,
           coalesce((select sum(e.amount_minor) from billing_transaction t
                       join billing_entry e on e.transaction_id = t.id
                       join billing_account a on a.id = e.account_id and a.kind = 'wallet'
                      where t.tenant_id = p.tenant_id and t.kind = 'refund' and split_part(t.provider_ref, ':', 1) = p.provider_payment_id), 0)::bigint
      from billing_payment p
     where p.tenant_id = p_tenant and p.provider_payment_id is not null
       and p.refunded_minor <> coalesce((select sum(e.amount_minor) from billing_transaction t
                       join billing_entry e on e.transaction_id = t.id
                       join billing_account a on a.id = e.account_id and a.kind = 'wallet'
                      where t.tenant_id = p.tenant_id and t.kind = 'refund' and split_part(t.provider_ref, ':', 1) = p.provider_payment_id), 0)
$$;

-- ---------------------------------------------------------------- who may route a provider event to a tenant

-- A provider event arrives before anyone knows whose it is. The receiver looks the owner up through this role, which can read exactly two tables
-- (the payments and the subscriptions, for their provider ids) and nothing else, and writes nothing. The ledger is never visible to it.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'billing_router') then
        create role billing_router;
    end if;
end
$$;

-- ---------------------------------------------------------------- row level security, enabled and forced on every tenant table

alter table billing_account         enable row level security;
alter table billing_transaction     enable row level security;
alter table billing_entry           enable row level security;
alter table billing_usage_price     enable row level security;
alter table billing_usage_event     enable row level security;
alter table billing_payment         enable row level security;
alter table billing_subscription    enable row level security;
alter table billing_plan_change     enable row level security;
alter table billing_webhook_event   enable row level security;
alter table billing_notification    enable row level security;

alter table billing_account         force row level security;
alter table billing_transaction     force row level security;
alter table billing_entry           force row level security;
alter table billing_usage_price     force row level security;
alter table billing_usage_event     force row level security;
alter table billing_payment         force row level security;
alter table billing_subscription    force row level security;
alter table billing_plan_change     force row level security;
alter table billing_webhook_event   force row level security;
alter table billing_notification    force row level security;

drop policy if exists billing_account_by_tenant on billing_account;
create policy billing_account_by_tenant on billing_account
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_transaction_by_tenant on billing_transaction;
create policy billing_transaction_by_tenant on billing_transaction
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_entry_by_tenant on billing_entry;
create policy billing_entry_by_tenant on billing_entry
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_usage_price_by_tenant on billing_usage_price;
create policy billing_usage_price_by_tenant on billing_usage_price
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_usage_event_by_tenant on billing_usage_event;
create policy billing_usage_event_by_tenant on billing_usage_event
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_payment_by_tenant on billing_payment;
create policy billing_payment_by_tenant on billing_payment
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_subscription_by_tenant on billing_subscription;
create policy billing_subscription_by_tenant on billing_subscription
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_plan_change_by_tenant on billing_plan_change;
create policy billing_plan_change_by_tenant on billing_plan_change
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_webhook_event_by_tenant on billing_webhook_event;
create policy billing_webhook_event_by_tenant on billing_webhook_event
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists billing_notification_by_tenant on billing_notification;
create policy billing_notification_by_tenant on billing_notification
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- the router reads payments and subscriptions across tenants, to find the owner of a provider id, and nothing more
drop policy if exists billing_payment_router_read on billing_payment;
create policy billing_payment_router_read on billing_payment for select to billing_router using (true);
drop policy if exists billing_subscription_router_read on billing_subscription;
create policy billing_subscription_router_read on billing_subscription for select to billing_router using (true);

-- ---------------------------------------------------------------- grants

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new tables owe their own.
-- The ledger is append only for the request role: it can read and insert, never update or delete. An account is created with its identity columns
-- only, so no caller can set a balance, and the balance trigger is the one writer of it.
grant select on billing_plan to app_request;
grant select on billing_account to app_request;
grant insert (id, tenant_id, kind, currency) on billing_account to app_request;
grant select, insert on billing_transaction, billing_entry, billing_usage_event, billing_plan_change to app_request;
grant select, insert, update on billing_usage_price, billing_payment, billing_subscription, billing_webhook_event, billing_notification to app_request;
grant usage, select on all sequences in schema public to app_request;
grant execute on function billing_discrepancies(uuid) to app_request;

grant usage on schema public to billing_router;
grant select on billing_payment, billing_subscription to billing_router;
