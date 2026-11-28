# Partner fulfilment

Orders sent to a fulfilment partner (for example a pharmacy that packs and ships) and the partner's status events (`app/fulfillment/`, migration `024_fulfillment.sql`, `app/api/portal.py`). An order here is **an opaque reference and a clinic**: no name, address, date of birth, diagnosis or drug is stored, audited or logged. The partner is given the reference and the clinic's short code and matches them to the prescription through the clinic's own secure channel, which is outside this system.

**What was and was not run.** Everything ran against a stand-in partner (`tests/fulfillment_stand_in.py`, a real localhost HTTP server) written from the contract below. The contract is **ours**, stated here and written once more as the stand-in; it is not a real partner's documentation, so a real partner's own pages decide what changes. No real partner and no real patient data was involved.

## The contract with the partner

**We send** `POST {base}/v1/orders` with `Authorization: Bearer <key>`, `Content-Type: application/json`, `Idempotency-Key: <8 to 120 characters>` and the body `{"order_ref": "ord_<24 hex>", "clinic_ref": "<clinic code>"}`. Nothing else is in the body.

| Answer | Meaning | What the client does |
|---|---|---|
| 201 `{"partner_order_id": "po_..."}` | the order was made | done |
| 200, the same body, `Idempotent-Replayed: true` | the same key and body again: the first answer, nothing made twice | done |
| 400 `invalid_order` / `idempotency_key_required`, 401 `unauthorized`, 415 `json_required`, 422 `idempotency_key_reused` or `duplicate_order` | a permanent no | never retried, dead letter |
| 409 `request_in_progress` with `Retry-After` | the first copy of this key is still being handled | retried, same key |
| 429 with `Retry-After`, 408, 425, 500, 502, 503, 504, a timeout, a dropped connection | transient | retried, same key |

**They send** each status event as `POST` to our receiver (`/api/portal/webhooks/fulfillment`) with `X-Partner-Signature: t=<unix time>,v1=<hex>`, the hex being an HMAC SHA256 over `<t>.<raw body>` under the shared secret (the scheme of `app/billing/signature.py`; several secrets are accepted while one is rolled) and the body

```json
{"event_id": "evt-123", "order_ref": "ord_...", "status": "accepted | packed | shipped | delivered | rejected | cancelled", "occurred_at": 1760000000,
 "carrier": "SwiftPost", "tracking_number": "TRK-123456"}
```

`carrier` and `tracking_number` belong to `shipped` and are required there. A partner may send more fields than these; the receiver reads the ones above and nothing else.

## Sending: retries, the key and the dead letter

`service.submit_order` reads the order and its submission row in one short transaction (the Idempotency-Key is made once, when the order is created, so every attempt and every later retry carries the same one), calls the partner with **no transaction open**, and records the answer in a second short one. `PartnerClient` retries what the table above says, at most 4 attempts in all, waiting 0.5 s doubled each time with a little jitter, or the partner's own `Retry-After`, never above 30 s (all injected, so the tests wait for nothing). A request whose answer was lost may well have been made, and that is exactly why the key never changes: the partner returns the first answer.

- A 2xx moves the order `created` to `submitted` and stores the partner's id. Two people pressing send at once both call the partner with the same key; one order exists there and one `submitted` audit row here.
- A permanent refusal writes a **dead letter row** (`permanent_rejection`, the HTTP status, the attempts). Retries that run out write `retries_exhausted`; a partner that never answered writes `unreachable`. The order stays `created`, a second send is refused until a person **requeues** it, and a requeued send carries the same key, so an order the partner did make while its answer was lost is found, not made twice.
- Only an order that is still `created` can be cancelled here; one the partner has is cancelled by the partner's own event.

## The state machine

States: `created` (ours), `submitted`, `accepted`, `packed`, `shipped`, `delivered`, and the ends that are not delivery, `rejected` and `cancelled`. The legal moves are the nine rows of `fulfillment_transition` and `states.TRANSITIONS` (a test fails if they differ), and a trigger refuses any other move, whoever writes (this code, a hand written UPDATE, the owner).

`states.decide` says what a partner event is against the order's **current** status:

