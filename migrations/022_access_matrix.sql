-- Four role access matrix for an ordering portal: clinics, prescribers, sales reps and admins, over orders, invoices, payments and commissions.
--
-- THE ONE TABLE. `access_matrix` holds a row for every (role, resource, action): 4 roles x 4 resources x 5 actions = 80 rows, each with a scope (none, all, clinic, assigned,
-- self). The same table is written once in code, app/access/matrix.py, and the rows below are generated from it (matrix.seed_sql(); a test fails if this file drifts). The row level
-- security policies of 023 and 024 read the table at query time through portal_scope() and portal_allows(), so changing a cell here changes the database and nothing else has to be edited.
--
-- THE SESSION. Three transaction local settings say who is asking, all set by the connection layer (Database.session) from the caller's credential and never from a request body:
--   app.tenant_id  the tenant, as everywhere else in this schema
--   app.role       clinic, prescriber, sales_rep or admin
--   app.user_id    the portal_user's id
-- With no role set every portal table answers nothing: the matrix fails closed. (billing_payment alone also keeps the older tenant wide behaviour when NO role is set, because the
-- wallet engine and its tests use that table without a portal user; see 023.)
--
-- WHAT EACH TABLE IS FOR.
--   clinic                a clinic of the tenant. `code` is an opaque short code the partner may be given in place of a name.
--   portal_user           a person: their role and, for a clinic user or a prescriber, their clinic. A user sees only their own row; an admin sees all.
--   portal_credential     the API key of a portal user, stored as a SHA-256 hash (like api_key, it is read before the tenant is known, so it has no row level security).
--   sales_rep_assignment  which clinic a sales rep covers, and the commission rate in basis points. A clinic has at most one rep. Only an admin writes it.
--   access_matrix         the matrix. Global (no tenant data), read only for the request role.
--
-- Safe to run twice. Needs 001 (tenant, app_request).

