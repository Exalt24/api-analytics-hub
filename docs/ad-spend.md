# Ad platforms, business systems and the ad spend view

Five more connectors behind the same contract as Shopify, GA4, Meta and Search Console, and one view that puts the ad platforms side by side. They were built for a small service business that runs paid ads and a shop on software: what did each ad platform cost, how many conversions did it bring, what did one cost, and how many jobs did the shop book in the same weeks.

**What they were tested against.** Every connector was written from its vendor's public API documentation (the pages, with a quote and what is UNVERIFIED, are in [AD_AND_BUSINESS_SOURCES.md](AD_AND_BUSINESS_SOURCES.md)) and run against a stand-in HTTP server written from the same pages. No Google Ads account, TikTok advertiser, Square seller, QuickBooks company or Shopmonkey shop was used, no real token or customer record was touched, and nothing here is a statement that a vendor accepts these requests. The first run against a real account is the next thing to do and the sources page lists what it will settle.

| Source | Reads | Daily metric keys | Built and tested against |
|---|---|---|---|
| Google Ads | `googleAds:search` (GAQL, `FROM customer` by `segments.date`), optionally `local_services_lead` | `ad_spend`, `ad_impressions`, `ad_clicks`, `ad_conversions`, `ad_conversion_value`, `ad_leads` | a stand-in server |
| TikTok Ads | `report/integrated/get/` basic report, advertiser level, 30 day runs | `ad_spend`, `ad_impressions`, `ad_clicks`, `ad_conversions` | a stand-in server |
| Square | `GET /v2/payments` for one location, cut at the location's midnight | `payments`, `payments_total`, `payments_refunded` | a stand-in server |
| QuickBooks Online | the `query` endpoint over `Invoice`, by invoice date | `invoices`, `invoiced_amount`, `invoice_open_balance` | a stand-in server |
| Shopmonkey | `GET /v3/order`, scanned and cut to the window | `work_orders`, `work_order_revenue` | a stand-in server |

Each is one class (`app/connectors/<name>.py`), one stand-in (`<name>_fake.py`), one registry entry and, for all five together, one migration ([013](../migrations/013_ads_and_business_metrics.sql)). The sync runner, the snapshot table and the dashboard did not change. The shared rules (integer minor units, half up rounding, a float refused, the bounded retry loop) are in `app/connectors/common.py`.

## Connect and sync one

A connection is a `platform_connection` row (`platform` is `google_ads`, `tiktok`, `square`, `quickbooks` or `shopmonkey`, `external_account` is the id below) and the credentials are encrypted per row like the others ([secrets](secrets.md)). A sync pulls one window into the dated snapshots and rewriting the same window writes the same rows:

```bash
export DATABASE_URL="postgresql://analytics:analytics@127.0.0.1:5433/analytics"
GOOGLE_ADS_CUSTOMER_ID=1234567890 GOOGLE_ADS_ACCESS_TOKEN=... python scripts/sync_google_ads.py --tenant <uuid> --connection <uuid> --days 14
TIKTOK_ADVERTISER_ID=7001234567890 TIKTOK_ACCESS_TOKEN=... python scripts/sync_tiktok.py --tenant <uuid> --connection <uuid>
SQUARE_LOCATION_ID=L... SQUARE_ACCESS_TOKEN=... python scripts/sync_square.py --tenant <uuid> --connection <uuid>
QUICKBOOKS_REALM_ID=9130... QUICKBOOKS_ACCESS_TOKEN=... python scripts/sync_quickbooks.py --tenant <uuid> --connection <uuid>
SHOPMONKEY_API_KEY=... SHOPMONKEY_CURRENCY=USD SHOPMONKEY_TIME_ZONE=America/New_York python scripts/sync_shopmonkey.py --tenant <uuid> --connection <uuid>
```

