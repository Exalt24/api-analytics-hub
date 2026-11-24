-- Purchase order tracking for the inventory plan: what was ordered from a supplier, whether it was confirmed, shipped or received, when it is expected, and an alert once when it is late.
--
-- WHY EACH TABLE IS HERE.
--   inventory_po_transition          the legal status moves, as rows. A trigger refuses any other move, so the rule holds for the Python code, the web page and a hand written UPDATE alike.
--   inventory_purchase_order         one order to one supplier: its status, the dates (ordered, confirmed, shipped, expected, closed) and the lead time it was ordered under.
--   inventory_purchase_order_line    one variant on the order: the quantity ordered, the quantity received so far, the unit cost in integer minor units with its currency.
--   inventory_po_event               what happened to an order, append only: its creation, each status move, each change of the expected date, each receipt, with who did it.
--   inventory_po_alert               one row per late order. UNIQUE (tenant, order, kind): an order is reported late once, however often the check runs.
--   inventory_po_alert_outbox        the alerts waiting to go out, the same shape and the same delivery code as inventory_alert_outbox in 020 (app/changes.py takes the table name).
--
-- STATUSES. draft, ordered, confirmed, shipped, partly_received, received, cancelled. A move to partly_received or received is made only by recording a receipt (inventory_po_receive),
-- never by hand, and `received` needs every line received in full. A cancelled or received order never moves again.
-- THE EXPECTED DATE is the day it was ordered plus the supplier's lead time at that moment (an input a person entered), unless one is given; it can be edited while the order is open.
-- MONEY is an integer in minor units with its currency beside it, as in 001. Every table carries tenant_id with row level security enabled and forced; the request role is not the owner.
--
-- Safe to run twice. Needs 020.

-- ---------------------------------------------------------------- the plan line says where its inbound figure came from

alter table inventory_plan_line add column if not exists inbound_source text not null default 'none' check (inbound_source in ('orders', 'shopify', 'none'));
alter table inventory_plan_line add column if not exists inbound_expected_on date;
alter table inventory_plan_line add column if not exists inbound_late boolean not null default false;

-- ---------------------------------------------------------------- the legal moves

create table if not exists inventory_po_transition (
    from_status text not null,
    to_status   text not null,
    primary key (from_status, to_status)
);

insert into inventory_po_transition (from_status, to_status) values
    ('draft', 'ordered'), ('draft', 'cancelled'),
    ('ordered', 'confirmed'), ('ordered', 'shipped'), ('ordered', 'partly_received'), ('ordered', 'received'), ('ordered', 'cancelled'),
    ('confirmed', 'shipped'), ('confirmed', 'partly_received'), ('confirmed', 'received'), ('confirmed', 'cancelled'),
    ('shipped', 'partly_received'), ('shipped', 'received'),
    ('partly_received', 'received'), ('partly_received', 'cancelled')
on conflict do nothing;

-- ---------------------------------------------------------------- orders and lines

create table if not exists inventory_purchase_order (
    id             uuid        primary key default gen_random_uuid(),
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    supplier_id    uuid        references inventory_supplier (id) on delete set null,
    -- the name at the time, so an old order does not change when a supplier is renamed
    supplier_name  text        not null,
    status         text        not null default 'draft' check (status in ('draft', 'ordered', 'confirmed', 'shipped', 'partly_received', 'received', 'cancelled')),
    currency       char(3),
    -- the supplier's lead time when the order was placed
    lead_time_days integer     check (lead_time_days is null or lead_time_days between 0 and 365),
    plan_as_of     date,
    ordered_on     date,
    expected_on    date,
    confirmed_on   date,
    shipped_on     date,
    closed_on      date,
    note           text,
    created_by     text        not null check (length(created_by) between 1 and 120),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint po_placed_has_a_date check (status in ('draft', 'cancelled') or ordered_on is not null),
    constraint po_expected_not_before_ordered check (expected_on is null or ordered_on is null or expected_on >= ordered_on)
);

