---
type: reference
updated: 2026-11-06
---
*A small SMS sender for the Twilio Programmable Messaging REST API, written from Twilio's documentation and tested only against a stand-in server.*

## Read this first

This sender has never been run against the live Twilio API. We hold no Twilio account. Everything here was written from Twilio's public documentation and checked against a stand-in HTTP server (`tests/sms_stand_in.py`) that serves the documented routes and shapes. A green test run means the code matches that reading of the documentation. It does not mean Twilio agrees. The first real use should be one message to a number you own, with `check` run on the result.

## What it does

`app/sms.py`, standard library only (`urllib`):

- `send(to, body)` posts the form to `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json` with HTTP basic auth (account sid and auth token), fields `To`, `Body` and either `From` or `MessagingServiceSid`. It returns the message sid and the first status.
- `check(sid)` fetches `GET .../Messages/{Sid}.json` until the status is final (`delivered`, `undelivered`, `failed`, `canceled`) with a doubling, capped delay, and reports the `error_code` of an undelivered or failed message. If the message is still in flight after the bounded number of looks it says so (`settled` is false) instead of guessing.
- Credentials come from `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`. The sender is `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`.

```python
from app import sms

sent = sms.send("+639171234567", "Your order has shipped")   # sent.status is "queued"
done = sms.check(sent.sid)                                   # polls until a final status
if done.failed:
    print(done.status, done.error_code)                      # for example: undelivered 30007
```

## Accepted is not delivered

A successful create answers with a Message resource whose status is `queued`. Twilio's own status table says `queued` means the API request was successful, `sent` means the nearest carrier accepted it, and only `delivered` is confirmation from the carrier. A client job once logged a success for every message an API accepted, and some of those were blocked by carriers later. So `send` returns acceptance only (its result has no `delivered` field) and `check` is a separate, explicit step. Twilio also warns that `error_code` values may change, so the code reports the number and does not branch on it.

## What happens when something goes wrong

| Situation | What the code does |
|---|---|
| 429 | Waits for the `Retry-After` value (capped at 60 seconds), else a doubling backoff, up to 3 retries, then `RateLimited`. |
| 5xx | Doubling backoff capped at 8 seconds, up to 3 retries, then an `ApiError` with the status. |
| Connection refused or name not found | Nothing was written, so it is retried with backoff, then `NetworkError`. |
| Timeout, dropped connection or unreadable 201 after the request was written | Not retried. `SendOutcomeUnknown` carries the idempotency key. The message may exist. |
| 4xx other than 429 | `ApiError` with the HTTP status and the vendor's `code`. No retry. |
| Bad number, empty or over-long body | `InvalidRequest` before any request is made. |

The rule behind the fourth row: a retry of a send that may have reached the vendor could text the customer twice. Reads (`check`) are safe to repeat, so a lost read is retried.

### Idempotency, and the limit of it

Every send carries an `Idempotency-Key` header (a random value unless the caller passes one). The stand-in refuses a repeated key with a 409 that names the first message, and `send` turns that into a result with `duplicate=True`. So after `SendOutcomeUnknown`, resending with the same key yields one message in total. The tests prove this and also prove that the same resend makes two messages when the vendor ignores the key.

That last case matters. The Twilio Messages documentation I read does not describe an idempotency header, so against the real API the header is probably ignored and a resend would be a second SMS. Until that is checked against a real account, treat `SendOutcomeUnknown` as "look before you resend": list recent messages to that number, or accept the small risk of a duplicate.

Other places where the code goes beyond what the Twilio pages I read state:

- The pages show the Message resource body but not the HTTP status of a create. The code expects 201 (the usual for a created resource) and treats any other 2xx as an unknown outcome.
- The pages I read do not mention `Retry-After` on a 429. The code honours it when present and falls back to a backoff when it is not.
- Every 5xx is retried, though only 503 is documented by Twilio as "try again later". With a key that the vendor ignores, a retried 500 could in principle duplicate a message that was created before the error.

## Secrets and phone numbers

- The auth token and account sid are read from the environment and never appear in a log line, an exception message, a repr or a traceback. Text that comes back from the vendor is scrubbed of both before it is raised.
- A phone number is masked to its last four digits (`***4567`) in every log line, and also inside vendor error text, because Twilio's error messages repeat the number.
- The message body is never logged.

## Tests

```
python -m pytest tests/test_sms.py -q            70 tests, about 4 seconds, no database, no Docker, no network
python tests/mutation_check_sms.py --check-targets
python tests/mutation_check_sms.py               37 mutants, about 3 minutes
```

The stand-in has switches for: a 201 then `delivered`, a 201 then `undelivered` with error code 30007, a failed message, a 429 with `Retry-After`, a 503 then success, a 401, a malformed JSON body, a lost response after the message was recorded, a response slower than the client timeout, and a repeated idempotency key (with enforcement on or off). Each behaviour has a test and, where it could pass for the wrong reason, a control: for example the double-send counter is shown to see two messages when the caller resends under a new key, and the secret and masking scans are shown to catch a planted leak.

`tests/mutation_check_sms.py` breaks one guard at a time in `app/sms.py` (the E.164 check and its bounds, the length limit, the use of `Retry-After`, the retry caps, the no-blind-retry rule, the token redaction, the number masking, the final-status loop) and requires a named test to fail on each. It edits `app/sms.py` in place, so run it in a copy of the repository.

## Not covered

TLS and certificate handling against the real host, real error texts, real delivery timing, the status callback webhook (this polls instead), MMS and media, scheduled messages, and Twilio's own SDK. Sources: Twilio's Message resource page (`twilio.com/docs/messaging/api/message-resource`) and its Response page (`twilio.com/docs/usage/twilios-response`).
