-- Inventory planning: the variants a shop sells, a dated stock snapshot for each, the order lines sold, the suppliers and promotions a person enters, and the plan computed from them.
--
-- WHY EACH TABLE IS HERE.
--   inventory_supplier      a supplier and its lead time (days, with a standard deviation). Entered by a person: no shop holds it.
--   inventory_variant       one row per Shopify product variant: names, SKU, unit cost when the shop has one, the supplier, the minimum order quantity and pack size.
--   inventory_snapshot      one row per variant per day: what was available when the sync read it, summed over locations, with each location in a jsonb column.
--                           History is accumulated, never fetched (the same rule as metric_snapshot in 001): Shopify serves the current quantity and no daily history, so the days a
--                           variant was out of stock can only be known from the snapshots this table keeps.
--   inventory_order_line    one row per order line: the units and the net revenue in minor units on the day the platform attributes the sale to (processedAt, UTC).
--   inventory_promo         a dated promotion factor for one variant or for all of them, entered by a person. A forecast is multiplied by it; nothing is guessed.
--   inventory_plan_run      one stored plan for a tenant and an as-of date (a rerun the same day replaces it) and the parameters it used.
--   inventory_plan_line     the plan: one row per variant, with the velocity, the cover, the reorder point, the class and label and the suggested order. It holds the names and the
--                           supplier it was worked out for, so an export of an old plan does not change when a supplier is renamed.
--   inventory_alert         one row per shortage (out of stock, cover shorter than the lead time, a projected stockout). Append only, and a UNIQUE natural key
--                           (tenant, variant, kind, event_ref) where event_ref names the stock cycle (the date of the last restock), so a daily rerun raises each shortage once.
--   inventory_alert_outbox  the alerts waiting to go out, the same shape and the same delivery code as change_outbox in 005 (app/changes.py takes the table name).
--
-- Every table carries tenant_id and has row level security enabled and forced, with the policy every other tenant table has. The sync worker role may cross tenants (it delivers the outbox).
-- MONEY is an integer in minor units with its currency beside it, as in 001.
--
-- Safe to run twice.

create table if not exists inventory_supplier (
    id                uuid        primary key default gen_random_uuid(),
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    name              text        not null check (length(name) between 1 and 120),
    lead_time_days    integer     not null check (lead_time_days between 0 and 365),
    lead_time_sd_days double precision not null default 0 check (lead_time_sd_days >= 0),
    note              text,
    created_at        timestamptz not null default now(),
    unique (tenant_id, name)
);

create table if not exists inventory_variant (
    id                 uuid        primary key default gen_random_uuid(),
    tenant_id          uuid        not null references tenant (id) on delete cascade,
    variant_gid        text        not null check (variant_gid like 'gid://shopify/ProductVariant/%'),
    inventory_item_gid text,
    product_gid        text,
    product_title      text        not null,
    variant_title      text        not null default '',
    sku                text,
    -- an untracked variant has no quantity to plan; it stays in the table so its sales are still counted
    tracked            boolean     not null default true,
    unit_cost_minor    bigint      check (unit_cost_minor is null or unit_cost_minor >= 0),
    currency           char(3),
    supplier_id        uuid        references inventory_supplier (id) on delete set null,
    moq                integer     not null default 1 check (moq >= 1),
    pack_size          integer     not null default 1 check (pack_size >= 1),
    updated_at         timestamptz not null default now(),
    unique (tenant_id, variant_gid),
    constraint cost_needs_currency check (unit_cost_minor is null or currency is not null)
);

create index if not exists idx_inventory_variant_supplier on inventory_variant (tenant_id, supplier_id);

create table if not exists inventory_snapshot (
    id            bigserial   primary key,
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    variant_id    uuid        not null references inventory_variant (id) on delete cascade,
    observed_on   date        not null,
    available     bigint      not null,
    on_hand       bigint,
    committed     bigint,
    incoming      bigint      not null default 0 check (incoming >= 0),
    -- each location's own quantities as read: [{"location": gid, "name": text, "available": n, "on_hand": n, "committed": n, "incoming": n}]
    levels        jsonb       not null default '[]'::jsonb,
    synced_at     timestamptz not null default now(),
    -- a second sync on the same day corrects that day's row instead of adding one
    unique (variant_id, observed_on)
);

create index if not exists idx_inventory_snapshot_read on inventory_snapshot (tenant_id, observed_on desc);

