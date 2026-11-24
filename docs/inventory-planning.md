# Inventory planning

Reads the stock of every Shopify variant, keeps it as a dated snapshot, and works out from the stored stock and the stored order lines how fast each variant sells, how long its stock lasts, when to order, how much, and which products are best sellers, rising or slow. The code is `app/inventory/`; the tables are `migrations/020_inventory_planning.sql`; the page is `/inventory` in `web/`.

It was built and run against the owner's Shopify development store. A supplier, its lead time and a promotion are figures a person enters: the store holds none of them, and nothing here invents them. [The recorded run](test-runs/2026-11-24-inventory-planning-dev-store.txt) says exactly what the store held and what was entered.

## The loop

```
python scripts/inventory_plan.py sync                    read variants, stock and the last 120 days of orders; store today's snapshot
python scripts/inventory_plan.py supplier --name "Acme Goods" --lead-days 21 --lead-sd 4
python scripts/inventory_plan.py assign --variant TEE-1 --supplier "Acme Goods" --moq 50 --pack 12
python scripts/inventory_plan.py promo --from 2026-12-01 --to 2026-12-07 --factor 1.5 --by "ads team" [--variant TEE-1]
python scripts/inventory_plan.py plan                    work the plan out, store it, print it
python scripts/inventory_plan.py alerts --deliver        raise each shortage once, push the pending ones to a webhook
python scripts/inventory_plan.py digest --state s.json   the shortages still open, worded once (the hub's alert policy)
```

Run `sync` daily (a stock history only exists for the days a sync ran), then `plan`, then `alerts`. Every command runs as the request role with the tenant set, so row level security scopes it. The `/inventory` page reads the stored plan and never calls Shopify.

## What is read from Shopify

`app/inventory/reader.py` sends two queries through the existing `ShopifyConnector` transport, which paces itself from the cost bucket each response reports. It never sends a mutation. For each product variant it reads the names, SKU, whether stock is tracked, the unit cost when the shop holds one, and for each location the `available`, `on_hand`, `committed` and `incoming` quantities (scope `read_inventory`). For orders it reads each line's variant, the quantity still standing after order edits and the unit price after line discounts, on the day the platform attributes the order to (`processedAt`, UTC). A cancelled order is skipped.

Limits: a refund that leaves the goods with the customer is not netted from the units (only an order edit that removes a line is); revenue is before order-level discounts and excludes shipping and tax; an order older than 60 days needs the `read_all_orders` scope.

## Which days a variant was on sale

Shopify serves the current quantity and no daily history, so the history is accumulated: `inventory_snapshot` holds one row per variant per day, written by each sync (a second sync the same day corrects that row). Each day of a window is then one of three things:

| State | When |
|---|---|
| in stock | a unit sold that day (a sale proves stock, even when the last unit left zero), or the snapshot that day shows stock |
| out of stock | the snapshot that day shows none and nothing sold, or there is no snapshot and the nearest earlier snapshot shows none and no later one shows stock |
| unknown | every other day with no snapshot, which includes every day before the first sync ever ran |

An out of stock day is never counted. An unknown day is counted as on sale by default (no record of a stockout is not a stockout), or left out when the plan is run with the `exclude` policy. Each plan line says how many days it counted, how many were out of stock and how many were assumed, and `coverage` is the share of the window whose state was observed.

## The formulas

Daily velocity `d` is the units sold over the counted days divided by the number of counted days, so a variant out of stock for ten days of twenty eight is divided by eighteen. The variance of the daily units over the same days gives `sd` (sample variance) and the standard error `sd / sqrt(n)`. The recent window is the 28 complete days before the plan date; the prior window is the 28 before that.

- Days of cover = available / d (none when nothing sells).
- Safety stock = z * sqrt(L * sd^2 + d^2 * sdL^2), rounded up, where L is the supplier's lead time in days, sdL its standard deviation and z the normal quantile of the service level (95 percent by default, z = 1.645).
- Reorder point = d * L + safety stock, rounded up. A variant is on the reorder list when available plus incoming is at or under it.
- Order quantity = d * (L + review days) + safety stock - (available + incoming), rounded up, then raised to the supplier's minimum order quantity and to whole packs. The review days (14 by default) are the cover the order buys beyond the lead time.

