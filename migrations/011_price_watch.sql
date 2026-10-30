-- Competitor price monitoring: our catalogue, the competitor pages we watch, what each scan saw, the shipping rate table,
-- the daily report, and a queue of proposed price changes that a person approves before anything is sent to a shop.
--
-- WHY EACH TABLE IS HERE.
--   catalogue_product              our own rows: price, the floor a proposal may never go below, weight for shipping estimates,
--                                  and the ids the two shops use for the same product.
--   competitor_listing             one competitor page, and how it matched (or did not match) a catalogue row.
--   competitor_price_observation   one scan of one page on one day. It keeps what the page STATED about shipping, and nothing
--                                  else about shipping: an estimate is worked out when the report is built and labelled there.
--   shipping_rate                  the tenant's own rate table: weight band and destination zone to a cost.
--   price_watch_report             one stored report for a day, with its markdown and CSV renders.
--   price_change_proposal          the review queue. States: proposed, approved, rejected, applied, failed. The sync keeps one
--                                  status for each shop, so after a partial failure only the failed side is tried again.
--   price_change_audit             one row for every state change, with who and when. Append only: the request role can read
--                                  and insert it and nothing else.
--
-- MONEY is an integer in minor units with its currency beside it, like the rest of the hub (see 001). A proposal can never
-- go below its floor or above the price it replaces: both are CHECK constraints here as well as in the Python, because a
-- later script or a manual insert skips the Python.
--
-- Row level security is on and forced on every table, with the same policy as every other tenant table. The contract the sync
-- adapters follow is taken from the vendors' official pages (docs/vendor-contract/) and has never been run against a live
-- Shopify or Zoho account.
--
-- Safe to run twice.

create table if not exists catalogue_product (
    id                    uuid        primary key default gen_random_uuid(),
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    sku                   text        not null check (length(sku) between 1 and 80),
    gtin                  text        check (gtin is null or gtin ~ '^[0-9]{8}$|^[0-9]{12,14}$'),
    brand                 text,
    title                 text        not null check (length(title) between 1 and 300),
    price_minor           bigint      not null check (price_minor > 0),
    currency              char(3)     not null,
    -- the lowest price a proposal may ever name for this product
    floor_minor           bigint      not null check (floor_minor >= 0),
    -- what we charge for shipping, so our landed price is comparable with a competitor's
    our_shipping_minor    bigint      not null default 0 check (our_shipping_minor >= 0),
    weight_grams          integer     check (weight_grams is null or weight_grams > 0),
    shopify_product_gid   text,
    shopify_variant_gid   text,
    zoho_item_id          text,
    zoho_item_name        text,
    created_at            timestamptz not null default now(),
    unique (tenant_id, sku),
    constraint shopify_ids_come_together check ((shopify_product_gid is null) = (shopify_variant_gid is null)),
    constraint zoho_update_needs_a_name check (zoho_item_id is null or zoho_item_name is not null)
);

create table if not exists competitor_listing (
    id                    uuid        primary key default gen_random_uuid(),
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    competitor            text        not null check (length(competitor) between 1 and 120),
    url                   text        not null check (url ~ '^https?://'),
    ship_zone             text        not null default 'domestic',
    catalogue_product_id  uuid        references catalogue_product (id) on delete set null,
    -- matched is automatic (GTIN, SKU or a title score over the auto threshold), confirmed is a person's decision, and
    -- needs_review is every match that was not certain enough. Only matched and confirmed ever reach a report.
    match_status          text        not null default 'unmatched'
                          check (match_status in ('unmatched', 'matched', 'confirmed', 'needs_review', 'no_match')),
    match_method          text        check (match_method is null or match_method in ('gtin', 'sku', 'title', 'manual')),
    match_score           numeric(4,3) check (match_score is null or match_score between 0 and 1),
    match_reason          text,
    seen_title            text,
    seen_sku              text,
    seen_gtin             text,
    last_scanned_at       timestamptz,
    created_at            timestamptz not null default now(),
    unique (tenant_id, url),
    constraint matched_has_a_product check (match_status not in ('matched', 'confirmed') or catalogue_product_id is not null)
);

create index if not exists idx_competitor_listing_product on competitor_listing (tenant_id, catalogue_product_id);

create table if not exists competitor_price_observation (
    id                    uuid        primary key default gen_random_uuid(),
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    listing_id            uuid        not null references competitor_listing (id) on delete cascade,
    observed_on           date        not null,
    observed_at           timestamptz not null default now(),
    price_minor           bigint      check (price_minor is null or price_minor >= 0),
    original_price_minor  bigint      check (original_price_minor is null or original_price_minor >= 0),
    currency              char(3),
    availability          text        not null default 'unknown' check (availability in ('in_stock', 'out_of_stock', 'unknown')),
    -- what the page said, never an estimate: null when it said nothing (or said "calculated at checkout"), 0 for free
    shipping_minor        bigint      check (shipping_minor is null or shipping_minor >= 0),
    shipping_text         text,
    problem               text,
    -- a price without a currency cannot be compared with anything
    constraint price_needs_currency check (price_minor is null or currency is not null),
    -- a re-scan on the same day replaces that day's row
    unique (listing_id, observed_on)
);