create table if not exists inventory_order_line (
    id                bigserial   primary key,
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    order_gid         text        not null,
    line_gid          text        not null,
    variant_id        uuid        references inventory_variant (id) on delete set null,
    variant_gid       text,
    sku               text,
    processed_on      date        not null,
    quantity          integer     not null check (quantity >= 0),
    net_revenue_minor bigint      not null check (net_revenue_minor >= 0),
    currency          char(3)     not null,
    synced_at         timestamptz not null default now(),
    unique (tenant_id, line_gid)
);

create index if not exists idx_inventory_order_line_variant on inventory_order_line (tenant_id, variant_id, processed_on);
create index if not exists idx_inventory_order_line_order on inventory_order_line (tenant_id, order_gid);

create table if not exists inventory_promo (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    -- null means every variant
    variant_id  uuid        references inventory_variant (id) on delete cascade,
    starts_on   date        not null,
    ends_on     date        not null,
    -- 1.5 means a promotion expected to sell half as much again as a normal day; below 1 is allowed (a quiet period) and 0 is not
    factor      double precision not null check (factor > 0 and factor <= 20),
    note        text,
    entered_by  text        not null check (length(entered_by) between 1 and 120),
    created_at  timestamptz not null default now(),
    constraint promo_dates_in_order check (ends_on >= starts_on),
    constraint promo_is_bounded check (ends_on - starts_on <= 366)
);

create index if not exists idx_inventory_promo_window on inventory_promo (tenant_id, starts_on, ends_on);

create table if not exists inventory_plan_run (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    as_of       date        not null,
    params      jsonb       not null,
    variants    integer     not null,
    created_at  timestamptz not null default now(),
    unique (tenant_id, as_of)
);

create table if not exists inventory_plan_line (
    id                 bigserial   primary key,
    tenant_id          uuid        not null references tenant (id) on delete cascade,
    run_id             uuid        not null references inventory_plan_run (id) on delete cascade,
    variant_id         uuid        not null references inventory_variant (id) on delete cascade,
    product_title      text        not null,
    variant_title      text        not null,
    sku                text,
    supplier_id        uuid,
    supplier_name      text,
    status             text        not null check (status in ('ok', 'no_supplier', 'untracked', 'no_stock_reading')),
    -- the day of the snapshot the stock figures come from: a plan made from a stale reading says so
    stock_read_on      date,
    available          bigint      not null,
    incoming           bigint      not null default 0,
    -- the stockout adjusted daily velocity over the recent window and what it was counted over
    velocity           double precision not null check (velocity >= 0),
    velocity_sd        double precision not null check (velocity_sd >= 0),
    counted_days       integer     not null check (counted_days >= 0),
    out_days           integer     not null check (out_days >= 0),
    unknown_days       integer     not null check (unknown_days >= 0),
    window_days        integer     not null check (window_days > 0),
    units_recent       integer     not null check (units_recent >= 0),
    units_prior        integer     not null check (units_prior >= 0),
    units_all          bigint      not null check (units_all >= 0),
    trend_ratio        double precision,
    revenue_minor      bigint      not null check (revenue_minor >= 0),
    currency           char(3),
    cover_days         double precision,
    forecast_cover_days double precision,
    stockout_date      date,
    promo_factor       double precision not null default 1 check (promo_factor > 0),
    lead_time_days     integer,
    lead_time_sd_days  double precision,
    safety_stock       integer,
    reorder_point      integer,
    needs_reorder      boolean     not null default false,
    suggested_qty      integer     not null default 0 check (suggested_qty >= 0),
    unit_cost_minor    bigint,
    abc_class          char(1)     not null check (abc_class in ('A', 'B', 'C')),
    label              text        not null check (label in ('best_seller', 'rising', 'slow_mover', 'steady')),
    -- the shortage this line shows, decided once in app/inventory/alerts.py: the alerts and the dashboard both read it
    shortage           text        check (shortage in ('out_of_stock', 'cover_below_lead_time', 'stockout_projected')),
    unique (run_id, variant_id)
);

create index if not exists idx_inventory_plan_line_run on inventory_plan_line (tenant_id, run_id);

create table if not exists inventory_alert (
    id            bigint generated always as identity primary key,
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    variant_id    uuid        not null references inventory_variant (id) on delete cascade,
    kind          text        not null check (kind in ('out_of_stock', 'cover_below_lead_time', 'stockout_projected')),
    -- the stock cycle the shortage belongs to: the date of the last restock, or 'start' when no restock was ever seen
    event_ref     text        not null,
    as_of         date        not null,
    detail        jsonb       not null,
    created_at    timestamptz not null default now(),
    constraint inventory_alert_natural_key unique (tenant_id, variant_id, kind, event_ref)
);

create index if not exists idx_inventory_alert_recent on inventory_alert (tenant_id, id desc);

