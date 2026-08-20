"""Shopify access scopes for this connector, and why each one is here.

TWO DIFFERENT ANSWERS, DEPENDING ON WHO OWNS THE STORE, and conflating them is
how an app either gets rejected or gets re-authorised every time it needs a new
field.

  * ON OUR OWN DEV STORE (what this project uses): request the FULL read surface.
    There is no merchant to alarm because we are the merchant, no app review to
    pass because it is custom distribution, and the cost of a missing scope is a
    fresh OAuth round trip later. So take everything readable up front.

  * ON A CLIENT'S STORE (what a real deployment would do): request the narrowest
    set that works. Shopify states it will restrict scopes for apps without a
    legitimate use, and the merchant sees every scope on the consent screen.

WRITE SCOPES ARE ABSENT IN BOTH CASES, and that is not a formality. A read-only
integration cannot corrupt a merchant's orders even if it has a bug, which is the
single most valuable property an analytics connector can have.

THE 60-DAY WALL, which is the constraint that shapes the whole design.
Only the last 60 days of orders are readable with `read_orders`. Reaching further
back requires `read_all_orders`, which is NOT self-serve: it needs Shopify's
approval, requested from the app's API access settings with a justification, and
it can only be added alongside `read_orders`.

The consequence is architectural rather than cosmetic. A dashboard showing 90 days
of history CANNOT be backfilled out of the API on a default install, so the
history has to be accumulated by the sync writing dated snapshots from the day it
goes live. That is the honest answer to "show me growth over the last 90 days":
the clock starts when the connector starts, and a client is better told that up
front than after they ask for last quarter.
"""
from __future__ import annotations

from .shopify_scope_catalog import validate as _validate_scopes

#: The minimum the current KPI set needs. Kept as its own tuple because a client
#: deployment should ask for this rather than FULL_READ_SCOPES.
BASE_READ_SCOPES: tuple[str, ...] = (
    # Orders are the spine: order count, gross, refunds, net, average value.
    "read_orders",
    # Distinct buyers per day. Without it, customer counts are unavailable rather
    # than zero, which is a difference the dashboard must not blur.
    "read_customers",
    # Product and variant names, so a KPI breakdown can be labelled with
    # something a human recognises instead of a numeric id.
    "read_products",
    # Refunds and returns are reported separately from order totals in some
    # shapes, and net sales is wrong if they are missed.
    "read_returns",
    # Discounts materially change gross-versus-net and are otherwise invisible.
    "read_discounts",
    # Multi-location shops attribute orders per location; without this a
    # per-location breakdown silently collapses into one bucket.
    "read_locations",
)

#: Requires Shopify approval. Requested from the app's API access settings with a
#: written justification, and only valid alongside read_orders. Kept separate so
#: nothing accidentally requests it and fails an install.
#: NOT grantable without Shopify reviewing the app. These are real, correctly
#: spelled scopes that the catalogue check passes, so this is a DIFFERENT failure
#: from an invented name: the install is rejected with
#: `missing_shopify_permission` listing them.
#:
#: Measured 2026-08-20 by attempting an install with all of them. Requesting any
#: one of these fails the WHOLE install, so they are kept out of the default
#: string and requested only after approval.
APPROVAL_REQUIRED_SCOPES: tuple[str, ...] = (
    # Orders older than 60 days. The one that actually matters for analytics.
    "read_all_orders",
    # Payment and financial surfaces: gated because they expose money movement
    # and stored payment instruments.
    "read_payment_gateways",
    "read_customer_payment_methods",
    # Marketplace and subscription surfaces, gated to channel/subscription apps.
    "read_marketplace_fulfillment_orders",
    "read_own_subscription_contracts",
    # Merchant risk signals, gated to a narrow set of app categories.
    "read_merchant_approval_signals",
)