| Connector | Required | Optional |
|---|---|---|
| Google Ads | `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_ACCESS_TOKEN` | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (a manager's id), `GOOGLE_ADS_LOCAL_SERVICES=1`, `GOOGLE_ADS_API_BASE_URL` |
| TikTok Ads | `TIKTOK_ADVERTISER_ID`, `TIKTOK_ACCESS_TOKEN` | `TIKTOK_API_BASE_URL` |
| Square | `SQUARE_LOCATION_ID`, `SQUARE_ACCESS_TOKEN` | `SQUARE_API_BASE_URL` |
| QuickBooks Online | `QUICKBOOKS_REALM_ID`, `QUICKBOOKS_ACCESS_TOKEN` | `QUICKBOOKS_CURRENCY`, `QUICKBOOKS_API_BASE_URL` |
| Shopmonkey | `SHOPMONKEY_API_KEY`, `SHOPMONKEY_CURRENCY` | `SHOPMONKEY_LOCATION_ID`, `SHOPMONKEY_TIME_ZONE`, `SHOPMONKEY_API_BASE_URL` |

The account in the environment must be the one the connection holds, or the run is refused before anything is written (a Google customer id with hyphens and without are the same account). A `*_API_BASE_URL` points a script at a stand-in. Every script exits 0 for a succeeded run, 1 for a failed one and 2 when it could not start.

## What each metric means

| Key | Unit | Meaning |
|---|---|---|
| `ad_spend` | minor units, ad account currency | cost on that day (Google Ads `cost_micros`, TikTok `spend`) |
| `ad_conversions` | thousandths | the platform's own conversion count: Google Ads counts the conversion actions the account includes, TikTok the optimisation event the ad group chose. Thousandths because a data driven count is fractional |
| `ad_conversion_value` | minor units | Google Ads `conversions_value`, in the account's currency |
| `ad_leads` | count | Local Services Ads leads created that day, in the account's time zone |
| `payments`, `payments_total`, `payments_refunded` | count, minor units | Square COMPLETED payments on the location's local day, their `total_money` (tip included) and the refunded part of those same payments |
| `invoices`, `invoiced_amount`, `invoice_open_balance` | count, minor units | QuickBooks invoices by `TxnDate`, voided ones left out; the open balance is as of the sync and a later sync restates it |
| `work_orders` | count | Shopmonkey orders created that day (the jobs booked), deleted ones left out |
| `work_order_revenue` | minor units | `totalCostCents` of orders invoiced that day, on the invoice date |

Money always carries its currency, enforced by the `currency_iff_money` check in the database as well as in the connector (migration 013 replaces the check with the six new money keys in it). Square, QuickBooks and Shopmonkey are not added to the store figures.

## The ad spend view

`app/report/adspend.py` answers the weekly question for the Google Ads, Meta and TikTok connections of a tenant: spend, conversions and cost per conversion, for the window and for each Monday to Sunday week in it, with the shop's booked jobs (Shopmonkey `work_orders`) beside them. It reads stored snapshots only, never a vendor API.

```bash
python scripts/ad_spend_report.py --tenant <uuid> --start 2026-10-01 --end 2026-10-15          # the text table
python scripts/ad_spend_report.py --tenant <uuid> --start 2026-10-01 --end 2026-10-15 --json
curl -H "Authorization: Bearer $KEY" "$HUB/api/reports/ad-spend?start=2026-10-01&end=2026-10-15"
```

The window is half open. Sample output, from invented figures (a Google Ads, a Meta and a TikTok account in USD and a Shopmonkey shop; it is the same call the tests make, not a live account):

```
Ad spend Oct 1 to Oct 14

Platform               Cur          Spend  Conversions Cost per conv.     Days
Fort Myers Google      USD        $665.00         28.5         $23.33    14/14
Fort Myers Meta        USD        $350.00            7         $50.00    14/14
Fort Myers TikTok      USD        $210.00           11         $19.09    14/14

Total in USD: $1,225.00 across Fort Myers Google, Fort Myers Meta, Fort Myers TikTok; conversions 46.5; blended cost per conversion $26.34
Booked jobs (Shopmonkey work orders): 15

By week
Week               Platform                      Spend  Conversions Cost per conv.     Booked
Oct 1 to Oct 4     Fort Myers Google           $170.00            8         $21.25          4
                   Fort Myers Meta             $100.00            2         $50.00           
                   Fort Myers TikTok            $60.00            3         $20.00           
Oct 5 to Oct 11    Fort Myers Google           $336.00         14.5         $23.17          8
                   Fort Myers Meta             $175.00            4         $43.75           
                   Fort Myers TikTok           $105.00            6         $17.50           
Oct 12 to Oct 14   Fort Myers Google           $159.00            6         $26.50          3
                   Fort Myers Meta              $75.00            1         $75.00           
                   Fort Myers TikTok            $45.00            2         $22.50           

Days is the days with a conversion figure over the days with spend.
A conversion is each platform's own: Google Ads: conversion actions the account counts; TikTok Ads: the optimisation event the ad group chose; Meta Ads: purchases, 7 day click.
Money is in each ad account's own currency and is never converted or added across currencies.
```

What it will not do, by rule:

- **Money is never added across currencies.** A platform in another currency has its own row in the totals; nothing is converted.
- **A conversion is not the same thing on every platform, and the table says which it is.** Meta's is purchases in a 7 day click window; Google Ads' and TikTok's are the account's own definition. The blended figure in a total is labelled blended for that reason.
- **Cost per conversion divides the spend of the days that have a conversion figure.** A day with spend and no figure does not pull it up, and the `Days` column says how many days the figure covers (conversion days over days with spend).
- **No conversions shows `n/a`**, not zero and not infinity.
- **Booked jobs are the shop's own count and are never attributed to a platform or divided by spend**: an order created this week need not come from an ad this week. They are `n/a`, not zero, when no day of the window was synced.
- A window is at most 366 days. One tenant with two active Shopmonkey connections is refused (the jobs would be counted twice), and two ad connections of one platform must have different display names.

## Limits, stated

- **None of this has run against a live account.** Where a vendor page did not say, the code says UNVERIFIED and the sources page lists it (for example: whether TikTok's `end_date` is inclusive, whether a Google Ads `FROM customer` query returns a row for a quiet day, whether Shopmonkey's `totalCostCents` includes tax).
- **Token exchange and refresh are not built.** Each connector takes an already issued token (or a function returning the current one), like GA4, Meta and Search Console. Google tokens last about an hour, Square's 30 days, Intuit's about an hour.
- **Square, QuickBooks and Shopmonkey write a window whole or not at all.** A day's total needs every page, so they read all pages and yield once, with no resume cursor. A failure writes nothing for that window ([test](../tests/test_ads_business_sync_db.py)).
- **Shopmonkey reads every order on each sync** and keeps the ones in the window, because the pages give no filter syntax. It is bounded at 500 pages of orders, and the first thing to replace for a large shop.
- **A Square refund is counted on the day of its payment**, not the day of the refund; the Refunds API would give that date.
- **QuickBooks' open balance falls as customers pay**, so it is a figure as of the last sync.

## Tests

```bash
python -m pytest tests/test_vendor_common.py tests/test_google_ads_connector.py tests/test_tiktok_connector.py tests/test_square_connector.py tests/test_quickbooks_connector.py tests/test_shopmonkey_connector.py tests/test_adspend_view.py tests/test_ads_business_sync_db.py -q
```

Each connector file drives its connector over real HTTP against its stand-in: the request it builds, the paging, the money and currency rules, the error taxonomy, bounded retries, shapes that must not become figures (each refusal after a positive control), the registry entry, and that the stand-in refuses what the saved pages say the vendor refuses. `test_vendor_common.py` and `test_adspend_view.py` run their rules over 300 seeded random worlds each and carry mutation controls: a deliberately broken retry loop, a total that adds currencies, a cost per conversion that divides all spend, a booked jobs figure that reads zero and a week that stops a day short must each turn the simulator red. `test_ads_business_sync_db.py` runs all five through the real sync runner into Postgres as the `sync_worker` role, checks the new database rules, row level security between tenants, the view read from stored rows, and the API call with real API keys. Output of a run: [test-runs/2026-11-07-ads-business-pytest.txt](test-runs/2026-11-07-ads-business-pytest.txt); 22 deliberate breakages of the new code, each run in a copy, each killed: [test-runs/2026-11-07-ads-business-mutation.txt](test-runs/2026-11-07-ads-business-mutation.txt).
