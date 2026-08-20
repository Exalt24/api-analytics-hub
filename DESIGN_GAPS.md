# Known gaps, in priority order

Findings from a research pass on 2026-08-20 across four areas: sync orchestration,
the five other platforms this design claims to support, dashboard UI references,
and credential security. Everything here is a gap in what exists today, with the
specific silent failure it causes. Sources are linked at the bottom of each section.

**Read this before adding a feature.** Several items are cheap now and expensive
after the first tenant, and two are schema decisions that are nearly free while the
tables hold zero rows.

---

## Already fixed in this session

- **Batch upsert aborting on duplicate keys.** Verified against this Postgres:
  two rows with the same conflict key in one statement raise `ON CONFLICT DO
  UPDATE command cannot affect row a second time` and kill the whole batch. Two
  pages of a paginated fetch covering one day produce exactly that. Fixed with
  `DISTINCT ON` in `upsert_snapshots()` (`migrations/002_control_plane.sql`).
- **`status='running'` forever.** A killed worker left a run that could not be
  distinguished from one still working, so the connection never synced again and
  never alerted. Fixed with `lease_expires_at`, `heartbeat_at`, `claim_token`,
  `worker_id` and `reap_expired_runs()`.
- **Two schedulers starting the same connection.** At claim time no run row
  exists, so `FOR UPDATE SKIP LOCKED` has nothing to lock. Fixed with
  `pg_try_advisory_xact_lock` keyed on the connection id, which exists whether or
  not a row does.
- **No across-run watermark.** `sync_run.resume_cursor` is within-run pagination
  state only, so every run re-derived its window from `now()`, losing data when a
  run was skipped and refetching everything when it was not. Fixed with
  `connection_stream_state`.

---

## P0: schema decisions that are nearly free now

### 1. Credentials are not actually encrypted

`001_init.sql` says "Fernet ciphertext" and `registry.py` says "encrypted at
rest". **No crypto module exists in this tree.** Nothing produces or consumes
`credentials_enc`. That is a comment describing code that was never written.

### 2. Use AES-256-GCM with AAD, not Fernet

Fernet is AES-128-CBC + HMAC and **has no AAD parameter**. Without AAD binding
`tenant_id || connection_id || platform`, a ciphertext blob copied from tenant B's
row into tenant A's row **decrypts cleanly**. RLS does not prevent that; only AAD
does. OWASP's current at-rest recommendation is AES-256-GCM or
ChaCha20-Poly1305.

### 3. `platform_connection` needs `key_version` and `dek_wrapped`

Without a key version column there is no two-key overlap window, so any key
rotation becomes a stop-the-world migration. With a per-tenant DEK wrapped by a
KEK in KMS, rotation re-wraps DEKs and never touches ciphertext or re-authorises a
tenant. NIST SP 800-57 puts symmetric key-wrapping cryptoperiods at two years or
less, so this comes due whether or not it is planned for.

Per-tenant DEKs also buy **cryptographic offboarding**: destroy one tenant's DEK
and their tokens are unrecoverable without touching another row, which is a much
cleaner deletion story than `DELETE`. Use one KEK with per-tenant DEKs, not a KMS
key per tenant, which is a cost and quota trap.

### 4. A `Secret` wrapper type

`registry.redact()` is opt-in: it only redacts a dict someone deliberately passed
through it. A connector holding `access_token` as a plain attribute leaks it via
any dataclass `repr`, f-string, `logger.exception` frame locals, or error-tracker
event. Needs a type whose `__repr__`/`__str__` return `'**********'`, plus
`__reduce__`/`__getstate__` overridden or pickle and `deepcopy` carry the
plaintext. Cheapest high-value item on this list.

