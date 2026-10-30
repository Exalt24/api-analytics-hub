"""Prepaid wallet and billing ledger: a double-entry ledger in integer minor units, a wallet for each tenant, usage metering, payments through a
Stripe-style provider, a signed webhook receiver, subscriptions with proration, and the reports that prove the books agree.

The tables are migrations/012_billing.sql. Everything here runs against a FAKE provider written from Stripe's public documentation
(docs/vendor-contract/stripe/); no real account, no real money and no card number was ever involved, and the provider client and the webhook
receiver have never been run against a live Stripe account. How it works, the failure handling, and what is not done: docs/billing.md.
"""
