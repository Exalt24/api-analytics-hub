-- Snapshot change detection: compare one day's file of carriers with what we hold, keep every change forever, and raise each alert once.
--
-- This slice is deliberately not tenant scoped. It works on a public, made-up dataset (see docs/change-detection.md) and does not touch the tenant tables from 001 to 004, so it
-- can be applied on its own.
--
-- THE FIVE DECISIONS THAT MATTER, each one silent when it is wrong:
--   1. Only TRACKED columns are hashed. A carrier whose name changed is not a carrier whose insurance changed, and examining 400,000 rows a day to find 2,000 is the cost this avoids.
--   2. The hash is computed by the database, by one function, for the staged file and for the current table alike, so the two sides can never disagree about how a row is hashed.
--   3. An event has a natural key (carrier, kind, filing reference) behind a UNIQUE constraint. A rerun, an overlapping file or a restart after a crash cannot make a second alert,
--      because the database refuses it, not because the code remembered.
--   4. History and events are append only. A trigger refuses UPDATE, DELETE and TRUNCATE on them.
--   5. Numbers are matched after normalising, in SQL, because a file writes the same motor carrier number as 12345, 0012345 or MC-0012345 and a join on the raw text misses two of three.

-- ------------------------------------------------------------------------------------------------ key normalisers

-- A USDOT number: digits only, leading zeros dropped. NULL when nothing is left, so a blank never matches a blank.
create function carrier_norm_dot(raw text) returns text
language sql immutable parallel safe as $$
    select nullif(ltrim(regexp_replace(coalesce(raw, ''), '\D', '', 'g'), '0'), '')
$$;

-- An operating authority (docket) number: a kind prefix plus digits without leading zeros. No prefix means MC. FF (freight forwarder) and MX (Mexico) are different dockets that
-- can share digits with an MC, so the prefix is kept and they never match each other.
create function carrier_norm_mc(raw text) returns text
language sql immutable parallel safe as $$
    select case when t.d is null then null else t.p || t.d end
      from (select case when upper(coalesce(raw, '')) ~ '^\s*(FF|MX)' then substring(upper(raw) from '^\s*(FF|MX)') else 'MC' end as p,
                   nullif(ltrim(regexp_replace(coalesce(raw, ''), '\D', '', 'g'), '0'), '') as d) t
$$;

-- The fingerprint of the tracked columns. md5 cast to uuid is 16 bytes instead of 33, and nothing here needs it to resist an attacker, only to move when a tracked value moves.
-- A NULL and an empty string hash differently (the cleaning functions below turn a blank into NULL first), and the unit separator keeps ('ab', 'c') apart from ('a', 'bc').
create function carrier_track_hash(insurer text, policy_status text, authority_status text, authority_date date, filing_id text, filing_date date, cancel_date date)
returns uuid
language sql immutable parallel safe as $$
    select md5(concat_ws(E'\x1f',
        coalesce(insurer, E'\x02'),
        coalesce(policy_status, E'\x02'),
        coalesce(authority_status, E'\x02'),
        coalesce((authority_date - date '1970-01-01')::text, E'\x02'),
        coalesce(filing_id, E'\x02'),
        coalesce((filing_date - date '1970-01-01')::text, E'\x02'),
        coalesce((cancel_date - date '1970-01-01')::text, E'\x02')))::uuid
$$;

-- ------------------------------------------------------------------------------------------------ the day's file

-- Cell cleaning, done by the database BEFORE anything is hashed. A quoted empty cell, an unquoted empty cell, a trailing space and a doubled space inside a name are the same value to
-- a person and must be the same value to the hash, or one export setting flips 400,000 rows into "changed" overnight. Blank becomes NULL; codes (insurer, statuses) are upper-cased.
create function carrier_clean_text(raw text) returns text
language sql immutable parallel safe as $$
    select nullif(btrim(regexp_replace(coalesce(raw, ''), '\s+', ' ', 'g')), '')
$$;

create function carrier_clean_code(raw text) returns text
language sql immutable parallel safe as $$
    select upper(carrier_clean_text(raw))
$$;

-- An ISO date or nothing. Any other text fails the whole load rather than being read as some other date under another DateStyle.
create function carrier_clean_date(raw text) returns date
language plpgsql immutable parallel safe as $$
declare t text := carrier_clean_text(raw);
begin
    if t is null then return null; end if;
    if t !~ '^\d{4}-\d{2}-\d{2}$' then raise exception 'not an ISO date: %', left(t, 40); end if;
    return t::date;
end
$$;

-- UNLOGGED: a staging table is rebuilt every day, so crash safety on it is wasted write-ahead log. It is emptied and refilled inside the same transaction as the whole day, which
-- means a crash leaves it as it was. (PostgreSQL also empties an unlogged table after a crash, which costs nothing here: it holds only the file being loaded, and the file is loaded again.) The file is copied into the *_raw columns as text, and the columns after them are generated from those, cleaned.
create unlogged table carrier_stage (
    seq                 bigint generated always as identity,
    usdot_raw           text,
    mc_raw              text,
    legal_name_raw      text,
    insurer_raw         text,
    policy_status_raw   text,
    authority_status_raw text,
    authority_date_raw  text,
    filing_id_raw       text,
    filing_date_raw     text,
    cancel_date_raw     text,
    usdot_norm          text generated always as (carrier_norm_dot(usdot_raw)) stored,
    mc_norm             text generated always as (carrier_norm_mc(mc_raw)) stored,
    legal_name          text generated always as (carrier_clean_text(legal_name_raw)) stored,
    insurer             text generated always as (carrier_clean_code(insurer_raw)) stored,
    policy_status       text generated always as (carrier_clean_code(policy_status_raw)) stored,
    authority_status    text generated always as (carrier_clean_code(authority_status_raw)) stored,
    authority_date      date generated always as (carrier_clean_date(authority_date_raw)) stored,
    filing_id           text generated always as (carrier_clean_text(filing_id_raw)) stored,
    filing_date         date generated always as (carrier_clean_date(filing_date_raw)) stored,
    cancel_date         date generated always as (carrier_clean_date(cancel_date_raw)) stored,
    track_hash          uuid generated always as (carrier_track_hash(carrier_clean_code(insurer_raw), carrier_clean_code(policy_status_raw), carrier_clean_code(authority_status_raw),
                                                                     carrier_clean_date(authority_date_raw), carrier_clean_text(filing_id_raw), carrier_clean_date(filing_date_raw),
                                                                     carrier_clean_date(cancel_date_raw))) stored
);

