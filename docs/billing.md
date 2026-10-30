# Prepaid wallet and billing ledger

A module of the hub (`app/billing/`, migration `012_billing.sql`, `scripts/billing.py`) for a multi-tenant SaaS that sells prepaid credit: each tenant has a wallet, tops it up through a Stripe-style payment provider, is charged per use at its own prices, can be refunded, and may hold a subscription whose invoices add included credit. Every movement of money is one balanced entry in a double-entry ledger.

**What was and was not run.** Everything here runs against a fake payment provider ([`fake_provider.py`](../app/billing/fake_provider.py)) written from Stripe's official documentation ([the saved pages and the lines relied on](vendor-contract/README.md)). No Stripe account, no real money and no card data was ever involved, and the only network use was fetching Stripe's public pages. The provider client and the webhook receiver have never been run against a live Stripe account, and the fake was written from the same pages as the client, so it cannot catch a misreading of them. The module never sees a card number: a payment method is a provider token (`pm_...`) and the tests use two invented ones, `pm_fake_ok` and `pm_fake_declined`.

## The plain answer: duplicate charges, accurate records, failed payments

**How a duplicate charge is prevented.** Every command that moves money carries an idempotency key. The same key with the same request returns the first result and writes nothing; the same key with a different request is refused loudly. The payment row, with the key we will send the provider, is saved before the provider is called, and every retry (after a 429, a 5xx or a timeout) sends that same key, so the provider answers with the first result instead of charging again. If every attempt is lost we look the payment up before we ever create another, and an unknown outcome is settled later with the same key. Provider events are written once by their event id, in the same transaction as the ledger lines they cause, and the money is also keyed on the payment, the invoice or the refunded total, so a second event for the same thing credits nothing. Two requests at the same time are serialised by a lock on the wallet.

**How the records stay accurate.** The ledger is double entry and append only: each business event is one transaction whose lines add up to zero, the database refuses one that does not, a wallet cannot go below zero, and a mistake is corrected by another transaction with who and why, never by an edit. A reconciliation function re-derives every balance from the entries and fails loudly if anything disagrees; statements and reports are read from the entries.

**How failed payments and failed requests are handled.** A declined card is recorded with the provider's own code and a notice, credits nothing and is not retried. A rate limit or a server error is retried with backoff and the same key. A timeout whose outcome is unknown is never answered with a second charge. A failure event never undoes a success, a refund that arrives before its payment is held and applied when the payment is, and a fault while applying an event rolls the whole delivery back so the provider's retry applies it cleanly. The sections below show where each of these lives and the test that proves it.

## The ledger

Money is an integer in minor units with its currency beside it, like the rest of the hub. Each tenant has five accounts for each currency it uses:

| Account | Holds | Normal balance |
|---|---|---|
| `wallet` | what the customer can spend (a liability: we owe it) | credit, so stored as zero or below |
| `platform_revenue` | usage charged, and the part of a subscription payment that is not credit | credit |
| `provider_clearing` | money the provider has taken or given back for us | debit |
| `refunds` | usage credited back as a service credit | debit |
| `adjustments` | what an admin added or removed, and subscription credit worth more than the payment | either |

An entry is a signed integer, debit positive and credit negative, so the lines of one transaction add up to zero. A wallet holding 50.00 has a balance of -5000; the module reports its negation as what the customer can spend.

| Event | Lines |
|---|---|
| top-up of 5000 | debit `provider_clearing` 5000, credit `wallet` 5000 |
| usage of 700 | debit `wallet` 700, credit `platform_revenue` 700 |
| refund of 2000 of a top-up | debit `wallet` 2000, credit `provider_clearing` 2000 |
| service credit of 300 | debit `refunds` 300, credit `wallet` 300 |
| admin adjustment of +n (or -n) | debit `adjustments` n, credit `wallet` n (or the reverse), with a reason |
| subscription invoice paid | debit `provider_clearing` paid, credit `wallet` the included credit, credit `platform_revenue` the rest |

### Invariants, and where each is enforced

Every one of these is enforced by the database, so a script, a manual insert or a bug in this package cannot skip it. The request role (`app_request`) is the one the tests connect as; the connection pool refuses to start as a superuser.