Note if using Pydantic's `SecretStr`: it has leaked contents in validation error
messages (pydantic#8303) and in serialisation (#6778), so do not rely on it alone.

---

## P1: needed before the second connector

### 5. `fetch()` cannot express an async report job

Four of the five other platforms are **submit → poll → download**, not a
synchronous query:

| Platform | Async model | Handle expiry |
|---|---|---|
| Amazon SP-API | `createReport` → poll → `getReportDocument` | pre-signed URL dies in **~5 minutes** |
| Meta | `POST /insights` → `report_run_id` → poll | **30 days**, jobs up to ~60 min |
| TikTok | create task → `task_id` → poll → download file | required for long ranges |
| Pinterest | create report → poll `report_status` | required for large pulls |

Needs `submit(window) -> handle`, `poll(handle) -> {pending|ready|empty|failed}`,
`download(handle) -> pages`, with the handle persisted across restarts. Also a
distinct **`empty`** terminal state: Amazon's `CANCELLED` means "no data for that
window", which is success with no rows, not an error.

### 6. `max_history_days` as one integer is wrong

It needs to be a function of `(metric_family, granularity)`:

- **Meta**: 37 months for aggregates, **13 months** for hourly and unique-count
  breakdowns, **6 months** for frequency breakdowns. Since 2025-06-10, `reach`
  beyond 13 months requires an async job, capped at **10 per ad account per day**.
- **Pinterest**: 90 days max at daily granularity, but **8 days back with a 3-day
  max range** at hourly.
- **GA4**: bounded by the customer's own **data retention setting** (14 months
  default, 26 or 50 max), discoverable only at runtime, and changes are not
  retroactive.
- **Amazon**: per report type, with a 2-year floor on orders, and generated
  reports retained only 30 days.

Also needed separately: **`max_window_span`** (TikTok ~30 days, Pinterest 90),
which forces the orchestrator to chunk a backfill into legal sub-windows.
Chunking is a platform requirement, not an optimisation.

### 7. Snapshot vs interval as a first-class point kind

Instagram `followers_count`, TikTok creator followers, Pinterest followers and
Amazon inventory are **point-in-time**. There is no history to fetch, so if a row
is not written today that day is gone permanently. `supports_backfill: false` does
not capture this: it is not "backfill unsupported", it is "**we are the system of
record for this series**", which means the scheduler owes a guaranteed daily tick
independent of any user-triggered sync.

### 8. Restatement windows: already-fetched numbers change

Shopify orders are essentially immutable. Nothing else here is:

- **Meta**: attribution windows restate for ~7 days; the 2025-06-10 change now
  ignores `use_unified_attribution_setting` and folds actions into `1d_click`/`1d_view`.
- **TikTok**: configurable click/view windows, and `stat_time_day` is in the
  **advertiser's** timezone, not UTC.
- **Amazon**: settlements, refunds and chargebacks restate revenue for weeks.
- **GA4**: not final for 24–48h, and data-driven attribution with up to a 90-day
  lookback rewrites historical channel credit.

Needs per-connector `restatement_window_days` driving a rolling re-fetch of
`[watermark - window, now]`, and for Meta/TikTok/Pinterest **the attribution
window belongs in the metric's identity key**, because the same (entity, date)
legitimately has several correct values.

### 9. Data-quality metadata on the page, not just rows

GA4 alone can return sampled rows, silently **thresholded (missing)** rows, and an
`(other)` cardinality bucket; Search Console omits rare queries by design, so
`sum(query rows) < site total` on purpose. Pages need
`{is_sampled, is_thresholded, has_other_bucket, is_partial}` so the aggregation
layer can refuse to present a number that does not sum.

### 10. Auth as a capability set, not an enum

`AuthKind` collapses distinctions that cost real work:

- **Meta has no refresh grant.** You extend a 60-day long-lived token, or use a
  non-expiring System User token.
- **Amazon needs three token types**: LWA user token, grantless-by-scope tokens,
  and a per-path **Restricted Data Token** minted per restricted call.
- **GA4/GSC** use service accounts granted **out of band via IAM**, so there is no
  consent redirect at all and onboarding cannot assume one.
- **Pinterest**: 60-day continuous refresh token, so **idle means permanently
  broken** and a keep-alive refresh job is mandatory.

### 11. Quota is keyed by tenant, not by connector

