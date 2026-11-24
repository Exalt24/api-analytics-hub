# LIFESPAN: permanent (the repo's own mutation check for the inventory planning module, run by hand)
"""Mutation control for the inventory planning code: break each guard one at a time and prove the inventory tests notice.

It follows tests/mutation_check_sms.py. It needs the compose Postgres (the database suite is one of the four it runs, about 10 seconds a mutant) and demands more than "the suite went
red": each mutant names the tests of which at least one must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS app/inventory/*.py, app/changes.py AND migrations/020_inventory_planning.sql IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original
bytes back. Run it in a COPY of the repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check.
A normal exit, Ctrl-C and SIGTERM all restore the file first.

    python tests/mutation_check_inventory.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_inventory.py --match velocity,abc only the guards whose name holds a word
    python tests/mutation_check_inventory.py                      every guard

An argument it does not know is refused with exit 2, and nothing is edited.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["tests/test_inventory_model.py", "tests/test_inventory_alerts.py", "tests/test_inventory_reader.py", "tests/test_inventory_db.py"]
BASELINE_TIMEOUT_SECONDS = 300
MUTANT_TIMEOUT_SECONDS = 240

TARGETS = {
    "model": ROOT / "app" / "inventory" / "model.py",
    "alerts": ROOT / "app" / "inventory" / "alerts.py",
    "store": ROOT / "app" / "inventory" / "store.py",
    "reader": ROOT / "app" / "inventory" / "reader.py",
    "changes": ROOT / "app" / "changes.py",
    "m020": ROOT / "migrations" / "020_inventory_planning.sql",
    "orders": ROOT / "app" / "inventory" / "orders.py",
    "order_store": ROOT / "app" / "inventory" / "order_store.py",
    "m021": ROOT / "migrations" / "021_purchase_orders.sql",
    "api": ROOT / "app" / "api" / "inventory.py",
    "auth": ROOT / "app" / "api" / "auth.py",
    "workbook": ROOT / "app" / "inventory" / "workbook.py",
}


def venv_python() -> Path:
    """The repository venv's interpreter: Scripts on Windows, bin elsewhere, else the running one."""
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (target, name, old text, new text, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- which days were on sale
    ("model", "velocity: a sale does not prove stock",
     "        if sales.get(day, 0) > 0:\n            states[day] = IN_STOCK", "        if False:\n            states[day] = IN_STOCK",
     ["a_sale_proves_stock", "hand_worked_velocity"], "the last unit can sell and leave the snapshot at zero"),
    ("model", "velocity: a zero snapshot counts as stock",
     "states[day] = IN_STOCK if snapshots[day] > 0 else OUT_OF_STOCK", "states[day] = IN_STOCK if snapshots[day] >= 0 else OUT_OF_STOCK",
     ["zero_snapshot_with_no_sale", "hand_worked_velocity"], "an empty shelf is out of stock"),
    ("model", "velocity: a gap beside stock is read as empty",
     "out = earlier is not None and earlier <= 0 and (later is None or later <= 0)", "out = earlier is not None and earlier <= 0",
     ["gap_between_two_empty_snapshots"], "an empty snapshot then stock does not say which day it restocked"),
    ("model", "velocity: the days after a last empty snapshot are not out",
     "(later is None or later <= 0)", "(later is not None and later <= 0)",
     ["gap_between_two_empty_snapshots"], "nothing later says the shelf refilled"),
    ("model", "velocity: an out of stock day is counted",
     "            out_days += 1\n            continue", "            out_days += 1",
     ["never_counts_a_day", "hand_worked_velocity", "does_not_make_a_product_look_slow"], "a stockout must not fool the forecast"),
    ("model", "velocity: the exclude policy keeps unknown days",
     "            if unknown_policy == \"exclude\":\n                continue", "            if False:\n                continue",
     ["unknown_days_are_counted"], "the policy is a named choice"),
    ("model", "velocity: divided by the window, not the counted days",
     "mean = units / n if n else 0.0", "mean = units / len(days) if days else 0.0",
     ["hand_worked_velocity", "never_counts_a_day", "does_not_make_a_product_look_slow"], "the whole point of the adjustment"),
    ("model", "velocity: population variance instead of sample variance",
     "/ (n - 1) if n >= 2 else 0.0", "/ n if n >= 2 else 0.0",
     ["hand_worked_variance"], "the variance estimate feeds the safety stock"),
    # -- promotions and cover
    ("model", "promo: overlapping promotions take the smallest factor",
     "return max(covering) if covering else 1.0", "return min(covering) if covering else 1.0",
     ["promotion_factor_covers"], "overlaps take the largest factor"),
    ("model", "promo: the last day is outside the promotion",
     "if p.starts_on <= day <= p.ends_on", "if p.starts_on <= day < p.ends_on",
     ["promotion_factor_covers"], "a promotion covers its end date"),
    ("model", "cover: an empty shelf has no cover figure",
     "    if available <= 0:\n        return 0.0\n    if baseline <= 0:\n        return None", "    if available <= 0:\n        return None\n    if baseline <= 0:\n        return None",
     ["hand_worked_demand_and_cover"], "an empty shelf has zero days of cover"),
    ("model", "cover: an oversold variant has negative cover",
     "return max(available, 0) / velocity", "return available / velocity",
     ["test_days_of_cover"], "cover is never negative"),
    # -- safety stock and the reorder point
    ("model", "safety: any service level accepted",
     "if not 0.5 <= service_level < 1:", "if not 0.4 <= service_level < 1:",
     ["the_normal_quantile"], "a service level under half is not a service level"),
    ("model", "safety: the lead time deviation is not squared with the demand",
     "lead_days * daily_sd ** 2 + daily_mean ** 2 * lead_sd_days ** 2", "lead_days * daily_sd ** 2 + daily_mean * lead_sd_days ** 2",
     ["hand_worked_safety_stock", "property_the_reorder_point"], "a late delivery adds d^2 * sd_L^2"),
    ("model", "safety: demand variation not scaled by the lead time",
     "lead_days * daily_sd ** 2 + daily_mean ** 2", "daily_sd ** 2 + daily_mean ** 2",
     ["hand_worked_safety_stock", "property_the_reorder_point"], "variation accumulates over the lead time"),
    ("model", "safety: a negative input is accepted",
     "if min(daily_mean, daily_sd, lead_days, lead_sd_days) < 0:", "if False:",
     ["no_variation_means_no_safety_stock"], "a negative deviation is a caller error"),
    ("model", "reorder point: safety stock left out",
     "return lead_demand + safety", "return lead_demand",
     ["hand_worked_safety_stock", "property_the_reorder_point"], "the reorder point carries the safety stock"),
    ("model", "order: the minimum order quantity ignored",
     "    quantity = max(quantity, moq)\n", "    quantity = quantity\n",
     ["round_to_order", "pack_multiple", "hand_worked_plan"], "a supplier does not take less than its minimum"),
    ("model", "order: not rounded to whole packs",
     "return int(math.ceil(quantity / pack_size)) * pack_size", "return quantity",
     ["round_to_order", "pack_multiple", "hand_worked_plan"], "a supplier ships whole packs"),
    ("model", "order: a negative quantity is ordered",
     "    if quantity <= 0:\n        return 0\n    quantity = max", "    if quantity < 0:\n        return 0\n    quantity = max",
     ["round_to_order", "pack_multiple"], "nothing due means no order, not a minimum order"),
    # -- classes and labels
    ("model", "abc: the 80 percent cut is inclusive",
     "classes[key] = \"A\" if before < a_cut else \"B\" if before < b_cut else \"C\"", "classes[key] = \"A\" if before <= a_cut else \"B\" if before < b_cut else \"C\"",
     ["hand_worked_abc"], "an A item starts under 80 percent of the revenue"),
    ("model", "abc: ties broken by input order",
     "sorted(revenue.items(), key=lambda kv: (-kv[1], kv[0]))", "sorted(revenue.items(), key=lambda kv: -kv[1])",
     ["classes_partition", "same_revenue_are_ranked_by_key"], "the answer must not depend on input order"),
    ("model", "label: a best seller needs no minimum of units",
     "if abc == \"A\" and units_all >= params.best_min_units:", "if abc == \"A\":",
     ["labels_each_rule"], "one big sale is not a best seller"),
    ("model", "label: rising needs no minimum of recent units",
     "if recent.units >= params.rising_min_units and", "if recent.units > params.rising_min_units and",
     ["labels_each_rule"], "the floor of five units is inclusive"),
    ("model", "plan: reorder only strictly under the reorder point",
     "needs = baseline > 0 and position <= rop", "needs = baseline > 0 and position < rop",
     ["every_variant_gets_one_class", "hand_worked_plan"], "at the reorder point is the moment to order"),
    ("model", "plan: a variant that does not sell is reordered",
     "needs = baseline > 0 and position <= rop", "needs = position <= rop",
     ["every_variant_gets_one_class", "does_not_sell_is_never_on_the_reorder_list"], "nothing sells, nothing to order"),
    ("model", "plan: incoming stock ignored in the position",
     "position = item.available + inbound", "position = item.available",
     ["every_variant_gets_one_class", "order_on_the_way"], "stock on its way counts against the order"),
    ("model", "plan: the order is sized for the lead time only",
     "target = demand_over(baseline, as_of, lead + params.review_days, item.promos) + ss", "target = demand_over(baseline, as_of, lead, item.promos) + ss",
     ["hand_worked_plan"], "the review period is the cover an order buys beyond the lead time"),
    ("model", "plan: an untracked variant is planned",
     "status = \"untracked\" if not item.tracked else \"no_stock_reading\" if not item.stock_known else", "status = \"no_stock_reading\" if not item.stock_known else",
     ["untracked_variant_and_one_never_read"], "a variant whose stock the shop does not track has no quantity to plan"),
    ("model", "restock: an equal quantity counts as a restock",
     "if previous is not None and snapshots[day] > previous:", "if previous is not None and snapshots[day] >= previous:",
     ["last_restock"], "a restock is stock arriving, not stock standing still"),
    # -- alerts
    ("alerts", "alert: an empty shelf with a negative count is not out of stock",
     "    if available <= 0:\n        return \"out_of_stock\"", "    if available < 0:\n        return \"out_of_stock\"",
     ["which_shortage_a_variant_shows"], "zero on the shelf is out of stock"),
    ("alerts", "alert: cover equal to the lead time is short",
     "if cover_days is not None and cover_days < lead_time_days:", "if cover_days is not None and cover_days <= lead_time_days:",
     ["which_shortage_a_variant_shows"], "exactly enough cover is not short"),
    ("alerts", "alert: a variant that does not sell is out of stock",
     "if status in (\"untracked\", \"no_stock_reading\") or velocity <= 0:", "if status in (\"untracked\", \"no_stock_reading\") or velocity < 0:",
     ["which_shortage_a_variant_shows"], "an empty shelf of a product nobody buys is not a shortage"),
    ("alerts", "alert: the stock cycle is not part of the event",
     "return restock.isoformat() if restock else \"start\"", "return \"start\"",
     ["event_ref_names", "later_day_in_the_same_stock_cycle", "raised_once"], "a restock begins a new cycle"),
    ("alerts", "alert: the idempotency key drops the cycle",
     "key = \"%s:%s:%s:%s\" % (tenant_id, variant_id, row_kind, ref)", "key = \"%s:%s:%s:start\" % (tenant_id, variant_id, row_kind)",
     ["raised_once"], "the sink recognises a repeat by the key"),
    ("alerts", "alert: a repeat is queued again",
     "        if alert_id is None:\n            seen += 1\n            continue\n", "        if alert_id is None:\n            seen += 1\n",
     ["raised_once", "never_alerts_again"], "an alert is raised once"),
    ("alerts", "alert: a throttled answer is read as a refusal",
     "        if resp.status_code == 429:", "        if False:",
     ["webhook_sink_maps_each_outcome"], "a 429 is retried, a refusal is final"),
    ("alerts", "alert: a server error is read as a refusal",
     "        if resp.status_code >= 500:", "        if resp.status_code >= 600:",
     ["webhook_sink_maps_each_outcome"], "a 5xx may have reached the sink and is retried with the same key"),
    ("alerts", "alert: the idempotency header is not sent",
     "headers={\"Idempotency-Key\": key}", "headers={}",
     ["webhook_sink_maps_each_outcome"], "the key is how a safe resend is recognised"),
    ("alerts", "alert: a transport error carries its message",
     "return SinkAnswer(\"ambiguous\", None, type(exc).__name__)", "return SinkAnswer(\"ambiguous\", None, str(exc))",
     ["webhook_sink_maps_each_outcome"], "an error message can carry the webhook URL"),
    ("alerts", "alert: the digest lists by name, not by urgency",
     "key=lambda s: (KINDS.index(s.kind), s.title, s.variant_id)", "key=lambda s: (s.title, s.variant_id)",
     ["open_shortages_lists_only_shortages"], "out of stock comes before a projected stockout"),
    # -- the database side
    ("store", "store: a refresh erases the cost a person set",
     "unit_cost_minor = coalesce(excluded.unit_cost_minor, inventory_variant.unit_cost_minor),", "unit_cost_minor = excluded.unit_cost_minor,",
     ["second_sync_the_same_day"], "a shop that reports no cost must not erase ours"),
    ("store", "store: a refresh erases the supplier",
     "product_gid = excluded.product_gid,\n                   product_title", "product_gid = excluded.product_gid, supplier_id = null,\n                   product_title",
     ["second_sync_the_same_day"], "what a person set is never overwritten"),
    ("store", "store: a second sync the same day keeps the old quantity",
     "on conflict (variant_id, observed_on) do update set available = excluded.available,", "on conflict (variant_id, observed_on) do update set available = inventory_snapshot.available,",
     ["second_sync_the_same_day"], "a second sync corrects that day's row"),
    ("store", "store: stale order lines are kept",
     "delete from inventory_order_line where processed_on >= $1 and processed_on < $2\"", "delete from inventory_order_line where processed_on >= $1 and processed_on < $2 and false\"",
     ["leaves_no_stale_line"], "a cancelled or edited order leaves no line behind"),
    ("store", "store: the prior window is not loaded",
     "window_start = as_of - timedelta(days=params.recent_days + params.prior_days)", "window_start = as_of - timedelta(days=params.recent_days)",
     ["stored_plan_matches_the_worked_example"], "the trend compares two windows"),
    ("store", "store: the same day's old plan is kept",
     "delete from inventory_plan_run where as_of = $1\"", "delete from inventory_plan_run where as_of = $1 and false\"",
     ["plan_again_the_same_day"], "a rerun replaces that day's plan"),
    # -- the delivery code shared with change detection
    ("changes", "outbox: any table name is accepted",
     "    if table not in OUTBOX_TABLES:", "    if False:",
     ["not_an_outbox"], "a table name goes into SQL"),
    ("changes", "outbox: the inventory outbox is not a known table",
     "OUTBOX_TABLES = (\"change_outbox\", \"inventory_alert_outbox\", \"inventory_po_alert_outbox\")", "OUTBOX_TABLES = (\"change_outbox\", \"inventory_po_alert_outbox\")",
     ["pending_alert_is_delivered", "failing_sink", "refusal_is_permanent"], "the inventory alerts use this delivery code"),
    # -- the migration
    ("m020", "schema: row level security not forced on the snapshots",
     "alter table inventory_snapshot      force row level security;", "select 1;",
     ["enabled_and_forced"], "the owner must not bypass the policy"),
    ("m020", "schema: the order line policy lets every tenant in",
     "create policy inventory_order_line_by_tenant on inventory_order_line\n    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)", "create policy inventory_order_line_by_tenant on inventory_order_line\n    using (true)",
     ["tenant_b_sees_none"], "tenant isolation is in the database"),
    ("m020", "schema: the alert key ignores the stock cycle",
     "constraint inventory_alert_natural_key unique (tenant_id, variant_id, kind, event_ref)", "constraint inventory_alert_natural_key unique (tenant_id, variant_id, kind, event_ref, as_of)",
     ["raised_once", "later_day_in_the_same_stock_cycle", "refuses_a_second_alert"], "one alert for one shortage, however often the plan runs"),
    ("m020", "schema: the request role may rewrite an alert",
     "grant select, insert on inventory_alert to app_request;", "grant select, insert, update, delete on inventory_alert to app_request;",
     ["refuses_a_second_alert"], "alerts are append only"),
    ("m020", "schema: a promotion factor of zero is allowed",
     "check (factor > 0 and factor <= 20)", "check (factor >= 0 and factor <= 20)",
     ["bad_factor"], "a zero factor would forecast no demand"),
    ("m020", "schema: a promotion can end before it starts",
     "constraint promo_dates_in_order check (ends_on >= starts_on)", "constraint promo_dates_in_order check (true)",
     ["bad_factor"], "dates are in order"),
    ("m020", "schema: the delivery worker sees no outbox row",
     "create policy inventory_alert_outbox_worker on inventory_alert_outbox to sync_worker using (true) with check (true);", "create policy inventory_alert_outbox_worker on inventory_alert_outbox to sync_worker using (false) with check (true);",
     ["pending_alert_is_delivered", "worker_role_reads_the_outbox"], "the worker crosses tenants by design"),
]


