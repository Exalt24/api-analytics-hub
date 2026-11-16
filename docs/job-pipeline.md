# Job pipeline: vendors in, one row per job out, a tailored application, a form filler that asks when it is not sure

What it is: a six step pipeline built on the hub's own parts (Postgres with forced row level security, the n8n workflows, the vendor stand-in pattern). It pulls job postings from ten job data vendors and takes pushes from two more, normalises them in JavaScript, dedupes them in Redis and Postgres, archives the raw JSON, embeds each new job into Qdrant, tailors a resume and cover letter to a job without inventing anything, maps and fills an application form with a confidence for every field, and stops for a person whenever it is not sure.

What it was built and tested against: vendor stand-ins written from each vendor's documentation ([job-vendors.md](job-vendors.md)), real Redis, Qdrant, RabbitMQ, Postgres, an S3 compatible server (SeaweedFS) and a real n8n container, a stand-in OpenAI server for the tests, a stand-in Browserbase API driven by Browserbase's own Python SDK, stand-in application pages in the shape of Greenhouse, Lever, Workday and a custom form, and real Chromium. Live runs and what they covered are in [Live runs](#live-runs). It has not been pointed at a real job vendor account or a real employer's application form, and a form is submitted only when the runtime is started with `JOBS_SUBMIT=1`.

## The six phases

| Phase | Code | What it does | Proof |
|---|---|---|---|
| 1. Ingest | `n8n/job_ingest.workflow.json`, `n8n/jobs/pull.js`, `n8n/jobs/normalize.js`, `app/jobs/pipeline.py` | an hourly pull of ten vendors and two webhooks, custom JavaScript to one canonical job and to the ATS (18 link rules and 8 page markers), then the hub: raw JSON to R2 or S3, a sliding window in Redis, an upsert in Postgres, an embedding in Qdrant | `tests/test_jobs_vendors_js.py`, `n8n/jobs/normalize.test.js`, `tests/test_jobs_dedupe.py`, `tests/test_jobs_pipeline_db.py`, `n8n/verify_job_pipeline_in_docker.py` |
| 2 and 3. Tailor and score | `app/jobs/tailor.py`, `app/jobs/render.py` | a resume and cover letter from the applicant's own facts through a structured-output model loop, an ATS match score from 0 to 100, a grounding check, DOCX and PDF | `tests/test_jobs_tailor.py` |
| 4 and 5. Observe and apply | `app/jobs/formfill.py`, `app/jobs/browser.py` | Playwright on local Chromium or a Browserbase session, Stagehand when the DOM cannot be read, a confidence for every field, 0.95 to type, verify the success state | `tests/test_jobs_formfill.py`, `tests/test_jobs_browserbase.py` |
| 6. Human in the loop | `app/jobs/hitl.py`, `app/jobs/worker.py`, `app/api/jobs.py`, `n8n/job_apply.workflow.json` | a review queue in Postgres, pause and resume with a one-time token, RabbitMQ between the parts, an n8n Wait node woken by the hub | `tests/test_jobs_hitl_db.py`, `tests/test_jobs_api_db.py`, `n8n/verify_job_pipeline_in_docker.py` |

## Ingest: what the rules are

- **A batch is recorded once.** The run row is unique on (tenant, vendor, batch id). n8n names a batch `<vendor>-<UTC hour>` (or the vendor's own collection id for a push), so a rerun in the hour, or n8n retrying a webhook, is answered from the first run and writes nothing. The raw JSON goes to object storage before anything else happens, so a mapping bug found later is fixed and replayed from what the vendor sent. If the archive refuses the write the batch stops before any row is written.
- **Dedupe is two layers, and only one is the truth.** The window (`app/jobs/dedupe.py`) is a Redis sorted set per tenant, trimmed and written by one Lua script, so two workers cannot both see "new". Its member is the job fingerprint and the vendor, so the same vendor repeating a job inside the window skips the database, while a second vendor reporting it reaches the upsert and is added to the row's vendor list. The truth is the unique index on (tenant, fingerprint): with Redis flushed or down, the run says `dedupe_unavailable` and Postgres still makes one row.
- **The fingerprint is the ATS id when the link names one**, else the company name, the title with brackets and case removed, and the place. The company name and not its web address, because a first version used the address when a vendor sent one and split the same job in two (found by the cross-vendor test: 32 keys for 30 jobs).
- **Embedding is only for rows that were inserted.** A repeat sighting costs no embedding call. If the embedding service fails the rows are stored, the run is `partial`, and the next call for that batch embeds what is still missing.
- **Tenants never meet.** Postgres by forced row level security, Redis by a key per tenant, Qdrant by a tenant filter written once and used by every read (the tenant field is a tenant index, so Qdrant lays the data out per tenant).

## Tailor: what it will and will not do

The loop starts from a document made without any model (the applicant's facts, ordered by relevance to the job), so there is always a true one to fall back on. Each model round returns structured JSON, is rendered, **grounded**, then **scored**. Grounding finds every claim-bearing token (a number, a name from a technology list, anything capitalised mid sentence, camel case, an acronym) and requires each to appear in the applicant's facts. One that does not rejects the round, and the next prompt names it. The score (75 points for the share of the job's terms present, 15 for four parseable headings, 10 for a plain length) drives the loop to 90 and stops when nothing addressable is left. A job that asks for a tool the facts do not hold cannot reach 90 honestly; the result says so (`unsupported`, `ceiling`) and the document stays true. The score is an explainable proxy and no vendor's real scorer.

## Apply: the 0.95 rule

A field is typed only at confidence 0.95 or more **and** with a stored value. A label becomes a key in order: an exact earlier answer (0.99), an `autocomplete` token or input type (0.99), an exact phrase (0.98), a contained phrase (0.95, or 0.90 when the phrase is under half the label), then an embedding suggestion capped at 0.90 so it is only ever a proposal. Consent boxes and self-identification questions are never answered by a rule. A page whose form cannot be read is never submitted. A submission that does not reach a success state is `failed` and becomes a review task. See the header of `app/jobs/formfill.py` for the full list.

## Pause and resume

`waiting_review` holds a one-time resume token (a CHECK ties the token to that state). Answering the last open task writes the answer as a fact under the exact question, and queues `apply.resume` in RabbitMQ; the worker spends the token in one `UPDATE`, so a message delivered twice resumes the run once and a stale token resumes nothing. A message that fails three times goes to the dead letter queue. A run created with an n8n `resume_url` wakes the Wait node when it reaches submitted, failed or manual review. The people-facing side is the HTTP API (`app/api/jobs.py`): any internal tool reads `/api/jobs/review` and writes `/api/jobs/review/{id}/answer`. Two screens were built on it: an Appsmith app ([docs/appsmith.md](appsmith.md), run in a real browser against a real hub, with the exported app file) and a Notion board (`app/jobs/notion.py`, a page for each open question marked when answered, written from Notion's reference pages and run against a stand-in). The hub's own table is the queue; a board that is down or refusing never loses or answers a task.

## Live runs

[`docs/test-runs/2026-11-17-job-pipeline-live.txt`](test-runs/2026-11-17-job-pipeline-live.txt) is the output of `scripts/job_pipeline_live_check.py`: the Redis sliding window against a free Upstash database over TLS (made and deleted by the script), `openai/text-embedding-3-small` through LangChain into a local Qdrant with the right job ranked first for each query, and the tailoring loop with `openai/gpt-4o-mini`, both through OpenRouter. The live tailoring run found a false alarm in the grounding check (a hyphenated word such as "Python-based" was judged as one unknown token), now fixed and tested. [`docs/test-runs/2026-11-17-job-pipeline-n8n.txt`](test-runs/2026-11-17-job-pipeline-n8n.txt) is `n8n/verify_job_pipeline_in_docker.py`: both workflows in a real n8n 2.44.6 container against the real hub, Redis, Qdrant, RabbitMQ, Postgres and an S3 compatible server, every check read from node outputs and the stores. It found two bugs the unit tests had not (a Postgres deadlock between parallel vendor batches, and an embedding update that deadlocked the same way), both fixed with a regression test.

## Configuration

`JOBS_S3_BUCKET` switches the pipeline on (without it the routes answer 503). Then: `JOBS_S3_ENDPOINT`, `JOBS_S3_ACCESS_KEY`, `JOBS_S3_SECRET_KEY`, `JOBS_S3_REGION` (Cloudflare R2: the account's S3 address and `auto`); `JOBS_REDIS_URL` (Upstash or any Redis 6 or later), `JOBS_DEDUPE_HOURS`; `JOBS_QDRANT_URL`, `JOBS_QDRANT_API_KEY`, `JOBS_COLLECTION`; `JOBS_OPENAI_API_KEY`, `JOBS_OPENAI_BASE_URL`, `JOBS_EMBED_MODEL`; `JOBS_AMQP_URL`; `JOBS_BROWSER` (`local` or `browserbase`, with `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`); `JOBS_RESUME_FILE`; `JOBS_SUBMIT=1` to let a run click submit. n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, `HUB_BASE_URL`, `HUB_API_KEY` and `JOB_VENDORS_JSON` (see [n8n/README.md](../n8n/README.md)).

Local services: `docker compose --profile jobs --profile s3 up -d --wait`.

## Limits

Written down so they are read here first:

- No vendor account was used. The vendor pulls follow the vendors' pages and run against stand-ins.
- Stagehand runs in its own browser and reads the first page of a form; a multi step flow that keeps state cannot be re-entered by a second browser, so a form that the DOM walk cannot read and that has several steps is a review, not a fill.
- The form filler has not been run against a real employer's application system, and the terms of an application system may restrict automated submission. The default is to fill and stop (`JOBS_SUBMIT` is off).
- The grounding check is token based. It catches an invented tool, number or name; it does not catch a sentence that is false in a way made only of true words (a real employer paired with a real skill the person never used there). The review of a tailored document by its owner is not removed by it.
- Embeddings in the tests are a hashed bag of words from the stand-in; ranking quality is not measured, only that vectors are stored, filtered by tenant and found.