- **apply**: the move is in the table. One audit row.
- **ignore**: the event says nothing new or says something older (a repeat, an `accepted` after `shipped`, a late `accepted` for a cancelled order). Recorded and answered 200 so the partner stops sending it; the state does not move, so it never goes backwards.
- **refuse**: not legal now (a `shipped` before `packed`, a `cancelled` after `delivered`, a `shipped` for a cancelled order). Audited, nothing moves, answered **409**. A `shipped` that overtook its `packed` is therefore refused, retried by the partner, and applied once `packed` has come: nothing is skipped and nothing is lost.

A second delivery of an **event id** changes nothing and writes nothing, whatever it now says. A refused event is not recorded by id, so its retry is judged again.

Answers of the receiver: 200 applied, ignored or duplicate; 400 not authentic, stale (more than 300 seconds from now, either way) or not a valid status event (the reason goes to our log as a code, never to the sender, and the body is never logged); 404 an order reference that is not ours; 409 refused.

## The audit log

`fulfillment_audit` has one row for every step of an order (created, submit attempt, submitted, dead lettered, requeued, transition, ignored, refused, verified), append only (a trigger, and no UPDATE or DELETE grant) and hash chained as `app/esign/chain.py` is: each row holds the hash of the one before and its own hash over its position, kind, statuses, event id, time, actor, detail and that previous hash. `service.verify` recomputes the chain and compares it with the order: an edited, deleted or reordered row and a status the log does not explain are named. The limit is the same as esign's: whoever can rewrite every later row and recompute every hash makes a chain that checks out; the head hash kept elsewhere is what catches that.

## Nothing readable about a person

- The order table cannot hold it: the reference is `ord_` and 24 hex digits (a check), the only free text is a carrier name and a tracking number (both with a shape check, the carrier without spaces so a full name is not a carrier), and a short error code.
- No function in the package takes a free-text field about an order, and the API refuses any body field it does not define (a patient name in a request is a 422 and nothing is created).
- The audit detail is a whitelist with a shape for each key (`audit.clean_detail`): a key outside the list is dropped, and so is a value that is not in its key's shape, so a name under `reason` is dropped as well as a name under a key of its own.
- A partner's status event is reduced to the fields above before anything is stored or logged; the rest is never read into an object.
- `test_nothing_readable_about_a_person_reaches_a_log_...` plays a whole order with every event carrying a recipient name, an address, a date of birth, a drug, a diagnosis and a phone number, then searches the captured log, every column of every fulfilment table, the order view and the answers for each of them.

**The limit.** A carrier name of one word, or a tracking number, is partner text that the shape checks accept. A partner that put a surname in the carrier field would get it stored. The checks stop accidents and free text, not a partner who means it.

## Access

Orders are the `orders` resource of the [access matrix](access-matrix.md): a clinic user and a prescriber see and make their clinic's orders, a sales rep reads the orders of its assigned clinics, an admin does everything. The children of an order (submission, dead letter, event, audit) carry the clinic and are as private as the order. A partner event finds its order through the narrow `fulfillment_router` role, which reads three columns of the order table and nothing else, and is applied as the system user.

## Tests

`tests/test_fulfillment_states.py` (the table against the migration, `decide` for all 56 pairs written out by hand, 300 seeded random streams: never off the table, never backwards), `tests/test_fulfillment_client.py` (15 tests against the stand-in: the key on every attempt, what is and is not retried, the exact waits and the cap, a lost answer ending in one order, no body text in an exception), `tests/test_fulfillment_db.py` (Postgres as the request role: the send, the dead letter and the requeue, forged, stale and unsigned events, every transition and refusal, the database refusing a hand written move, the append only logs, the router's three columns, the no personal data proof, the audit chain and its tampering, who sees which order, 15 seeded worlds of events arriving late, twice and out of order), and the mutation guards in `tests/mutation_check_fulfillment.py`.

## Not done

- **No real partner.** The contract is ours; a real partner will differ (field names, its own signature scheme, a callback URL we would register, order contents).
- **No order contents.** An order carries no items, quantities or ship-to, by design; how the partner learns what to pack is the clinic's secure channel, which is not built here.
- **No background worker.** Nothing resends a dead letter on a clock or checks for an order stuck in `submitted`; each is a function (`requeue`, `submit_order`).
- **A cancel after the partner has the order is not sent to the partner.** Only the partner's own `cancelled` event cancels it.
- **The receiver answers after it has applied the event,** in one short transaction, as the billing receiver does.
- **Replay inside the window is stopped by the event id, not by the clock.** The 300 second window refuses a captured event replayed later; inside it the id deduplicates.