-- One row per load, so a rerun is visible as a load that raised no events. rows_missing counts carriers we hold that this file does not mention: a truncated file and a feed that
-- pruned a carrier look the same, so absence raises no event and is only counted.
create table carrier_load (
    id                    bigint generated always as identity primary key,
    snapshot_date         date        not null,
    baseline              boolean     not null,
    loaded_at             timestamptz not null default now(),
    rows_staged           integer,
    rows_rejected         integer,
    rows_duplicate        integer,
    rows_changed          integer,
    rows_new              integer,
    rows_missing          integer,
    events_new            integer,
    events_suppressed     integer,
    unknown_policy_status jsonb,
    previous_rows_staged  integer
);

-- ------------------------------------------------------------------------------------------------ what we hold

create table carrier_current (
    usdot_norm        text primary key,
    mc_norm           text,
    legal_name        text,
    insurer           text,
    policy_status     text,
    authority_status  text,
    authority_date    date,
    filing_id         text,
    filing_date       date,
    cancel_date       date,
    track_hash        uuid generated always as (carrier_track_hash(insurer, policy_status, authority_status, authority_date, filing_id, filing_date, cancel_date)) stored,
    first_seen_load   bigint not null references carrier_load (id),
    last_changed_load bigint not null references carrier_load (id),
    -- Set only in the optional absent_is_event mode: the load in which this carrier was first missing from a file. Cleared when it returns.
    absent_load       bigint references carrier_load (id)
);

-- Every state a carrier has been in, one row per load in which a tracked column moved. The first row of a carrier has no prev_hash.
create table carrier_history (
    id             bigint generated always as identity primary key,
    load_id        bigint not null references carrier_load (id),
    snapshot_date  date   not null,
    usdot_norm     text   not null,
    mc_norm        text,
    insurer        text,
    policy_status  text,
    authority_status text,
    authority_date date,
    filing_id      text,
    filing_date    date,
    cancel_date    date,
    prev_hash      uuid,
    track_hash     uuid   not null
);
create index idx_carrier_history_carrier on carrier_history (usdot_norm, id);

-- ------------------------------------------------------------------------------------------------ events

-- event_ref is the filing id for a filing event, the new status plus its date for an authority change, and empty for a new carrier. It is what lets one carrier lose its authority
-- twice in a year and alert both times, while a repeat of the same filing alerts once.
create table change_event (
    id            bigint generated always as identity primary key,
    load_id       bigint not null references carrier_load (id),
    snapshot_date date   not null,
    usdot_norm    text   not null,
    mc_norm       text,
    kind          text   not null check (kind in ('new_entity', 'insurer_changed', 'cancellation_filed', 'replacement_filed', 'authority_status_changed', 'dropped_from_file')),
    event_ref     text   not null default '',
    detail        jsonb  not null,
    created_at    timestamptz not null default now(),
    constraint change_event_natural_key unique (usdot_norm, kind, event_ref)
);
create index idx_change_event_load on change_event (load_id);

-- The companies we already know, with the two numbers a file can name them by. Either number may be missing; each is unique once normalised.
create table carrier_company (
    id         bigint generated always as identity primary key,
    name       text not null,
    usdot_no   text,
    mc_no      text,
    usdot_norm text generated always as (carrier_norm_dot(usdot_no)) stored unique,
    mc_norm    text generated always as (carrier_norm_mc(mc_no)) stored unique,
    check (usdot_norm is not null or mc_norm is not null)
);

-- One row per company an event matched. Two rows for one event means the USDOT number and the MC number point at different companies: a conflict for a person, never a pick.
create table change_event_match (
    event_id   bigint not null references change_event (id),
    company_id bigint not null references carrier_company (id),
    matched_by text   not null check (matched_by in ('usdot', 'mc', 'both')),
    primary key (event_id, company_id)
);

-- Alerts waiting to go out. The payload is built once, when the event is raised, and every retry sends those same bytes. idempotency_key goes to the sink so a sink that honours it
-- creates one task however many times it is called.
create table change_outbox (
    event_id         bigint primary key references change_event (id),
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
create index idx_change_outbox_due on change_outbox (next_attempt_at) where status = 'pending';

-- ------------------------------------------------------------------------------------------------ append only

create function carrier_forbid_rewrite() returns trigger language plpgsql as $$
begin
    raise exception '% is append only: % refused', tg_table_name, tg_op;
end
$$;

create trigger carrier_history_append_only before update or delete on carrier_history
    for each row execute function carrier_forbid_rewrite();
create trigger carrier_history_no_truncate before truncate on carrier_history
    for each statement execute function carrier_forbid_rewrite();
create trigger change_event_append_only before update or delete on change_event
    for each row execute function carrier_forbid_rewrite();
create trigger change_event_no_truncate before truncate on change_event
    for each statement execute function carrier_forbid_rewrite();
