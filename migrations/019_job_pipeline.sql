-- The job pipeline: postings pulled from job data vendors, one row per job however many vendors reported it, the run log of every batch,
-- the tailored resumes and cover letters, the applicant's knowledge base, and the apply runs with their human review queue.
-- (app/jobs, docs/job-pipeline.md)
--
-- WHY EACH TABLE IS HERE.
--   job_posting          one row per distinct job. `fingerprint` is the hash of the normaliser's dedupe key (the ATS id when the link names one,
--                        else company, title and place), so the same job reported by two vendors is one row with `seen_count` 2 and both
--                        vendors in `vendors`. Upserts only ever fill a missing value and move `last_seen_at` forward.
--   job_ingest_run       one batch from one vendor: what arrived, what was new, what the dedupe window already held, what was refused as
--                        unreadable, and the object key of the raw JSON archived before anything else happened to it.
--   applicant_fact       the knowledge base the form filler answers from: one row per (applicant, key). `source` says whether a person wrote it
--                        (profile) or corrected it in the review queue (correction), and a later correction replaces an earlier one.
--   tailored_document    a resume or cover letter produced for one job: the final ATS match score, how many rounds it took, whether the
--                        grounding check passed and where the file is stored.
--   application_run      one attempt to apply. A state machine (queued, filling, waiting_review, filled, submitted, failed, manual) whose
--                        waiting_review state holds a resume token, so a pause survives a restart and a second resume is refused.
--   review_task          (with the id of its Notion page when a board is set) one field the filler was not sure enough about (confidence under the threshold) or a submission that failed. A
--                        person answers it; the answer is written to applicant_fact and the run is resumed.
--
-- Row level security is on and forced on every table, with the same policy as every other tenant table. Safe to run twice.

create table if not exists job_posting (
    id              uuid        primary key default gen_random_uuid(),
    tenant_id       uuid        not null references tenant (id) on delete cascade,
    fingerprint     text        not null check (length(fingerprint) = 64),
    dedupe_key      text        not null check (length(dedupe_key) between 1 and 600),
    title           text        not null check (length(title) between 1 and 400),
    company_name    text,
    company_domain  text,
    location        text,
    remote          boolean,
    url             text,
    apply_url       text,
    ats             text,
    ats_org         text,
    ats_job_id      text,
    posted_at       timestamptz,
    description     text,
    salary_min      numeric(14,2),
    salary_max      numeric(14,2),
    salary_currency char(3),
    salary_period   text,
    employment_type text,
    technologies    text[]      not null default '{}',
    vendors         text[]      not null default '{}',
    first_vendor    text        not null,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    seen_count      integer     not null default 1 check (seen_count >= 1),
    embedded_at     timestamptz,
    unique (tenant_id, fingerprint),
    constraint salary_range_in_order check (salary_min is null or salary_max is null or salary_min <= salary_max)
);

create index if not exists idx_job_posting_recent on job_posting (tenant_id, last_seen_at desc);
create index if not exists idx_job_posting_ats on job_posting (tenant_id, ats) where ats is not null;

create table if not exists job_ingest_run (
    id            uuid        primary key default gen_random_uuid(),
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    vendor        text        not null,
    batch_id      text        not null,
    trigger       text        not null default 'schedule' check (trigger in ('schedule', 'webhook', 'manual')),
    received      integer     not null default 0 check (received >= 0),
    invalid       integer     not null default 0 check (invalid >= 0),
    new_jobs      integer     not null default 0 check (new_jobs >= 0),
    duplicates    integer     not null default 0 check (duplicates >= 0),
    embedded      integer     not null default 0 check (embedded >= 0),
    archive_key   text,
    status        text        not null default 'ok' check (status in ('ok', 'partial', 'failed')),
    error_detail  text,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    -- a batch is recorded once: n8n retries a failed webhook delivery, and the second try must find the first
    unique (tenant_id, vendor, batch_id)
);

