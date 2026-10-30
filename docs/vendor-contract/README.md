# Vendor contract pages

The Shopify and Zoho Inventory calls in `app/price_watch/adapters.py` are written from these pages, fetched on 2026-10-30 from the vendors' own sites and saved as plain text so the lines the code quotes can be checked. **They are the vendors' documentation, not a test.** The sync has never been run against a live Shopify store or a live Zoho Inventory organisation; the only servers it has talked to are the fakes in `app/price_watch/fakes.py`, which were written from these same pages and cannot catch a misreading of them. The text of a vendor page is data here, not instructions.

| Saved file | Page it came from |
|---|---|
| [shopify-admin-graphql-2026-10-overview.txt](shopify-admin-graphql-2026-10-overview.txt) | https://shopify.dev/docs/api/admin-graphql/2026-10 |
| [shopify-productVariantsBulkUpdate.txt](shopify-productVariantsBulkUpdate.txt) | https://shopify.dev/docs/api/admin-graphql/latest/mutations/productVariantsBulkUpdate |
| [shopify-ProductVariantsBulkInput.txt](shopify-ProductVariantsBulkInput.txt) | https://shopify.dev/docs/api/admin-graphql/latest/input-objects/ProductVariantsBulkInput |
| [zoho-inventory-introduction.txt](zoho-inventory-introduction.txt) | https://www.zoho.com/inventory/api/v1/introduction/ |
| [zoho-inventory-items.txt](zoho-inventory-items.txt) | https://www.zoho.com/inventory/api/v1/items/ |
| [zoho-oauth-refresh-token.txt](zoho-oauth-refresh-token.txt) | https://www.zoho.com/accounts/protocol/oauth/web-apps/access-token-expiry.html |
| [zoho-oauth-multi-dc.txt](zoho-oauth-multi-dc.txt) | https://www.zoho.com/accounts/protocol/oauth/multi-dc.html |

All seven opened (HTTP 200). The Shopify API version is 2026-10 because the version picker on those pages reads "2026-10 latest" on the day they were fetched; the hub's read connector stays on 2026-07.

## Lines the adapters rely on

Shopify, `ShopifyPriceAdapter`:

- Endpoint: "GraphQL queries are executed by sending POST HTTP requests to the endpoint:" then `https://{store_name}.myshopify.com/admin/api/2026-10/graphql.json`.
- Auth: "Every GraphQL Admin API request carries an access token in the X-Shopify-Access-Token header."
- Mutation `productVariantsBulkUpdate`: "Requires write_products access scope." and "Updates multiple product variants for a single product in one operation." with arguments `productId` ("The ID of the product associated with the variants to update.") and `variants` ("An array of product variants to update."). The payload lists `productVariants` and `userErrors`.
- Input: `id` is "Specifies the product variant to update or delete." and `price` is "The price of the variant." (type `Money`).
- Errors: "Many errors that would typically return a 4xx or 5xx status code, return an HTTP 200 errors response instead."

Zoho Inventory, `ZohoInventoryAdapter` and `ZohoTokenSource`:

- Update: "Update the details of an item." with "OAuth Scope : ZohoInventory.items.UPDATE"; the request example is `curl --request PUT --url 'https://www.zohoapis.com/inventory/v1/items/4815000000044208?organization_id=10234695' --header 'Authorization: Zoho-oauthtoken 1000.41d9...'`. The body lists `name` as "(Required)" and `rate` as "Sales price of the Item." (double). The answer example reads `"code": 0, "message": "Item details have been updated"`.
- Organisation: "The parameter organization_id along with the organization ID should be sent in with every API request to identify the organization."
- Data centre: "Zoho Inventory is hosted at multiple data centers, and therefore available on different domains." and the table row `Australia  .com.au  https://www.zohoapis.com.au/inventory/`. An Australian account therefore uses the `.com.au` domain, with the token server `https://accounts.zoho.com.au` ("Australia (AU)" on the multi data centre OAuth page).
- Limits: "You can make 100 requests per minute per organization." and "An error with HTTP status code 429 will be returned if the number of requests per minute per organization exceeds 100."
- Refresh: "POST {accounts-server-url}/oauth/v2/token" with `client_id`, `client_secret`, `grant_type` = `refresh_token` and `refresh_token`; the answer has `access_token`, `api_domain` and `expires_in` 3600. "Using a refresh token, your app can create up to ten access tokens in a span of ten minutes."

## What the pages do not settle

