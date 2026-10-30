# Competitor price watch

A module of the hub (`app/price_watch/`, migration `011_price_watch.sql`, `scripts/price_watch.py`) that watches competitor product pages, works out where a competitor is cheaper once shipping is counted, lets a person approve a price change, and then pushes the approved price to Shopify and Zoho Inventory.

**What was and was not run.** Everything here runs against invented data. The competitor pages are eight made up product pages served from `127.0.0.1` ([`fake_site.py`](../app/price_watch/fake_site.py)), read in real Chromium through Playwright. The Shopify and Zoho calls are written from the vendors' official documentation ([saved pages and the lines relied on](vendor-contract/README.md)) and were tested only against fake vendors written from those same pages ([`fakes.py`](../app/price_watch/fakes.py)). They have never been run against a live Shopify store or a live Zoho Inventory organisation, and no real competitor site was ever scanned. The first real account will find whatever a misreading of the documentation hides.

## The loop

1. **Scan.** `PageScraper` opens each watched competitor page in Chromium and reads the title, SKU or GTIN, price and currency, availability and any shipping line. It copes with a price in JSON-LD, in microdata, in a plain span, put on the page by a script after load, with a thousands separator or a decimal comma, and with a struck-through original price beside a sale price. A page with no price says so; a dead page is a recorded problem, not a crash.
2. **Match.** Each page is matched to a catalogue row: GTIN first (check digit verified), then SKU, then normalised title tokens. The score and thresholds are in [`matching.py`](../app/price_watch/matching.py): 0.85 and over is matched, 0.60 to 0.85 goes to the needs-review list, under 0.60 is no match. **A match that is not certain is never made automatically.** Same brand and name with a different size or model number (750ml against 500ml, 48V against 36V) is capped below the review line, a tie between two catalogue rows goes to review, and two different valid GTINs rule a candidate out whatever the titles say. A person settles a review with `confirm-match`, and that decision survives later scans.
3. **Shipping.** If the page states a shipping cost (or "free"), that is the competitor's figure. If it states none, the cost is estimated from the tenant's rate table (`shipping_rate`: destination zone, weight band, cost) and **labelled ESTIMATED** in the report and the CSV and on the proposal. With no product weight, no band for the weight, or two bands that both fit, the line is excluded and says why. An estimate is never shown as the competitor's own figure.
4. **Report.** For a day, every compared product where the competitor's landed price (price plus shipping) is lower than ours (price plus our own shipping). A different currency, an out-of-stock competitor, a missing price, unknown shipping and a scan from another day are excluded with the reason printed. The proposed price makes our landed price equal theirs (or beat it by `--undercut-minor`) and **never goes below the product's floor**; where the floor stops it, the line says so. The report is stored (`price_watch_report`, one row per tenant and day, replaced if run again) with a markdown and a CSV render.
5. **Review.** Each product with a lower competitor gets one proposal in `price_change_proposal` (one open proposal per product at a time). States: `proposed`, `approved`, `rejected`, `applied`, `failed`. Every move goes through one function that locks the row, refuses a move the table does not allow, and writes an audit row (who, when, from, to, note) in the same transaction. A second approval is an error and writes nothing. The audit table is append only for the request role.
6. **Sync.** Only an approved proposal (or a failed one, which is an approved proposal with a shop still owing) is sent. It is a dry run unless `--apply` is given. Each shop has its own status on the proposal, so after Shopify succeeds and Zoho fails the retry calls Zoho only. An applied proposal run again sends nothing. A 429, a 5xx, a 408 or a dropped connection is retried with backoff (a numeric `Retry-After` is honoured, capped at 30 seconds); any other 4xx marks that side failed with the vendor's own message. The catalogue takes the new price only when every shop the product is wired to has accepted it, and the sync refuses a proposal whose catalogue price or floor has moved since it was made.

## Run it

Needs Docker (the compose Postgres), Python 3.13 with the repo's requirements, and a Chromium for Playwright. From the repo root, in Git Bash; the demo seed drops the `public` schema of whatever database it points at, so use the compose database or a scratch one.

```bash
docker compose up -d --wait
. .venv/Scripts/activate                       # macOS or Linux: . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium         # once; browsers are cached per machine (PLAYWRIGHT_BROWSERS_PATH to move them)

python seed_price_watch_demo.py                # applies every migration, loads 7 products, 8 listings and a rate table; prints the tenant id
export DATABASE_URL="postgresql://analytics:analytics@127.0.0.1:5433/analytics"
export PRICE_WATCH_TENANT=<the tenant id it printed>

python scripts/price_watch.py demo-server &    # the invented competitor shop on 127.0.0.1:8791
python scripts/price_watch.py scan             # Chromium reads the eight pages; prints what each matched
python scripts/price_watch.py report --out-dir out        # stored report, plus out/price-report-<date>.md and .csv; queues proposals
python scripts/price_watch.py listings --status needs_review   # pages left for a person
python scripts/price_watch.py proposals
python scripts/price_watch.py approve --id <first 8 characters of an id> --actor you@example.com
python scripts/price_watch.py reject  --id <...> --actor you@example.com --note "looks like a typo"
python scripts/price_watch.py sync --fakes                # dry run: shows what would be sent, changes nothing
python scripts/price_watch.py sync --fakes --apply        # against the in-process fake Shopify and Zoho
python scripts/price_watch.py proposals --state applied
```

