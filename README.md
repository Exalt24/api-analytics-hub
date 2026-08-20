# api-analytics-hub

Multi-tenant analytics over stored platform data. External API → scheduled sync →
normalize → PostgreSQL → backend → dashboard. The dashboard **never** calls a
vendor API to render a page; it reads snapshots the sync wrote.

Currently one live connector, **Shopify Admin GraphQL**, verified against a real
development store.

---

## Run it

Requirements: Docker, Python 3.13, Node 20+.

```bash
# 1. Postgres
docker compose up -d                      # exposes 5433, deliberately not 5432

# 2. Backend
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
export CREDENTIAL_KEYS="1:$(./.venv/Scripts/python.exe -c 'from app.crypto import generate_key; print(generate_key())')"
export DASHBOARD_ORIGIN="http://localhost:3100"

# migrations, in order
for f in migrations/00*.sql; do psql "$DATABASE_URL" -f "$f"; done

./.venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8000

# 3. Dashboard
cd web && npm install
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npm run dev -- -p 3100
```

`seed_local.py` loads real figures from the dev store and prints an admin and a
viewer API key, which is the fastest way to see the role split in the browser.

Paste a key into the browser console once: `localStorage.setItem('apiKey', '<key>')`.

### Ports

Postgres is on **5433** and the dashboard on **3100**, both non-default. Every
other Postgres on a developer machine is on 5432 and something is always already
on 3000; a collision there presents as an authentication error against the wrong
database, which is a genuinely confusing hour.

---

## Layout

```
app/
  connectors/        one module per platform + the shared contract
    base.py          Connector protocol, CanonicalPoint, error taxonomy
    shopify.py       Admin GraphQL: pacing, day bucketing, money, degradation
    shopify_oauth.py install flow, offline tokens
    shopify_webhooks.py  HMAC verification over the raw body
    tokens.py        refresh behind one lock, absolute expiry
    registry.py      per-platform auth kind and rate-limit style
  crypto.py          AES-256-GCM credential encryption, AAD bound to the row
  db.py              pool, tenant-scoped transactions, RLS role assertion
  sync/runner.py     claim, lease, heartbeat, resume, finish
  api/               FastAPI, bearer auth, RBAC
migrations/          001 core + RLS, 002 control plane, 003 API keys
web/                 Next.js dashboard
tests/               167 tests + a mutation harness
```

---

## Architecture, briefly

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names, so `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means writing one connector and registering it; the scheduler,
the API and the dashboard do not change.

**Money is an integer in minor units with its currency beside it**, enforced by a
`currency_iff_money` CHECK in the database as well as at the connector boundary,
because a later connector or a manual backfill bypasses the Python guard. Parsing
uses `Decimal`: `float("19.99")` is `19.989999999999998` and a day of orders drifts
by cents. Zero-decimal currencies such as JPY are not scaled.

**History is accumulated, not fetched.** Snapshots are dated rows per (connection,
metric, day), never overwritten, keyed on the date the *platform* attributed the
value to. That is what makes 90-day growth answerable when an API only exposes a
current value, and it is why the clock starts when a connector goes live. Shopify
exposes only 60 days of orders without approval for `read_all_orders`.

**Revenue is attributed to `processedAt`, not `createdAt`.** They differ for every
imported, migrated or backdated order, and the Admin API lets you set the former
and not the latter. Bucketing on `createdAt` renders a migrated shop's entire
history as one enormous day, with every individual figure correct.

**The tenant boundary is in the database.** Row level security is enabled *and
forced* on every tenant table, policies read the tenant from a transaction-local
GUC, and the pool drops into a non-superuser role and refuses to start if that role
can bypass RLS. The API never writes `WHERE tenant_id = ...` for isolation, so a
forgotten clause cannot leak anything.

**Credentials are encrypted with the row bound in.** AES-256-GCM, and the AAD is
`(tenant_id, connection_id, platform)`. RLS stops a tenant *reading* another row and
does nothing about a ciphertext being *copied* between rows by anything with write
access; the binding makes a moved blob undecryptable.

**Syncs are leased, not flagged.** `claim_sync` takes a non-blocking advisory lock,
so a scheduled run and a Sync Now click cannot overlap. The claim expires unless the
worker keeps renewing it, and a reaper turns a died-silently worker into a visible
failure instead of a row stuck at `running` forever.

---

## Testing

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q      # 167
./.venv/Scripts/python.exe tests/mutation_check.py  # 18 guards
./.venv/Scripts/python.exe live_check.py            # against the real store
```

The mutation harness is the part worth reading. It breaks each load-bearing guard
in turn and asserts the suite goes red, because a green suite is not evidence. It
has caught four of this project's own vacuous tests, including one that measured a
constant using that same constant, so mutating the constant moved the test with it.

---

## What I would improve for production

Honest list, in the order I would actually do it.

1. **Nothing is deployed.** It runs locally and against a live Shopify store; there
   is no hosting, no TLS termination, no managed Postgres, no backups. That is the
   first gap and it is not a small one.
2. **The scheduler is a runner without a clock.** `sync/runner.py` claims, leases,
   heartbeats and resumes correctly, and something still has to *call* it on a
   cadence. The schedule table and jitter column exist; the daemon does not.
3. **Key management is an environment variable.** `CREDENTIAL_KEYS` should be a KMS
   or a secrets manager with an audited rotation path. The versioned envelope means
   rotation is already a config change rather than a migration, which is the part
   that is expensive to retrofit, but the key still sits in the process environment.
4. **API keys are the whole auth model.** No users, no sessions, no SSO, no
   per-key scoping beyond the three roles. Fine for a service integration, not fine
   for a team of people.
5. **`customers` is a per-day distinct count and cannot be summed.** The dashboard
   shows buyers per day for exactly this reason. A true window-distinct needs either
   identities retained per day or an aggregate computed at source, and both cost
   storage or an extra call.
6. **One live connector.** The abstraction is only honest once a second platform
   lands: the second connector is what reveals whether the first one's field names
   leaked into the core. I would build Meta or GA4 next, before adding features.
7. **No alerting.** Failures are recorded in `sync_run` and visible on the
   dashboard, and nobody is told. A failed sync at 03:00 should page someone or at
   minimum post to a channel.
8. **Quarantine is a table, not a workflow.** Rows that will not normalise are
   captured, and there is no way to review or replay them.
9. **Observability stops at the run log.** No traces, no metrics, no structured
   logs; diagnosing a slow sync means reading code.
10. **Backfill is manual.** Windows are passed by hand. A real backfill needs
    chunking, resumability across process restarts, and a rate-limit budget shared
    with the live syncs so a backfill cannot starve them.