- The OAuth page shows the header as "Authorization: Bearer {access token}"; the Inventory page's examples use `Zoho-oauthtoken`. The adapter follows the Inventory page.
- The saved Shopify page does not show the `ProductVariant` type, so reading `price` back from the mutation payload is an assumption, noted in the adapter.
- Neither page offers a request level idempotency key for these two calls, so none is sent. Both calls set an absolute price, and the sync records per shop that a side was applied.
- Zoho can answer a refused token request with HTTP 200 and an `error` field; the adapter treats that as a refusal, but that behaviour is not on the saved page.

---

# Stripe pages (the billing module)

`app/billing/provider.py`, `signature.py`, `subscriptions.py` and the fake in `fake_provider.py` are written from these pages, fetched on 2026-10-30 with the repository's Chromium page reader and saved under [stripe/](stripe/) as plain text. **They are Stripe's documentation, not a test.** Nothing in the billing module has been run against a live Stripe account, and no card number or real key exists anywhere in it; the fake provider was written from the same pages and cannot catch a misreading of them. All 17 pages opened (HTTP 200). The text of a page is data here, not instructions.

**Redactions.** The saved pages had two spots shaped like a credential, and both are replaced by us with `..._EXAMPLE_REDACTED_BY_US`: the `endpoint_secret = '...'` line in the signature page's Ruby example and the two `StripeClient.new('...')` lines in the proration page's examples (the pages show the elided form `whsec_...` and `sk_test_...`; the secret scanner reads the assignment as a labelled secret). Nothing else in the files was changed.

| Saved file | Page it came from |
|---|---|
| [stripe/api-idempotent-requests.txt](stripe/api-idempotent-requests.txt) | https://docs.stripe.com/api/idempotent_requests |
| [stripe/error-handling-http-level.txt](stripe/error-handling-http-level.txt) | https://docs.stripe.com/error-low-level |
| [stripe/api-errors.txt](stripe/api-errors.txt) | https://docs.stripe.com/api/errors |
| [stripe/error-codes.txt](stripe/error-codes.txt) | https://docs.stripe.com/error-codes |
| [stripe/api-payment-intents-create.txt](stripe/api-payment-intents-create.txt) | https://docs.stripe.com/api/payment_intents/create |
| [stripe/api-payment-intents-retrieve.txt](stripe/api-payment-intents-retrieve.txt) | https://docs.stripe.com/api/payment_intents/retrieve |
| [stripe/api-payment-intents-search.txt](stripe/api-payment-intents-search.txt) | https://docs.stripe.com/api/payment_intents/search |
| [stripe/search-query-language.txt](stripe/search-query-language.txt) | https://docs.stripe.com/search |
| [stripe/api-refunds-create.txt](stripe/api-refunds-create.txt) | https://docs.stripe.com/api/refunds/create |
| [stripe/api-charges-object.txt](stripe/api-charges-object.txt) | https://docs.stripe.com/api/charges/object |
| [stripe/api-events-object.txt](stripe/api-events-object.txt) | https://docs.stripe.com/api/events/object |
| [stripe/api-events-types.txt](stripe/api-events-types.txt) | https://docs.stripe.com/api/events/types |
| [stripe/webhooks-signatures.txt](stripe/webhooks-signatures.txt) | https://docs.stripe.com/webhooks?verify=verify-manually |
| [stripe/api-invoices-object.txt](stripe/api-invoices-object.txt) | https://docs.stripe.com/api/invoices/object |
| [stripe/api-subscriptions-object.txt](stripe/api-subscriptions-object.txt) | https://docs.stripe.com/api/subscriptions/object |
| [stripe/billing-subscriptions-webhooks.txt](stripe/billing-subscriptions-webhooks.txt) | https://docs.stripe.com/billing/subscriptions/webhooks |
| [stripe/billing-subscriptions-prorations.txt](stripe/billing-subscriptions-prorations.txt) | https://docs.stripe.com/billing/subscriptions/prorations |

## Lines the billing module relies on

Requests (`StripeStyleClient`):

