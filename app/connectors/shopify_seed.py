"""Seed a Shopify DEV store with realistic orders, so there is something to sync.

WHY A SEEDER EXISTS AT ALL. A connector with no data proves that requests
authenticate and nothing else. The interesting behaviour, day bucketing across a
timezone boundary, refunds separating gross from net, distinct-customer counts,
multi-day windows, only appears once the store holds orders spread over time.
Shopify's "generate test data" option gives products and a few orders; this
creates a controlled spread we can assert against.

THE REFUSAL GUARD IS THE IMPORTANT PART. This writes to a real Shopify store, and
the cost of being wrong about WHICH store is asymmetric: seeding a dev store is
free, seeding a live shop puts fake orders in a merchant's books and their
accounting. So it refuses unless the store looks like a development store, and it
refuses if the store already holds more orders than it is about to create. The
same reasoning as the Silid incident, where a script named like a test read
`.env.local` and wrote to production.

Orders are created as DRAFT ORDERS then completed, which is the supported path to
a real Order with line items rather than fabricating one. Payments go through the
Bogus gateway, which is what a dev store provides.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

import httpx

API_VERSION = "2026-07"

#: Refuse to seed a store already holding more orders than this. A dev store we
#: just created holds a handful; a real shop holds thousands. The check is a
#: cheap way to make "wrong store" fail loudly instead of expensively.
MAX_EXISTING_ORDERS = 200

#: Shopify's order-creation limit reports itself as a userErrors message on an
#: HTTP 200, with no code and no Retry-After, so it has to be matched on text.
TOO_MANY_ATTEMPTS = "too many attempts"

#: Seconds to wait between order creations. Measured: five succeeded then the
#: sixth was refused, so this paces under one per twelve seconds rather than
#: discovering the ceiling on every run. Slow on purpose; a seeder has no reason
#: to race.
ORDER_CREATE_PACING_SECONDS = 12.0

#: How many times to back off before giving up on one order.
ORDER_CREATE_MAX_RETRIES = 5

SHOP_QUERY = """
query { shop { name myshopifyDomain currencyCode plan { displayName partnerDevelopment shopifyPlus } } }
"""

ORDER_COUNT_QUERY = """
query { ordersCount { count } }
"""

PRODUCT_CREATE = """
mutation ProductCreate($input: ProductCreateInput!) {
  productCreate(product: $input) {
    product { id title variants(first: 1) { nodes { id price } } }
    userErrors { field message }
  }
}
"""

VARIANT_PRICE_UPDATE = """
mutation VariantPrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price }
    userErrors { field message }
  }
}
"""

PRODUCTS_BY_TITLE = """
query ProductsByTitle($q: String!) {
  products(first: 10, query: $q) {
    nodes { id title variants(first: 1) { nodes { id price } } }
  }
}
"""

CUSTOMERS_BY_EMAIL = """
query CustomersByEmail($q: String!) {
  customers(first: 5, query: $q) { nodes { id email } }
}
"""

CUSTOMER_CREATE = """
mutation CustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id displayName }
    userErrors { field message }
  }
}
"""

ORDER_CREATE = """
mutation OrderCreate($order: OrderCreateOrderInput!) {
  orderCreate(order: $order) {
    order {
      id
      name
      createdAt
      processedAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
    }
    userErrors { field message }
  }
}
"""

DRAFT_ORDER_CREATE = """
mutation DraftOrderCreate($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder { id }
    userErrors { field message }
  }
}
"""

DRAFT_ORDER_COMPLETE = """
mutation DraftOrderComplete($id: ID!) {
  draftOrderComplete(id: $id, paymentPending: false) {
    draftOrder { id order { id name createdAt } }
    userErrors { field message }
  }
}
"""

CATALOGUE = [
    ("Studio Monitor Headphones", "189.00"),
    ("USB Audio Interface", "125.00"),
    ("Standing Desk Frame", "420.00"),
    ("Monitor Arm", "89.00"),
    ("65% Mechanical Keyboard", "149.00"),
    ("Low Profile Wireless Keyboard", "99.00"),
]

BUYERS = [
    ("Ana", "Reyes"),
    ("Miguel", "Santos"),
    ("Lena", "Fischer"),
    ("Tom", "Novak"),
    ("Priya", "Nair"),
]


class SeedRefused(RuntimeError):
    """The target does not look like a dev store, or already holds real data."""


class ShopifySeeder:
    def __init__(self, shop_domain: str, access_token: str, client: httpx.AsyncClient | None = None):
        self.shop_domain = shop_domain.replace("https://", "").strip("/")
        self._token = access_token
        self._client = client or httpx.AsyncClient(timeout=60.0)
        #: Set when Shopify refuses the Customer object. Reported by the CLI
        #: so a run with zero customers is never mistaken for a store with
        #: zero customers.
        self.customer_access_denied = False

    async def _gql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict:
        r = await self._client.post(
            f"https://{self.shop_domain}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": self._token, "Content-Type": "application/json"},
            content=json.dumps({"query": query, "variables": dict(variables or {})}),
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors'][:2]}")
        data = payload.get("data") or {}
        # Shopify reports business-rule failures in userErrors with a 200, so a
        # seeder that only checks HTTP status silently creates nothing.
        for key, value in data.items():
            if isinstance(value, dict) and value.get("userErrors"):
                raise RuntimeError(f"{key} userErrors: {value['userErrors']}")
        return data

    # ------------------------------------------------------------------ guard

    async def preflight(self, force: bool = False) -> dict:
        """Prove this is a dev store with little data BEFORE writing anything."""
        shop = (await self._gql(SHOP_QUERY))["shop"]
        plan = shop.get("plan") or {}
        is_dev = bool(plan.get("partnerDevelopment"))

        counts = await self._gql(ORDER_COUNT_QUERY)
        existing = ((counts.get("ordersCount") or {}).get("count")) or 0

        if not is_dev and not force:
            raise SeedRefused(
                f"{shop.get('myshopifyDomain')} does not report partnerDevelopment. "
                "Refusing to write fake orders to what may be a real shop. Pass --force "
                "only if you are certain."
            )
        if existing > MAX_EXISTING_ORDERS and not force:
            raise SeedRefused(
                f"store already holds {existing} orders, more than the {MAX_EXISTING_ORDERS} "
                "threshold. That does not look like a fresh dev store."
            )

        return {
            "name": shop.get("name"),
            "domain": shop.get("myshopifyDomain"),
            "currency": shop.get("currencyCode"),
            "plan": plan.get("displayName"),
            "partner_development": is_dev,
            "existing_orders": existing,
        }

    # ------------------------------------------------------------------ seeding

    async def ensure_products(self) -> list[tuple[str, Decimal]]:
        """Create products, then price their default variants.

        Two mutations rather than one because ProductInput no longer accepts
        `variants` (verified against 2026-07). productCreate makes a default
        variant; productVariantsBulkUpdate sets its price.
        """
        out: list[tuple[str, Decimal]] = []
        for title, price in CATALOGUE:
            # Look before creating. Two interrupted runs left 23 products for a
            # 6-item catalogue, which then skews any per-product assertion.
            found = await self._gql(PRODUCTS_BY_TITLE, {"q": f"title:'{title}'"})
            existing = ((found.get("products") or {}).get("nodes") or [])
            match = next((p for p in existing if p.get("title") == title), None)
            if match:
                variants = ((match.get("variants") or {}).get("nodes") or [])
                if variants:
                    out.append((variants[0]["id"], Decimal(price)))
                    continue

            data = await self._gql(
                PRODUCT_CREATE,
                {"input": {"title": title, "status": "ACTIVE"}},
            )
            product = (data.get("productCreate") or {}).get("product") or {}
            product_id = product.get("id")
            variants = ((product.get("variants") or {}).get("nodes") or [])
            if not product_id or not variants:
                raise RuntimeError(f"product {title} created with no default variant")

            variant_id = variants[0]["id"]
            priced = await self._gql(
                VARIANT_PRICE_UPDATE,
                {"productId": product_id, "variants": [{"id": variant_id, "price": price}]},
            )
            updated = ((priced.get("productVariantsBulkUpdate") or {}).get("productVariants") or [])
            if not updated:
                raise RuntimeError(f"could not set a price on {title}")
            out.append((variant_id, Decimal(price)))
        return out

    async def ensure_customers(self) -> list[str]:
        """Create buyers, or return none if the app is not approved for them.

        The Customer object sits behind Shopify's protected customer data
        approval, which is SEPARATE from scopes: the install can grant
        read_customers and every customer query still fails with ACCESS_DENIED
        until the app is approved. Measured against the live store 2026-08-21.

        Refusing to seed at all would be the wrong call, because orders, sales,
        refunds and AOV are all still exercised without a customer attached. The
        one metric that degrades is distinct-customers, and a caller that cannot
        tell the difference between zero customers and no access would be the
        real bug, so this returns an empty list and the caller reports it.
        """
        ids: list[str] = []
        for first, last in BUYERS:
          email = f"{first.lower()}.{last.lower()}@example.com"
          try:
            # Look up first. A rerun otherwise dies on "Email has already been
            # taken", which is a userError rather than an exception, so a seeder
            # that only checked HTTP status would have reported success.
            found = await self._gql(CUSTOMERS_BY_EMAIL, {"q": f"email:{email}"})
            match = next(
                (c for c in ((found.get("customers") or {}).get("nodes") or [])
                 if (c.get("email") or "").lower() == email),
                None,
            )
            if match:
                ids.append(match["id"])
                continue

            data = await self._gql(
                CUSTOMER_CREATE,
                {"input": {
                    "firstName": first,
                    "lastName": last,
                    # example.com is IANA-reserved and cannot receive mail, so
                    # a seeder can never accidentally email a stranger. Note that
                    # example.invalid is also reserved, and customerCreate accepts
                    # it while draftOrderCreate refuses it as an invalid domain.
                    "email": email,
                }},
            )
            ids.append(((data.get("customerCreate") or {}).get("customer") or {})["id"])
          except RuntimeError as exc:
            if "protected-customer-data" not in str(exc) and "ACCESS_DENIED" not in str(exc):
                raise
            self.customer_access_denied = True
            return []
        return ids

    async def _create_order_with_backoff(self, order_input: dict) -> dict:
        """Create one order, backing off through the order-creation throttle.

        The throttle is invisible to the cost-bucket pacing the connector does,
        because it is not a cost limit: it arrives as a userErrors message on a
        200 with no Retry-After header to obey. So the delay is doubled locally
        rather than read from the response, and a failure that is NOT the throttle
        is re-raised immediately instead of being retried into a wall.
        """
        delay = ORDER_CREATE_PACING_SECONDS
        for attempt in range(ORDER_CREATE_MAX_RETRIES):
            try:
                data = await self._gql(ORDER_CREATE, {"order": order_input})
            except RuntimeError as exc:
                if TOO_MANY_ATTEMPTS not in str(exc).lower():
                    raise
                if attempt == ORDER_CREATE_MAX_RETRIES - 1:
                    raise
                print(f"  order creation throttled, waiting {delay:.0f}s")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return (data.get("orderCreate") or {}).get("order") or {}
        return {}

    async def seed_orders(self, days: int, per_day: int, seed: int = 7) -> list[str]:
        """Create `per_day` orders on each of the last `days` days.

        Real history, not a pile of orders stamped today: `orderCreate` accepts
        `processedAt`, which is the field Shopify's own sales reporting attributes
        revenue to. `createdAt` is still assigned by the platform and cannot be
        set, so anything bucketing strictly by createdAt will see one busy day.
        That is a genuine property of the API rather than a limitation of this
        seeder, and it is why the connector prefers processedAt when present.

        Days run oldest-first and stop before today, so every seeded day is a
        complete day and a partial final day cannot look like a decline.
        """
        rng = random.Random(seed)
        variants = await self.ensure_products()
        customers = await self.ensure_customers()

        created: list[str] = []
        today = datetime.now(timezone.utc).date()
        for day_offset in range(days):
            # Oldest first, and never today, so every seeded day is complete.
            day = today - timedelta(days=days - day_offset)
            for slot in range(per_day):
                variant_id, _price = rng.choice(variants)
                # Spread within the day so a day holds several distinct times
                # rather than a pile at midnight.
                stamp = datetime(
                    day.year, day.month, day.day,
                    9 + (slot * 3) % 12, rng.randint(0, 59),
                    tzinfo=timezone.utc,
                )
                order_input: dict[str, Any] = {
                    "lineItems": [{"variantId": variant_id, "quantity": rng.randint(1, 3)}],
                    # PAID, so the order counts as revenue rather than sitting
                    # pending and being excluded from most sales reports.
                    "financialStatus": "PAID",
                    # The field that makes a multi-day window real. createdAt is
                    # assigned by Shopify and cannot be set, so a seeder that only
                    # had createdAt could never produce history.
                    "processedAt": stamp.isoformat(),
                }
                if customers:
                    order_input["customer"] = {
                        "toAssociate": {"id": rng.choice(customers)}
                    }
                order = await self._create_order_with_backoff(order_input)
                if order.get("id"):
                    created.append(order["id"])
                # Pace AFTER a success rather than before, so the first order in
                # a run is not delayed for no reason.
                await asyncio.sleep(ORDER_CREATE_PACING_SECONDS)
        return created


async def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a Shopify dev store with test orders.")
    ap.add_argument("--store", required=True, help="the *.myshopify.com domain")
    ap.add_argument("--token", required=True, help="Admin API access token")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--per-day", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="skip the dev-store refusal guard, only if you are certain")
    ap.add_argument("--dry-run", action="store_true", help="preflight only, write nothing")
    args = ap.parse_args()

    seeder = ShopifySeeder(args.store, args.token)
    try:
        info = await seeder.preflight(force=args.force)
    except SeedRefused as exc:
        print("REFUSED:", exc)
        return 1

    print("target store:")
    for k, v in info.items():
        print(f"  {k}: {v}")

    if args.dry_run:
        print("\ndry run, nothing written")
        return 0

    orders = await seeder.seed_orders(args.days, args.per_day)
    print(f"\ncreated {len(orders)} orders")
    if seeder.customer_access_denied:
        print("NOTE: this app is not approved for protected customer data, so orders")
        print("      carry no customer. Every metric except distinct-customers is")
        print("      still exercised. Approval is requested per app, not per scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
