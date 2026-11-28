# LIFESPAN: permanent (the repo's own mutation check for the ordering portal: bank debits, invoices, commissions, the access matrix and partner fulfilment; run by hand and its --check-targets in CI)
"""Mutation control for the ordering portal: break each guard one at a time and prove the tests notice.

It follows tests/mutation_check_esign.py and demands more than "the suite went red": each mutant names the tests that must be among the failures, so a guard cannot be "covered" by an
unrelated test that happens to break. Each mutant runs only the tests it names (a `-k` selection over the portal test files), the baseline and the final restore run all of them. It needs the
compose Postgres (the database suites drop and recreate the `public` schema of the database they point at) and, for the stripe-mock tests, a running stripe-mock (or they are skipped, which the
harness refuses: a skipped test cannot kill a mutant).

THIS EDITS SOURCE FILES IN PLACE (app/access, app/billing, app/fulfillment, app/api/portal.py and migrations 022 to 024). It writes each mutation into the real file, runs the tests, and writes
the original bytes back. Run it in a COPY of the repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check. A
normal exit, Ctrl-C and SIGTERM all restore every file first.

    python tests/mutation_check_fulfillment.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_fulfillment.py --match lock,expiry  only the guards whose name holds a word
    python tests/mutation_check_fulfillment.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["tests/test_billing_ach_db.py", "tests/test_billing_invoices_db.py", "tests/test_billing_stripe_sdk_ach.py", "tests/test_fulfillment_states.py", "tests/test_fulfillment_client.py",
          "tests/test_fulfillment_db.py", "tests/test_access_matrix.py", "tests/test_access_matrix_db.py", "tests/test_access_api_db.py", "tests/test_portal_guards_db.py"]
BASELINE_TIMEOUT_SECONDS = 900
MUTANT_TIMEOUT_SECONDS = 300
CACHES = [ROOT / "app" / d / "__pycache__" for d in ("access", "billing", "fulfillment", "api")]

TARGETS = {
    "matrix": ROOT / "app" / "access" / "matrix.py",
    "principal": ROOT / "app" / "access" / "principal.py",
    "engine": ROOT / "app" / "access" / "engine.py",
    "portal": ROOT / "app" / "api" / "portal.py",
    "commissions": ROOT / "app" / "billing" / "commissions.py",
    "invoices": ROOT / "app" / "billing" / "invoices.py",
    "wallet": ROOT / "app" / "billing" / "wallet.py",
    "payments": ROOT / "app" / "billing" / "payments.py",
    "provider": ROOT / "app" / "billing" / "provider.py",
    "sdk": ROOT / "app" / "billing" / "stripe_sdk.py",
    "states": ROOT / "app" / "fulfillment" / "states.py",
    "audit": ROOT / "app" / "fulfillment" / "audit.py",
    "client": ROOT / "app" / "fulfillment" / "client.py",
    "service": ROOT / "app" / "fulfillment" / "service.py",
    "webhook": ROOT / "app" / "fulfillment" / "webhook.py",
    "m022": ROOT / "migrations" / "022_access_matrix.sql",
    "m023": ROOT / "migrations" / "023_ach_invoices_commissions.sql",
    "m024": ROOT / "migrations" / "024_fulfillment.sql",
}


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# the fragments name tests (lower case substrings of their ids)
GEN_DB = "test_every_user_resource_and_action_is_allowed_or_denied_by_the_database_as_the_table_says"
GEN_API = "test_every_user_and_cell_of_the_matrix_is_allowed_or_forbidden_by_the_api_as_the_table_says"
SEEDED_INV = "test_over_seeded_random_worlds_the_wallet_the_invoices_and_the_commissions"
SEEDED_FUL = "test_over_seeded_worlds_events_arriving_late_twice_and_out_of_order"
DECIDE = "test_decide_for_every_pair_of_statuses"

# (name, target, old text, new text, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # ---------------------------------------------------------------- the matrix, in code
    ("matrix: an unknown role, resource or action is allowed",
     "matrix", '    return _GRANTS.get(role, {}).get(resource, {}).get(action, "none")', '    return _GRANTS.get(role, {}).get(resource, {}).get(action, "all")',
     ["a_role_resource_or_action_the_table_does_not_know"], "a cell the table does not have grants nothing"),
    ("matrix: allows is always true",
     "matrix", '    return scope(role, resource, action) != "none"', "    return True", ["a_role_resource_or_action_the_table_does_not_know", GEN_API], "a role holding none is refused"),
    ("matrix: the seed leaves every scope at none",
     "matrix", "act, scope(r, res, act))", 'act, "none")', ["the_migration_holds_exactly_the_cells_of_the_table"], "the database's copy is generated from the code's table"),
    ("matrix: a clinic user may create invoices",
     "matrix", '"invoices": {"read": "clinic", "settle": "clinic"},', '"invoices": {"read": "clinic", "settle": "clinic", "create": "clinic"},',
     ["the_migration_holds_exactly_the_cells_of_the_table", "only_an_admin_creates_invoices_refunds_them"], "a clinic pays an invoice, it never makes one"),
    ("matrix: a sales rep may update orders",
     "matrix", '"orders": {"read": "assigned"},', '"orders": {"read": "assigned", "update": "assigned"},',
     ["the_migration_holds_exactly_the_cells_of_the_table", "a_sales_rep_can_only_read"], "a rep only reads"),
    ("matrix: a prescriber may read invoices",
     "matrix", '    "prescriber": {\n        "orders": {"read": "clinic", "create": "clinic", "update": "clinic"},',
     '    "prescriber": {\n        "invoices": {"read": "clinic"},\n        "orders": {"read": "clinic", "create": "clinic", "update": "clinic"},',
     ["the_migration_holds_exactly_the_cells_of_the_table", "a_prescriber_never_sees_money"], "a prescriber never sees money"),
    # ---------------------------------------------------------------- the access migration
    ("scope: the clinic scope allows every clinic",
     "m022", "when 'clinic'   then coalesce(p_clinic = portal_user_clinic(), false)", "when 'clinic'   then true", [GEN_DB], "a clinic user sees only its own clinic"),
    ("scope: the assigned scope allows every clinic",
     "m022", "when 'assigned' then p_clinic is not null and exists (select 1 from sales_rep_assignment a where a.rep_user_id = portal_user_id() and a.clinic_id = p_clinic)",
     "when 'assigned' then true", [GEN_DB], "a rep sees only the clinics assigned to it"),
    ("scope: the self scope allows every rep",
     "m022", "when 'self'     then coalesce(p_rep = portal_user_id(), false)", "when 'self'     then true", [GEN_DB], "a rep sees only its own commissions"),
    ("scope: an unknown scope allows",
     "m022", "        else false\n    end\n$$;", "        else true\n    end\n$$;", [GEN_DB], "none means none"),
    ("scope: a role the table has no row for gets all",
     "m022", "m.resource = p_resource and m.action = p_action), 'none')", "m.resource = p_resource and m.action = p_action), 'all')",
     ["a_user_who_is_not_in_the_table_or_has_a_role_it_has_no_row_for"], "an unknown role gets nothing"),
    ("scope: the user's clinic is any user's clinic",
     "m022", "select u.clinic_id from portal_user u where u.id = portal_user_id()", "select u.clinic_id from portal_user u limit 1", [GEN_DB], "the clinic is the asking user's own"),
    ("rls: clinic is not forced", "m022", "alter table clinic                force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    ("rls: portal_user is not enabled", "m022", "alter table portal_user           enable row level security;", "select 1;", ["every_new_table_has_row_level_security"], "row level security is on"),
    ("rls: the assignment table is not forced", "m022", "alter table sales_rep_assignment  force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    ("policy: any user may write clinics",
     "m022", "create policy clinic_write on clinic for all using (tenant_id = portal_tenant() and portal_role() = 'admin') with check (tenant_id = portal_tenant() and portal_role() = 'admin');",
     "create policy clinic_write on clinic for all using (tenant_id = portal_tenant()) with check (tenant_id = portal_tenant());", ["a_rep_cannot_assign_itself_a_clinic"], "only an admin writes clinics"),
    ("policy: any user may write users",
     "m022", "create policy portal_user_write on portal_user for all using (tenant_id = portal_tenant() and portal_role() = 'admin') with check (tenant_id = portal_tenant() and portal_role() = 'admin');",
     "create policy portal_user_write on portal_user for all using (tenant_id = portal_tenant()) with check (tenant_id = portal_tenant());", ["a_rep_cannot_assign_itself_a_clinic"], "nobody promotes itself"),
    ("policy: any user may write assignments",
     "m022", "create policy sales_rep_assignment_write on sales_rep_assignment for all using (tenant_id = portal_tenant() and portal_role() = 'admin')\n    with check (tenant_id = portal_tenant() and portal_role() = 'admin');",
     "create policy sales_rep_assignment_write on sales_rep_assignment for all using (tenant_id = portal_tenant())\n    with check (tenant_id = portal_tenant());", ["a_rep_cannot_assign_itself_a_clinic"], "a rep cannot give itself a clinic or a rate"),
    ("policy: a user sees every user of the tenant",
     "m022", "using (tenant_id = portal_tenant() and (id = portal_user_id() or portal_role() = 'admin'));", "using (tenant_id = portal_tenant());", ["a_rep_cannot_assign_itself_a_clinic"], "a user sees only itself"),
    ("policy: a user sees every clinic",
     "m022", "tenant_id = portal_tenant() and (portal_role() = 'admin' or id = portal_user_clinic()", "tenant_id = portal_tenant() and (true or id = portal_user_clinic()", ["a_rep_cannot_assign_itself_a_clinic"], "a user sees only its clinics"),
    ("policy: a rep sees every assignment",
     "m022", "using (tenant_id = portal_tenant() and (rep_user_id = portal_user_id() or portal_role() = 'admin'));", "using (tenant_id = portal_tenant());", ["a_rep_cannot_assign_itself_a_clinic"], "a rep sees only its own assignments"),
    ("assignment: a clinic can be given to a user who is not a rep",
     "m022", "u.tenant_id = new.tenant_id and u.role = 'sales_rep')", "u.tenant_id = new.tenant_id)", ["an_assignment_to_a_user_who_is_not_a_rep"], "only a rep has clinics assigned"),
    ("credential: a revoked credential still resolves",
     "m022", "where pc.key_hash = p_key_hash and pc.revoked_at is null;", "where pc.key_hash = p_key_hash;", ["a_credential_resolves_to_its_user", "no_key_an_unknown_key_a_revoked_key"], "a revoked key stops working"),
    ("grant: the request role can write the matrix",
     "m022", "grant select on access_matrix to app_request;", "grant select, insert, update, delete on access_matrix to app_request;", ["the_request_role_cannot_change_the_matrix"], "the matrix is read only for the application"),
    ("grant: the request role can write credentials",
     "m022", "grant select on portal_credential to app_request;", "grant select, insert, update, delete on portal_credential to app_request;", ["the_request_role_cannot_change_the_matrix"], "credentials are not the application's to edit"),
    ("user: a clinic user may have no clinic",
     "m022", "constraint clinic_roles_have_a_clinic check ((role in ('clinic', 'prescriber')) = (clinic_id is not null))", "constraint clinic_roles_have_a_clinic check (true)", ["a_clinic_user_and_a_prescriber_belong_to_a_clinic"], "a clinic role has a clinic"),
    # ---------------------------------------------------------------- bank debit, invoices, commissions: the migration
    ("payment: a bank debit may be in another currency",
     "m023", "(currency = 'USD' and customer_ref is not null and mandate_ref is not null)", "(customer_ref is not null and mandate_ref is not null)", ["the_table_refuses_a_bank_debit"], "a bank debit is in US dollars"),
    ("payment: a bank debit may have no mandate",
     "m023", "(currency = 'USD' and customer_ref is not null and mandate_ref is not null)", "(currency = 'USD' and customer_ref is not null)", ["the_table_refuses_a_bank_debit"], "a debit needs the mandate the customer accepted"),
    ("payment: an invoice payment may credit the wallet",
     "m023", "check (purpose <> 'invoice' or (credited_minor = 0 and refunded_minor = 0))", "check (true)", ["the_table_refuses_a_bank_debit"], "a payment for an invoice never credits the wallet"),
    ("ledger: invoice refunds are not a kind of transaction",
     "m023", "'subscription_payment', 'invoice_payment', 'invoice_refund'))", "'subscription_payment', 'invoice_payment'))", ["a_wallet_paid_invoice_is_refunded"], "an invoice refund is booked"),
    ("invoice: a refund may pass the total",
     "m023", "constraint refund_within_total check (refunded_minor <= total_minor),", "constraint refund_within_total check (true),", ["a_refunded_total_beyond_the_total_or_going_down"], "a refund never passes what was paid"),
    ("invoice: an open invoice may have no total",
     "m023", "constraint open_or_paid_has_a_total check (status = 'draft' or status = 'void' or total_minor > 0),", "constraint open_or_paid_has_a_total check (true),", ["an_open_invoice_has_a_total"], "an open invoice has a total"),
    ("invoice: many live payments for one invoice",
     "m023", "where invoice_id is not null and status in ('created', 'processing', 'unknown', 'succeeded');", "where invoice_id is not null and status in ('created');", ["an_open_invoice_has_a_total"], "one live payment for each invoice"),
    ("invoice: settling is the update action",
     "m023", "('open', 'paid', 'settle')", "('open', 'paid', 'update')", ["a_clinic_pays_its_own_invoice_from_the_wallet", GEN_DB], "a clinic pays, it does not update"),
    ("invoice: an open invoice cannot be voided",
     "m023", "('open', 'void', 'update'), ", "", ["a_draft_and_an_open_invoice_can_be_voided"], "an open invoice can be voided"),
    ("invoice: the trigger does not ask the matrix for the move",
     "m023", "        if not portal_allows('invoices', v_action, new.clinic_id) then", "        if false then", [GEN_DB, "the_invoice_triggers_refuse_on_their_own"], "each move needs its action"),
    ("invoice: the trigger does not ask the matrix for a refund",
     "m023", "            if not portal_allows('invoices', 'refund', new.clinic_id) then", "            if false then", [GEN_DB, "the_invoice_triggers_refuse_on_their_own"], "a refund needs the refund action"),
    ("invoice: a refunded total can go down",
     "m023", "            if old.status <> 'paid' or new.refunded_minor < old.refunded_minor then", "            if false then", ["a_refunded_total_beyond_the_total_or_going_down"], "a refunded total only grows"),
    ("invoice: the total can change after it is finalized",
     "m023", "    else\n        if new.total_minor <> old.total_minor then", "    else\n        if false then", ["a_draft_cannot_be_paid_a_finalized_invoice_has_frozen_lines"], "the total is fixed at finalizing"),
    ("invoice: an invoice can move to another clinic",
     "m023", "is distinct from (old.id, old.tenant_id, old.clinic_id, old.number", "is distinct from (old.id, old.tenant_id, new.clinic_id, old.number", ["a_refunded_total_beyond_the_total_or_going_down"], "the clinic never changes"),
    ("invoice: a total that is not the sum of the lines is finalized",
     "m023", "            if v_sum <= 0 or new.total_minor <> v_sum then", "            if false then", ["a_total_that_is_not_the_sum_of_the_lines_cannot_be_finalized_by_hand"], "the total is the sum of the lines"),
    ("invoice: lines change after finalizing",
     "m023", "    if v_status is distinct from 'draft' then", "    if false then", ["a_draft_cannot_be_paid_a_finalized_invoice_has_frozen_lines"], "lines move only in a draft"),
    ("invoice: an unpaid invoice is refunded",
     "m023", "where i.id = new.invoice_id and i.status = 'paid') then", "where i.id = new.invoice_id) then", ["an_open_invoice_has_a_total_a_refund_row_needs_a_paid_invoice"], "only a paid invoice is refunded"),
    ("commission: rows can be edited",
     "m023", "create trigger commission_append_only before update or delete on commission for each row execute function commission_append_only();", "select 1;",
     ["a_ledger_row_and_a_commission_row_can_never_be_edited"], "commissions are append only"),
    ("commission: the net can go below zero",
     "m023", "    if v_net < 0 then", "    if false then", ["a_ledger_row_and_a_commission_row_can_never_be_edited"], "a commission never goes below zero"),
    ("commission: a negative accrual",
     "m023", "constraint accrual_is_positive_reversal_is_negative check ((kind = 'accrual' and amount_minor >= 0) or (kind = 'reversal' and amount_minor < 0)),",
     "constraint accrual_is_positive_reversal_is_negative check (true),", ["a_ledger_row_and_a_commission_row_can_never_be_edited"], "an accrual is positive and a reversal negative"),
    ("proof: a paid invoice not in the ledger is not reported",
     "m023", "       and i.total_minor <> coalesce((select -sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'\n                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_payment'",
     "       and false and i.total_minor <> coalesce((select -sum(e.amount_minor) from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'\n                      where t.tenant_id = i.tenant_id and t.kind = 'invoice_payment'",
     ["billing_invoice_discrepancies_names_each_way"], "the proof finds a paid invoice with no ledger payment"),
    ("proof: an unpaid invoice with a ledger payment is not reported",
     "m023", "     where i.tenant_id = p_tenant and i.status <> 'paid'\n       and exists", "     where i.tenant_id = p_tenant and i.status <> 'paid' and false\n       and exists", ["billing_invoice_discrepancies_names_each_way"], "the proof finds a ledger payment for an unpaid invoice"),
    ("proof: a wrong refunded total is not reported",
     "m023", "     where i.tenant_id = p_tenant\n       and i.refunded_minor <> coalesce(", "     where i.tenant_id = p_tenant and false\n       and i.refunded_minor <> coalesce(", ["billing_invoice_discrepancies_names_each_way"], "the proof finds a refunded total the ledger does not hold"),
    ("proof: a wrong commission is not reported",
     "m023", "    having sum(c.amount_minor) <> ((i.total_minor - i.refunded_minor) * max(c.rate_bp) / 10000)", "    having false", ["billing_invoice_discrepancies_names_each_way"], "the proof finds a commission that is not the rate"),
    ("policy: any role reads invoices of the tenant",
     "m023", "create policy billing_invoice_select on billing_invoice for select using (tenant_id = portal_tenant() and portal_allows('invoices', 'read', clinic_id));",
     "create policy billing_invoice_select on billing_invoice for select using (tenant_id = portal_tenant());", [GEN_DB], "an invoice is read by its clinic and rep only"),
    ("policy: any role inserts invoices",
     "m023", "create policy billing_invoice_insert on billing_invoice for insert with check (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id));",
     "create policy billing_invoice_insert on billing_invoice for insert with check (tenant_id = portal_tenant());", [GEN_DB], "only a role that may create invoices does"),
    ("policy: any role updates invoices",
     "m023", "using (tenant_id = portal_tenant() and (portal_allows('invoices', 'update', clinic_id) or portal_allows('invoices', 'settle', clinic_id) or portal_allows('invoices', 'refund', clinic_id)))",
     "using (tenant_id = portal_tenant())", ["the_invoice_policies_refuse_on_their_own"], "a row is not even visible to update for a role without a right to it"),
    ("policy: any role reads invoice lines",
     "m023", "create policy billing_invoice_line_select on billing_invoice_line for select using (tenant_id = portal_tenant() and portal_allows('invoices', 'read', clinic_id));",
     "create policy billing_invoice_line_select on billing_invoice_line for select using (tenant_id = portal_tenant());", ["a_clinic_pays_an_invoice_by_bank_debit", "an_invoice_is_made_and_finalized_by_an_admin"], "lines are as private as their invoice"),
    ("policy: any role writes invoice lines",
     "m023", "create policy billing_invoice_line_write on billing_invoice_line for all using (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id))\n    with check (tenant_id = portal_tenant() and portal_allows('invoices', 'create', clinic_id));",
     "create policy billing_invoice_line_write on billing_invoice_line for all using (tenant_id = portal_tenant())\n    with check (tenant_id = portal_tenant());", ["invoice_lines_are_written_only_by_a_role_that_may_create_invoices"], "only the role that makes an invoice makes its lines"),
    ("policy: any role inserts refunds",
     "m023", "create policy billing_invoice_refund_insert on billing_invoice_refund for insert with check (tenant_id = portal_tenant() and portal_allows('invoices', 'refund', clinic_id));",
     "create policy billing_invoice_refund_insert on billing_invoice_refund for insert with check (tenant_id = portal_tenant());", ["invoice_lines_are_written_only_by_a_role_that_may_create_invoices"], "only a role that may refund asks for a refund"),
    ("policy: any role reads commissions",
     "m023", "create policy commission_select on commission for select using (tenant_id = portal_tenant() and portal_allows('commissions', 'read', clinic_id, rep_user_id));",
     "create policy commission_select on commission for select using (tenant_id = portal_tenant());", [GEN_DB], "a rep sees its own commissions only"),
    ("policy: a reversal needs the create action",
     "m023", "portal_allows('commissions', case when kind = 'reversal' then 'refund' else 'create' end, clinic_id, rep_user_id)", "portal_allows('commissions', 'create', clinic_id, rep_user_id)",
     ["a_cell_changed_in_the_database_table_changes_what_the_policies_allow"], "a reversal is the refund action"),
    ("policy: any role inserts commissions",
     "m023", "create policy commission_insert on commission for insert\n    with check (tenant_id = portal_tenant() and", "create policy commission_insert on commission for insert\n    with check (tenant_id = portal_tenant() or", [GEN_DB], "only an admin writes commissions"),
    ("policy: any role reads payments",
     "m023", "as restrictive for select using (portal_role() is null or portal_allows('payments', 'read', clinic_id));", "as restrictive for select using (true);", [GEN_DB], "a payment is read by its clinic and rep only"),
    ("policy: any role inserts payments",
     "m023", "as restrictive for insert with check (portal_role() is null or portal_allows('payments', 'create', clinic_id));", "as restrictive for insert with check (true);", [GEN_DB], "a clinic makes payments for its own clinic only"),
    ("policy: any role updates payments",
     "m023", "    using (portal_role() is null or portal_allows('payments', 'update', clinic_id)) with check (portal_role() is null or portal_allows('payments', 'update', clinic_id));",
     "    using (true) with check (true);", [GEN_DB], "only an admin moves a payment"),
    ("policy: no role at all is a role",
     "m023", "as restrictive for select using (portal_role() is null or", "as restrictive for select using (portal_role() is not null or", ["with_no_role_set_every_portal_table_answers_nothing"], "the older tenant wide behaviour of payments is exactly the unset role"),
    ("m023: invoices are not forced",
     "m023", "alter table billing_invoice           force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    ("m023: commissions are not forced",
     "m023", "alter table commission                force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    # ---------------------------------------------------------------- fulfilment migration
    ("order: the reference may be readable text",
     "m024", "order_ref        text        not null unique check (order_ref ~ '^ord_[0-9a-f]{24}$'),", "order_ref        text        not null unique,", ["an_order_whose_reference_is_not_opaque"], "an order reference is opaque"),
    ("order: a shipped order needs no tracking",
     "m024", "constraint shipped_has_carrier_and_tracking check ((status in ('shipped', 'delivered')) = (carrier is not null and tracking_number is not null))",
     "constraint shipped_has_carrier_and_tracking check (true)", ["an_order_whose_reference_is_not_opaque"], "shipped has a carrier and a tracking number"),
    ("order: the table of moves lacks packed to shipped",
     "m024", "    ('packed', 'shipped'),\n", "", ["the_code_table_and_the_migration_table_are_the_same_set_of_moves", "the_whole_road_to_delivery"], "the table of moves is the code's"),
    ("order: any move is legal",
     "m024", "        if not exists (select 1 from fulfillment_transition t where t.from_status = old.status and t.to_status = new.status) then", "        if false then",
     ["a_hand_written_move_that_is_not_in_the_table"], "the database refuses a move that is not in the table"),
    ("order: the reference can change",
     "m024", "(new.id, new.tenant_id, new.clinic_id, new.order_ref, new.created_by, new.created_at)", "(new.id, new.tenant_id, new.clinic_id, old.order_ref, new.created_by, new.created_at)",
     ["a_hand_written_move_that_is_not_in_the_table"], "the reference never changes"),
    ("order: the trigger does not ask the matrix for a move",
     "m024", "        if not portal_allows('orders', 'update', new.clinic_id) then\n            raise exception 'the asking role may not update order %', old.id using errcode = 'insufficient_privilege';\n        end if;\n    elsif",
     "        if false then\n            raise exception 'the asking role may not update order %', old.id using errcode = 'insufficient_privilege';\n        end if;\n    elsif", ["the_order_policy_refuses_on_its_own"], "a move needs the update action"),
    ("order: any role updates orders",
     "m024", "create policy fulfillment_order_update on fulfillment_order for update using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))",
     "create policy fulfillment_order_update on fulfillment_order for update using (tenant_id = portal_tenant())", ["the_order_policy_refuses_on_its_own"], "only a role that may update an order does"),
    ("order: any role reads orders",
     "m024", "create policy fulfillment_order_select on fulfillment_order for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));",
     "create policy fulfillment_order_select on fulfillment_order for select using (tenant_id = portal_tenant());", [GEN_DB], "an order is read by its clinic and rep only"),
    ("order: an order can be inserted in any status",
     "m024", "with check (tenant_id = portal_tenant() and status = 'created' and portal_allows('orders', 'create', clinic_id));", "with check (tenant_id = portal_tenant() and portal_allows('orders', 'create', clinic_id));",
     ["an_order_cannot_be_inserted_in_any_status_but_created"], "an order starts as created"),
    ("order: any role inserts orders",
     "m024", "with check (tenant_id = portal_tenant() and status = 'created' and portal_allows('orders', 'create', clinic_id));", "with check (tenant_id = portal_tenant() and status = 'created');", [GEN_DB], "only a role that may create orders does"),
    ("children: any role reads submissions",
     "m024", "create policy fulfillment_submission_select on fulfillment_submission for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));",
     "create policy fulfillment_submission_select on fulfillment_submission for select using (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "an order's children are as private as the order"),
    ("children: any role writes submissions",
     "m024", "create policy fulfillment_submission_write on fulfillment_submission for all\n    using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))\n    with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));",
     "create policy fulfillment_submission_write on fulfillment_submission for all\n    using (tenant_id = portal_tenant())\n    with check (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "only a role that may update an order moves its send"),
    ("children: any role reads dead letters",
     "m024", "create policy fulfillment_dead_letter_select on fulfillment_dead_letter for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));",
     "create policy fulfillment_dead_letter_select on fulfillment_dead_letter for select using (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "dead letters are as private as the order"),
    ("children: any role writes dead letters",
     "m024", "create policy fulfillment_dead_letter_write on fulfillment_dead_letter for all\n    using (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id))\n    with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));",
     "create policy fulfillment_dead_letter_write on fulfillment_dead_letter for all\n    using (tenant_id = portal_tenant())\n    with check (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "only a role that may update an order dead letters it"),
    ("children: any role reads events",
     "m024", "create policy fulfillment_event_select on fulfillment_event for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));",
     "create policy fulfillment_event_select on fulfillment_event for select using (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "events are as private as the order"),
    ("children: any role inserts events",
     "m024", "create policy fulfillment_event_insert on fulfillment_event for insert with check (tenant_id = portal_tenant() and portal_allows('orders', 'update', clinic_id));",
     "create policy fulfillment_event_insert on fulfillment_event for insert with check (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "only a role that may update an order records an event"),
    ("children: any role reads audit rows",
     "m024", "create policy fulfillment_audit_select on fulfillment_audit for select using (tenant_id = portal_tenant() and portal_allows('orders', 'read', clinic_id));",
     "create policy fulfillment_audit_select on fulfillment_audit for select using (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "the audit log is as private as the order"),
    ("children: any role inserts audit rows",
     "m024", "with check (tenant_id = portal_tenant() and (portal_allows('orders', 'update', clinic_id) or (kind = 'created' and portal_allows('orders', 'create', clinic_id))));",
     "with check (tenant_id = portal_tenant());", ["a_role_that_may_only_read_orders_cannot_write_the_children"], "only a role that may act on an order writes its audit"),
    ("router: the router reads every column",
     "m024", "grant select (id, tenant_id, order_ref) on fulfillment_order to fulfillment_router;", "grant select on fulfillment_order to fulfillment_router;", ["the_router_role_reads_an_orders_id_tenant_and_reference"], "the router reads three columns"),
    ("router: the router reads no order",
     "m024", "create policy fulfillment_order_router_read on fulfillment_order for select to fulfillment_router using (true);", "create policy fulfillment_order_router_read on fulfillment_order for select to fulfillment_router using (false);",
     ["the_router_role_reads_an_orders_id_tenant_and_reference", "a_forged_tampered_stale_or_unsigned_event"], "a partner event finds its order"),
    ("audit: the audit log can be edited by the owner",
     "m024", "create trigger fulfillment_audit_append_only before update or delete on fulfillment_audit for each row execute function fulfillment_append_only();", "select 1;",
     ["the_audit_and_event_rows_are_append_only"], "an audit row is never edited"),
    ("audit: the event log can be edited by the owner",
     "m024", "create trigger fulfillment_event_append_only before update or delete on fulfillment_event for each row execute function fulfillment_append_only();", "select 1;",
     ["the_audit_and_event_rows_are_append_only"], "an event row is never edited"),
    ("audit: the request role can edit the logs",
     "m024", "grant select, insert on fulfillment_event, fulfillment_audit to app_request;", "grant select, insert, update, delete on fulfillment_event, fulfillment_audit to app_request;",
     ["the_audit_and_event_rows_are_append_only"], "the application may only add rows"),
    ("m024: orders are not forced", "m024", "alter table fulfillment_order        force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    ("m024: the audit log is not forced", "m024", "alter table fulfillment_audit        force row level security;", "select 1;", ["every_new_table_has_row_level_security"], "the owner is filtered too"),
    # ---------------------------------------------------------------- bank debit in the provider interface
    ("provider: a debit is accepted in another currency",
     "provider", '    if currency.lower() != "usd":', "    if False:", ["the_request_is_refused_here_before_anything_is_sent"], "a bank debit is in US dollars"),
    ("provider: a debit is accepted without its customer or mandate",
     "provider", "    if not customer or not mandate:\n        raise ValueError(\"a bank debit needs", "    if False:\n        raise ValueError(\"a bank debit needs", ["the_request_is_refused_here_before_anything_is_sent"], "a bank debit needs both"),
    ("sdk: the bank debit fields are not sent",
     "sdk", '        if payment_method_type != "card":', "        if False:", ["the_sdk_sends_the_bank_debit_as_the_documented_form"], "the SDK sends the customer and the mandate"),
    ("payments: the bank debit fields are not sent",
     "payments", '    if row["payment_method_type"] == "card":\n        return {}', "    if True:\n        return {}", ["a_bank_debit_is_processing_when_created", "the_wallet_is_credited_by_the_succeeded_event"], "the debit reaches the provider as a debit"),
    ("payments: a bank debit with no customer is tried",
     "payments", '    if method_type == "us_bank_account" and (currency.upper() != "USD" or not customer or not mandate):', "    if False:", ["a_bank_debit_without_its_customer_or_mandate"], "a debit without its customer is refused before the provider"),
    ("payments: an invoice payment without an invoice is tried",
     "payments", '    if purpose not in ("topup", "invoice") or (purpose == "invoice") != (invoice_id is not None):', "    if False:", ["a_payment_for_an_invoice_must_name_its_invoice"], "an invoice payment names its invoice"),
    ("payments: a payment for an invoice credits the wallet",
     "payments", '    if row["purpose"] == "invoice":\n        from . import invoices      # here and not at the top: invoices.py imports this module', '    if False:\n        from . import invoices      # here and not at the top: invoices.py imports this module',
     ["paying_by_card_charges_once_pays_the_invoice", SEEDED_INV], "a payment for an invoice pays the invoice and never the wallet"),
    ("payments: a refund of an invoice payment is a top-up refund",
     "payments", '    if row["purpose"] == "invoice":\n        from . import invoices\n        return await invoices.apply_provider_refund', '    if False:\n        from . import invoices\n        return await invoices.apply_provider_refund',
     ["a_card_paid_invoice_is_refunded_through_the_provider"], "a refund of an invoice payment reverses the invoice"),
    ("payments: an invoice payment can be refunded as a top-up",
     "payments", '    if row["purpose"] == "invoice":\n        raise BillingError("a payment made for an invoice', '    if False:\n        raise BillingError("a payment made for an invoice', ["a_payment_made_for_an_invoice_is_refunded_through_its_invoice"], "an invoice payment is refunded through its invoice"),
    # ---------------------------------------------------------------- wallet, invoices, commissions
    ("wallet: an invoice may overdraw the wallet", "wallet", "    if from_wallet and amount_minor > available:", "    if False:", ["not_enough_credit_writes_nothing"], "the wallet pays only with credit"),
    ("wallet: a card payment debits the wallet",
     "wallet", '    source = "wallet" if from_wallet else "provider_clearing"', '    source = "wallet"', ["paying_by_card_charges_once_pays_the_invoice"], "a card payment does not touch the wallet"),
    ("wallet: a provider refund credits the wallet",
     "wallet", '    sink = "wallet" if to_wallet else "provider_clearing"', '    sink = "wallet"', ["a_card_paid_invoice_is_refunded_through_the_provider", SEEDED_INV], "money refunded to a card does not go to the wallet"),
    ("invoices: a reused key with other lines is answered",
     "invoices", '        if row["request_hash"] != req:\n            raise IdempotencyConflict(key)\n        if created:', "        if False:\n            raise IdempotencyConflict(key)\n        if created:", ["the_same_key_with_other_lines_is_refused"], "a reused key with other content is refused"),
    ("invoices: a boolean is a quantity",
     "invoices", "not isinstance(value, int) or isinstance(value, bool) or not 0 < value", "not isinstance(value, int) or not 0 < value", ["the_same_key_with_other_lines_is_refused"], "a quantity is a real integer"),
    ("invoices: a paid invoice can be finalized",
     "invoices", '        if inv["status"] != "draft":\n            raise InvoiceStateError("invoice is %s, so it cannot be finalized"', '        if False:\n            raise InvoiceStateError("invoice is %s, so it cannot be finalized"',
     ["a_draft_and_an_open_invoice_can_be_voided"], "only a draft is finalized"),
    ("invoices: a paid invoice can be voided",
     "invoices", '        if inv["status"] not in ("draft", "open"):', "        if False:", ["a_draft_and_an_open_invoice_can_be_voided"], "a paid invoice is refunded, not voided"),
    ("invoices: an invoice with a payment in flight can be voided",
     "invoices", "        if await _live_payment(conn, invoice_id) is not None:\n            raise InvoicePaymentPending(\"a payment for this invoice is in flight\")",
     "        if False:\n            raise InvoicePaymentPending(\"a payment for this invoice is in flight\")", ["a_bank_debit_leaves_the_invoice_open_until_the_succeeded_event"], "an invoice with money in flight is not voided"),
    ("invoices: a wallet payment is allowed while a debit is in flight",
     "invoices", '        if live is not None and (method == "wallet" or live["idempotency_key"] != key):', "        if False:", ["a_bank_debit_leaves_the_invoice_open_until_the_succeeded_event"], "one payment in flight at a time"),
    ("invoices: an invoice paid by one method is paid again by another",
     "invoices", '            if inv["pay_method"] != method:\n                raise InvoiceStateError("invoice is already paid, by %s"', '            if False:\n                raise InvoiceStateError("invoice is already paid, by %s"',
     ["paying_from_the_wallet_debits_it_once"], "a paid invoice is paid once"),
    ("invoices: a draft can be paid",
     "invoices", '        if inv["status"] != "open":\n            raise InvoiceStateError("invoice is %s, so it cannot be paid"', '        if False:\n            raise InvoiceStateError("invoice is %s, so it cannot be paid"',
     ["a_draft_cannot_be_paid"], "only an open invoice is paid"),
    ("invoices: not enough credit is marked paid anyway",
     "invoices", "            if not outcome.ok:\n                return PayResult(\"insufficient_funds\"", "            if False:\n                return PayResult(\"insufficient_funds\"", ["not_enough_credit_writes_nothing"], "an unpaid invoice is not marked paid"),
    ("invoices: a replayed event is held instead of answered",
     "invoices", '        if inv["status"] == "paid" and inv["payment_id"] == payment["id"]:', "        if False:", ["paying_by_card_charges_once_pays_the_invoice"], "a repeat of the paying event writes nothing and is not held"),
    ("invoices: a refund past the total is tried",
     "invoices", "        if amount_minor > inv[\"total_minor\"] - asked:", "        if False:", ["a_wallet_paid_invoice_is_refunded_in_part"], "a refund never passes the total"),
    ("invoices: a reused refund key with another amount is answered",
     "invoices", '            if existing["request_hash"] != req:\n                raise IdempotencyConflict(key)\n            fresh', '            if False:\n                raise IdempotencyConflict(key)\n            fresh',
     ["a_wallet_paid_invoice_is_refunded_in_part"], "a reused key with other content is refused"),
    ("invoices: a repeated refund key is not recognised",
     "invoices", "        if existing is not None:\n            if existing", "        if False:\n            if existing", ["a_wallet_paid_invoice_is_refunded_in_part", "a_card_paid_invoice_is_refunded_through_the_provider"], "asking twice refunds once"),
    ("invoices: an unpaid invoice is refunded",
     "invoices", '        if inv["status"] != "paid":\n            raise InvoiceStateError("invoice is %s, so it cannot be refunded"', '        if False:\n            raise InvoiceStateError("invoice is %s, so it cannot be refunded"',
     ["only_a_paid_invoice_is_refunded"], "only a paid invoice is refunded"),
    ("invoices: a wallet refund does not go to the wallet",
     "invoices", "currency=inv[\"currency\"].strip(), to_wallet=True, ref=refund_id", "currency=inv[\"currency\"].strip(), to_wallet=False, ref=refund_id", ["a_wallet_paid_invoice_is_refunded_in_part"], "a wallet refund goes back to the wallet"),
    ("invoices: a refund event above the total is applied",
     "invoices", "        if refunded_total_minor > inv[\"total_minor\"]:", "        if False:", ["a_refund_event_above_the_invoice_total_is_held"], "a refund above the total is held"),
    ("invoices: a refund event that adds nothing is booked",
     "invoices", "        if delta <= 0:\n            return \"ignored\"", "        if delta < 0:\n            return \"ignored\"", ["a_refund_event_above_the_invoice_total_is_held"], "an event that adds nothing writes nothing"),
    ("commissions: the rate is not basis points",
     "commissions", "    return basis_minor * rate_bp // 10000", "    return round(basis_minor * rate_bp / 10000)", ["commission_for_is_integer_floor"], "a commission is an integer rounded down"),
    ("commissions: a reversal that changes nothing is written",
     "commissions", "        if delta >= 0:\n            return 0", "        if delta > 0:\n            return 0", ["a_refund_event_above_the_invoice_total_is_held"], "a reversal of nothing is not written"),
    ("commissions: a reversal uses a full rate",
     "commissions", 'commission_for(invoice["total_minor"] - refunded_total_minor, accrual["rate_bp"])', 'commission_for(invoice["total_minor"] - refunded_total_minor, 10000)',
     ["the_commission_is_the_rate_of_the_total_rounded_down", SEEDED_INV], "a reversal uses the rate the invoice was paid at"),
    ("commissions: no accrual",
     "commissions", "        if assignment is None:\n            return None", "        if True:\n            return None", ["the_commission_is_the_rate_of_the_total_rounded_down"], "a paid invoice earns its rep a commission"),
    # ---------------------------------------------------------------- the engine and the API
    ("engine: the acting database opens a transaction with no role",
     "engine", "        return self._db.session(tenant_id, self.role, self.user_id)", "        return self._db.tenant(tenant_id)",
     ["the_engine_acts_as_admin_inside_the_block", "an_order_is_made_for_the_users_own_clinic"], "every transaction of a portal user carries the user's role"),
    ("engine: the engine's block keeps its elevation",
     "engine", """        await conn.execute("select set_config('app.role', $1, true), set_config('app.user_id', $2, true)", before["role"] or "", before["uid"] or "")""", "        pass",
     ["the_engine_acts_as_admin_inside_the_block"], "the caller's own role comes back after the engine's step"),
    ("engine: the block does not act as admin",
     "engine", """await conn.execute("select set_config('app.role', 'admin', true), set_config('app.user_id', $1, true)", SYSTEM_USER_ID)""",
     """await conn.execute("select set_config('app.role', 'clinic', true), set_config('app.user_id', $1, true)", SYSTEM_USER_ID)""",
     ["the_engine_acts_as_admin_inside_the_block", "a_clinic_pays_its_own_invoice_from_the_wallet"], "the engine's step runs as the engine"),
    ("engine: an acting database needs no role",
     "engine", """        if not role or not user_id:
            raise ValueError("an acting database needs a role and a user")""", """        if False:
            raise ValueError("an acting database needs a role and a user")""",
     ["the_engine_acts_as_admin_inside_the_block"], "an acting database has a role and a user"),
    ("engine: the acting database is the plain database",
     "principal", "        return ActingDatabase(db, self.role, self.user_id)", "        return db", ["an_order_is_made_for_the_users_own_clinic", "an_invoice_is_made_and_finalized_by_an_admin"], "every route opens its transactions as the caller"),
    ("api: the guard lets every role through",
     "principal", "        if not principal.may(resource, action):", "        if False:", [GEN_API, "an_unknown_role_holds_nothing_in_the_guard"], "a role holding none is refused"),
    ("api: a role may do anything",
     "principal", "        return matrix.allows(self.role, resource, action)", "        return True", [GEN_API, "an_unknown_role_holds_nothing_in_the_guard"], "a role holds what the table says"),
    ("api: the key is looked up unhashed",
     "principal", 'conn.fetchrow("select * from portal_resolve_credential($1)", hash_key(raw_key))', 'conn.fetchrow("select * from portal_resolve_credential($1)", raw_key)', [GEN_API], "keys are stored and looked up hashed"),
    ("api: a missing credential is not refused",
     "principal", "    if creds is None or not creds.credentials:", "    if False:", ["no_key_an_unknown_key_a_revoked_key"], "no key is 401"),
    ("api: an unknown role is given a default",
     "principal", '        if row["role"] not in matrix.ROLES:', "        if False:", ["a_role_the_table_has_no_row_for_is_refused_at_authentication"], "an unknown role fails closed"),
    ("api: an order may be made for another clinic",
     "portal", "    if principal.clinic_id and body.clinic_id and body.clinic_id != principal.clinic_id:", "    if False:", ["an_order_is_made_for_the_users_own_clinic"], "a clinic user orders for its own clinic"),
    ("api: a not found is a 403",
     "portal", "        return HTTPException(status_code=404, detail=str(exc))", "        return HTTPException(status_code=403, detail=str(exc))", ["an_order_is_made_for_the_users_own_clinic", "a_clinic_pays_its_own_invoice_from_the_wallet"], "another clinic's row answers like a missing one"),
    ("api: a body may carry extra fields",
     "portal", 'model_config = ConfigDict(extra="forbid")', 'model_config = ConfigDict(extra="ignore")', ["an_order_takes_no_free_text"], "an order takes no free text"),
    ("api: the idempotency key is optional",
     "portal", "    if not idempotency_key or not 0 < len(idempotency_key) <= 255:", "    if False:", ["an_invoice_is_made_and_finalized_by_an_admin"], "a money call needs its key"),
    ("api: the webhook works with no router configured",
     "portal", '    needs(runtime.fulfillment_router, "the partner webhook")\n', "", ["a_provider_that_is_not_configured_is_a_503"], "an unconfigured webhook is a 503"),
    ("api: a card payment needs no provider",
     "portal", '    if body.method != "wallet":\n        needs(runtime.provider, "the payment provider")', "    if False:\n        needs(runtime.provider, \"the payment provider\")", ["a_provider_that_is_not_configured_is_a_503"], "no provider is a 503"),
    ("api: the finalize route asks for the read cell",
     "portal", '@router.post("/invoices/{invoice_id}/finalize")\nasync def finalize_invoice(invoice_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "update"))):',
     '@router.post("/invoices/{invoice_id}/finalize")\nasync def finalize_invoice(invoice_id: str, request: Request, principal: PortalPrincipal = Depends(requires_access("invoices", "read"))):',
     ["every_portal_route_asks_for_the_cell_of_what_it_does"], "each route asks for the cell of its action"),
    ("api: the refund route asks for the settle cell",
     "portal", 'principal: PortalPrincipal = Depends(requires_access("invoices", "refund"))):', 'principal: PortalPrincipal = Depends(requires_access("invoices", "settle"))):',
     ["every_portal_route_asks_for_the_cell_of_what_it_does"], "each route asks for the cell of its action"),
    ("api: the commission list asks for the orders cell",
     "portal", 'async def list_commissions(request: Request, principal: PortalPrincipal = Depends(requires_access("commissions", "read"))):',
     'async def list_commissions(request: Request, principal: PortalPrincipal = Depends(requires_access("orders", "read"))):', ["every_portal_route_asks_for_the_cell_of_what_it_does"], "each route asks for the cell of its action"),
    # ---------------------------------------------------------------- the state machine
    ("states: a repeat is applied", "states", '    if target == current:\n        return "ignore"', '    if target == current:\n        return "apply"', [DECIDE], "a repeat of the current status changes nothing"),
    ("states: a legal move is ignored", "states", '    if can_move(current, target):\n        return "apply"', '    if can_move(current, target):\n        return "ignore"', [DECIDE], "a legal move is applied"),
    ("states: a skip ahead is applied",
     "states", '    if target in _RANK and _RANK[target] < _RANK[current]:\n        return "ignore"\n    return "refuse"', '    if target in _RANK and _RANK[target] < _RANK[current]:\n        return "ignore"\n    return "apply"', [DECIDE], "an event not legal yet is refused"),
    ("states: an older status is refused instead of ignored",
     "states", '    if target in _RANK and _RANK[target] < _RANK[current]:', '    if target in _RANK and _RANK[target] > _RANK[current]:', [DECIDE], "an older status is ignored"),
    ("states: a ship of a cancelled order is ignored",
     "states", '        return "ignore" if target in ("submitted", "accepted") else "refuse"', '        return "ignore"', [DECIDE], "a ship of a cancelled order is refused, not silently dropped"),
    ("states: a shipped order can be cancelled",
     "states", '    "shipped": frozenset({"delivered"}),', '    "shipped": frozenset({"delivered", "cancelled"}),', ["the_code_table_and_the_migration_table_are_the_same_set_of_moves", DECIDE], "shipped goes to delivered only"),
    ("states: an unknown status is decided on",
     "states", '    if current not in STATES or target not in PARTNER_STATUSES:\n        return "refuse"', '    if False:\n        return "refuse"', ["a_status_the_partner_may_not_report_or_an_unknown_one_is_refused"], "an unknown status is refused"),
    # ---------------------------------------------------------------- audit
    ("audit: a row's hash leaves out its detail",
     "audit", '"actor": actor, "detail": detail, "prev_hash": prev_hash}', '"actor": actor, "prev_hash": prev_hash}', ["an_edited_detail_is_named_by_verify"], "an edited detail changes the hash"),
    ("audit: an edited row is not detected", "audit", "        if again != r.entry_hash:", "        if False:", ["an_edited_deleted_or_reordered_audit_row", "an_edited_detail_is_named_by_verify"], "an edited audit row shows"),
    ("audit: a broken link is not detected", "audit", "        if r.prev_hash != prev:", "        if False:", ["an_edited_deleted_or_reordered_audit_row"], "a deleted row shows"),
    ("audit: a gap is not detected", "audit", "        if r.seq > expected:", "        if False:", ["an_edited_deleted_or_reordered_audit_row"], "a deleted row is named"),
    ("audit: a key outside the whitelist is kept",
     "audit", "    for key, value in (detail or {}).items():\n        if key in DETAIL_TEXT and", "    for key, value in (detail or {}).items():\n        if True or key in DETAIL_TEXT and", ["a_detail_with_a_name_in_it_is_stripped"], "a stray key never reaches the log"),
    ("audit: a text value is kept whatever its shape",
     "audit", "(value is None or (isinstance(value, str) and DETAIL_TEXT[key].match(value)))", "(value is None or isinstance(value, str))", ["a_name_under_a_whitelisted_key_is_dropped_too"], "a name under an allowed key is dropped"),
    # ---------------------------------------------------------------- the client
    ("client: a permanent refusal is retried", "client", "                if resp.status_code not in RETRYABLE:", "                if False:", ["a_permanent_4xx_is_never_retried"], "a 4xx is never retried"),
    ("client: the wait is not capped", "client", "        return min(wait, self._max_delay)", "        return wait", ["the_wait_is_exponential_capped"], "the wait has a ceiling"),
    ("client: the partner's Retry-After is ignored",
     "client", "        wait = hinted if hinted is not None else self._base_delay", "        wait = self._base_delay", ["a_transient_failure_is_retried_with_the_same_key", "the_wait_is_exponential_capped"], "the partner's own wait is honoured"),
    ("client: each attempt makes a new key",
     "client", 'resp = await self._http.post(self._base + "/v1/orders", json=body, headers=headers)', 'resp = await self._http.post(self._base + "/v1/orders", json=body, headers={**headers, "Idempotency-Key": headers["Idempotency-Key"] + str(attempt)})',
     ["a_transient_failure_is_retried_with_the_same_key", "an_answer_lost_after_the_partner_made_the_order"], "every attempt carries the same key"),
    ("client: there is no wait between attempts", "client", "            if attempt + 1 < self._max_attempts:\n                await self._sleep(", "            if False:\n                await self._sleep(", ["a_transient_failure_is_retried_with_the_same_key"], "retries back off"),
    ("client: a dropped connection is not retried", "client", "            except httpx.TransportError:", "            except httpx.ConnectError:", ["nothing_answering_at_all_is_unavailable", "a_transient_failure_is_retried_with_the_same_key"], "a dropped connection is retried"),
    ("client: a yes with no partner id is accepted",
     "client", "                    if not isinstance(partner_id, str) or not _PARTNER_ID.match(partner_id):", "                    if False:", ["a_yes_without_a_usable_partner_order_id_is_permanent"], "a yes must name the order"),
    ("client: the partner's error text is kept", "client", "    return code if isinstance(code, str) and _CODE.match(code) else \"error\"", "    return code if isinstance(code, str) else \"error\"", ["no_body_text_reaches_an_exception_message"], "no body text reaches an exception"),
    ("client: a short key is sent", "client", "        if not 8 <= len(idempotency_key) <= 120:", "        if False:", ["a_yes_without_a_usable_partner_order_id_is_permanent"], "a key has a length"),
    # ---------------------------------------------------------------- the service and the receiver
    ("service: a dead lettered order is sent without a requeue",
     "service", '        if sub["state"] == "dead":', "        if False:", ["a_permanent_refusal_is_a_dead_letter"], "a dead letter waits for a person"),
    ("service: every dead letter is retries exhausted",
     "service", '        reason = "permanent_rejection" if isinstance(exc, PartnerRejected) else "retries_exhausted"', '        reason = "retries_exhausted"', ["a_permanent_refusal_is_a_dead_letter"], "the reason says why"),
    ("service: a partner that never answers is retries exhausted",
     "service", '            reason = "unreachable"', '            reason = "retries_exhausted"', ["retries_that_run_out_and_a_partner_that_never_answers"], "no answer is its own reason"),
    ("service: a second sender moves the order again",
     "service", '        if order["status"] != "created":                       # a second sender, or the partner\'s own event, got here first', "        if False:", ["two_people_sending_at_once"], "two senders make one order"),
    ("service: any order can be requeued", "service", '        if order["status"] != "created" or sub["state"] != "dead":', "        if False:", ["a_permanent_refusal_is_a_dead_letter"], "only a dead letter is requeued"),
    ("service: a sent order can be cancelled here", "service", '        if order["status"] != "created":\n            raise OrderStateError("order is %s, so only the partner can cancel it"', '        if False:\n            raise OrderStateError("order is %s, so only the partner can cancel it"',
     ["only_an_order_that_is_created_can_be_cancelled_here"], "only the partner cancels a sent order"),
    ("service: an event id seen before is applied again", "service", "        if seen is not None:\n            return EventResult(\"duplicate\"", "        if False:\n            return EventResult(\"duplicate\"", ["a_repeat_of_an_event_changes_nothing"], "a repeat writes nothing"),
    ("service: a refused event is applied", "service", '        if verdict == "refuse":', "        if False:", ["a_move_that_is_not_legal_yet_is_refused"], "an event not legal now is refused"),
    ("service: an ignored event is applied", "service", '        if verdict == "ignore":', "        if False:", ["an_older_or_repeated_status_never_moves_an_order_backwards"], "an older event moves nothing"),
    ("service: carrier and tracking are wiped by a later event",
     "service", "carrier = coalesce($3, carrier), tracking_number = coalesce($4, tracking_number)", "carrier = $3, tracking_number = $4", ["the_whole_road_to_delivery"], "delivered keeps the tracking number"),
    ("service: a shipped event needs no tracking", "service", '    if event.status == "shipped" and not (', "    if False and not (", ["shipped_needs_a_carrier_and_a_tracking_number"], "shipped needs both"),
    ("service: a stray carrier is accepted", "service", '    if event.status != "shipped" and (event.carrier or event.tracking_number):', "    if False:", ["shipped_needs_a_carrier_and_a_tracking_number"], "a carrier belongs to shipped only"),
    ("service: verify does not compare the status with the log", "service", '        if last != order["status"]:', "        if False:", ["a_status_the_log_does_not_explain_is_reported"], "the status is explained by the log"),
    ("webhook: the signature is not checked",
     "webhook", "    verify(payload, header, secrets, now=time.time() if now is None else now, tolerance=tolerance)\n", "    pass\n", ["a_forged_tampered_stale_or_unsigned_event"], "an event is authenticated by its signature"),
    ("webhook: an order reference of any shape is accepted",
     "webhook", "    if not isinstance(ref, str) or not _ORDER_REF.match(ref):", "    if not isinstance(ref, str):", ["an_event_for_an_order_that_is_not_ours_is_a_404_and_a_body_that_is_not_an_event"], "an order reference has its shape"),
    ("webhook: an event id of any shape is accepted",
     "webhook", "    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):", "    if False:", ["an_event_without_a_usable_id_or_with_a_bad_one"], "an event id has its shape"),
    ("webhook: a refused event is answered 200", "webhook", '    if got.status == "refused":', "    if False:", ["a_move_that_is_not_legal_yet_is_refused", "a_cancel_after_delivery"], "an event not legal now is a 409"),
    ("webhook: an unknown order is not refused", "webhook", "    if found is None:\n        raise EventUnroutable", "    if False:\n        raise EventUnroutable", ["an_event_for_an_order_that_is_not_ours_is_a_404"], "an unknown order is a 404"),
]


def run_suite(select: str | None = None, timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung. `select` is a -k expression. A run that skipped tests is a failure to run, never a result."""
    for cache in CACHES:
        for stale in cache.glob("*.pyc"):
            stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [str(PY), "-B", "-m", "pytest", *SUITES, "-q", "--tb=no", "-rfs", "-p", "no:cacheprovider"]
    if select:
        cmd += ["-k", select]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    skipped = [line for line in r.stdout.splitlines() if line.startswith("SKIPPED")]
    if skipped:
        print("      %d test(s) were SKIPPED (is Postgres or stripe-mock down?): %s" % (len(skipped), skipped[0][:120]))
        raise SystemExit("a run with skipped tests proves nothing; start the services and run again")
    if select and r.returncode == 5:
        print("      no test matched %r" % select)
    return r.returncode == 0, failed


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def missing_targets() -> list[str]:
    """Guards whose old text is gone from their file or appears more than once, or that change nothing. Such a guard proves nothing."""
    texts = {key: read_text(path) for key, path in TARGETS.items()}
    problems = []
    for name, key, old, new, *_ in MUTATIONS:
        count = texts[key].count(old)
        if count != 1:
            problems.append(f"{name}: its target text appears {count} times in {TARGETS[key].name}, it must appear once")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    names = [m[0] for m in MUTATIONS]
    problems += [f"{n}: the name is used twice" for n in sorted({n for n in names if names.count(n) > 1})]
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

    def restore() -> None:
        for key, path in TARGETS.items():
            if path.read_bytes() != originals[key]:
                path.write_bytes(originals[key])

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        green, _ = run_suite(None, BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("baseline suite is RED or hung, fix that before mutating")
            return 1
        print("baseline: green")
        ran = 0
        for name, key, old, new, expected, why in MUTATIONS:
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
            select = " or ".join(expected)
            try:
                TARGETS[key].write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite(select)
            finally:
                TARGETS[key].write_bytes(original)
            if green is None:
                print(f"[ok]   {name}: suite HUNG, so the guard also stops an endless loop")
                killed += 1
            elif green:
                print(f"[FAIL] {name}: the named tests stayed GREEN, so nothing tests that {why}")
                survived += 1
            elif any(frag.lower() in test.lower() for test in failed for frag in expected):
                print(f"[ok]   {name}: failed {len(failed)} test(s), including the expected one")
                killed += 1
            else:
                print(f"[FAIL] {name}: the suite went red but not in {expected}; failed: {failed[:4]}")
                wrong_test += 1

        print()
        green, _ = run_suite(None, BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("suite is RED or hung after restoring, a mutation leaked")
            return 1
        print("source restored, suite green again")
        print(f"mutants run {ran}: killed {killed}, survived {survived}, killed by the wrong test {wrong_test}")
        bad = survived + wrong_test
        print(f"mutation control: {'PASS' if bad == 0 else str(bad) + ' UNTESTED GUARD(S)'}")
        return 1 if bad else 0
    finally:
        restore()


if __name__ == "__main__":
    sys.exit(main())
