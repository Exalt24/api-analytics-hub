The design decisions behind the connectors, storage and the tenant boundary. The run steps are in the [README](../README.md).

# Architecture

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names, so `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means one connector and a registry entry; the scheduler,
the API and the dashboard do not change.

**Money is an integer in minor units with its currency beside it.** A
`currency_iff_money` CHECK enforces that in the database as well as at the connector boundary. Parsing
uses `Decimal`, since `float("19.99")` is `19.989999999999998`.

**History is accumulated, not fetched.** Snapshots are dated rows per (connection,
metric, day), keyed on the date the *platform* attributed the value to, so 90-day growth is answerable when an API only exposes a current value.

**Revenue is attributed to `processedAt`, not `createdAt`.** They differ for every
imported order. `createdAt` renders a migrated shop's history as one enormous day.

**The tenant boundary is in the database.** Row level security is enabled *and
forced* on every tenant table (the exception is `api_key`, read before the tenant is known), with policies that read a transaction-local
GUC. The pool drops into a non-superuser role and refuses to start if that role
can bypass RLS, so a forgotten `WHERE tenant_id` cannot leak.

**Credentials are encrypted with the row bound in.** AES-256-GCM, and the AAD is
`(tenant_id, connection_id, platform)`. RLS stops a tenant *reading* another row, not a ciphertext being *copied* between rows; the binding makes a moved blob undecryptable.

**Syncs are leased, not flagged.** `claim_sync` takes a non-blocking advisory lock, so a
scheduled run and a Sync Now click cannot overlap; the claim expires unless renewed, and a
reaper turns a silently dead worker into a visible failure.