#: WRITE SCOPES, FOR SEEDING OUR OWN DEV STORE ONLY.
#:
#: The first version of this file requested read-only and called it a safety
#: property. It is a safety property for the CONNECTOR, and it was a mistake for
#: the PROJECT: with no write access there is no way to create the orders the
#: connector reads, so there is nothing to sync and no demo to show. The blocker
#: was functional, not philosophical.
#:
#: The resolution is two scope sets against one app, with the split enforced in
#: code: `ShopifyConnector` never issues a mutation, and only the seeder is handed
#: these. So the read-only guarantee survives where it matters (the thing that
#: would run against a client's live store) while the dev store stays usable.
SEED_WRITE_SCOPES: tuple[str, ...] = (
    "write_draft_orders",
    "write_orders",
    "write_order_edits",
    "write_products",
    "write_inventory",
    "write_customers",
    "write_customer_merge",
    "write_discounts",
    "write_price_rules",
    "write_returns",
    "write_fulfillments",
    "write_merchant_managed_fulfillment_orders",
    "write_assigned_fulfillment_orders",
    "write_third_party_fulfillment_orders",
    "write_gift_cards",
    "write_marketing_events",
    "write_analytics_annotations",
    "write_content",
    "write_files",
    "write_themes",
    "write_script_tags",
    "write_locales",
    "write_markets",
    "write_translations",
    "write_locations",
    "write_metaobjects",
    "write_metaobject_definitions",
    "write_payment_terms",
    "write_shipping",
    "write_pixels",
    "write_inventory_shipments",
    "write_inventory_transfers",
    "write_validations",
    "write_cart_transforms",
    "write_delivery_customizations",
    "write_payment_customizations",
)

#: Deliberately NOT requested even on our own store. Listed so each omission is a
#: decision on the record rather than an oversight someone "fixes" later.
DELIBERATELY_EXCLUDED = {
    "read_customers_pii": "names, emails and addresses are not needed for aggregate "
                          "KPIs, and holding real PII in a portfolio project is a "
                          "liability with no upside",
    "read_users": "staff account data has nothing to do with sales analytics",
    "write_shopify_payments_payouts": "no scope here mutates money movement",
}


def scope_string(include_all_orders: bool = False) -> str:
    """Comma-separated scopes for `shopify store auth --scopes`."""
    scopes = list(BASE_READ_SCOPES)
    if include_all_orders:
        scopes += list(APPROVAL_REQUIRED_SCOPES)
    return ",".join(scopes)


#: Orders older than this are unreachable without APPROVAL_REQUIRED_SCOPES. The
#: sync uses it to refuse a backfill window it cannot actually satisfy, instead of
#: returning an empty range that charts as zero sales.
DEFAULT_ORDER_HISTORY_DAYS = 60

#: EVERYTHING READABLE that a dev store grants without review. Used for our own
#: store so a later metric never costs another OAuth round trip. Ordered roughly
#: by how likely a future connector metric is to need it.
FULL_READ_SCOPES: tuple[str, ...] = BASE_READ_SCOPES + (
    "read_inventory",
    "read_fulfillments",
    "read_assigned_fulfillment_orders",
    "read_merchant_managed_fulfillment_orders",
    "read_third_party_fulfillment_orders",
    "read_price_rules",
    "read_shipping",
    "read_reports",
    "read_analytics_annotations",
    "read_marketing_events",
    "read_customer_events",
    "read_gift_cards",
    "read_draft_orders",
    "read_order_edits",
    "read_payment_terms",
    "read_shopify_payments_payouts",
    "read_shopify_payments_disputes",
    "read_store_credit_accounts",
    "read_store_credit_account_transactions",
    "read_customer_merge",
    "read_content",
    "read_online_store_pages",
    "read_online_store_navigation",
    "read_files",
    "read_themes",
    "read_script_tags",
    "read_locales",
    "read_markets",
    "read_translations",
    "read_locations",
    "read_metaobjects",
    "read_metaobject_definitions",
    "read_legal_policies",
    "read_privacy_settings",
    "read_validations",
    "read_cart_transforms",
    "read_delivery_customizations",
    "read_payment_customizations",
    "read_checkout_branding_settings",
    "read_checkout_and_accounts_configurations",
    "read_inventory_shipments",
    "read_inventory_transfers",
)