create table if not exists applicant_fact (
    id            uuid        primary key default gen_random_uuid(),
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    applicant     text        not null default 'default',
    fact_key      text        not null check (fact_key ~ '^[a-z][a-z0-9_.]{0,79}$'),
    fact_value    text        not null check (length(fact_value) between 1 and 4000),
    source        text        not null default 'profile' check (source in ('profile', 'correction')),
    updated_at    timestamptz not null default now(),
    unique (tenant_id, applicant, fact_key)
);

create table if not exists tailored_document (
    id            uuid        primary key default gen_random_uuid(),
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    job_id        uuid        not null references job_posting (id) on delete cascade,
    kind          text        not null check (kind in ('resume', 'cover_letter')),
    -- only a resume is scored (the match score is a measure of a resume against a job); a cover letter is grounded and stored, with no score
    score         integer     check (score between 0 and 100),
    rounds        integer     not null check (rounds >= 1),
    grounded      boolean     not null,
    reached_threshold boolean not null,
    object_key    text,
    created_at    timestamptz not null default now(),
    unique (tenant_id, job_id, kind),
    constraint only_a_resume_is_scored check ((kind = 'resume') = (score is not null))
);

create table if not exists application_run (
    id            uuid        primary key default gen_random_uuid(),
    tenant_id     uuid        not null references tenant (id) on delete cascade,
    job_id        uuid        references job_posting (id) on delete set null,
    apply_url     text        not null check (apply_url ~ '^https?://'),
    state         text        not null default 'queued'
                  check (state in ('queued', 'filling', 'waiting_review', 'filled', 'submitted', 'failed', 'manual')),
    resume_token  text        unique,
    resume_url    text,
    filled        integer     not null default 0 check (filled >= 0),
    needs_review  integer     not null default 0 check (needs_review >= 0),
    detail        text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    -- a run that waits has the token that resumes it, and one that does not wait has none
    constraint token_iff_waiting check ((state = 'waiting_review') = (resume_token is not null))
);

create table if not exists review_task (
    id               uuid        primary key default gen_random_uuid(),
    tenant_id        uuid        not null references tenant (id) on delete cascade,
    run_id           uuid        not null references application_run (id) on delete cascade,
    field_label      text        not null,
    field_selector   text,
    proposed_key     text,
    proposed_value   text,
    confidence       numeric(4,3) not null check (confidence between 0 and 1),
    reason           text        not null,
    status           text        not null default 'pending' check (status in ('pending', 'resolved', 'manual')),
    answer           text,
    resolved_by      text,
    notion_page_id   text,
    created_at       timestamptz not null default now(),
    resolved_at      timestamptz,
    constraint resolved_has_an_answer check (status <> 'resolved' or answer is not null)
);

create index if not exists idx_review_task_pending on review_task (tenant_id, created_at) where status = 'pending';

alter table job_posting      enable row level security;
alter table job_ingest_run   enable row level security;
alter table applicant_fact   enable row level security;
alter table tailored_document enable row level security;
alter table application_run  enable row level security;
alter table review_task      enable row level security;

alter table job_posting      force row level security;
alter table job_ingest_run   force row level security;
alter table applicant_fact   force row level security;
alter table tailored_document force row level security;
alter table application_run  force row level security;
alter table review_task      force row level security;

drop policy if exists job_posting_by_tenant on job_posting;
create policy job_posting_by_tenant on job_posting
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists job_ingest_run_by_tenant on job_ingest_run;
create policy job_ingest_run_by_tenant on job_ingest_run
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists applicant_fact_by_tenant on applicant_fact;
create policy applicant_fact_by_tenant on applicant_fact
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists tailored_document_by_tenant on tailored_document;
create policy tailored_document_by_tenant on tailored_document
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists application_run_by_tenant on application_run;
create policy application_run_by_tenant on application_run
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists review_task_by_tenant on review_task;
create policy review_task_by_tenant on review_task
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- 001 granted every table that existed then; a grant is not retroactive (see 003), so the new tables owe their own.
-- Nothing is deleted by the request role.
grant select, insert, update on job_posting, job_ingest_run, applicant_fact, tailored_document, application_run, review_task to app_request;
grant usage, select on all sequences in schema public to app_request;
