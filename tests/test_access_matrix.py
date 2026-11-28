"""The access matrix as a table, with no database: its shape, the migration's copy of it, and a handful of statements of intent that hold whatever a cell says.

The per cell tests are generated from the table in tests/test_access_api_db.py (the API layer) and tests/test_access_matrix_db.py (row level security); this file checks the table those read.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.access import matrix  # noqa: E402


def migration_rows() -> dict[tuple[str, str, str], str]:
    sql = (ROOT / "migrations" / "022_access_matrix.sql").read_text(encoding="utf-8")
    block = sql[sql.index("-- BEGIN ACCESS MATRIX SEED"):sql.index("-- END ACCESS MATRIX SEED")]
    return {(r, res, a): s for r, res, a, s in re.findall(r"\('(\w+)', '(\w+)', '(\w+)', '(\w+)'\)", block)}


def test_the_table_is_closed_every_cell_has_a_known_scope_and_there_are_eighty_of_them():
    matrix.validate()
    cells = list(matrix.cells())
    assert len(cells) == len(matrix.ROLES) * len(matrix.RESOURCES) * len(matrix.ACTIONS) == 80 and len(set(cells)) == 80
    assert all(matrix.scope(*c) in matrix.SCOPES for c in cells)


def test_a_role_resource_or_action_the_table_does_not_know_holds_none_never_an_error_and_never_an_allow():
    assert matrix.scope("viewer", "orders", "read") == "none" and matrix.scope("admin", "secrets", "read") == "none" and matrix.scope("admin", "orders", "delete") == "none"
    assert not matrix.allows("", "", "") and not matrix.allows("ADMIN", "orders", "read")


def test_the_migration_holds_exactly_the_cells_of_the_table_and_the_seed_text_is_the_generated_text():
    rows = migration_rows()
    assert rows == {c: matrix.scope(*c) for c in matrix.cells()}
    sql = (ROOT / "migrations" / "022_access_matrix.sql").read_text(encoding="utf-8")
    assert matrix.seed_sql() in sql, "run app/access/matrix.py seed_sql() and put its text between the markers"


def test_a_cell_changed_in_code_without_the_migration_would_be_caught_by_the_comparison(monkeypatch):
    changed = dict(matrix._GRANTS["sales_rep"]["orders"])
    changed["create"] = "all"
    monkeypatch.setitem(matrix._GRANTS["sales_rep"], "orders", changed)
    assert {c: matrix.scope(*c) for c in matrix.cells()} != migration_rows()


def test_the_cells_no_operation_exists_for_are_none_for_every_role():
    for role in matrix.ROLES:
        for resource, action in matrix.NO_OPERATION:
            assert matrix.scope(role, resource, action) == "none", (role, resource, action)


# statements of intent: true of the table, whatever else is edited in it
def test_only_an_admin_creates_invoices_refunds_them_or_writes_commissions_and_only_an_admin_edits_a_payment():
    for role in matrix.ROLES:
        privileged = [matrix.allows(role, "invoices", "create"), matrix.allows(role, "invoices", "refund"), matrix.allows(role, "commissions", "create"),
                      matrix.allows(role, "commissions", "refund"), matrix.allows(role, "payments", "update"), matrix.allows(role, "invoices", "update")]
        assert all(privileged) == (role == "admin") and any(privileged) == (role == "admin"), role


def test_a_sales_rep_can_only_read_and_never_see_a_row_outside_its_clinics_or_its_own_commissions():
    for resource in matrix.RESOURCES:
        for action in matrix.ACTIONS:
            sc = matrix.scope("sales_rep", resource, action)
            if action != "read":
                assert sc == "none", (resource, action)
            elif resource == "commissions":
                assert sc == "self"
            else:
                assert sc == "assigned"


def test_a_clinic_and_a_prescriber_are_confined_to_their_own_clinic_and_never_see_commissions_and_a_prescriber_never_sees_money():
    for role in ("clinic", "prescriber"):
        assert all(matrix.scope(role, r, a) in ("none", "clinic") for r in matrix.RESOURCES for a in matrix.ACTIONS)
        assert not matrix.allows(role, "commissions", "read")
    assert not any(matrix.allows("prescriber", r, a) for r in ("invoices", "payments", "commissions") for a in matrix.ACTIONS)
    assert matrix.allows("prescriber", "orders", "create") and matrix.allows("clinic", "invoices", "settle") and not matrix.allows("clinic", "invoices", "create")


@pytest.mark.parametrize("resource,action", [(r, a) for r in matrix.RESOURCES for a in matrix.ACTIONS if (r, a) not in matrix.NO_OPERATION])
def test_the_admin_holds_all_rows_for_every_action_that_has_an_operation(resource, action):
    assert matrix.scope("admin", resource, action) == "all"