def full_scope_string(include_gated: bool = False) -> str:
    """Every readable scope, for authorising against our own dev store.

    `read_all_orders` is included by default here because it is the one that
    actually matters (see the 60-day wall above), but be aware it is NOT ours to
    grant: Shopify approves it per app from the API access settings. If the
    install fails, drop it and re-run, then request approval separately.
    """
    scopes = list(FULL_READ_SCOPES)
    if include_gated:
        scopes += list(APPROVAL_REQUIRED_SCOPES)
    return ",".join(scopes)


# ---------------------------------------------------------------- token lifetime
#
# "MAKE IT PERMANENT" HAS A SUBTLETY WORTH WRITING DOWN, because the intuitive
# answer is now the wrong one.
#
#   * ONLINE tokens expire when the user logs out or after 24 hours. Never use
#     one for a background sync.
#   * OFFLINE tokens were historically permanent, revoked only on uninstall.
#     They can NOW expire after 60 minutes and come with a refresh token.
#   * Expiring offline tokens became mandatory for NEW PUBLIC apps on
#     2026-04-01, and for ALL public apps from 2027-01-01, after which
#     non-expiring tokens return authentication errors.
#
# A custom-distribution app on a store we own is not a public app, so a
# non-expiring offline token is still obtainable today. But chasing permanence is
# chasing something Shopify is deliberately retiring, and an integration that
# breaks on 2027-01-01 is not permanent in any useful sense.
#
# So the design choice is the opposite of what "permanent" suggests: request
# OFFLINE access, and implement refresh so that expiry is a non-event. That is
# durable under both regimes, and it is the same mechanism that answers "what
# happens when a token expires mid-sync" with code rather than an opinion.
# ---------------------------------------------------------------- plan differences
#
# PLAN DIFFERENCES IN THE RATE BUDGET, and why the connector does not care.
#
#   Standard plans : 1,000 point bucket, 50 points/sec restore
#   Plus           : 2,000 point bucket, 100 points/sec restore
#   Every plan     : a single query is still capped at 1,000 points
#
# Plus buys a deeper bucket, not bigger queries. This matters as a DEVELOPMENT
# HAZARD more than a feature: building against a Plus dev store means twice the
# headroom, so a pacing bug that would throttle a client on Basic may never
# surface locally. The mitigation is already in the design rather than bolted on:
# ShopifyConnector reads maximumAvailable and restoreRate from every response and
# paces off those, so it adapts to whichever plan it is pointed at, and the test
# fixtures deliberately use the tighter standard 1000/50 budget rather than the
# Plus one the dev store will report.
PLAN_BUCKETS = {"standard": (1000.0, 50.0), "plus": (2000.0, 100.0)}
SINGLE_QUERY_COST_CAP = 1000

ACCESS_MODE = "offline"


#: EVERYTHING, read plus write, for authorising against our OWN dev store so the
#: seeder and the connector share one install. A client deployment must use
#: `scope_string()` instead: the write half exists to build test data, not to
#: touch a merchant's shop.
def dev_store_scope_string(include_gated: bool = False) -> str:
    scopes = list(FULL_READ_SCOPES) + list(SEED_WRITE_SCOPES)
    if include_gated:
        # Only after Shopify approves them. Requesting one unapproved gated scope
        # rejects the entire install, so this defaults to off.
        scopes += list(APPROVAL_REQUIRED_SCOPES)
    # Deduplicate while preserving order, since a scope listed twice is rejected.
    seen: set[str] = set()
    ordered = [x for x in scopes if not (x in seen or seen.add(x))]
    return ",".join(ordered)


#: Asserted by the test suite: the connector module must contain no mutation. The
#: read-only guarantee is worth nothing if it lives only in a comment.
CONNECTOR_IS_READ_ONLY = True


if __name__ == "__main__":
    print(dev_store_scope_string())


# Fail at IMPORT rather than at the authorisation screen. An install rejects on
# the first bad name, so a list with several mistakes costs several failed browser
# round trips; this costs nothing and happens before any of them.
_validate_scopes(FULL_READ_SCOPES)
_validate_scopes(SEED_WRITE_SCOPES)
_validate_scopes(BASE_READ_SCOPES)
_validate_scopes(APPROVAL_REQUIRED_SCOPES)
