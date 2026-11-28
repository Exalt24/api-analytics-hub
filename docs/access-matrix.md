# Four role access matrix

Who may do what to **orders, invoices, payments and commissions** in the ordering portal (`app/access/`, migration `022_access_matrix.sql`, `app/api/portal.py`). Four roles, four resources, five actions, one table, enforced twice.

| Role | Who | Sees and does |
|---|---|---|
| `clinic` | a user of one clinic | its clinic's orders (read, create, update), its clinic's invoices (read, pay), its clinic's payments (read, create) |
| `prescriber` | a prescriber of one clinic | its clinic's orders (read, create, update); no invoices, payments or commissions |
| `sales_rep` | a rep with clinics assigned | read only: the orders, invoices and payments of the assigned clinics, and its own commissions |
| `admin` | the operator | every row of the tenant, every action that has an operation |

## One table

The matrix is `_GRANTS` in [`app/access/matrix.py`](../app/access/matrix.py): a cell is `(role, resource, action)` and holds a scope. The actions are `read`, `create`, `update` (an order's status, finalize or void an invoice), `settle` (pay an invoice) and `refund` (refund an invoice, reverse a commission). The scopes:

| Scope | Rows it reaches |
|---|---|
| `none` | no row; the action is refused |
| `all` | every row of the tenant |
| `clinic` | the rows of the user's own clinic |
| `assigned` | the rows of the clinics assigned to the user |
| `self` | the commissions that name the user as the rep |

A cell that is not written is `none`: the table is closed, so a new role, resource or action grants nothing until it is written. The cells no operation exists for (an order is not paid, a payment is moved by the provider's events and not by a role, a commission is never edited) are `none` for every role, and `matrix.NO_OPERATION` lists them.

The same 80 cells are rows of `access_matrix` in the database. `matrix.seed_sql()` generates them and the migration holds that text between two markers; `tests/test_access_matrix.py` fails when the file drifts from the code, and `tests/test_access_matrix_db.py` compares the live table with the code row by row.

## Enforced twice

**In the API.** Each route carries `requires_access(resource, action)` ([`app/access/principal.py`](../app/access/principal.py)), which looks the cell up before the route body runs and answers 403 for a role holding `none`. Tenant, user, role and clinic come from the API key and nowhere else: the key is hashed and resolved by `portal_resolve_credential`, and a header, a query parameter or a body naming another tenant, clinic or role changes nothing. A missing, unknown or revoked key is one 401. A role the table has no row for is a 403, never a default.

**In the database.** The route then opens its transactions as the caller (`principal.acting(db)`, which gives the billing and fulfilment code a `Database` whose every transaction sets `app.tenant_id`, `app.role` and `app.user_id`, transaction local). Row level security is enabled and forced on every new table, and each policy asks the table at query time:

```sql
portal_allows('invoices', 'read', clinic_id)       -- true / false from access_matrix, the session role and the row's clinic
```

`portal_scope()` reads the cell, `portal_allows()` applies the scope (`clinic` against the user's own clinic, `assigned` against `sales_rep_assignment`, `self` against the row's rep). A change to a cell in the database table changes what is allowed with no other edit, and a test does exactly that. Where a table has a state machine as well (an invoice, an order) a trigger asks the matrix for the action each move needs: finalize and void are `update`, paying is `settle`, refunding is `refund`.

With no role set every portal table answers nothing and accepts nothing. A request role cannot write the matrix or the credentials (no grant), a rep cannot assign itself a clinic or raise its rate, and a clinic user cannot read other users.

## What this does not claim

- **The session settings are the boundary, as `app.tenant_id` already was.** A connection that can run arbitrary SQL as the application role can set `app.role` itself. The settings are set by `Database.session` from the credential, are transaction local, and the pool refuses a role that can bypass row level security; nothing here defends against code that already runs as the application with the pool in its hand.
- **One table is not covered the same way: `billing_payment`.** The wallet engine, its webhook receiver and their tests open it with the tenant only, so its policies are restrictive and let an unset role through (the older tenant wide behaviour). Every portal request sets a role, so the matrix applies to every payment a portal user makes or reads. A test pins both halves.
- **The ledger is tenant level.** `billing_account`, `billing_transaction` and `billing_entry` are not filtered by role: a wallet belongs to the tenant (the organisation that runs the portal for its clinics, for example a clinic group) and not to a clinic. Reading the ledger is not an action in this matrix.
- **The engine acts as itself in a few places** (`engine_actor` in `app/access/engine.py`): writing the commission an invoice earns, recording a provider's answer on a payment row, settling an invoice from a signed event. Each follows an action already authorised, runs as the admin and the system user for the length of one block and puts the caller's settings back, also when the block fails. A test covers the block.
- **A partner event runs as the system user**, not as a clinic: it has no person behind it.

## How it was proved

- `tests/test_access_matrix.py`: the table (closed, 80 cells, the migration's copy identical, statements of intent such as "only an admin creates invoices").
- `tests/test_access_api_db.py`: **generated** from the table, one probe route for each cell and each of six users (120 requests, 200 or 403 as the table says), then the real routes driven as the users who may and must not, the key checks, the credential being the only source of who is asking, and each route's own cell.
- `tests/test_access_matrix_db.py`: **generated** from the table, as the application role and in plain SQL with no API: for each of six users and the 14 operations a resource has (84 tests) the operation is tried against a row of each of the three clinics, and what is allowed is worked out from the cell's scope and the user's own clinic or assignments. The 36 cells with no operation are checked to be `none` in the database and in the code. Then no role, another tenant's user, an unknown role, a rep assigning itself, the matrix and credentials being read only.
- `tests/test_portal_guards_db.py`: each layer under a table refuses on its own (triggers off, policies on; policies off, triggers on), the policies following a cell changed in the database, and the constraints no ordinary flow reaches.
- `tests/mutation_check_fulfillment.py` breaks each guard above, one at a time ([the run](test-runs/2026-11-28-portal-mutation.txt)).