create table if not exists inventory_alert_outbox (
    event_id         bigint primary key references inventory_alert (id),
    tenant_id        uuid   not null references tenant (id) on delete cascade,
    idempotency_key  text   not null unique,
    payload          jsonb  not null,
    status           text   not null default 'pending' check (status in ('pending', 'delivered', 'dead')),
    attempts         integer not null default 0,
    requeued_count   integer not null default 0,
    next_attempt_at  timestamptz not null default now(),
    claim_token      uuid,
    lease_expires_at timestamptz,
    last_attempt_at  timestamptz,
    last_outcome     text,
    sink_ref         text,
    delivered_at     timestamptz,
    created_at       timestamptz not null default now()
);

create index if not exists idx_inventory_alert_outbox_due on inventory_alert_outbox (next_attempt_at) where status = 'pending';

-- ---------------------------------------------------------------- append only

create or replace function inventory_forbid_rewrite() returns trigger language plpgsql as $$
begin
    raise exception '% is append only: % refused', tg_table_name, tg_op;
end
$$;

drop trigger if exists inventory_alert_append_only on inventory_alert;
create trigger inventory_alert_append_only before update or delete on inventory_alert
    for each row execute function inventory_forbid_rewrite();
drop trigger if exists inventory_alert_no_truncate on inventory_alert;
create trigger inventory_alert_no_truncate before truncate on inventory_alert
    for each statement execute function inventory_forbid_rewrite();

-- ---------------------------------------------------------------- row level security, enabled and forced on every table

alter table inventory_supplier      enable row level security;
alter table inventory_variant       enable row level security;
alter table inventory_snapshot      enable row level security;
alter table inventory_order_line    enable row level security;
alter table inventory_promo         enable row level security;
alter table inventory_plan_run      enable row level security;
alter table inventory_plan_line     enable row level security;
alter table inventory_alert         enable row level security;
alter table inventory_alert_outbox  enable row level security;

alter table inventory_supplier      force row level security;
alter table inventory_variant       force row level security;
alter table inventory_snapshot      force row level security;
alter table inventory_order_line    force row level security;
alter table inventory_promo         force row level security;
alter table inventory_plan_run      force row level security;
alter table inventory_plan_line     force row level security;
alter table inventory_alert         force row level security;
alter table inventory_alert_outbox  force row level security;

drop policy if exists inventory_supplier_by_tenant on inventory_supplier;
create policy inventory_supplier_by_tenant on inventory_supplier
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_variant_by_tenant on inventory_variant;
create policy inventory_variant_by_tenant on inventory_variant
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_snapshot_by_tenant on inventory_snapshot;
create policy inventory_snapshot_by_tenant on inventory_snapshot
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_order_line_by_tenant on inventory_order_line;
create policy inventory_order_line_by_tenant on inventory_order_line
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_promo_by_tenant on inventory_promo;
create policy inventory_promo_by_tenant on inventory_promo
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_plan_run_by_tenant on inventory_plan_run;
create policy inventory_plan_run_by_tenant on inventory_plan_run
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_plan_line_by_tenant on inventory_plan_line;
create policy inventory_plan_line_by_tenant on inventory_plan_line
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_alert_by_tenant on inventory_alert;
create policy inventory_alert_by_tenant on inventory_alert
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_alert_outbox_by_tenant on inventory_alert_outbox;
create policy inventory_alert_outbox_by_tenant on inventory_alert_outbox
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- The delivery worker crosses tenants by design (it services all of them), so it gets its own policy on the two alert tables, as in 001.
drop policy if exists inventory_alert_worker on inventory_alert;
create policy inventory_alert_worker on inventory_alert to sync_worker using (true) with check (true);
drop policy if exists inventory_alert_outbox_worker on inventory_alert_outbox;
create policy inventory_alert_outbox_worker on inventory_alert_outbox to sync_worker using (true) with check (true);

-- ---------------------------------------------------------------- grants

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new tables owe their own.
-- The request role keeps the tables a person edits (suppliers, variants, promotions) and the sync writes, and can read and append alerts, never change one (the trigger above refuses it anyway).
grant select, insert, update, delete on inventory_supplier, inventory_variant, inventory_snapshot, inventory_order_line, inventory_promo, inventory_plan_run, inventory_plan_line to app_request;
grant select, insert on inventory_alert to app_request;
grant select, insert, update on inventory_alert_outbox to app_request;
grant usage on schema public to sync_worker;
grant select on inventory_alert, inventory_alert_outbox to sync_worker;
grant update on inventory_alert_outbox to sync_worker;
grant usage, select on all sequences in schema public to app_request;
grant usage, select on all sequences in schema public to sync_worker;