Meta/Instagram quota is derived from the tenant's own traffic
(`4800 × impressions` per 24h), and Amazon's dynamic usage plans scale with the
seller's business metrics. A global limiter starves small tenants and under-uses
large ones. Budget state belongs on `(tenant, endpoint_group)`.

### 12. Units and day boundaries differ per account

Pinterest returns **micro-currency** (divide by 1e6). Amazon returns
`{amount, currencyCode}` objects per marketplace. Meta and TikTok are per-account
currency. Day boundaries: TikTok is advertiser-local, Amazon marketplace-local,
Pinterest UTC, GA4 property-local. Canonical points need explicit `currency`,
`unit_scale` and `day_definition`, resolved by a **discover step that runs before
the first data fetch** and currently has nowhere to live.

---

## P1: operational

### 13. Freshness SLO monitor

Alert on the **absence of success**, never the presence of failure. A pipeline
that ran and processed nothing reports success. Concretely: `age(last_succeeded_at)
> 3 × cadence`, zero runs started in a cadence window, `rows_written == 0` where
the trailing median is above zero, and a staleness check on `max(observed_on)`
itself. The schema comment in 001 already names this failure; nothing watches for
it yet.

### 14. Schema-shape fingerprinting

Table exists in 002 (`payload_shape`); nothing populates it. A field renamed
`spend` → `spend_micros` raises no exception: it charts as null, or as a million
times the truth. Classify `non_breaking` (new field, log and continue) versus
`breaking` (a mapped-from field or the cursor field disappeared or changed type →
**auto-pause the connection**).

### 15. Quarantine population and replay

Table exists in 002; nothing routes to it. Per-record `UpstreamBroken` should
quarantine rather than abort a 90-day backfill, and quarantine **volume** is how a
schema change announces itself. Replay must re-validate, and fixes apply to the
whole affected cohort, not record by record, which is what `connector_version` on
the run and the quarantine row is for.

### 16. Backfill as a separate job class with tenant fairness

Priority alone starves the low lane when the high lane is continuously
replenished. Needs a **fairness key of tenant** with round-robin, or one new
tenant's two-year backfill monopolises every worker and trips #13 for everybody.

### 17. Cross-process refresh lock

`tokens.py` uses `asyncio.Lock`, which coordinates **within one process only**.
Two workers reintroduce exactly the mint-storm the docstring warns about. Needs
`pg_advisory_xact_lock(hashtext(connection_id))`, and the refreshed token must be
**persisted atomically with the refresh**: many providers rotate the refresh token
on use, so a crash between "provider issued a new one" and "we saved it" bricks
the connection permanently. `TokenManager` currently has no persistence callback
at all.

### 18. Offboarding executed as code

On disconnect, in order: call the provider's **RFC 7009 revocation endpoint**
(deleting our copy leaves a token the provider still honours), delete webhook
subscriptions, null the credential and destroy the tenant DEK, **cancel queued and
in-flight sync jobs** (the most commonly forgotten one, and it presents as a
worker retrying a revoked credential forever), purge caches, scrub
`metric_snapshot.raw` (it holds untranslated vendor payloads, i.e. PII, and the
`on delete cascade` never fires if the connection is merely marked `revoked`), then
write the audit event.

For Shopify specifically, `app/uninstalled` arrives with the token **already
revoked**, so any API-based cleanup must happen before it, not after. The mandatory
`customers/data_request`, `customers/redact` and `shop/redact` webhooks are absent
from `ANALYTICS_TOPICS`; `shop/redact` arrives 48h after uninstall and is required
for app-store approval.

### 19. Webhook secret rotation with an overlap window

`WebhookReceiver.secret` is a single string, so rotation is a hard cutover that
drops every in-flight delivery and its 48h retry backlog. The lifecycle differs
from an access token in a way most designs conflate: a webhook secret is
**per-app, shared across every shop, rotated only by us, with no refresh concept**,
which makes it a single point of compromise across all tenants and deserves
shorter custody than a tenant credential. Verify against `{new, old}` for 24–48h,
then retire, and **emit a metric when the old secret succeeds** so the cutover can
be proven rather than assumed.