A hand worked case, which the tests pin: a variant sells 2 a day with a standard deviation of 1, the lead time is 9 days, there are 10 on the shelf, the minimum order is 50 and packs are 12. Safety stock is 1.645 * sqrt(9 * 1) = 4.93, held as 5. The reorder point is 2 * 9 + 4.93 = 22.93, held as 23. Cover is 10 / 2 = 5 days, under the 9 day lead time. The target is 2 * (9 + 14) + 5 = 51, less 10 on hand is 41, raised to the minimum 50 and to whole packs of 12: 60.

## Promotions

A promotion is a dated factor a person enters for one variant or for all (1.5 is half as much again as a normal day). Overlapping promotions do not multiply: the largest factor covering a day applies. The forecast demand over the lead time and the review period is the baseline velocity times each day's factor, so a promotion raises the reorder point and the order, and the projected cover (days until the shelf is empty at the promoted demand, with the date) can fall under the lead time while the normal cover does not. That case is its own alert, `stockout_projected`.

## Classes and labels

ABC classes are worked out from each variant's revenue across all the order lines held: ranked by revenue, a variant is A while the revenue ranked above it is under 80 percent of the total, B under 95 percent, C after that or with no revenue. Every variant gets exactly one class, and the answer does not depend on input order.

Labels, first match wins: best seller (class A with at least 10 units sold in all the history held), rising (at least 5 units in the recent window and a recent velocity at least 1.5 times the prior window's, or a prior velocity of zero), slow mover (under 0.1 units a day over the recent window), otherwise steady. The thresholds are `PlanParams` in `app/inventory/model.py` and are stored with each run.

## Alerts

Three kinds, one at most for a variant on a run: out of stock while it sells; cover under the supplier's lead time; a projected stockout inside the lead time that only the promotion causes. A variant that does not sell, is not tracked, or has no stock reading raises nothing; one with no supplier can be out of stock but has no lead time to compare cover with.

Each shortage is raised once. The unit is a stock cycle: `event_ref` is the date of the last restock the snapshots show, and `inventory_alert` has a UNIQUE key on (tenant, variant, kind, event_ref) with an `on conflict do nothing` insert, the pattern of `change_event` in migration 005. A daily run and a rerun cannot raise a second alert; a restock starts a new cycle, so the next shortage alerts again. Alerts are append only.

Delivery is the hub's own outbox code: `inventory_alert_outbox` has the columns of `change_outbox`, and `app.changes.deliver_pending(..., table="inventory_alert_outbox")` claims, pushes and records the outcome with its lease, backoff and dead letter rules, a stable idempotency key on every retry. The digest of what is still open goes through `app.health.decide`, the repeat and recovery policy the sync health check and the backup check use.

## Purchase order drafts

The Inventory page groups the variants that need an order by supplier and writes one draft for each, as CSV, Excel or a tab separated paste for Google Sheets, through Server Actions with Zod checks. It reuses the Reports export code (the CSV cell quoting that defuses a formula, the integer money text, the ExcelJS workbook style, the download helper and the action result type). A line with no unit cost is written with a blank cost, never a zero, and the draft says how many lines the total leaves out. A draft is for a person to approve and send; nothing is sent to a supplier.

## Not done

- Seasonality is not modelled; a person enters promotions and that is the only demand adjustment.
- Daily demand is treated as independent from day to day, and the lead time as normally distributed.
- No purchase order is tracked after the draft: there is no record of what was ordered, no supplier confirmation and no arrival date, so the only inbound figure is Shopify's `incoming` quantity.
- Stock is summed over locations; a plan per location is not made.
- The history starts on the first sync. The days before it are unknown, so a plan made in the first weeks rests mostly on assumed days, and the page says so on each figure.
- Real Shopify read (inventory levels, order lines) is verified on the development store only; nothing was run against a merchant's live shop.