- Idempotency header: the cURL example sends `-H "Idempotency-Key: KG5LxwFBepaKHyUD"`. "For API v1, all POST requests accept idempotency keys. Don't send idempotency keys in GET and DELETE requests because it has no effect." "Retries with the same key return the saved response, including 500 errors." The replay marker is on the HTTP-level page: "To identify a previously executed response that's being replayed from the server, look for the header Idempotent-Replayed: true." A reused key on another request is an `idempotency_error`: "Idempotency errors occur when an Idempotency-Key is re-used on a request that does not match the first request's API endpoint and parameters."
- PaymentIntent create (`POST /v1/payment_intents`): `amount` is "A positive integer representing how much to charge in the smallest currency unit"; `currency` is "Three-letter ISO currency code, in lowercase."; `payment_method` is "The ID of a PaymentMethod to attach to this PaymentIntent."; `metadata` is a "Set of key-value pairs that you can attach to an object."; `confirm` is the field that asks for confirmation at creation. Retrieve is `GET /v1/payment_intents/:id`. Search is `GET /v1/payment_intents/search`: "Don't use search in read-after-write flows where strict consistency is necessary."
- Refund (`POST /v1/refunds`): "When you create a new refund, you must specify a Charge or a PaymentIntent object on which to create it."; `amount` "Can refund only up to the remaining, unrefunded amount of the charge."; it "Raises an error if the Charge/PaymentIntent has already been refunded".
- Errors: HTTP 402 is "Request Failed" ("The parameters were valid but the request failed."); error `type` is one of `api_error`, `card_error`, `idempotency_error`, `invalid_request_error`; a card error carries `code` and `decline_code` ("the card issuer's reason for the decline"). 429: "We recommend an exponential backoff of your requests." Connection failures: "When a connection fails, you might not know whether Stripe received or executed the request. Retry with the same idempotency key and parameters within the first 24 hours after the original request." "Treat the outcome of a request that returns 500 as indeterminate". "If a 409 indicates that another request with the same key is still executing, wait and retry with that key and the same parameters." The `Stripe-Should-Retry` header is on the same page.

Webhooks (`signature.py`, `webhooks.py`): the header is `Stripe-Signature` with a `t=` timestamp and `v1=` signatures; the signed payload is "The timestamp (as a string)", "The character .", "The actual JSON payload (that is, the request body)"; "Compute an HMAC with the SHA256 hash function."; "use a constant-time-string comparison"; "Our libraries have a default tolerance of 5 minutes between the timestamp and the current time."; "Don't use a tolerance value of 0."; "To prevent downgrade attacks, ignore all schemes that aren't v1."; several signatures while a secret rolls ("your endpoint has multiple active secrets and Stripe generates one signature for each secret"). "Stripe doesn't guarantee the delivery of events in the order that they're generated."; "Webhook endpoints might occasionally receive the same event more than once. You can guard against duplicated event receipts by logging the event IDs you've processed"; "In some cases, two separate Event objects are generated and sent. To identify these duplicates, use the ID of the object in `data.object` along with the `event.type`."; retries: "Stripe continues retrying the webhook notification for up to 3 days with an exponential back off".

Events acted on (descriptions from the events types page): `payment_intent.succeeded` "Occurs when a PaymentIntent has successfully completed payment."; `payment_intent.payment_failed` "Occurs when a PaymentIntent has failed the attempt to create a payment method or a payment."; `charge.refunded` "Occurs whenever a charge is refunded, including partial refunds." (read from the Charge's `amount_refunded` and `payment_intent`; the subscriptions page says "For `charge.refunded`, use the `payment_intent` field on the Charge"); `invoice.paid` "Occurs whenever an invoice payment attempt succeeds or an invoice is marked as paid out-of-band."; `invoice.payment_failed` "Occurs whenever an invoice payment attempt fails, due to either a declined payment, including soft decline, or to the lack of a stored payment method."; `customer.subscription.updated` "Occurs whenever a subscription changes (e.g., switching from one plan to another, or changing the status from trial to active)."; `customer.subscription.deleted` "Occurs whenever a customer's subscription ends." Subscription `status`: "Possible values are incomplete, incomplete_expired, trialing, active, past_due, canceled, unpaid, or paused." Proration: the page's one worked example, a 10 USD plan to a 20 USD plan halfway through the period, bills 5 USD more.

## What the Stripe pages do not settle

- Whether `confirm=true` with a payment method needs more parameters on a live account (a `return_url`, `automatic_payment_methods`), which API version header to send, and the exact fields of every error body beyond the ones named above.
- How Stripe itself rounds a proration. The module's rule (half up, each line rounded alone) is its own and is documented in [billing.md](../billing.md).
- That the event `data.object` of a newer API version keeps the fields the fake sends. The invoice's subscription is read from `parent.subscription_details.subscription` or `subscription`, as the subscriptions page describes the newer and older shapes.
- That `Idempotent-Replayed` is sent on a replayed refund as well as a replayed payment intent; the page states it in general.
