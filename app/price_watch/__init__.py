"""Competitor price monitoring: read competitor pages, match them to our catalogue, work out the landed price, report where a
competitor is cheaper, queue a price change for a person to approve, and push approved changes to Shopify and Zoho Inventory.

The tables are migrations/011_price_watch.sql. Everything here runs against invented pages and fake vendor servers; the Shopify
and Zoho contracts come from the vendors' official pages (docs/vendor-contract/) and were never run against a live account.
How the loop runs, and what is not done: docs/price-watch.md.
"""
