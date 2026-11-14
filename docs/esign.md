# E-signatures in the client portal

A firm puts a document in a client's portal, the client reads it, types their name, ticks a box and signs, and the hub keeps a signature record with an audit trail that shows if anyone changes it later. Built and tested against a local Postgres; a signing record with a tamper-evident audit trail, not a certified e-signature service, not legal advice, not used by a firm.

It needs no vendor account. No DocuSign, Dropbox Sign or other service is called or imitated.

## The flow

1. **A firm user creates a request** (`POST /api/esign/requests`, an operator or admin key). The exact bytes of the document are stored, their SHA-256 is recorded, a signer is named by name and email, and an expiry is set (one hour to a year). A `created` audit row pins the hash, the size, the consent version, the expiry and a hash of the signer's name and email.
2. **The client opens it** in the portal (`/sign/<id>` on the tenant's subdomain, or `GET /api/esign/requests/<id>`) with a viewer key for that tenant. They see the document's SHA-256, a link to the document, and the consent text of the version the request carries. Opening it writes a `viewed` row.
3. **The client signs**: types their full name, ticks the consent box, presses Sign (`POST .../sign`). The consent text says that the signer agrees to sign electronically and to use electronic records, that typing the name is a signature with the effect of a handwritten one, that the signature belongs to this document by its fingerprint, and that the record and the audit trail are kept. These are the four ideas the US ESIGN Act and UETA turn on: consent, intent to sign, association of the signature with the record, and retention.
4. **The signature record** holds the request id, the tenant, the SHA-256 of the exact bytes at signing time, the name as typed, the consent version and the SHA-256 of its text, the signing time, a keyed hash of the user agent and of the client address (never the raw values), the hash of the audit row before it and its own hash. The consent, the signature, the `signed` row and the status change are one transaction.
5. **Every event** (`created`, `viewed`, `consented`, `signed`, `expired`, `declined`, `verified`) is appended to the request's audit log. Each row holds the hash of the row before it and its own hash. `POST .../verify` recomputes the document hash and the whole chain and says exactly what changed. A signer can also decline (`POST .../decline`).
6. **The completion certificate** (`GET .../certificate`, or `?format=text`) lists the request, the signer, the times, the document hash, every audit row up to the signed one and the chain head. A third party can check it without the database: `python -m app.esign.certificate verify certificate.json --document letter.pdf`. A PDF is not produced because the repository has no Python PDF library; the certificate is JSON and a plain text rendering of the same facts.

## The rules, each with the test that pins it

Files: `tests/test_esign_chain.py` (no database), `tests/test_esign_db.py` (Postgres, as the request role), `tests/test_esign_api_db.py` (HTTP), `tests/test_esign_simulation.py` (seeded worlds).

| Rule | Where it is enforced | Test |
|------|----------------------|------|
| The exact bytes are stored and their SHA-256 recorded | `service.create_request`, a check on the column | `test_creating_a_request_stores_the_exact_bytes_and_their_sha256_and_pins_them_in_the_created_row` |
| A request that cannot be stored is refused: empty, over 5 MiB, bad email, blank names, an expiry in the past or beyond a year | `service.create_request` | `test_a_request_that_cannot_be_stored_is_refused_and_leaves_nothing` |
| A request is signed once | the code, an advisory lock, and `unique (request_id)` in Postgres | `test_a_second_attempt_with_a_different_name_is_refused_and_no_second_signature_exists`, `test_concurrent_attempts_make_exactly_one_signature`, `test_a_second_signature_row_for_one_request_is_refused_by_the_database_itself` |
| A retry after a lost answer returns the first result and writes nothing | `service.sign` | `test_a_retry_after_a_lost_answer_returns_the_first_result_and_writes_nothing`, `test_a_retry_after_a_lost_answer_is_200_with_the_first_result_and_another_name_is_409` |
| An expired request cannot be signed, at the instant of expiry and after | `service.sign`, `_expire_if_due` | `test_an_expired_request_cannot_be_signed_and_the_expiry_is_written_once`, `test_the_expiry_boundary_is_the_instant_itself` |
| A declined request cannot be signed, a signed one cannot be declined | `service.sign`, `service.decline`, the status trigger | `test_a_declined_request_cannot_be_signed`, `test_declining_twice_changes_nothing_and_a_signed_or_expired_request_cannot_be_declined` |
| The typed name must be the named signer's name (see below) | `consent.names_match` | `test_name_rule_accepts_case_spacing_and_compatibility_forms`, `test_name_rule_refuses_empty_partial_reordered_and_different_names`, `test_an_empty_or_mismatched_typed_name_is_refused_and_a_matching_one_signs` |
| No consent, no signature; only a real `true` counts | `service.sign` | `test_no_consent_means_no_signature` |
| The signer must have seen this request's consent version | `service.sign` | `test_a_consent_version_other_than_the_requests_is_refused` |
| The document must hash to the stored hash at signing time, and to the hash the signer saw | `service.sign` | `test_a_replaced_document_is_refused_at_signing_even_when_its_hash_column_was_replaced_too`, `test_a_signer_who_saw_a_different_hash_is_refused` |
| One tenant's key never reads, signs, declines, verifies or certifies another's request, and the refusal is the same as for an id that does not exist | forced row level security, `RequestNotFound` | `test_another_tenants_key_cannot_open_download_sign_decline_verify_or_certify_and_the_refusal_matches_a_missing_id`, `test_another_tenants_key_gets_the_same_404_as_for_an_id_that_does_not_exist`, `test_tenant_a_sees_none_of_tenant_bs_rows_in_any_esign_table_and_all_of_its_own`, `test_tenant_a_cannot_write_a_row_for_tenant_b_in_any_esign_table`, `test_every_esign_table_has_row_level_security_enabled_forced_and_a_policy` |
| The audit log and the signature are append only; the request changes only its status, forward | no UPDATE or DELETE grant, triggers that hold for the owner | `test_the_request_role_cannot_update_or_delete_an_audit_row_or_a_signature`, `test_even_the_owner_cannot_update_or_delete_them_because_of_the_triggers`, `test_a_request_status_moves_forward_from_pending_only` |
| No raw address, user agent or document byte in a log or a stored audit row | keyed hashes in `service.sign`, the log lines carry ids only | `test_audit_rows_and_the_signature_hold_no_raw_agent_address_or_document_bytes`, `test_no_raw_agent_address_or_document_byte_reaches_a_log_or_an_audit_row`, `test_personal_values_are_keyed_hashes_labelled_by_kind_and_never_the_plain_hash` |
| verify() is true for an untouched record and names each kind of change | `chain.check_record` | `test_an_untouched_record_verifies_in_every_state_and_each_run_writes_a_verified_row`, `test_verify_reports_each_kind_of_tampering_by_its_code_and_passed_before_it` (18 kinds), `test_verify_says_exactly_which_row_and_which_hash_changed` |
| A certificate checks out offline and fails when any part is edited | `certificate.verify_certificate` | `test_a_certificate_of_a_signed_request_verifies_offline_and_lists_the_chain_head`, `test_a_certificate_edited_in_any_part_is_reported`, `test_the_command_line_check_passes_a_good_file_and_fails_a_changed_document` |
| Only an operator or admin creates a request; a viewer can read and sign | `PERMISSIONS` in `app/api/auth.py` | `test_creating_needs_a_key_and_an_operator_or_admin_while_a_viewer_may_read_and_sign` |
| All of it together, over seeded random worlds | all of the above | `test_simulation_world_keeps_every_signing_rule` (50 seeds) |

**The name rule.** The typed name equals the named signer's name after both are normalised: Unicode NFKC, runs of whitespace collapsed to one space, the ends trimmed, case folded. Upper and lower case, extra spaces and full width letters do not matter. A different spelling, a missing surname, a middle initial, a reordered name or an empty name is refused. The rule is strict so that a signer who types the name on the request has signed on purpose. It does not prove who typed it.

**What `verify` reports.** An edited audit row (`row_edited`, with its number), a deleted row (`row_missing`, `link_broken`), reordered rows (`rows_reordered`), a changed document byte (`document_changed`, with both hashes), the document and its hash replaced together or the signer, email, expiry or consent version edited (`request_changed`, because the `created` row pinned them), an edited signature (`signature_edited`, `signature_document_mismatch`, `signature_not_in_chain`, `signature_link_broken`), a deleted signature or one relabelled to another tenant (`signature_missing`), and a status that disagrees with the log (`status_mismatch`).

## The simulation

`tests/test_esign_simulation.py` plays 50 seeded worlds, each with two tenants and several requests with short and long expiries, a clock that moves, right and wrong names, no consent, a wrong consent version and a wrong hash, retries after a success, bursts of right and wrong attempts at the same moment, decline, the other tenant's key trying every call, and finally a random tampering of a random record. It checks that no request has two signatures, that every call returns what a small model says it must, that the other tenant never gets anything and gets the same answer as for an id nobody has, that verify is true for every untouched record and false for every tampered one with a code that tampering must produce, and that no audit row holds a raw value. A second test replays six more worlds and counts the cases reached (signed, declined, expired, pending, tampered, cross tenant attempts), so a world that never reached them would fail.

## Mutation guards

`tests/mutation_check_esign.py` breaks one guard at a time in a copy of the source (the double sign check, the retry, the advisory lock, the expiry check and its boundary, the declined check, the consent check, the name match, the document hash comparison, the tenant policy, forced row level security, the append only triggers and grants, the chain link, each part of verify, the keyed hash, a raw address in a log, the certificate checks, the role table, the statuses) and runs only the tests that must catch it. `web/scripts/mutation_check.mjs --only esign` does the same for the portal client (17 guards). Outputs: [pytest](test-runs/2026-11-14-esign-pytest.txt), [mutation](test-runs/2026-11-14-esign-mutation.txt), [web tests and guards](test-runs/2026-11-14-esign-web-tests.txt), [the portal in a browser](test-runs/2026-11-14-esign-portal-browser.txt), and the whole suite in [full-pytest](test-runs/2026-11-14-full-pytest.txt).

## Run it

```bash
python -m pytest tests/test_esign_chain.py tests/test_esign_db.py tests/test_esign_api_db.py tests/test_esign_simulation.py -q   # needs the compose Postgres; drops the public schema of the test database
python tests/mutation_check_esign.py                                                                                             # edits source in place, so use a copy
cd web && npm test -- tests/esign.test.ts                                                                                          # the portal client
cd web && node scripts/esign-e2e.mjs http://127.0.0.1:3200 http://127.0.0.1:8100                                                      # the portal in a browser against a running backend (setup in the script's header)
```

`ESIGN_PII_KEY` is the key for the hashes of the user agent and the address. With none set the API makes a random key for the life of the process and logs a warning, so the hashes then cannot be compared across restarts. `ESIGN_TRUST_FORWARDED=1` makes the API take the address from the first `X-Forwarded-For` entry, which is what the portal server sends; set it only behind a proxy you control. `ESIGN_API_BASE` is the backend address the portal server calls (it falls back to `NEXT_PUBLIC_API_BASE`).

## Limits

- **No identity verification beyond the portal key.** Whoever holds a viewer key for the tenant and types the signer's name can sign. Nothing checks that the person is the named signer.
- **No qualified or advanced signature level.** It is a typed name with consent and an audit trail. It is not a certificate based signature, and nothing here says it meets any country's requirements for a given kind of document.
- **The audit chain proves the integrity of this store only.** Someone who can rewrite every later row and recompute every hash makes a chain that checks out, and rows deleted from the end leave no gap. A copy of the chain head kept somewhere else (the certificate carries it) is what catches both. A test pins that limit.
- **Refused attempts are not written to the audit log.** A wrong name or a missing tick returns an error and writes nothing; only the expiry, which is a fact about the request, is committed with its refusal.
- **The address is the connection's.** Behind a proxy it is the proxy's unless `ESIGN_TRUST_FORWARDED=1`, and a caller that can set that header can forge it. It is evidence, not authentication.
- **Documents are stored in the database**, up to 5 MiB each. There is no virus scan, no preview and no page level signing field.
- **No email is sent** to the signer and no reminder is scheduled; the request is found in the portal. `POST /api/esign/expire` marks overdue requests and can be called from a scheduler.
- **No PDF certificate.** JSON and plain text only.
- Run against a local Postgres 17. The portal pages were driven once in Chromium against a running backend and the built portal on `*.portal.localhost` names ([output](test-runs/2026-11-14-esign-portal-browser.txt)); no wildcard DNS, no certificate and no deployed portal were used. Behind a proxy that does not set `X-Forwarded-For`, the address the backend records is the portal server's.
