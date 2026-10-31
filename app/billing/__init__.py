"""Prepaid wallet and billing ledger: a double-entry ledger in integer minor units, a wallet for each tenant, usage metering, payments through the official
stripe SDK or a Stripe-style client, a signed webhook receiver, subscriptions with proration, and the reports that prove the books agree.

The tables are migrations/012_billing.sql. The stripe SDK provider ran against stripe-mock and a stand-in; the rest runs against a FAKE provider written from
Stripe's public documentation (docs/vendor-contract/stripe/). No real account, no real money and no card number was ever involved, and no provider client or
webhook receiver here has been run against a live Stripe account. How it works, the failure handling, and what is not done: docs/billing.md.
"""
