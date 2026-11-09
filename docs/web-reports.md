# Report export from the dashboard

The Reports page in `web/` exports one tenant's month of stored figures as CSV, Excel or PDF. This page holds the detail behind the README section: what runs where, what each test ran against, and the limits.

## Run it

```bash
docker compose up -d --wait                       # Postgres 17 with pgvector on 5433
cat migrations/*.sql | psql "postgresql://analytics:analytics@127.0.0.1:5433/analytics" -v ON_ERROR_STOP=1
python seed_demo.py                               # prints a viewer and an admin key (sample figures)
cd web && npm ci
DATABASE_URL="postgresql://analytics:analytics@127.0.0.1:5433/analytics" npm run build
DATABASE_URL="postgresql://analytics:analytics@127.0.0.1:5433/analytics" npm start
```

Open `http://localhost:3000/reports`, paste a key, pick a month. The export runs in Next.js Server Actions, so the server needs `DATABASE_URL`. The other settings are optional.

| Variable | What it does |
|---|---|
| `DATABASE_URL` | the Postgres the actions read |
| `DATABASE_ROLE` | the role to drop to, default `app_request` |
| `ANTHROPIC_API_KEY` | turns the AI summary on; without it the plain template is used and the file says so |
| `ANTHROPIC_MODEL` | model name, default `claude-haiku-4-5` |
| `ANTHROPIC_BASE_URL` | another endpoint for the same API (the browser test points it at a stand-in) |
| `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | the bucket for attached source documents; without `S3_BUCKET` the upload says it is not set up |

## What is exported

Daily rows and month totals of the `delta` metrics whose unit is a count or an amount of money (orders, gross sales, refunds, spend, and so on). Gauges such as customers and average order value are left out, because a month total of a gauge means nothing. Two connections that report the same metric on the same day are added into one row. Money stays an integer in minor units until it is printed, by integer division, so a total past 2^53 is still exact.

- CSV: `period, metric_key, metric, value, currency`. `period` is a date, or `total`. Rows named `summary`, `summary_source` and `source_document` carry the prose. A text cell that starts with `=`, `+`, `-` or `@` gets a leading apostrophe.
- Excel: sheets Summary, Totals and Daily. Numbers are numbers, money has two decimals shown.
- PDF: title, summary, totals and the daily rows on as many pages as it takes.

## The tenant boundary

`web/lib/server/db.ts` mirrors `app/db.py`. A request resolves its API key to a tenant by the SHA-256 of the key (the lookup `app/api/auth.py` does; `api_key` has no row level security, as its migration says). Every tenant query then runs in `withTenant`: a transaction that runs `set local role app_request` and `select set_config('app.tenant_id', $1, true)`. Policies read that setting, so no query carries a `WHERE tenant_id`. `assertRoleCannotBypassRls` refuses a role that is a superuser or holds BYPASSRLS.

One difference from the Python pool: Python runs `set role` once per pooled connection, Kysely code runs `set local role` inside each tenant transaction, which needs no per-connection hook.

Every function takes a Kysely `Db`. `pgDialect` (node-postgres) is the only dialect that was run. No other driver was tried.

Tests (`web/tests/tenant-isolation.test.ts`, 13 tests, real Postgres): the rows tenant A gets equal the fixture rows worked out by hand; tenant B's 777, 555 and EUR figures never appear; a raw query with no `WHERE` returns one tenant; a `WHERE tenant_id = B` returns nothing; 40 concurrent requests for two tenants on a pool of two connections each see only their own; with no tenant set a query sees nothing; a malformed tenant id is refused before any SQL; on a pool of one connection the next query finds the setting and the role gone; a login role that is not a superuser holds too; a superuser role is refused at start, and the control query shows a superuser does see both tenants.

## The AI summary

`web/lib/report/summary.ts` calls the model through the Vercel AI SDK: `generateText` with `Output.object`, so the reply is an object checked by Zod, with `metricKey` limited to the metrics in the data. Provider: `@ai-sdk/anthropic`.

`web/lib/report/guard.ts` reads every sentence before it is accepted. A number that is not a figure of the data is refused: digits, thousands separators, full width digits, a percentage, a rounded figure, a sum the model worked out, and numbers written as words (forty-two, a dozen, twice, hundreds, ordinals). A digit from another script is refused outright. The report's own month and the data's own dates are labels, not figures. `one` counts as a number except in "one of" and "no one". A refused summary goes back once with the figures named. A second refusal, a model error or a missing key gives the plain template, and the reason is printed in the CSV, the workbook and the PDF.

The guard does not catch a right figure in the wrong role, a wrong direction word, a cause the data does not show, or roman numerals. It over-refuses on purpose: a false refusal costs a plain summary, a miss puts a wrong figure in a client's report.

Tests: `web/tests/guard.test.ts` (17) including the template passing its own guard over 300 seeded random months and an invented figure refused in each; `web/tests/summary.test.ts` (12) against a scripted fake model (`MockLanguageModelV4` from `ai/test`): clean first try, retry on an invented figure with the figure named to the model, fallback after two refusals, a number written as a word, a reply that is not the schema, a thrown error, no key, and exactly as many calls as allowed.

No test calls a real model. The browser test calls `web/e2e/stand-ins/anthropic.mts`, a script written from the Messages API reference and from the request the SDK's Anthropic provider sends (captured once). It reads the totals out of the prompt and quotes them, and for the month 2026-09 it always invents a figure, so the retry and the fallback happen in the real built app.

## Attached source document

`web/lib/s3/presign.ts` signs a PUT URL with `@aws-sdk/s3-request-presigner`. The key is built on the server under `tenants/<tenant>/reports/<month>/<uuid>-<clean file name>`; the browser never picks it. The URL is for one key, one content type and one length (all three signed), and lasts 300 seconds. The export refuses a document key that is not under the caller's own tenant prefix.

Finding from the stand-in: the SDK's default adds a CRC32 of the request body to a presigned URL, and at signing time the body is empty, so the URL carries the checksum of nothing and the real upload is refused as a bad digest. The client sets `requestChecksumCalculation: "WHEN_REQUIRED"`, and a test fails without it.

Tested against SeaweedFS 4.53 as the stand-in S3 (`docker compose --profile s3 up -d --wait s3`), which checks AWS signature version 4 against one fake identity. It is not AWS and no AWS account was used. Results (`web/tests/s3.test.ts`): a correct PUT is stored and read back byte for byte, through the client and through a presigned read link; a PUT that changes the content type, sends a different length, uses a URL with its key changed, waits past the expiry or is signed with the wrong secret is refused with 403. The browser test attaches a file through the form and reads the object back.

A browser PUT to a bucket needs the bucket's CORS rules to allow the dashboard's origin. The stand-in allowed it by default; a real bucket needs the rule set.

## AI search, tenant isolation

`web/lib/server/search.ts` is the TypeScript side of `app/similar_queries.py` and migration 010. `embed` is a port of the Python function (BLAKE2b with an 8 byte digest from `@noble/hashes`) and matches 12 vectors the Python one wrote, including accents, Japanese text, an emoji and a 300 character query (`web/tests/fixtures/embed_golden.json`). Lookups run in `withTenant` and turn on `hnsw.iterative_scan = strict_order`.

Tests: `web/tests/search.test.ts` (10): tenant B's own text asked as tenant A returns only A's rows, a text both tenants stored comes back once, a write for another tenant is refused by the policy's check, and a tenant owning a tenth of 6,000 random vectors gets a full 20 back. The Python test `test_a_query_vector_taken_from_one_tenant_never_returns_another_tenants_rows` uses tenant B's stored vector as the probe from tenant A and asks for 200 rows.

## The page state in React Context

`web/components/ReportsProvider.tsx` holds what the Reports page components share in a React Context: the key that picks the tenant (the dashboard has no tenant picker, so the key is the selection), the month, the format, the AI summary switch, the attached document, and the last export with its status (idle, exporting, saved or failed). `useReports()` returns it and throws outside the provider. The rules sit in a plain reducer, `web/lib/report/reports-state.ts`, which refuses a format other than csv, xlsx or pdf. The key bar, the form and the result alerts read and write through the context, with no props passed between them; React Hook Form and Zod still validate the form at submit, and the Server Actions are unchanged.

Tests: `web/tests/reports-state.test.ts` (11, the reducer alone) and `web/tests/reports-provider.test.tsx` (16, in jsdom): a change by one sibling component shows in the other; the hook throws outside the provider; a format of `docx` is refused and the state stays; against a fake export action the last export, its status, the saved file and the error are recorded, the action of the chosen format is the one called, a second click while one runs does nothing, and with no key the action is never called; the real form's month and summary switch reach the context and the submit sends them; a key typed into the key bar is the key the export sends. Twelve of the mutation guards below break this code, and the 10 browser tests pass against the built page unchanged. These tests call a fake action, so the Server Action itself is covered by the browser tests, not by these.

## Compiler, linter and mutation check

`web/tsconfig.json` has `strict` and `noImplicitAny`. `web/tests/typescript-strict.test.ts` compiles probes under it and expects an implicit any and a possibly null use to be refused. ESLint with typescript-eslint runs `@typescript-eslint/no-explicit-any` as an error over `app`, `components`, `lib`, `tests` and `e2e`. It lives in `web/lint/` with its own `package.json`, because typescript-eslint runs on the TypeScript 5 and 6 API and `web/` type checks with TypeScript 7, which has none. `web/lint/lint.test.mjs` checks the rule fires at error level on an `any` in a parameter, a cast and a generic.

`node scripts/mutation_check.mjs` breaks 65 guards one at a time in the real source (the tenant setting, the role switch, each part of the figure guard, the retry, the fallback, each signed header, the CRC32 setting, the HNSW setting, the Reports context, the compiler flags, the lint rule) and expects a test to go red. It restores every file after each run, on exit and on Ctrl-C. Output: [2026-11-09-web-mutation.txt](test-runs/2026-11-09-web-mutation.txt). Its own tests are in `web/tests/mutation-harness.test.ts`.

## Limits

- The Reports page runs from `next start` with `DATABASE_URL` set, because the export is built in Server Actions that read Postgres.
- Only the node-postgres Kysely dialect was run.
- The summary was only ever produced by a scripted fake model and a stand-in server. No real model was called.
- `exceljs` brings in `uuid` below 11.1.1, which `npm audit` rates moderate (a bounds check when a buffer argument is passed). This code never passes one.
- The browser test runs Chromium only.