The demo shows each case once: a stated shipping figure matched by GTIN; a shipping cost estimated from the rate table (the lamp, labelled ESTIMATED); a price with a thousands separator matched by title only; a sale price held at the floor; a page with no price (excluded); a competitor that is dearer once its shipping is added (compared, not lower); a euro price whose title scores 0.80 and waits for a person (`listings --status needs_review` shows it, `confirm-match --listing <first 8 characters of its id> --product ASG-L --actor you@example.com` settles it, and the next report excludes it for the currency); and a 500ml bottle that does not match the 750ml one.

`sync` without `--fakes` and with `--apply` talks to the real vendors from the environment, and nothing in this repository has ever done that:

| Variable | Used by |
|---|---|
| `SHOPIFY_SHOP_DOMAIN` (the `.myshopify.com` domain), `SHOPIFY_ADMIN_TOKEN` | `ShopifyPriceAdapter` (needs the `write_products` scope) |
| `ZOHO_REGION` (`com`, `eu`, `in`, `com.au`, `jp`, `ca`, `sa`), `ZOHO_ORGANIZATION_ID`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` | `ZohoInventoryAdapter` (needs the `ZohoInventory.items.UPDATE` scope). An Australian account is `com.au`: API domain `https://www.zohoapis.com.au`, token server `https://accounts.zoho.com.au`. |

No token is stored in code, in the tests or in the database. The Shopify call uses Admin GraphQL `productVariantsBulkUpdate` on API version **2026-10** (the version the Shopify pages list as latest on 2026-10-30). The Zoho call is `PUT /inventory/v1/items/{item_id}?organization_id=...` with an `Authorization: Zoho-oauthtoken` header, a fresh access token from the refresh token flow, and a body of the item's `name` (required by that endpoint) and its new `rate`.

## What is in the database

`011_price_watch.sql`: `catalogue_product`, `competitor_listing`, `competitor_price_observation`, `shipping_rate`, `price_change_proposal`, `price_change_audit` and `price_watch_report` (the stored daily report; the brief named the first six, this seventh holds the report row). Money is an integer in minor units with its currency beside it. Row level security is enabled and forced on every table with the same policy as the other tenant tables, and the request role is granted select, insert and update on the working tables and select and insert only on the audit table. A proposal below its floor, not below the current price, or `applied` while a shop still owes the change is refused by a CHECK constraint, not only by the Python.

## Tests and mutation controls

Seven test files (`tests/test_price_watch_*.py`, plus `tests/price_watch_fixtures.py`): the scraper against the eight pages in real Chromium (skipped where no Chromium exists; CI installs one and sets `PRICE_WATCH_REQUIRE_BROWSER=1` so a missing browser fails there), matching (exact, fuzzy, near miss, tie), shipping and the report, the adapters against the fake vendors, the database tests as the request role (tenant A cannot see, write to or move tenant B's rows, in all seven tables), the queue and its audit trail, the sync, a seeded simulation of random vendor failures over several sync rounds, and the whole loop through the command line. `tests/mutation_check.py` carries 25 guards for this module (a low-confidence title match made automatically, a tie broken by picking one, a size conflict not held back, an estimate passed off as the competitor's figure, the floor, the currency, out-of-stock and stale-scan exclusions, an unapproved proposal syncing, an applied one sent again, a retry hitting the shop that already accepted, the 429 and 4xx rules, and the row level security, the grants and the CHECK constraints). The results of the run are in the pull request or commit that added them; run it in a copy of the repository, because it edits source in place.

## Not done

- No live Shopify store and no live Zoho Inventory organisation: the sync is tested against fakes only, and the vendor contracts come from official documentation.
- No real competitor site. The scraper has only read the invented pages; pointing it at a real shop is a decision about that shop's terms and robots.txt, and nothing here checks either. It reads one page at a time with a declared user agent.
- No scheduling. `scan`, `report` and `sync` are commands; nothing calls them on a clock in production.
- No exchange rates: a competitor in another currency is excluded.
- No discovery of competitor listings: someone adds the URLs (`competitor_listing`), and nothing searches a site for the matching product.
- No web screen for the review queue: approving is a command, and every approval is attributed to the `--actor` string it is given, which is not an authenticated identity.
- No request level idempotency key at either vendor (the pages as read offer none for these calls). The sync sets an absolute price and remembers per shop that it was accepted; if the process dies between a vendor accepting and the database committing, the retry sets the same price again.
- The Zoho call sends `name` and `rate` only; whether a live organisation wants more fields on an item update is not known.