create table if not exists clinic (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    code        text        not null check (code ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'),
    name        text        not null check (length(btrim(name)) between 1 and 200),
    created_at  timestamptz not null default now(),
    unique (tenant_id, code),
    unique (id, tenant_id)
);

create table if not exists portal_user (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    role        text        not null check (role in ('clinic', 'prescriber', 'sales_rep', 'admin')),
    clinic_id   uuid,
    label       text        not null check (length(btrim(label)) between 1 and 120),
    created_at  timestamptz not null default now(),
    unique (id, tenant_id),
    foreign key (clinic_id, tenant_id) references clinic (id, tenant_id),
    -- a clinic user and a prescriber belong to a clinic; a rep and an admin do not
    constraint clinic_roles_have_a_clinic check ((role in ('clinic', 'prescriber')) = (clinic_id is not null))
);

create table if not exists portal_credential (
    id          uuid        primary key default gen_random_uuid(),
    tenant_id   uuid        not null references tenant (id) on delete cascade,
    user_id     uuid        not null,
    key_hash    char(64)    not null unique,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz,
    foreign key (user_id, tenant_id) references portal_user (id, tenant_id) on delete cascade
);
comment on table portal_credential is 'Auth lookup table. Intentionally not RLS-scoped: the lookup happens before a tenant is known, and it is keyed by the hash of a 256-bit secret.';

create table if not exists sales_rep_assignment (
    id             uuid        primary key default gen_random_uuid(),
    tenant_id      uuid        not null references tenant (id) on delete cascade,
    rep_user_id    uuid        not null,
    clinic_id      uuid        not null,
    -- the commission rate in basis points (100 = 1%) on this clinic's paid invoices
    commission_bp  integer     not null check (commission_bp between 0 and 10000),
    created_at     timestamptz not null default now(),
    unique (tenant_id, clinic_id),
    foreign key (rep_user_id, tenant_id) references portal_user (id, tenant_id) on delete cascade,
    foreign key (clinic_id, tenant_id) references clinic (id, tenant_id) on delete cascade
);
create index if not exists idx_sales_rep_assignment_rep on sales_rep_assignment (rep_user_id);

-- an assignment names a user whose role is sales_rep
create or replace function portal_assignment_is_for_a_rep() returns trigger language plpgsql as $$
begin
    if not exists (select 1 from portal_user u where u.id = new.rep_user_id and u.tenant_id = new.tenant_id and u.role = 'sales_rep') then
        raise exception 'user % is not a sales rep, so a clinic cannot be assigned to them', new.rep_user_id using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
drop trigger if exists portal_assignment_is_for_a_rep on sales_rep_assignment;
create trigger portal_assignment_is_for_a_rep before insert or update on sales_rep_assignment for each row execute function portal_assignment_is_for_a_rep();

-- ---------------------------------------------------------------- the matrix itself

create table if not exists access_matrix (
    role      text not null check (role in ('clinic', 'prescriber', 'sales_rep', 'admin')),
    resource  text not null check (resource in ('orders', 'invoices', 'payments', 'commissions')),
    action    text not null check (action in ('read', 'create', 'update', 'settle', 'refund')),
    scope     text not null check (scope in ('none', 'clinic', 'assigned', 'self', 'all')),
    primary key (role, resource, action)
);

-- BEGIN ACCESS MATRIX SEED (generated by app/access/matrix.py seed_sql(); do not edit by hand)
insert into access_matrix (role, resource, action, scope) values
    ('clinic', 'orders', 'read', 'clinic'),
    ('clinic', 'orders', 'create', 'clinic'),
    ('clinic', 'orders', 'update', 'clinic'),
    ('clinic', 'orders', 'settle', 'none'),
    ('clinic', 'orders', 'refund', 'none'),
    ('clinic', 'invoices', 'read', 'clinic'),
    ('clinic', 'invoices', 'create', 'none'),
    ('clinic', 'invoices', 'update', 'none'),
    ('clinic', 'invoices', 'settle', 'clinic'),
    ('clinic', 'invoices', 'refund', 'none'),
    ('clinic', 'payments', 'read', 'clinic'),
    ('clinic', 'payments', 'create', 'clinic'),
    ('clinic', 'payments', 'update', 'none'),
    ('clinic', 'payments', 'settle', 'none'),
    ('clinic', 'payments', 'refund', 'none'),
    ('clinic', 'commissions', 'read', 'none'),
    ('clinic', 'commissions', 'create', 'none'),
    ('clinic', 'commissions', 'update', 'none'),
    ('clinic', 'commissions', 'settle', 'none'),
    ('clinic', 'commissions', 'refund', 'none'),
    ('prescriber', 'orders', 'read', 'clinic'),
    ('prescriber', 'orders', 'create', 'clinic'),
    ('prescriber', 'orders', 'update', 'clinic'),
    ('prescriber', 'orders', 'settle', 'none'),
    ('prescriber', 'orders', 'refund', 'none'),
    ('prescriber', 'invoices', 'read', 'none'),
    ('prescriber', 'invoices', 'create', 'none'),
    ('prescriber', 'invoices', 'update', 'none'),
    ('prescriber', 'invoices', 'settle', 'none'),
    ('prescriber', 'invoices', 'refund', 'none'),
    ('prescriber', 'payments', 'read', 'none'),
    ('prescriber', 'payments', 'create', 'none'),
    ('prescriber', 'payments', 'update', 'none'),
    ('prescriber', 'payments', 'settle', 'none'),
    ('prescriber', 'payments', 'refund', 'none'),
    ('prescriber', 'commissions', 'read', 'none'),
    ('prescriber', 'commissions', 'create', 'none'),
    ('prescriber', 'commissions', 'update', 'none'),
    ('prescriber', 'commissions', 'settle', 'none'),
    ('prescriber', 'commissions', 'refund', 'none'),
    ('sales_rep', 'orders', 'read', 'assigned'),
    ('sales_rep', 'orders', 'create', 'none'),
    ('sales_rep', 'orders', 'update', 'none'),
    ('sales_rep', 'orders', 'settle', 'none'),
    ('sales_rep', 'orders', 'refund', 'none'),
    ('sales_rep', 'invoices', 'read', 'assigned'),
    ('sales_rep', 'invoices', 'create', 'none'),
    ('sales_rep', 'invoices', 'update', 'none'),
    ('sales_rep', 'invoices', 'settle', 'none'),
    ('sales_rep', 'invoices', 'refund', 'none'),
    ('sales_rep', 'payments', 'read', 'assigned'),
    ('sales_rep', 'payments', 'create', 'none'),
    ('sales_rep', 'payments', 'update', 'none'),
    ('sales_rep', 'payments', 'settle', 'none'),
    ('sales_rep', 'payments', 'refund', 'none'),
    ('sales_rep', 'commissions', 'read', 'self'),
    ('sales_rep', 'commissions', 'create', 'none'),
    ('sales_rep', 'commissions', 'update', 'none'),
    ('sales_rep', 'commissions', 'settle', 'none'),
    ('sales_rep', 'commissions', 'refund', 'none'),
    ('admin', 'orders', 'read', 'all'),
    ('admin', 'orders', 'create', 'all'),
    ('admin', 'orders', 'update', 'all'),
    ('admin', 'orders', 'settle', 'none'),
    ('admin', 'orders', 'refund', 'none'),
    ('admin', 'invoices', 'read', 'all'),
    ('admin', 'invoices', 'create', 'all'),
    ('admin', 'invoices', 'update', 'all'),
    ('admin', 'invoices', 'settle', 'all'),
    ('admin', 'invoices', 'refund', 'all'),
    ('admin', 'payments', 'read', 'all'),
    ('admin', 'payments', 'create', 'all'),
    ('admin', 'payments', 'update', 'all'),
    ('admin', 'payments', 'settle', 'none'),
    ('admin', 'payments', 'refund', 'none'),
    ('admin', 'commissions', 'read', 'all'),
    ('admin', 'commissions', 'create', 'all'),
    ('admin', 'commissions', 'update', 'none'),
    ('admin', 'commissions', 'settle', 'none'),
    ('admin', 'commissions', 'refund', 'all')
on conflict (role, resource, action) do update set scope = excluded.scope;
-- END ACCESS MATRIX SEED

-- ---------------------------------------------------------------- the session, and the question every policy asks

create or replace function portal_tenant() returns uuid language sql stable as $$ select nullif(current_setting('app.tenant_id', true), '')::uuid $$;
create or replace function portal_role() returns text language sql stable as $$ select nullif(current_setting('app.role', true), '') $$;
create or replace function portal_user_id() returns uuid language sql stable as $$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

-- the clinic of the asking user (null for a rep or an admin). These three helpers are SECURITY DEFINER so that a policy on another table (and the narrow fulfillment_router role) can ask
-- them without holding a grant on access_matrix, portal_user or sales_rep_assignment; they read only the matrix and the asking user's own rows, keyed on the session settings.
create or replace function portal_user_clinic() returns uuid language sql stable security definer set search_path = public, pg_temp as $$
    select u.clinic_id from portal_user u where u.id = portal_user_id()
$$;

-- the scope the asking role holds for an action; a role, resource or action not in the table is 'none'
create or replace function portal_scope(p_resource text, p_action text) returns text language sql stable security definer set search_path = public, pg_temp as $$
    select coalesce((select m.scope from access_matrix m where m.role = portal_role() and m.resource = p_resource and m.action = p_action), 'none')
$$;

-- may the asking user do this action to a row of this clinic (and, for a commission, of this rep)? A null clinic is reachable only through scope 'all'.
create or replace function portal_allows(p_resource text, p_action text, p_clinic uuid, p_rep uuid default null) returns boolean language sql stable security definer set search_path = public, pg_temp as $$
    select case portal_scope(p_resource, p_action)
        when 'all'      then true
        when 'clinic'   then coalesce(p_clinic = portal_user_clinic(), false)
        when 'assigned' then p_clinic is not null and exists (select 1 from sales_rep_assignment a where a.rep_user_id = portal_user_id() and a.clinic_id = p_clinic)
        when 'self'     then coalesce(p_rep = portal_user_id(), false)
        else false
    end
$$;

-- ---------------------------------------------------------------- row level security on the tables above

alter table clinic                enable row level security;
alter table portal_user           enable row level security;
alter table sales_rep_assignment  enable row level security;
alter table clinic                force row level security;
alter table portal_user           force row level security;
alter table sales_rep_assignment  force row level security;

-- a clinic is visible to an admin, to the user whose clinic it is, and to the rep it is assigned to; only an admin writes
drop policy if exists clinic_select on clinic;
create policy clinic_select on clinic for select using (
    tenant_id = portal_tenant() and (portal_role() = 'admin' or id = portal_user_clinic()
                                     or exists (select 1 from sales_rep_assignment a where a.rep_user_id = portal_user_id() and a.clinic_id = clinic.id)));
drop policy if exists clinic_write on clinic;
create policy clinic_write on clinic for all using (tenant_id = portal_tenant() and portal_role() = 'admin') with check (tenant_id = portal_tenant() and portal_role() = 'admin');

drop policy if exists portal_user_select on portal_user;
create policy portal_user_select on portal_user for select using (tenant_id = portal_tenant() and (id = portal_user_id() or portal_role() = 'admin'));
drop policy if exists portal_user_write on portal_user;
create policy portal_user_write on portal_user for all using (tenant_id = portal_tenant() and portal_role() = 'admin') with check (tenant_id = portal_tenant() and portal_role() = 'admin');

drop policy if exists sales_rep_assignment_select on sales_rep_assignment;
create policy sales_rep_assignment_select on sales_rep_assignment for select using (tenant_id = portal_tenant() and (rep_user_id = portal_user_id() or portal_role() = 'admin'));
drop policy if exists sales_rep_assignment_write on sales_rep_assignment;
create policy sales_rep_assignment_write on sales_rep_assignment for all using (tenant_id = portal_tenant() and portal_role() = 'admin')
    with check (tenant_id = portal_tenant() and portal_role() = 'admin');

-- ---------------------------------------------------------------- authentication: a key to a user

-- The API key is read before anyone knows the tenant, so this function (SECURITY DEFINER, like the api_key lookup it stands beside) takes the hash, finds a credential that is not revoked, and
-- returns the user's tenant, id, role and clinic. It sets the user and tenant the policy on portal_user needs for the one read, and clears them before it returns. A hash that matches nothing,
-- or a revoked credential, returns no rows.
create or replace function portal_resolve_credential(p_key_hash text)
returns table (tenant_id uuid, user_id uuid, role text, clinic_id uuid) language plpgsql security definer set search_path = public, pg_temp as $$
declare
    c record;
begin
    select pc.tenant_id as t, pc.user_id as u into c from portal_credential pc where pc.key_hash = p_key_hash and pc.revoked_at is null;
    if not found then
        return;
    end if;
    perform set_config('app.tenant_id', c.t::text, true);
    perform set_config('app.user_id', c.u::text, true);
    return query select pu.tenant_id, pu.id, pu.role, pu.clinic_id from portal_user pu where pu.id = c.u;
    perform set_config('app.tenant_id', '', true);
    perform set_config('app.user_id', '', true);
end;
$$;
revoke all on function portal_resolve_credential(text) from public;
grant execute on function portal_resolve_credential(text) to app_request;

-- ---------------------------------------------------------------- grants

-- the request role reads the matrix and can never change it; it has no grant to write the credential table either (an admin tool does that as the owner)
grant select on access_matrix to app_request;
grant select, insert, update, delete on clinic, portal_user, sales_rep_assignment to app_request;
grant select on portal_credential to app_request;
grant usage, select on all sequences in schema public to app_request;