create index if not exists idx_inventory_po_status on inventory_purchase_order (tenant_id, status);
create index if not exists idx_inventory_po_supplier on inventory_purchase_order (tenant_id, supplier_id);

create table if not exists inventory_purchase_order_line (
    id                bigserial   primary key,
    tenant_id         uuid        not null references tenant (id) on delete cascade,
    order_id          uuid        not null references inventory_purchase_order (id) on delete cascade,
    variant_id        uuid        references inventory_variant (id) on delete set null,
    sku               text,
    description       text        not null,
    quantity_ordered  integer     not null check (quantity_ordered > 0),
    quantity_received integer     not null default 0 check (quantity_received >= 0),
    -- true only when more than was ordered arrived and a person said to accept it
    over_received     boolean     not null default false,
    unit_cost_minor   bigint      check (unit_cost_minor is null or unit_cost_minor >= 0),
    currency          char(3),
    constraint po_line_cost_needs_currency check (unit_cost_minor is null or currency is not null),
    constraint po_line_received_within_ordered check (quantity_received <= quantity_ordered or over_received),
    constraint po_line_over_flag_is_true check (not over_received or quantity_received > quantity_ordered),
    unique (order_id, variant_id)
);

create index if not exists idx_inventory_po_line_variant on inventory_purchase_order_line (tenant_id, variant_id);
create index if not exists idx_inventory_po_line_order on inventory_purchase_order_line (order_id);

create table if not exists inventory_po_event (
    id          bigint generated always as identity primary key,
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    order_id    uuid        not null references inventory_purchase_order (id) on delete cascade,
    kind        text        not null check (kind in ('created', 'status', 'expected_on', 'receipt')),
    from_status text,
    to_status   text,
    detail      jsonb       not null default '{}'::jsonb,
    by_whom     text        not null,
    created_at  timestamptz not null default now()
);

create index if not exists idx_inventory_po_event_order on inventory_po_event (order_id, id);

create table if not exists inventory_po_alert (
    id         bigint generated always as identity primary key,
    tenant_id  uuid        not null references tenant (id) on delete cascade,
    order_id   uuid        not null references inventory_purchase_order (id) on delete cascade,
    kind       text        not null check (kind in ('po_late')),
    as_of      date        not null,
    detail     jsonb       not null,
    created_at timestamptz not null default now(),
    constraint inventory_po_alert_natural_key unique (tenant_id, order_id, kind)
);