| Invariant | Enforced by |
|---|---|
| The lines of a transaction sum to zero, and there are at least two | a deferred constraint trigger (`billing_entry_balanced`, `billing_transaction_has_lines`), so lines can be written one at a time and the check runs once at commit |
| An account, its entries and their transaction share a tenant and a currency | composite foreign keys, so no line mixes currencies |
| Entries and transactions are never updated or deleted | a trigger that holds for the table's owner too, and no `UPDATE` or `DELETE` grant for the request role; a tenant's removal cascades |
| A wallet never goes below zero | `wallet_never_overdrawn` check on the stored balance, which the entry trigger moves in the same statement |
| Nobody writes a balance | the request role can insert an account with its identity columns only; `billing_apply_entry` (security definer) is the one writer |
| Usage charged equals price times quantity | `usage_is_price_times_quantity` check |
| An adjustment carries a reason | `adjustment_needs_a_reason` check |
| A failed payment is never credited; a refund never exceeds what was paid | `failed_never_credits`, `credit_within_payment`, `refund_within_credit` checks |
| A provider event is applied once | unique `(tenant_id, provider_event_id)`, and a unique `(tenant, kind, provider_ref)` for the money it moved |
| One tenant never sees another's rows | row level security enabled **and forced** on all ten tenant tables, one policy reading `app.tenant_id` |

The reconciliation function `billing_discrepancies(tenant)` proves the books agree: it returns one row for every stored balance that differs from the sum of its entries, every transaction that does not sum to zero or has fewer than two lines, and every payment whose credited or refunded total differs from the ledger. `reports.reconcile` adds the trial balance of each currency (every entry added up is zero) and the no-wallet-below-zero check; an empty result is the proof, and `assert_reconciled` raises `ReconciliationFailed` so a caller cannot carry on over bad books.

## The money commands

Every command that moves money runs in one tenant transaction and follows the same order (`wallet.py`):

1. **Take the wallet's lock** (a transaction scoped advisory lock keyed on the wallet account, then read the balance). Commands for one tenant and currency run one after the other, and the second sees what the first committed. It is an advisory lock, not `select for update`, because `FOR UPDATE` needs an `UPDATE` grant and the request role must never be able to write a balance.
2. **Look for the idempotency key.** The same key with the same request returns the original result (`status: replayed`) and writes nothing. The same key with a different request raises `IdempotencyConflict`, loudly, and writes nothing. The request is hashed from the command and its parameters; the key is scoped to the tenant.
3. **Check the funds.** A debit that would take the wallet below zero returns `insufficient_funds` with the shortfall and records nothing at all. The database would refuse it as well; the module keeps ordinary traffic away from that refusal, and if the database refuses anyway it is raised as `LedgerInvariantViolation` and never turned into a polite answer.
4. **Write one transaction**, then let the deferred trigger check it.