Also missing and cheap: a replay window. The dedup log is bounded at 4096 entries,
so a delivery replayed after eviction verifies and reprocesses.

### 20. Column-level revoke on the credential

`grant select on all tables to app_request` lets the request-serving tier read
`credentials_enc` for its own tenants. One line fixes it:
`revoke select (credentials_enc) on platform_connection from app_request`. Pair
with granting KMS decrypt only to the worker identity, so the web tier cannot
unwrap even the ciphertext it can see.

### 21. Audit trail separate from operational logs

`sync_run` is good operational logging and is **not** a security audit trail.
Needs an append-only `credential_audit` recording `decrypt|use|refresh_ok|
refresh_fail|revoke|rotate`, actor (human vs worker), source, `key_version`, and a
**truncated salted-HMAC fingerprint** of the token so events can be correlated
without storing the secret. Grant insert only: the current blanket grants would let
the application rewrite its own audit history.

### 22. Secret scanning before the first commit

This repo has **zero commits**, which is the cheapest possible moment to add
pre-commit and CI secret scanning. After the first commit a leaked fixture is
permanent history.

---

## Frontend, when it gets built

Read from live UIs rather than marketing copy: Plausible's demo, a Fathom shared
dashboard, and Metabase's embedding demo, plus Databox's own product screenshots.

- **KPI tiles are controls, not readouts.** Plausible's active tile is filled and
  drives one shared chart below. Best-in-class does **not** put a sparkline in every
  tile.
- **Delta colour is semantic, not directional.** Bounce rate up is *bad* and is
  coloured accordingly, while duration down is also bad. A naive tile paints every
  up-arrow green.
- **Comparison is opt-in but delta is not.** Fathom defaults to
  `comparison=none` in the URL; Plausible still shows percent-vs-previous on the
  tiles and reserves "Compare" for a second charted series. Copy that split.
- **The date trigger shows the resolved range**, not just the preset: "Last 7
  Days: Aug 14 to Aug 20, 2026". Presets rail plus a dual-month calendar in one
  popover, state URL-encoded so a view is shareable. Plausible uses 7/28/91 days
  deliberately, whole weeks, which removes day-of-week bias from deltas.
- **Tenant switcher: a select-only combobox, NOT a menu button.** Checked against
  the W3C APG: a menu button "does not have a value so is not suitable for
  conveying the user's choice in its collapsed state", and the older collapsible
  dropdown listbox pattern is **deprecated**. Metabase's demo ships
  `aria-haspopup="menu"`, which means a screen-reader user hears the control's name
  but not which client is selected. In a tool where every number belongs to
  whichever client is active, mis-reading client A's revenue as client B's is the
  worst failure the product has.
- **Zero, blank and stale must be three visually distinct things.** Databox
  separates "Missing permissions" with a Reconnect button, "Temporary sync delay"
  naming the rate limit and promising the next attempt, and "Could not reach host
  in time". Status is a first-class column so an agency sees every client's
  connector health in one list rather than tile by tile.
- **The caveat travels with the number.** A per-tile amber corner badge whose
  tooltip stacks the specific caveat and the freshness line beats a page-level
  "some data may be delayed" banner, which is unattributable: the user cannot tell
  which of twelve tiles is lying, so they distrust all twelve.
- **Draw the incomplete current period differently.** Fathom switches the final
  chart segment to a dotted line into today. Otherwise today renders as a solid
  catastrophic drop and generates a support ticket every single day.
- **Stamp the timezone in the chrome.** Fathom shows a clock icon and `UTC`. For an
  agency comparing a Shopify day boundary against an ad platform's, this prevents a
  whole class of "your numbers don't match mine" arguments.

---

## Not claimed

No reference was found for Shopify's own analytics UI (Cloudflare interstitial
from headless Chromium) or GA4's report UI (login-gated), so nothing here
describes them from first-hand observation.