create table if not exists inventory_po_alert_outbox (
    event_id         bigint primary key references inventory_po_alert (id),
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

create index if not exists idx_inventory_po_alert_outbox_due on inventory_po_alert_outbox (next_attempt_at) where status = 'pending';

-- ---------------------------------------------------------------- the rules, in the database

-- inventory_forbid_rewrite comes from 020. An event and an alert are never rewritten.
drop trigger if exists inventory_po_event_append_only on inventory_po_event;
create trigger inventory_po_event_append_only before update or delete on inventory_po_event
    for each row execute function inventory_forbid_rewrite();
drop trigger if exists inventory_po_event_no_truncate on inventory_po_event;
create trigger inventory_po_event_no_truncate before truncate on inventory_po_event
    for each statement execute function inventory_forbid_rewrite();
drop trigger if exists inventory_po_alert_append_only on inventory_po_alert;
create trigger inventory_po_alert_append_only before update or delete on inventory_po_alert
    for each row execute function inventory_forbid_rewrite();
drop trigger if exists inventory_po_alert_no_truncate on inventory_po_alert;
create trigger inventory_po_alert_no_truncate before truncate on inventory_po_alert
    for each statement execute function inventory_forbid_rewrite();

-- Errors raised here carry their own SQLSTATE so a caller can tell them apart: PO001 an illegal move, PO003 not allowed in the order's status, PO004 more received than ordered,
-- PO005 nothing to order or a draft already open, PO006 a bad quantity, PO404 no such order, line, supplier or plan.
create or replace function inventory_po_guard() returns trigger language plpgsql as $$
declare
    n_lines integer;
    n_done  integer;
    n_part  integer;
begin
    if new.status is distinct from old.status then
        if not exists (select 1 from inventory_po_transition t where t.from_status = old.status and t.to_status = new.status) then
            raise exception 'a purchase order cannot move from % to %', old.status, new.status using errcode = 'PO001';
        end if;
        select count(*), count(*) filter (where quantity_received >= quantity_ordered), count(*) filter (where quantity_received > 0)
          into n_lines, n_done, n_part from inventory_purchase_order_line where order_id = new.id;
        if new.status = 'ordered' and n_lines = 0 then
            raise exception 'an order with no lines cannot be placed' using errcode = 'PO001';
        end if;
        if new.status = 'received' and (n_lines = 0 or n_done < n_lines) then
            raise exception 'an order is received only when every line is received in full' using errcode = 'PO001';
        end if;
        if new.status = 'partly_received' and (n_part = 0 or n_done = n_lines) then
            raise exception 'partly received needs a received quantity and a line still open' using errcode = 'PO001';
        end if;
        if new.status = 'ordered' then
            new.ordered_on := coalesce(new.ordered_on, current_date);
            if new.supplier_id is not null then
                select s.lead_time_days into new.lead_time_days from inventory_supplier s where s.id = new.supplier_id;
            end if;
            if new.lead_time_days is not null then
                new.expected_on := coalesce(new.expected_on, new.ordered_on + new.lead_time_days);
            end if;
        elsif new.status = 'confirmed' then
            new.confirmed_on := coalesce(new.confirmed_on, current_date);
        elsif new.status = 'shipped' then
            new.shipped_on := coalesce(new.shipped_on, current_date);
        elsif new.status in ('received', 'cancelled') then
            new.closed_on := coalesce(new.closed_on, current_date);
        end if;
    elsif new.expected_on is distinct from old.expected_on and old.status not in ('ordered', 'confirmed', 'shipped', 'partly_received') then
        raise exception 'the expected date of a % order cannot be changed', old.status using errcode = 'PO003';
    end if;
    new.updated_at := now();
    return new;
end
$$;

drop trigger if exists inventory_po_guard on inventory_purchase_order;
create trigger inventory_po_guard before update on inventory_purchase_order
    for each row execute function inventory_po_guard();

create or replace function inventory_po_line_guard() returns trigger language plpgsql as $$
declare
    s text;
begin
    select o.status into s from inventory_purchase_order o where o.id = case when tg_op = 'DELETE' then old.order_id else new.order_id end;
    if s is null then
        return case when tg_op = 'DELETE' then old else new end;   -- the order itself is going (a tenant removed), nothing left to protect
    end if;
    if tg_op = 'INSERT' then
        if s <> 'draft' then
            raise exception 'a line can be added only to a draft order, this one is %', s using errcode = 'PO003';
        end if;
        return new;
    elsif tg_op = 'DELETE' then
        if s <> 'draft' then
            raise exception 'a line can be removed only from a draft order, this one is %', s using errcode = 'PO003';
        end if;
        return old;
    end if;
    if (new.quantity_ordered, new.unit_cost_minor, new.currency) is distinct from (old.quantity_ordered, old.unit_cost_minor, old.currency) and s <> 'draft' then
        raise exception 'the quantity and cost of a line can change only on a draft order, this one is %', s using errcode = 'PO003';
    end if;
    if new.quantity_received is distinct from old.quantity_received then
        if s not in ('ordered', 'confirmed', 'shipped', 'partly_received') then
            raise exception 'nothing can be received on a % order', s using errcode = 'PO003';
        end if;
        if new.quantity_received < old.quantity_received then
            raise exception 'a received quantity is never reduced' using errcode = 'PO004';
        end if;
    end if;
    return new;
end
$$;

drop trigger if exists inventory_po_line_guard on inventory_purchase_order_line;
create trigger inventory_po_line_guard before insert or update or delete on inventory_purchase_order_line
    for each row execute function inventory_po_line_guard();

-- ---------------------------------------------------------------- the operations, one place for every client

-- A draft from the stored plan: the variants of one supplier that the latest plan says to order, with the plan's quantities and costs. Refused when nothing is due, when a draft for
-- the supplier is already open (two drafts would order the same goods twice) or when the lines carry two currencies.
create or replace function inventory_po_create_from_plan(p_supplier text, p_by text) returns inventory_purchase_order language plpgsql as $$
declare
    v_tenant uuid := nullif(current_setting('app.tenant_id', true), '')::uuid;
    v_run uuid;
    v_as_of date;
    v_supplier uuid;
    v_lead integer;
    v_currencies text[];
    v_n integer;
    o inventory_purchase_order;
begin
    if v_tenant is null then
        raise exception 'no tenant is set' using errcode = 'PO404';
    end if;
    select r.id, r.as_of into v_run, v_as_of from inventory_plan_run r order by r.as_of desc limit 1;
    if v_run is null then
        raise exception 'no inventory plan is stored yet' using errcode = 'PO404';
    end if;
    select s.id, s.lead_time_days into v_supplier, v_lead from inventory_supplier s where s.name = p_supplier;
    if v_supplier is null then
        raise exception 'no supplier named %', p_supplier using errcode = 'PO404';
    end if;
    if exists (select 1 from inventory_purchase_order x where x.supplier_id = v_supplier and x.status = 'draft') then
        raise exception 'a draft order for % is already open: place it or cancel it first', p_supplier using errcode = 'PO005';
    end if;
    select count(*), array_agg(distinct l.currency::text) filter (where l.currency is not null) into v_n, v_currencies
      from inventory_plan_line l where l.run_id = v_run and l.supplier_name = p_supplier and l.status = 'ok' and l.needs_reorder and l.suggested_qty > 0;
    if v_n = 0 then
        raise exception 'the plan has nothing to order from %', p_supplier using errcode = 'PO005';
    end if;
    if coalesce(array_length(v_currencies, 1), 0) > 1 then
        raise exception 'the lines for % carry more than one currency', p_supplier using errcode = 'PO005';
    end if;
    insert into inventory_purchase_order (tenant_id, supplier_id, supplier_name, currency, lead_time_days, plan_as_of, created_by)
        values (v_tenant, v_supplier, p_supplier, v_currencies[1], v_lead, v_as_of, p_by) returning * into o;
    insert into inventory_purchase_order_line (tenant_id, order_id, variant_id, sku, description, quantity_ordered, unit_cost_minor, currency)
        select v_tenant, o.id, l.variant_id, l.sku, case when l.variant_title in ('', 'Default Title') then l.product_title else l.product_title || ' - ' || l.variant_title end,
               l.suggested_qty, l.unit_cost_minor, case when l.unit_cost_minor is null then null else coalesce(l.currency, v_currencies[1]) end
          from inventory_plan_line l where l.run_id = v_run and l.supplier_name = p_supplier and l.status = 'ok' and l.needs_reorder and l.suggested_qty > 0 order by l.sku, l.id;
    insert into inventory_po_event (tenant_id, order_id, kind, to_status, detail, by_whom)
        values (v_tenant, o.id, 'created', 'draft', jsonb_build_object('plan_as_of', v_as_of, 'lines', v_n), p_by);
    return o;
end
$$;

-- Move an order to another status. The trigger above refuses an illegal move; this refuses the two moves only a receipt may make.
create or replace function inventory_po_move(p_order uuid, p_to text, p_by text, p_on date default null, p_expected date default null, p_note text default null)
returns inventory_purchase_order language plpgsql as $$
declare
    o inventory_purchase_order;
    v_from text;
begin
    select * into o from inventory_purchase_order where id = p_order for update;
    if not found then
        raise exception 'no such purchase order' using errcode = 'PO404';
    end if;
    if p_to in ('partly_received', 'received') then
        raise exception 'record a receipt instead: a receipt moves an order to partly received or received' using errcode = 'PO001';
    end if;
    v_from := o.status;
    if p_to = v_from then
        raise exception 'a purchase order cannot move from % to %', v_from, p_to using errcode = 'PO001';
    end if;
    update inventory_purchase_order set
            status = p_to,
            ordered_on = case when p_to = 'ordered' then coalesce(p_on, ordered_on) else ordered_on end,
            confirmed_on = case when p_to = 'confirmed' then coalesce(p_on, confirmed_on) else confirmed_on end,
            shipped_on = case when p_to = 'shipped' then coalesce(p_on, shipped_on) else shipped_on end,
            closed_on = case when p_to = 'cancelled' then coalesce(p_on, closed_on) else closed_on end,
            expected_on = coalesce(p_expected, expected_on),
            note = coalesce(p_note, note)
      where id = p_order returning * into o;
    insert into inventory_po_event (tenant_id, order_id, kind, from_status, to_status, detail, by_whom)
        values (o.tenant_id, o.id, 'status', v_from, o.status, jsonb_build_object('expected_on', o.expected_on, 'on', p_on), p_by);
    return o;
end
$$;

-- Change the expected arrival date of an open order.
create or replace function inventory_po_set_expected(p_order uuid, p_expected date, p_by text) returns inventory_purchase_order language plpgsql as $$
declare
    o inventory_purchase_order;
    v_old date;
begin
    select * into o from inventory_purchase_order where id = p_order for update;
    if not found then
        raise exception 'no such purchase order' using errcode = 'PO404';
    end if;
    if o.status not in ('ordered', 'confirmed', 'shipped', 'partly_received') then
        raise exception 'the expected date of a % order cannot be changed', o.status using errcode = 'PO003';
    end if;
    v_old := o.expected_on;
    update inventory_purchase_order set expected_on = p_expected where id = p_order returning * into o;
    insert into inventory_po_event (tenant_id, order_id, kind, detail, by_whom)
        values (o.tenant_id, o.id, 'expected_on', jsonb_build_object('from', v_old, 'to', o.expected_on), p_by);
    return o;
end
$$;

-- Record units received on one line. More than was ordered is refused unless p_allow_over says to accept it, and then the line is flagged. The order becomes received when every line
-- is in full, otherwise partly received.
create or replace function inventory_po_receive(p_order uuid, p_line bigint, p_qty integer, p_allow_over boolean, p_by text, p_on date default null)
returns inventory_purchase_order language plpgsql as $$
declare
    o inventory_purchase_order;
    l inventory_purchase_order_line;
    v_from text;
    v_total integer;
    v_open integer;
begin
    if p_qty is null or p_qty <= 0 then
        raise exception 'a received quantity must be above zero' using errcode = 'PO006';
    end if;
    select * into o from inventory_purchase_order where id = p_order for update;
    if not found then
        raise exception 'no such purchase order' using errcode = 'PO404';
    end if;
    if o.status not in ('ordered', 'confirmed', 'shipped', 'partly_received') then
        raise exception 'nothing can be received on a % order', o.status using errcode = 'PO003';
    end if;
    select * into l from inventory_purchase_order_line where id = p_line and order_id = p_order for update;
    if not found then
        raise exception 'no such line on this order' using errcode = 'PO404';
    end if;
    v_total := l.quantity_received + p_qty;
    if v_total > l.quantity_ordered and not coalesce(p_allow_over, false) then
        raise exception 'receiving % would bring % to % of % ordered: say to accept more than was ordered', p_qty, coalesce(l.sku, l.description), v_total, l.quantity_ordered using errcode = 'PO004';
    end if;
    update inventory_purchase_order_line set quantity_received = v_total, over_received = v_total > quantity_ordered where id = p_line;
    insert into inventory_po_event (tenant_id, order_id, kind, detail, by_whom)
        values (o.tenant_id, o.id, 'receipt', jsonb_build_object('line_id', p_line, 'sku', l.sku, 'quantity', p_qty, 'total_received', v_total, 'ordered', l.quantity_ordered, 'on', p_on), p_by);
    select count(*) into v_open from inventory_purchase_order_line where order_id = p_order and quantity_received < quantity_ordered;
    v_from := o.status;
    if v_open = 0 then
        update inventory_purchase_order set status = 'received', closed_on = coalesce(p_on, closed_on) where id = p_order returning * into o;
    elsif v_from <> 'partly_received' then
        update inventory_purchase_order set status = 'partly_received' where id = p_order returning * into o;
    else
        update inventory_purchase_order set updated_at = now() where id = p_order returning * into o;
    end if;
    if o.status <> v_from then
        insert into inventory_po_event (tenant_id, order_id, kind, from_status, to_status, detail, by_whom)
            values (o.tenant_id, o.id, 'status', v_from, o.status, jsonb_build_object('by_receipt', true, 'on', p_on), p_by);
    end if;
    return o;
end
$$;

-- ---------------------------------------------------------------- row level security, enabled and forced on every table

alter table inventory_purchase_order       enable row level security;
alter table inventory_purchase_order_line  enable row level security;
alter table inventory_po_event             enable row level security;
alter table inventory_po_alert             enable row level security;
alter table inventory_po_alert_outbox      enable row level security;

alter table inventory_purchase_order       force row level security;
alter table inventory_purchase_order_line  force row level security;
alter table inventory_po_event             force row level security;
alter table inventory_po_alert             force row level security;
alter table inventory_po_alert_outbox      force row level security;

drop policy if exists inventory_purchase_order_by_tenant on inventory_purchase_order;
create policy inventory_purchase_order_by_tenant on inventory_purchase_order
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_purchase_order_line_by_tenant on inventory_purchase_order_line;
create policy inventory_purchase_order_line_by_tenant on inventory_purchase_order_line
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_po_event_by_tenant on inventory_po_event;
create policy inventory_po_event_by_tenant on inventory_po_event
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_po_alert_by_tenant on inventory_po_alert;
create policy inventory_po_alert_by_tenant on inventory_po_alert
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists inventory_po_alert_outbox_by_tenant on inventory_po_alert_outbox;
create policy inventory_po_alert_outbox_by_tenant on inventory_po_alert_outbox
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- The delivery worker crosses tenants by design (it services all of them), as for the shortage alerts in 020.
drop policy if exists inventory_po_alert_worker on inventory_po_alert;
create policy inventory_po_alert_worker on inventory_po_alert to sync_worker using (true) with check (true);
drop policy if exists inventory_po_alert_outbox_worker on inventory_po_alert_outbox;
create policy inventory_po_alert_outbox_worker on inventory_po_alert_outbox to sync_worker using (true) with check (true);

-- ---------------------------------------------------------------- grants

-- The request role is not the owner. It reads and moves orders (the triggers hold the rules), adds lines only to a draft (the line trigger), appends events and alerts and never rewrites one.
-- An order is never deleted: a cancelled order stays as the record.
grant select on inventory_po_transition to app_request;
grant select, insert, update on inventory_purchase_order to app_request;
grant select, insert, update, delete on inventory_purchase_order_line to app_request;
grant select, insert on inventory_po_event to app_request;
grant select, insert on inventory_po_alert to app_request;
grant select, insert, update on inventory_po_alert_outbox to app_request;
grant usage on schema public to sync_worker;
grant select on inventory_po_alert, inventory_po_alert_outbox to sync_worker;
grant update on inventory_po_alert_outbox to sync_worker;
grant usage, select on all sequences in schema public to app_request;
grant usage, select on all sequences in schema public to sync_worker;
