"""The authoritative list of Shopify Admin API access scopes.

WHY THIS FILE EXISTS. On 2026-08-20 an install failed with
`invalid_scope: The access scope is invalid: read_sales_channels`. That scope does
not exist; I had written a plausible-sounding list from memory. Checking against
Shopify's published scope reference found **ten** invented names in one tuple:

    read_product_listings, read_analytics, read_checkouts, read_publications,
    read_sales_channels, read_apps, write_publications, write_checkouts,
    write_product_listings, write_customer_events

Shopify rejects on the FIRST invalid scope, so a wrong list costs one failed
browser round trip per bad name. Hence a checked catalogue and a test that
validates every requested scope before anyone opens a browser.

Transcribed from Shopify's Admin API access scopes reference, 2026-08-20. If a
scope is missing here, verify it against that page rather than adding it on the
strength of it sounding right, which is exactly how the ten got in.
"""
from __future__ import annotations

VALID_ADMIN_SCOPES: frozenset[str] = frozenset({
    "read_all_orders",
    "read_analytics_annotations", "write_analytics_annotations",
    "write_app_proxy",
    "read_assigned_fulfillment_orders", "write_assigned_fulfillment_orders",
    "read_merchant_managed_fulfillment_orders", "write_merchant_managed_fulfillment_orders",
    "read_third_party_fulfillment_orders", "write_third_party_fulfillment_orders",
    "read_marketplace_fulfillment_orders",
    "read_cart_transforms", "write_cart_transforms",
    "read_checkout_branding_settings", "write_checkout_branding_settings",
    "read_checkout_and_accounts_configurations", "write_checkout_and_accounts_configurations",
    "read_content", "write_content",
    "read_online_store_pages",
    "read_customer_events", "write_pixels",
    "read_customer_merge", "write_customer_merge",
    "read_customer_payment_methods",
    "read_customers", "write_customers",
    "read_delivery_customizations", "write_delivery_customizations",
    "read_discounts", "write_discounts",
    "read_draft_orders", "write_draft_orders",
    "read_files", "write_files",
    "read_fulfillments", "write_fulfillments",
    "read_gift_cards", "write_gift_cards",
    "read_inventory", "write_inventory",
    "read_inventory_shipments", "write_inventory_shipments",
    "read_inventory_shipments_received_items", "write_inventory_shipments_received_items",
    "read_inventory_transfers", "write_inventory_transfers",
    "read_legal_policies",
    "read_locales", "write_locales",
    "read_locations", "write_locations",
    "read_markets", "write_markets",
    "read_marketing_events", "write_marketing_events",
    "read_merchant_approval_signals",
    "read_metaobject_definitions", "write_metaobject_definitions",
    "read_metaobjects", "write_metaobjects",
    "read_online_store_navigation", "write_online_store_navigation",
    "read_order_edits", "write_order_edits",
    "read_orders", "write_orders",
    "read_own_subscription_contracts", "write_own_subscription_contracts",
    "read_payment_customizations", "write_payment_customizations",
    "read_payment_gateways", "write_payment_gateways",
    "read_payment_mandate", "write_payment_mandate",
    "write_payment_sessions",
    "read_payment_terms", "write_payment_terms",
    "read_price_rules", "write_price_rules",
    "read_privacy_settings", "write_privacy_settings",
    "read_products", "write_products",
    "read_reports", "write_reports",
    "read_returns", "write_returns",
    "read_script_tags", "write_script_tags",
    "read_shipping", "write_shipping",
    "read_shopify_payments_disputes",
    "read_shopify_payments_dispute_evidences", "write_shopify_payments_dispute_evidences",
    "read_shopify_payments_dispute_file_uploads", "write_shopify_payments_dispute_file_uploads",
    "read_shopify_payments_payouts",
    "read_store_credit_accounts",
    "read_store_credit_account_transactions", "write_store_credit_account_transactions",
    "read_themes", "write_themes",
    "read_translations", "write_translations",
    "read_users",
    "read_validations", "write_validations",
})

#: Names that LOOK right and are not. Kept so the same mistake is caught by
#: recognition rather than by another failed browser round trip.
KNOWN_INVENTED_SCOPES: frozenset[str] = frozenset({
    "read_sales_channels",      # the one that actually failed the install
    "read_apps",
    "read_analytics",           # the real one is read_analytics_annotations
    "read_checkouts", "write_checkouts",
    "read_publications", "write_publications",
    "read_product_listings", "write_product_listings",
    "write_customer_events",    # the real one is write_pixels
})


class InvalidScope(ValueError):
    pass


def validate(scopes) -> None:
    """Raise before anyone opens a browser.

    Shopify rejects on the first bad name, so a list with three mistakes costs
    three failed authorisation attempts. Failing locally costs nothing.
    """
    bad = [s for s in scopes if s not in VALID_ADMIN_SCOPES]
    if bad:
        hints = []
        for s in bad:
            if s in KNOWN_INVENTED_SCOPES:
                hints.append(f"{s} (known non-existent scope)")
            else:
                near = sorted(v for v in VALID_ADMIN_SCOPES if s.split("_", 1)[-1][:6] in v)[:3]
                hints.append(f"{s}" + (f" (did you mean: {', '.join(near)})" if near else ""))
        raise InvalidScope("not real Shopify scopes: " + "; ".join(hints))