# ---------------------------------------------------------------------------------------------------------------- purchase orders and the workbook
# The seventh item of a guard is the suites that run for it (the default is SUITES). The purchase order guards run only the order suites, which is what the order code is tested by.
ORDER_PURE = ["tests/test_inventory_orders_model.py"]
ORDER_DB = ["tests/test_inventory_orders_model.py", "tests/test_inventory_orders_db.py"]
ORDER_API = ["tests/test_inventory_orders_api.py"]
WORKBOOK_SUITE = ["tests/test_inventory_workbook.py", "-x", "-k", "(recalculates or reproduces or four_sheets or comparison_names) and not formulas_package"]

MUTATIONS += [
    # -- the pure order rules
    ("orders", "orders: a shipped order may be cancelled",
     "    SHIPPED: frozenset({PARTLY_RECEIVED, RECEIVED}),", "    SHIPPED: frozenset({PARTLY_RECEIVED, RECEIVED, CANCELLED}),",
     ["transition_table_is_exactly", "shipped_order_is_not_cancelled"], "goods in transit are not cancelled", ORDER_PURE),
    ("orders", "orders: a draft may be shipped",
     "    DRAFT: frozenset({ORDERED, CANCELLED}),", "    DRAFT: frozenset({ORDERED, SHIPPED, CANCELLED}),",
     ["transition_table_is_exactly", "draft_cannot_skip"], "a draft is placed before anything else happens to it", ORDER_PURE),
    ("orders", "orders: a person may move an order to received",
     "    if to_status in RECEIPT_ONLY:\n        raise IllegalMove(", "    if False:\n        raise IllegalMove(",
     ["neither_receipt_status"], "only a receipt moves an order to partly received or received", ORDER_PURE),
    ("orders", "orders: the open quantity can go negative",
     "    return max(ordered - received, 0)", "    return ordered - received",
     ["open_quantity_is_never_negative", "property_inbound_equals"], "an over-receipt leaves nothing open, not a negative amount", ORDER_PURE),
    ("orders", "orders: more than ordered is accepted without saying so",
     "    if total > ordered and not allow_over:", "    if False:",
     ["receiving_adds_to_the_line"], "an over-receipt must be accepted on purpose", ORDER_PURE),
    ("orders", "orders: a received quantity of zero is accepted",
     "    if quantity <= 0:\n        raise BadQuantity", "    if quantity < 0:\n        raise BadQuantity",
     ["receiving_adds_to_the_line"], "a receipt of nothing is a mistake", ORDER_PURE),
    ("orders", "orders: an accepted over-receipt is not flagged",
     "    return total, total > ordered", "    return total, False",
     ["receiving_adds_to_the_line"], "an over-receipt stays on record", ORDER_PURE),
    ("orders", "orders: received when any line is in full",
     "    if lines and all(received >= ordered for ordered, received in lines):", "    if lines and any(received >= ordered for ordered, received in lines):",
     ["status_after_a_receipt"], "received needs every line in full", ORDER_PURE),
    ("orders", "orders: late on the expected day itself",
     "expected_on is not None and expected_on < as_of and open_units > 0", "expected_on is not None and expected_on <= as_of and open_units > 0",
     ["when_an_order_is_late"], "an order expected today is not late yet", ORDER_PURE),
    ("orders", "orders: late with nothing left to arrive",
     "expected_on is not None and expected_on < as_of and open_units > 0", "expected_on is not None and expected_on < as_of",
     ["when_an_order_is_late"], "nothing left to arrive is nothing late", ORDER_PURE),
    ("orders", "orders: a draft is inbound",
     "        if status in OPEN_STATUSES and left > 0:", "        if left > 0:",
     ["inbound_is_the_open_quantity", "property_inbound_equals"], "a draft ordered nothing", ORDER_PURE),
    ("orders", "orders: the lead time is ignored for the expected date",
     "    return ordered_on + timedelta(days=lead_time_days) if lead_time_days is not None else None", "    return ordered_on if lead_time_days is not None else None",
     ["expected_date_is_the_order_date_plus"], "the expected date is the order date plus the lead time", ORDER_PURE),
    # -- how the plan reads open orders. Two guards were dropped as equivalent: the `on_time` filter in plan_variant and the offset test in projected_cover each only repeat what the loop
    # already does (an arrival before the plan date is never reached), so no input tells a mutant of either from the original. The same goes for the status list in the SQL that reads the open orders (store.py): `arrivals_by_variant` filters on the status
    # again, and the guard on that one (`orders: a draft is inbound`) is the one that stands.
    ("model", "plan: open orders are not the inbound stock",
     '    if item.arrivals:\n        inbound, source = sum(a.quantity for a in item.arrivals), "orders"', '    if False:\n        inbound, source = sum(a.quantity for a in item.arrivals), "orders"',
     ["order_on_the_way", "orders_replace_shopifys", "two_open_orders"], "stock on order counts against the next order", ORDER_PURE),
    ("model", "plan: an arrival is added after the day's sales",
     "        remaining += landing.get(i, 0)\n", "        remaining += landing.get(i - 1, 0)\n",
     ["projected_cover_adds_an_arrival"], "an arrival lands at the start of its day", ORDER_PURE),
    ("model", "plan: the cover for the alert ignores the arrival",
     "alert_cover = projected_cover(item.available, baseline, as_of, (), arrivals=on_time) if on_time else cover", "alert_cover = cover",
     ["order_on_the_way_before_the_shelf_empties", "property_adding_an_open_order"], "an order that lands in time is no shortage", ORDER_PURE),
    ("model", "plan: a late order is not flagged",
     "    inbound_late = any(d < as_of for d in dated)", "    inbound_late = any(d < as_of - timedelta(days=9999) for d in dated)",
     ["order_past_its_date"], "the page says an order is late", ORDER_PURE),
    ("store", "store: the orders are not given to the plan",
     "arrivals=tuple(arrivals.get(key, []))))", "arrivals=()))",
     ["order_on_the_way_means_no_shortage"], "the plan reads what is on order", ORDER_DB),
    ("store", "store: the alert uses the plain cover",
     "r.lead_time_days, r.alert_cover_days, r.forecast_cover_days)", "r.lead_time_days, r.cover_days, r.forecast_cover_days)",
     ["order_on_the_way_means_no_shortage"], "an order on the way is not a shortage", ORDER_DB),
    # -- the database side of the orders
    ("order_store", "orders: a repeat late alert is queued again",
     "            seen += 1\n            continue\n        new += 1\n        key = \"%s:%s:%s\" % (tenant_id, r[\"id\"], alerts.LATE_KIND)",
     "            seen += 1\n        new += 1\n        key = \"%s:%s:%s\" % (tenant_id, r[\"id\"], alerts.LATE_KIND)",
     ["late_order_is_reported_once"], "a late order is reported once", ORDER_DB),
    ("order_store", "orders: expected today is raised as late",
     "o.expected_on is not null and o.expected_on < $2", "o.expected_on is not null and o.expected_on <= $2",
     ["only_an_open_order_past_its_date"], "an order expected today is not late yet", ORDER_DB),
    ("order_store", "orders: the idempotency key drops the order",
     'key = "%s:%s:%s" % (tenant_id, r["id"], alerts.LATE_KIND)', 'key = "%s:%s:%s" % (tenant_id, "order", alerts.LATE_KIND)',
     ["late_order_is_reported_once"], "the sink recognises a repeat by the key, and two orders are not one", ORDER_DB),
    ("order_store", "orders: a refusal escapes as a database error",
     "        cls = orders.ERROR_BY_CODE.get(getattr(exc, \"sqlstate\", None) or \"\")", "        cls = None",
     ["illegal_move_is_refused_loudly", "refused_when_nothing_is_due"], "a caller gets the order rule's own refusal", ORDER_DB),
    ("m021", "schema: row level security not forced on the orders",
     "alter table inventory_purchase_order       force row level security;", "select 1;",
     ["enabled_and_forced"], "the owner must not bypass the policy", ORDER_DB),
    ("m021", "schema: the purchase order line policy lets every tenant in",
     "create policy inventory_purchase_order_line_by_tenant on inventory_purchase_order_line\n    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)",
     "create policy inventory_purchase_order_line_by_tenant on inventory_purchase_order_line\n    using (true)",
     ["tenant_b_sees_none"], "tenant isolation is in the database", ORDER_DB),
    ("m021", "schema: the transition trigger is gone",
     "create trigger inventory_po_guard before update on inventory_purchase_order\n    for each row execute function inventory_po_guard();", "select 1;",
     ["hand_written_update", "illegal_move_is_refused_loudly", "terminal_order_never_moves"], "the rule holds for a hand written UPDATE too", ORDER_DB),
    ("m021", "schema: a shipped order may be cancelled in the table",
     "('shipped', 'partly_received'), ('shipped', 'received'),", "('shipped', 'partly_received'), ('shipped', 'received'), ('shipped', 'cancelled'),",
     ["table_matches_the_python_rules"], "the database and the Python rules agree", ORDER_DB),
    ("m021", "schema: an over-receipt needs no flag",
     "constraint po_line_received_within_ordered check (quantity_received <= quantity_ordered or over_received),", "constraint po_line_received_within_ordered check (true),",
     ["refuses_a_received_quantity_that_exceeds"], "an over-receipt is explicit", ORDER_DB),
    ("m021", "schema: received without every line in full",
     "if new.status = 'received' and (n_lines = 0 or n_done < n_lines) then", "if false then",
     ["hand_written_update"], "received needs every line in full, whoever says so", ORDER_DB),
    ("m021", "schema: the expected date ignores the lead time",
     "new.expected_on := coalesce(new.expected_on, new.ordered_on + new.lead_time_days);", "new.expected_on := new.expected_on;",
     ["sets_the_expected_date_from_the_lead_time"], "the expected date is the order date plus the lead time", ORDER_DB),
    ("m021", "schema: the lead time is read at the draft, not at placing",
     "select s.lead_time_days into new.lead_time_days from inventory_supplier s where s.id = new.supplier_id;", "null;",
     ["sets_the_expected_date_from_the_lead_time"], "the lead time is the one in force when the order was placed", ORDER_DB),
    ("m021", "schema: two drafts for one supplier",
     "if exists (select 1 from inventory_purchase_order x where x.supplier_id = v_supplier and x.status = 'draft') then", "if false then",
     ["draft_is_refused_when_nothing_is_due"], "two drafts would order the same goods twice", ORDER_DB),
    ("m021", "schema: an order can move to its own status",
     "    if p_to = v_from then", "    if false then",
     ["illegal_move_is_refused_loudly"], "a status cannot move to itself", ORDER_DB),
    ("m021", "schema: an over-receipt is taken without the flag",
     "if v_total > l.quantity_ordered and not coalesce(p_allow_over, false) then", "if false then",
     ["receiving_more_than_ordered_is_refused"], "more than ordered needs to be accepted", ORDER_DB),
    ("m021", "schema: the order is never marked received",
     "    if v_open = 0 then", "    if false then",
     ["partial_receipt_then_the_rest"], "every line in full is received", ORDER_DB),
    ("m021", "schema: an order event can be rewritten",
     "create trigger inventory_po_event_append_only before update or delete on inventory_po_event", "create trigger inventory_po_event_append_only before delete on inventory_po_event",
     ["cannot_delete_an_order_or_rewrite_history"], "history is append only", ORDER_DB),
    ("m021", "schema: a late alert key that allows a second alert",
     "constraint inventory_po_alert_natural_key unique (tenant_id, order_id, kind)", "constraint inventory_po_alert_natural_key unique (tenant_id, order_id, kind, as_of)",
     ["late_order_is_reported_once"], "one alert for one late order", ORDER_DB),
    ("m021", "schema: the request role may delete an order",
     "grant select, insert, update on inventory_purchase_order to app_request;", "grant select, insert, update, delete on inventory_purchase_order to app_request;",
     ["cannot_delete_an_order_or_rewrite_history"], "a cancelled order stays as the record", ORDER_DB),
    ("m021", "schema: a line can be changed after the order is placed",
     "is distinct from (old.quantity_ordered, old.unit_cost_minor, old.currency) and s <> 'draft' then", "is distinct from (old.quantity_ordered, old.unit_cost_minor, old.currency) and false then",
     ["lines_cannot_be_added_removed_or_changed"], "what was ordered is not rewritten", ORDER_DB),
    ("m021", "schema: a received quantity can be reduced",
     "if new.quantity_received < old.quantity_received then", "if false then",
     ["never_reduced_by_a_direct_update"], "a receipt is never taken back", ORDER_DB),
    ("m021", "schema: a cost needs no currency",
     "constraint po_line_cost_needs_currency check (unit_cost_minor is null or currency is not null),", "constraint po_line_cost_needs_currency check (true),",
     ["money_is_integer_minor_units"], "money carries its currency", ORDER_DB),
    # -- the HTTP door
    ("api", "api: a missing order is a 400",
     "orders.BadQuantity: 422, orders.NotFound: 404,", "orders.BadQuantity: 422, orders.NotFound: 400,",
     ["another_tenants_order_and_a_missing_one"], "a missing order is a 404", ORDER_API),
    ("api", "api: a malformed order id is a 422",
     "    except ValueError:\n        raise HTTPException(status_code=404, detail=\"no such purchase order\") from None", "    except ValueError:\n        raise HTTPException(status_code=422, detail=\"no such purchase order\") from None",
     ["another_tenants_order_and_a_missing_one"], "a malformed id is the same 404", ORDER_API),
    ("api", "api: making an order needs only read",
     "async def create_order(body: CreateOrder, request: Request, principal: Principal = Depends(requires(\"write:inventory\"))):",
     "async def create_order(body: CreateOrder, request: Request, principal: Principal = Depends(requires(\"read:inventory\"))):",
     ["viewer_may_read_but_not_write"], "a viewer reads and does not write", ORDER_API),
    ("api", "api: a receipt needs only read",
     "async def receive(order_id: str, body: ReceiptBody, request: Request, principal: Principal = Depends(requires(\"write:inventory\"))):",
     "async def receive(order_id: str, body: ReceiptBody, request: Request, principal: Principal = Depends(requires(\"read:inventory\"))):",
     ["a_viewer_cannot_move_or_receive"], "a viewer reads and does not write", ORDER_API),
    ("api", "api: an unknown field in a body is ignored",
     "    model_config = ConfigDict(extra=\"forbid\")", "    model_config = ConfigDict(extra=\"ignore\")",
     ["unknown_supplier_is_a_404"], "no tenant id in a body", ORDER_API),
    ("auth", "api: a viewer may write inventory",
     "    Role.VIEWER: frozenset({\"read:inventory\"}),", "    Role.VIEWER: frozenset({\"read:inventory\", \"write:inventory\"}),",
     ["permissions_a_viewer_reads", "viewer_may_read_but_not_write"], "a viewer reads and does not write", ORDER_API),
    # -- the workbook's formulas
    ("workbook", "workbook: the z score is for another service level",
     'ws["B9"] = "=NORMSINV(B4)"', 'ws["B9"] = "=NORMSINV(B4-0.04)"',
     ["four_sheets", "recalculates", "reproduces"], "safety stock reads the service level", WORKBOOK_SUITE),
    ("workbook", "workbook: safety stock rounded down",
     "ROUNDUP(ROUND(%s*SQRT(%s*(%s*%s)^2", "ROUNDDOWN(ROUND(%s*SQRT(%s*(%s*%s)^2",
     ["recalculates", "reproduces"], "safety stock is held in whole units rounded up", WORKBOOK_SUITE),
    ("workbook", "workbook: the stock position leaves the inbound out",
     'f["position"] = "=%s+%s" % (c("onhand"), c("inbound"))', 'f["position"] = "=%s+0*%s" % (c("onhand"), c("inbound"))',
     ["recalculates", "reproduces"], "stock on its way counts against the order", WORKBOOK_SUITE),
    ("workbook", "workbook: reorder strictly under the reorder point",
     "AND(%s>0,%s<=%s)", "AND(%s>0,%s<%s)",
     ["recalculates", "reproduces"], "at the reorder point is the moment to order", WORKBOOK_SUITE),
    ("workbook", "workbook: the minimum order is the pack size",
     'c("needs"), c("raw"), c("raw"), c("moq"), c("pack"))', 'c("needs"), c("raw"), c("raw"), c("pack"), c("pack"))',
     ["recalculates", "reproduces"], "a supplier does not take less than its minimum", WORKBOOK_SUITE),
    ("workbook", "workbook: the promotion window is swapped",
     '% (blank, ASOF, c("lead"), PROMO_END, ASOF, PROMO_START)', '% (blank, ASOF, c("lead"), PROMO_START, ASOF, PROMO_END)',
     ["recalculates", "reproduces"], "the promotion applies between its start and its end", WORKBOOK_SUITE),
    ("workbook", "workbook: the lead time deviation comes from the wrong column",
     '_lookup("C", c("supplier"), last_supplier_row)', '_lookup("B", c("supplier"), last_supplier_row)',
     ["recalculates", "reproduces"], "the supplier's deviation is not its lead time", WORKBOOK_SUITE),
    ("workbook", "workbook: an arrival never counts for the cover",
     'plain = "IF(%s>0,%s*%s>%s+IF(', 'plain = "IF(%s>5000000,%s*%s>%s+IF(',
     ["reproduces"], "an order landing in time is no shortage", WORKBOOK_SUITE),
    ("workbook", "workbook: the revenue column sums the stock",
     'value="=SUMIFS(%s,%s,$A%d)" % (rng("revenue"), by, r))', 'value="=SUMIFS(%s,%s,$A%d)" % (rng("onhand"), by, r))',
     ["recalculates", "reproduces"], "sales share is of revenue", WORKBOOK_SUITE),
    ("workbook", "workbook: the labels are counted by class",
     'by = rng("abc") if group in ("A", "B", "C") else rng("label")', 'by = rng("abc")',
     ["recalculates", "reproduces"], "best seller, rising and slow mover are counted by label", WORKBOOK_SUITE),
    ("workbook", "workbook: the reorder point before leaves the safety stock out",
     "ROUND(%s*%s+$D%d,9)", "ROUND(%s*%s+0*$D%d,9)",
     ["recalculates", "reproduces"], "the scenario's before is the plan without the promotion", WORKBOOK_SUITE),
    ("workbook", "workbook: the after column reads the reorder point for the safety stock",
     '"ss_after": \'=IF(%s,"",%s)\' % (no_lead, p("safety")),', '"ss_after": \'=IF(%s,"",%s)\' % (no_lead, p("rop")),',
     ["recalculates", "reproduces"], "the scenario's after reads the plan", WORKBOOK_SUITE),
]


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS, suites: list[str] | None = None):
    """Returns (all_green, failed test names), or (None, []) when the run hung. `suites` are the pytest arguments to run (the default is SUITES)."""
    # A mutant often has the same byte length as the original and is written within a second of the restore, which is what Python's .pyc staleness check cannot tell apart.
    # Compiled copies are deleted and not written so every run reads the real source.
    for pyc in list((ROOT / "app" / "inventory" / "__pycache__").glob("*.pyc")) + list((ROOT / "app" / "__pycache__").glob("changes.*.pyc")) + list((ROOT / "app" / "api" / "__pycache__").glob("*.pyc")):
        pyc.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([str(PY), "-B", "-m", "pytest", *(suites or SUITES), "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    if re.search(r"\b\d+ skipped\b", r.stdout):
        # a database suite that skips because Postgres is gone reads as green; that must never count as a guard that held
        raise RuntimeError("the suite skipped tests (is the compose Postgres up?), so a green run would prove nothing")
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def missing_targets() -> list[str]:
    """Guards whose old text is gone from their file, or that change nothing. Such a guard proves nothing."""
    problems = []
    for key, name, old, new, *_ in MUTATIONS:
        text = TARGETS[key].read_bytes().decode("utf-8").replace("\r\n", "\n")
        if text.count(old) != 1:
            problems.append(f"{name}: its target text appears {text.count(old)} times in {TARGETS[key].name}, it must appear once")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--check-targets", action="store_true", help="only check that every guard finds its target text, edit nothing")
    parser.add_argument("--match", help="comma separated words; only guards whose name holds one run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems = missing_targets()
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    if args.check_targets:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0

    match = args.match.split(",") if args.match else None
    originals = {key: path.read_bytes() for key, path in TARGETS.items()}

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        green, _ = run_suite(BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("baseline suite is RED or hung, fix that before mutating")
            return 1
        print("baseline: green")
        ran = 0
        for key, name, old, new, expected, why, *rest in MUTATIONS:
            if match and not any(word in name for word in match):
                continue
            ran += 1
            original = originals[key]
            crlf = b"\r\n" in original
            text = original.decode("utf-8").replace("\r\n", "\n")
            mutated = text.replace(old, new, 1)
            assert mutated != text, f"{name}: the mutation changed nothing, so a green result would mean nothing"
            if crlf:
                mutated = mutated.replace("\n", "\r\n")
            try:
                TARGETS[key].write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite(suites=rest[0] if rest else None)
            finally:
                TARGETS[key].write_bytes(original)
            if green is None:
                print(f"[ok]   {name}: suite HUNG, so the guard also stops an endless loop")
                killed += 1
            elif green:
                print(f"[FAIL] {name}: suite stayed GREEN, so nothing tests that {why}")
                survived += 1
            elif any(frag in test for test in failed for frag in expected):
                print(f"[ok]   {name}: failed {len(failed)} test(s), including the expected one")
                killed += 1
            else:
                print(f"[FAIL] {name}: the suite went red but not in {expected}; failed: {failed[:4]}")
                wrong_test += 1

        print()
        green, _ = run_suite(BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("suite is RED or hung after restoring, a mutation leaked")
            return 1
        print("source restored, suite green again")
        print(f"mutants run {ran}: killed {killed}, survived {survived}, killed by the wrong test {wrong_test}")
        bad = survived + wrong_test
        print(f"mutation control: {'PASS' if bad == 0 else str(bad) + ' UNTESTED GUARD(S)'}")
        return 1 if bad else 0
    finally:
        for key, path in TARGETS.items():
            if path.read_bytes() != originals[key]:
                path.write_bytes(originals[key])


if __name__ == "__main__":
    sys.exit(main())
