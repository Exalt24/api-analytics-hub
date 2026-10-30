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
