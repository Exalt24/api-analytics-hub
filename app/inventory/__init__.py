"""Inventory planning: read stock levels per Shopify variant, keep them as dated snapshots, and work out velocity, cover, reorder points, classes, alerts and purchase orders.

The tables are migrations/020_inventory_planning.sql. `model.py` is pure (no database, no network) so the rules are tested alone; `store.py` is the database side as the request
role; `reader.py` reads Shopify through the existing connector; `alerts.py` raises each shortage once; `po.py` groups the reorder list into one purchase order draft per supplier.
The formulas, the rules for a day with no snapshot and what is not done: docs/inventory-planning.md.
"""
