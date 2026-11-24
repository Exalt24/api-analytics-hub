"""Read stock levels and order lines from the Shopify Admin GraphQL API, through the connector the hub already has.

The ShopifyConnector owns the transport: the pinned API version, the token header, the cost-bucket pacing read from every response, and the error taxonomy (a 401, a 402, a 403 and a
THROTTLED answer each mean something different). This module adds two queries and their mapping and calls `connector._graphql`, so a stock read is paced against the same bucket
as an order read. It never writes to the shop.

WHAT IS READ. For each product variant: its names, SKU, whether stock is tracked, the unit cost when the shop holds one, and for each location the available, on hand, committed and
incoming quantities (`inventoryLevels` -> `quantities`, which needs the read_inventory scope). For orders: each line's variant, SKU, the quantity still standing after order edits
(`currentQuantity`) and the unit price after line discounts, on the day the platform attributes the order to (`processedAt`, UTC, as the connector buckets it). A cancelled order is not read.

LIMITS, stated so they are not found later. A refund that leaves the goods with the customer is not netted from the units (only an order edit that removes a line is). The revenue is
before order-level discounts and excludes shipping and tax. An order older than 60 days needs the read_all_orders scope, which a store may not have granted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.connectors.base import UpstreamBroken
from app.connectors.shopify import ShopifyConnector, _money_to_minor

VARIANTS_QUERY = """
query Variants($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      sku
      title
      product { id title }
      inventoryItem {
        id
        tracked
        unitCost { amount currencyCode }
        inventoryLevels(first: 20) {
          nodes {
            location { id name }
            quantities(names: ["available", "on_hand", "committed", "incoming"]) { name quantity }
          }
        }
      }
    }
  }
}
"""

ORDERS_QUERY = """
query OrderLines($first: Int!, $after: String, $q: String!) {
  orders(first: $first, after: $after, query: $q, sortKey: PROCESSED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      processedAt
      createdAt
      cancelledAt
      lineItems(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          sku
          quantity
          currentQuantity
          variant { id }
          discountedUnitPriceSet { shopMoney { amount currencyCode } }
        }
      }
    }
  }
}
"""

MORE_LINES_QUERY = """
query MoreLines($id: ID!, $after: String) {
  order(id: $id) {
    lineItems(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        sku
        quantity
        currentQuantity
        variant { id }
        discountedUnitPriceSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
"""

#: Variants a page asks for. A page of 100 costs a few hundred points of the bucket's thousand or more, and the connector paces from what each response reports.
VARIANT_PAGE = 100
ORDER_PAGE = 50


@dataclass(frozen=True)
class LocationLevel:
    location_gid: str
    name: str
    available: int
    on_hand: int
    committed: int
    incoming: int


@dataclass(frozen=True)
class VariantStock:
    variant_gid: str
    inventory_item_gid: str | None
    product_gid: str | None
    product_title: str
    variant_title: str
    sku: str | None
    tracked: bool
    unit_cost_minor: int | None
    currency: str | None
    levels: tuple[LocationLevel, ...] = field(default_factory=tuple)

    @property
    def available(self) -> int:
        return sum(level.available for level in self.levels)

    @property
    def on_hand(self) -> int:
        return sum(level.on_hand for level in self.levels)

    @property
    def committed(self) -> int:
        return sum(level.committed for level in self.levels)

    @property
    def incoming(self) -> int:
        return sum(level.incoming for level in self.levels)


@dataclass(frozen=True)
class OrderLine:
    order_gid: str
    line_gid: str
    variant_gid: str | None
    sku: str | None
    processed_on: date
    quantity: int
    net_revenue_minor: int
    currency: str


def _quantities(node: dict[str, Any]) -> dict[str, int]:
    out = {q["name"]: int(q["quantity"]) for q in (node.get("quantities") or [])}
    return out


def parse_variant(node: dict[str, Any]) -> VariantStock:
    """One `productVariants` node into a VariantStock. A level that does not report `available` is refused, since a quantity guessed as zero would read as a stockout."""
    item = node.get("inventoryItem") or {}
    levels: list[LocationLevel] = []
    for lv in ((item.get("inventoryLevels") or {}).get("nodes") or []):
        q = _quantities(lv)
        if "available" not in q:
            raise UpstreamBroken("an inventory level of %s has no available quantity" % node.get("id"))
        loc = lv.get("location") or {}
        levels.append(LocationLevel(loc.get("id") or "", loc.get("name") or "", q["available"], q.get("on_hand", q["available"]), q.get("committed", 0), q.get("incoming", 0)))
    cost = item.get("unitCost") or None
    product = node.get("product") or {}
    return VariantStock(
        variant_gid=node["id"],
        inventory_item_gid=item.get("id"),
        product_gid=product.get("id"),
        product_title=product.get("title") or "",
        variant_title=node.get("title") or "",
        sku=(node.get("sku") or None),
        tracked=bool(item.get("tracked", True)),
        unit_cost_minor=_money_to_minor(cost["amount"], cost["currencyCode"]) if cost and cost.get("amount") is not None else None,
        currency=cost["currencyCode"] if cost and cost.get("amount") is not None else None,
        levels=tuple(levels),
    )


def parse_order_lines(order: dict[str, Any], lines: list[dict[str, Any]]) -> list[OrderLine]:
    """The lines of one order. A line whose quantity was edited to zero is kept at zero units (it is a fact about the order), a missing price is refused."""
    stamp = order.get("processedAt") or order.get("createdAt")
    if not stamp:
        raise UpstreamBroken("order %s has neither processedAt nor createdAt" % order.get("id"))
    day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    out: list[OrderLine] = []
    for ln in lines:
        units = ln.get("currentQuantity")
        units = int(ln["quantity"] if units is None else units)
        price = ((ln.get("discountedUnitPriceSet") or {}).get("shopMoney")) or {}
        if price.get("amount") is None or not price.get("currencyCode"):
            raise UpstreamBroken("line %s has no unit price" % ln.get("id"))
        out.append(OrderLine(order["id"], ln["id"], (ln.get("variant") or {}).get("id"), ln.get("sku") or None, day, units,
                             _money_to_minor(price["amount"], price["currencyCode"]) * units, price["currencyCode"]))
    return out


class ShopifyInventoryReader:
    """Reads variants with their stock and the order lines of a window, paced by the connector it wraps."""

    def __init__(self, connector: ShopifyConnector):
        self._c = connector
        self.api_calls = 0

    async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data, _ = await self._c._graphql(query, variables)
        self.api_calls += 1
        return data

    async def read_variants(self) -> list[VariantStock]:
        out: list[VariantStock] = []
        after: str | None = None
        while True:
            data = await self._gql(VARIANTS_QUERY, {"first": VARIANT_PAGE, "after": after})
            page = data.get("productVariants") or {}
            out.extend(parse_variant(n) for n in (page.get("nodes") or []))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")

    async def read_order_lines(self, window_start: date, window_end: date) -> tuple[list[OrderLine], int]:
        """The lines of every non-cancelled order processed in [window_start, window_end). Returns the lines and how many orders were read."""
        q = "processed_at:>=%s processed_at:<=%s" % (window_start.isoformat(), (window_end - timedelta(days=1)).isoformat())
        out: list[OrderLine] = []
        orders = 0
        after: str | None = None
        while True:
            data = await self._gql(ORDERS_QUERY, {"first": ORDER_PAGE, "after": after, "q": q})
            page = data.get("orders") or {}
            for order in (page.get("nodes") or []):
                orders += 1
                if order.get("cancelledAt"):
                    continue
                items = order.get("lineItems") or {}
                nodes = list(items.get("nodes") or [])
                info = items.get("pageInfo") or {}
                while info.get("hasNextPage"):                       # an order with over 100 lines: read the rest, never drop them
                    more = await self._gql(MORE_LINES_QUERY, {"id": order["id"], "after": info.get("endCursor")})
                    block = ((more.get("order") or {}).get("lineItems")) or {}
                    nodes.extend(block.get("nodes") or [])
                    info = block.get("pageInfo") or {}
                out.extend(parse_order_lines(order, nodes))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out, orders
            after = info.get("endCursor")