`top_up`, `debit_usage` (metering at the tenant's own price table, `billing_usage_price`), `refund_topup`, `refund_usage` (a service credit) and `adjust` (admin, reason required) are in `wallet.py`. A top-up and a refund are also keyed on the **provider's object** (the payment intent, the payment intent and the refunded total), so two routes to the same payment credit it once whatever key either used.

## How the duplicate charge is prevented

A duplicate charge has four sources, and each has its own guard:

| Source | Guard |
|---|---|
| The customer's request is sent twice (a retry, a double click, two workers) | `start_topup` stores a `billing_payment` row under our idempotency key **before** the provider is called; a second request with the same key finds it and calls nothing, with a different amount or method it raises `IdempotencyConflict`. Ten concurrent copies make one row and one provider call (tested). |
| A request to the provider times out, or its answer is lost, after it ran | The provider key is stored on the row before the call and every attempt sends that same `Idempotency-Key`, so the provider answers with the first result (`Idempotent-Replayed: true`) and charges once. If every attempt is lost the client looks the intent up by the metadata it put on it, and if that finds nothing (search lags) it reports an unknown outcome and **never creates again with a new key**; `refresh_payment` settles it later with the same key. A test proves a lost response then a retry leaves exactly one charge at the provider. |
| The provider sends the same event twice, or two events for one payment | The event id is unique per tenant and is written in the same transaction as the ledger lines, so the second delivery writes nothing. Stripe's page says "In some cases, two separate Event objects are generated and sent", so the money is also keyed on the payment, the invoice or the refunded total, not on the event: two event objects for one payment credit once. |
| Two routes reach one payment (the event and a status check) | Both end in `wallet.top_up`, keyed on the provider's payment id; whichever runs second finds the first's transaction. |

## Accurate records

The ledger is double entry and append only, so a figure is never edited: a mistake is corrected by another balanced transaction, with who and why. A stored balance is only ever moved by the trigger that inserts an entry, and `reconcile` re-derives it from the entries. Each transaction keeps its actor, its reason, its idempotency key, the hash of what was asked, and the provider's id for what caused it. Webhook events are stored whole (`billing_webhook_event.payload`) with their final status and the reason, so an event that was ignored or held can be explained later. The statement and the daily report are read from the entries, never from a stored total, so they can disagree with a balance and show it.

## Failed payments and failed requests

- **A declined card** (`card_error`, HTTP 402) is a clear answer: the payment row becomes `failed` with the provider's code (`decline_code` first), a notice is queued in `billing_notification`, nothing is credited, and the request is **not retried**. The table forbids a failed payment with credit. Asking again with the same key returns the failure and does not call the provider.
- **A failure event** (`payment_intent.payment_failed`) is recorded and noticed but never credits, and never undoes a success already recorded; a success that comes after a recorded failure is credited once, because the provider has the last word.
- **429 and 5xx** are retried with exponential backoff (a `Retry-After` is honoured, capped at 30 seconds) and the same idempotency key. If the attempts run out and only 429s were seen nothing was created, so the payment stays `created` and can be asked again; if a 5xx or a timeout was seen the outcome is unknown and is settled with the same key.
- **Another 4xx** (a bad parameter, an `idempotency_error`) is recorded as a failed payment and not retried.
- **A subscription invoice that fails** moves the plan to `past_due` and queues a notice; it never credits.
- **A refund the wallet cannot cover** (the customer already spent the money) is stored as held with a notice and applied by `reprocess_held` when the balance allows. The wallet cannot go negative, so the provider's refunded total may exceed ours only while such an event is held.

## The webhook receiver

`webhooks.receive` (and `handle_http`, which turns it into a status and a body) does three things in this order. Details are in the module's docstring.

1. **Verify first.** `signature.verify` checks the `Stripe-Signature` header exactly as Stripe documents: `t` and `v1`, an HMAC SHA256 over `timestamp.payload` keyed with the endpoint secret (the raw body bytes, never a reserialised one), a constant time compare against every `v1`, several `v1` values accepted while a secret is rolled, only scheme `v1` honoured, and a recency window (300 seconds by default, applied to the absolute difference, and a tolerance of zero or less refused). Nothing is read, looked up or written for an event that fails. The sender is told only `400 invalid webhook`; the reason goes to our log.
2. **Route.** The event arrives before anyone knows whose it is. The receiver asks the narrow `billing_router` role (it can read payments and subscriptions for their provider ids, and nothing else) and routes by **our** rows: the payment's provider id or our payment id, the subscription's customer or subscription id. A body that names another tenant than the one its payment belongs to is refused. An event with nothing of ours to match (a refund that beat its payment) is answered `503` so the provider sends it again.
3. **Write once.** One tenant transaction inserts the event row, applies the handler inside a savepoint and records the final status. A handler fault rolls the whole delivery back, the answer is a 5xx and the provider's retry applies it; a known refusal (a payment of another amount than we asked for, a subscription we do not hold) is stored as `held` with its reason.

| Event | Effect |
|---|---|
| `payment_intent.succeeded` | credit the wallet once (the amount must match what we asked for, else held) |
| `payment_intent.payment_failed` | record the failure and a notice, never credit |
| `charge.refunded` | reverse exactly the newly refunded part of the charge's `amount_refunded`; an older or repeated total reverses nothing |
| `invoice.paid` | book the payment, the plan's included credit in proportion to what was paid (rounded down), the rest as revenue; the plan is active again |
| `invoice.payment_failed` | `past_due` and a notice; nothing credited |
| `customer.subscription.updated` | the provider's status (mapped, or kept as `provider_status` only when unknown) and the plan by price id |
| `customer.subscription.deleted` | `canceled`, which is final |
| anything else | stored as `ignored` |

**Out of order.** Stripe's page says it "doesn't guarantee the delivery of events in the order that they're generated" and not to use `created` for ordering, so the money side needs no order (it is keyed on the object and applied whenever it arrives), a refund that beats its payment is held and applied when the payment is, a canceled subscription cannot be revived by a late event, and the state side applies an event only when its `created` is not older than the newest applied.

## Subscriptions and proration

`billing_plan` (global, read only to the request role) holds a code, a monthly price and the credits included in it. `billing_subscription` is one per tenant: `active`, `past_due` or `canceled`, the provider ids, and the period. A mid cycle plan change (`change_plan`, idempotent on its own key) is computed in code by `proration.prorate`:

- `remaining` is the whole seconds from the change to the period's end, clamped between none and the whole period.
- `credit = old_price * remaining / period` and `charge = new_price * remaining / period`, each **rounded half up** to a whole minor unit with integers only, in `(2n + d) // 2d` form.
- `net = charge - credit`, the difference of the two rounded lines, never rounded again, so the lines a customer sees add up to what they are asked to pay.

The rule reproduces the one worked example on Stripe's proration page (a 10 USD plan to a 20 USD plan halfway through the period bills 5 USD more). The page does not say how Stripe rounds, so the rule is this module's own and nothing claims to match a live invoice to the cent. The money for the difference is billed by the provider; the wallet moves when that invoice is paid.

## Reports

`reports.py`, as markdown and CSV, read from the entries:

- **Statement** (`statement`): opening balance, each line (what the customer can spend changed by, and the running balance), closing balance. Opening plus the lines is the closing balance, and a range ending today agrees with the wallet.
- **Daily revenue and usage** (`daily_report`): for each UTC day the top-ups, subscription credits, usage, refunds, service credits, adjustments, platform revenue and failed payments, and the usage by event type. A failed payment is counted on the day its row was recorded.
- **Reconciliation** (`reconcile`, `assert_reconciled`): above. `scripts/billing.py reconcile` exits 1 and names every disagreement.

A CSV cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets an apostrophe in front, because a spreadsheet would run it as a formula and reasons, actors and provider ids are text somebody else chose. Amounts are written from integers and are not run through it, so a negative amount stays a number. A pipe in a markdown cell is escaped.

## Run it

Needs Docker (the compose Postgres) and the repo's Python requirements. From the repo root, in Git Bash. The seed drops the `public` schema of the database it points at, so use the compose database or a scratch one.

```bash
docker compose up -d --wait
. .venv/Scripts/activate                       # macOS or Linux: . .venv/bin/activate
python seed_billing_demo.py                    # applies every migration, loads 2 plans and 2 invented tenants; prints their ids and the next commands
export DATABASE_URL="postgresql://<user>:<password>@127.0.0.1:5433/analytics"   # the compose file's own credentials (compose.yaml)
python scripts/billing.py demo --tenant-a <id A> --tenant-b <id B> --out-dir out     # plays the week, prints and writes the statements, daily reports and reconciliations
python scripts/billing.py --tenant <id A> reconcile
python scripts/billing.py --tenant <id A> statement --currency AUD
python scripts/billing.py --tenant <id A> debit --event-type sync_run --quantity 3 --key try-1    # run it twice: the second says replayed and changes nothing
python scripts/billing.py --tenant <id A> debit --event-type sync_run --quantity 4 --key try-1    # same key, different request: refused
python scripts/billing.py --tenant <id A> simulate-webhook --type charge.refunded --payment <first 8 characters of a payment id> --amount-minor 300 --repeat 2
python scripts/billing.py --tenant <id A> simulate-webhook --type payment_intent.succeeded --payment <...> --tamper       # 400, nothing written
```

The demo is two invented tenants with their own prices for the same events. Orchard Analytics starts on Starter and Birchwood Outfitters on Growth. Over a week of UTC days both are metered; Orchard's second top-up is delivered twice and written once; a top-up with the declined test token fails with `insufficient_funds` and credits nothing; Orchard moves from Starter to Growth part way through the period (credit, charge and bill are printed) and its invoice is booked; a 20.00 top-up is refunded and reversed exactly; a batch of 1000 reports is refused for lack of funds and records nothing; Birchwood gets an admin credit with its reason and then a failed subscription invoice, which makes it `past_due` with a notice. It ends with both tenants reconciled. A second run on the same tenants is refused, because the fake provider's payments live in memory and could not be settled again.

`scripts/billing.py` takes the tenant from `--tenant` or `BILLING_TENANT`. Each run starts its own fake provider, so `refund` teaches the fake the stored payment before asking it. The signing secret is the fake's own; nothing reads a real key. A login role that is not a superuser needs `grant billing_router to <login>` before the receiver can use the router role.

## Tests and mutation controls

Eight test files (`tests/test_billing_*.py`, plus `tests/billing_fixtures.py`):

- `test_billing_signature.py` and `test_billing_proration.py` (pure): the header, tampering of every byte over seeded random bodies, the window and its edges, rotation, downgrade, malformed headers; the proration against an exact fraction oracle over a generated table and Stripe's worked example.
- `test_billing_provider.py` (pure, an httpx mock transport): the request shape, the same key on every retry, backoff, a card error not retried, the lost-response trap ending in one charge over many seeded failure patterns.
- `test_billing_ledger_db.py`, `test_billing_payments_db.py`, `test_billing_webhooks_db.py`, `test_billing_reports_db.py` (Postgres, as the request role): the migration twice, forced row level security with negative tests (tenant A cannot see or write tenant B's rows in any table, the request role cannot edit the ledger or write a balance, the router cannot read the ledger), unbalanced, one-line and cross-currency transactions refused at commit, the overdraft refused by the module and by the database, a 30-debit race against a wallet that covers ten, 20 concurrent copies of one debit, ten concurrent copies of one webhook, a fault that rolls the event back with the ledger, held and out-of-order events, the reports and the CSV neutralising.
- `test_billing_simulation.py`: seeded worlds of failures, retries, duplicate, late, tampered and stale events, refunds in pieces and concurrent bursts, checked against invariants after every step and again at the end (one charge at the provider for each payment asked for, each credited once, the wallet equal to what came in less what went out).
- `test_billing_demo_cli.py`: the demo's facts and the command line, including exit 1 on bad books.

`tests/mutation_check.py` carries 35 guards for this module, each breaking one rule and requiring the scoped billing suite to go red: the balance trigger and its two-line minimum, append only (the trigger and the grant), the overdraft check (in the database and in the module), the wallet lock, same key same result, same key different request, event dedupe, the signature check, the stale timestamp, the v0 downgrade, the constant time compare, a failed payment carrying credit, a late failure after a success, refund reversal (the new part only, and a held refund), a refund over what is refundable, a retry without the idempotency key, an unknown payment settled with a new key, a card error retried, proration rounding, an older subscription event over a newer state, a swallowed fault, tenant isolation (the ledger policy, the payment policy, forced security, the router grant), the usage-amount check, the adjustment reason, and CSV formulas. Run it in a copy of the repository, because it edits source in place. The result of the run is in [the saved output](test-runs/2026-10-30-billing-mutation.txt).

## Not done

- **No live Stripe account and no money.** The client and the receiver are tested only against the fake. The first real account will find whatever a shared misreading of the pages hides (for example whether `confirm=true` with a payment method needs a `return_url` on a live account).
- **No card data.** The module holds a provider token and never a card number; collecting a card (Elements, a hosted page) is the front end's job and is not built here.
- **No tax and no invoice PDFs.** Amounts are what was charged; nothing computes a tax or renders an invoice.
- **No currency conversion.** A wallet is for one currency; an invoice in another currency than its plan is held, and a plan change cannot change currency.
- **No scheduler.** Nothing renews a subscription, retries a held event, expires a stuck payment or runs the reports on a clock; each is a function or a command.
- **The receiver is not mounted on the HTTP API.** It is `webhooks.handle_http` and `scripts/billing.py simulate-webhook`; wiring it to a route and the signing secret to configuration is a few lines and is not done.
- **It answers after it has applied the event**, where Stripe's page asks for a quick 2xx "before any complex logic". The work is one short database transaction; a slow handler would need the event queued first.
- **Event order within one second is not resolved.** The page says not to use `created` for order; the state side does and applies events that share a second in arrival order. The remedy the page names (retrieve the subscription from the API) is not implemented. The money side does not depend on order.
- **Access is not revoked** for a canceled or unpaid subscription (the module has no access control), and `invoice.paid` does not re-read the subscription before extending anything, as the page advises.
- **A refund the wallet cannot cover is held, not recorded as a debt.** The provider has already given the money back; the customer's spent credit is not clawed back below zero.
- **Backdated entries** (`at`, used for a provider event's own time and by the demo) are checked against the balance when they are written, not at their date.
- **Failed payments in the daily report** are counted on the day the row was recorded.
- **`livemode` is not checked.** A live event delivered to a test configuration would be signed with a different secret and rejected, but nothing refuses it by its flag.
- **The included credit rule is this module's own** (the plan's credits in proportion to the share of the price paid, rounded down); Stripe has no such concept.
- **Plans are loaded by the table's owner.** The request role can only read them; there is no admin screen.