create table if not exists shipping_rate (
    id                    uuid        primary key default gen_random_uuid(),
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    destination_zone      text        not null,
    weight_from_g         integer     not null check (weight_from_g >= 0),
    -- the band is weight_from_g <= weight < weight_to_g
    weight_to_g           integer     not null,
    cost_minor            bigint      not null check (cost_minor >= 0),
    currency              char(3)     not null,
    unique (tenant_id, destination_zone, currency, weight_from_g),
    constraint band_is_not_empty check (weight_to_g > weight_from_g)
);

create table if not exists price_watch_report (
    id                    uuid        primary key default gen_random_uuid(),
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    report_date           date        not null,
    lines                 integer     not null,
    needs_review          integer     not null,
    summary               jsonb       not null,
    markdown              text        not null,
    csv                   text        not null,
    created_at            timestamptz not null default now(),
    unique (tenant_id, report_date)
);

create table if not exists price_change_proposal (
    id                      uuid        primary key default gen_random_uuid(),
    tenant_id               uuid        not null references tenant (id) on delete cascade,
    catalogue_product_id    uuid        not null references catalogue_product (id) on delete cascade,
    report_date             date        not null,
    currency                char(3)     not null,
    current_price_minor     bigint      not null check (current_price_minor > 0),
    proposed_price_minor    bigint      not null,
    -- the floor when the proposal was made; the sync checks the product's floor again
    floor_minor             bigint      not null check (floor_minor >= 0),
    competitor              text        not null,
    competitor_landed_minor bigint      not null,
    -- stated is the competitor's own shipping figure, estimated is ours from the rate table
    shipping_source         text        not null check (shipping_source in ('stated', 'estimated')),
    evidence                jsonb       not null default '{}'::jsonb,
    state                   text        not null default 'proposed'
                            check (state in ('proposed', 'approved', 'rejected', 'applied', 'failed')),
    -- derived from the id and never reused, so a retry that reaches a shop twice carries the same key
    idempotency_key         text        not null,
    decided_by              text,
    decided_at              timestamptz,
    decision_note           text,
    shopify_status          text        not null default 'pending' check (shopify_status in ('pending', 'applied', 'failed', 'skipped')),
    shopify_error           text,
    zoho_status             text        not null default 'pending' check (zoho_status in ('pending', 'applied', 'failed', 'skipped')),
    zoho_error              text,
    sync_attempts           integer     not null default 0,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    unique (tenant_id, idempotency_key),
    constraint proposal_respects_the_floor check (proposed_price_minor >= floor_minor),
    constraint proposal_is_a_reduction check (proposed_price_minor < current_price_minor),
    constraint decided_has_an_actor check (state = 'proposed' or decided_by is not null),
    constraint applied_means_every_side_done check (
        state <> 'applied' or (shopify_status in ('applied', 'skipped') and zoho_status in ('applied', 'skipped')))
);

-- one open proposal for a product at a time, so a second report does not stack another on top of the one under review
create unique index if not exists idx_one_open_proposal
    on price_change_proposal (tenant_id, catalogue_product_id) where state in ('proposed', 'approved', 'failed');

create table if not exists price_change_audit (
    id                    bigserial   primary key,
    tenant_id             uuid        not null references tenant (id) on delete cascade,
    proposal_id           uuid        not null references price_change_proposal (id) on delete cascade,
    from_state            text,
    to_state              text        not null,
    actor                 text        not null check (length(actor) between 1 and 120),
    at                    timestamptz not null default now(),
    note                  text
);

create index if not exists idx_price_change_audit_proposal on price_change_audit (tenant_id, proposal_id, id);

-- ---------------------------------------------------------------- row level security, enabled and forced on every table

alter table catalogue_product             enable row level security;
alter table competitor_listing            enable row level security;
alter table competitor_price_observation  enable row level security;
alter table shipping_rate                 enable row level security;
alter table price_watch_report            enable row level security;
alter table price_change_proposal         enable row level security;
alter table price_change_audit            enable row level security;

alter table catalogue_product             force row level security;
alter table competitor_listing            force row level security;
alter table competitor_price_observation  force row level security;
alter table shipping_rate                 force row level security;
alter table price_watch_report            force row level security;
alter table price_change_proposal         force row level security;
alter table price_change_audit            force row level security;

drop policy if exists catalogue_product_by_tenant on catalogue_product;
create policy catalogue_product_by_tenant on catalogue_product
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists competitor_listing_by_tenant on competitor_listing;
create policy competitor_listing_by_tenant on competitor_listing
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists competitor_price_observation_by_tenant on competitor_price_observation;
create policy competitor_price_observation_by_tenant on competitor_price_observation
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists shipping_rate_by_tenant on shipping_rate;
create policy shipping_rate_by_tenant on shipping_rate
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists price_watch_report_by_tenant on price_watch_report;
create policy price_watch_report_by_tenant on price_watch_report
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists price_change_proposal_by_tenant on price_change_proposal;
create policy price_change_proposal_by_tenant on price_change_proposal
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists price_change_audit_by_tenant on price_change_audit;
create policy price_change_audit_by_tenant on price_change_audit
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new tables owe their own.
-- Nothing is deleted by the request role. The audit table can be read and appended to, never changed.
grant select, insert, update on catalogue_product, competitor_listing, competitor_price_observation,
      shipping_rate, price_watch_report, price_change_proposal to app_request;
grant select, insert on price_change_audit to app_request;
grant usage, select on all sequences in schema public to app_request;
