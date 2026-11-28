"""Row level security against the access matrix, GENERATED from the table (app/access/matrix.py), over real Postgres and as the application role.

For every user of the portal (a clinic user, a second clinic user, a prescriber, two sales reps, an admin), every resource and every action (6 x 4 x 5 = 120 cells) there is one test. It
makes a row of the resource for EACH of the three clinics as the schema owner, then performs the operation that action stands for, as that user and directly in SQL (no API in the way),
against each of the three rows. What is allowed is not retyped here: it is worked out from the cell's scope and the user's own clinic or assignments:

    none      refused for every row            all       allowed for every row
    clinic    allowed only for the user's clinic     assigned  allowed only for the clinics assigned to the user
    self      allowed only for commissions that name the user as their rep

An operation is "allowed" when it takes effect and "refused" when the database raises (a row level security check, a trigger asking the matrix) or the row is simply not there for that user
(an update or a read that matches no visible row). A refusal is a DENY AT THE SQL LEVEL: the test never goes through the API. The cells no operation exists for are checked to be `none` in the
database's own copy of the table. Then the policies that are not per cell: no role at all sees nothing, another tenant's user sees nothing, the request role cannot change the matrix.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from portal_fixtures import needs_db, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN  # noqa: E402

pytestmark = needs_db

from app.access import matrix  # noqa: E402

LABELS = ("clinic1", "clinic2", "prescriber1", "rep1", "rep2", "admin")
CLINICS = ("c1", "c2", "c3")
OPERATIONS = [(r, a) for r in matrix.RESOURCES for a in matrix.ACTIONS if (r, a) not in matrix.NO_OPERATION]


class Facts:
    """What the owner knows about the seeded portal: each user's role, clinic and assigned clinics, and the rep of each clinic."""


@pytest.fixture(scope="module")
def world():
    a, b = reset_world("tenant-a", "tenant-b")
    f = Facts()
    f.a, f.b = a, b
    f.users = {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for label, uid in a.users.items():
            cur.execute("select role, clinic_id from portal_user where id = %s", (uid,))
            role, clinic = cur.fetchone()
            cur.execute("select clinic_id from sales_rep_assignment where rep_user_id = %s", (uid,))
            f.users[label] = {"id": uid, "role": role, "clinic": str(clinic) if clinic else None, "assigned": {str(r[0]) for r in cur.fetchall()}}
        cur.execute("select clinic_id, rep_user_id from sales_rep_assignment where tenant_id = %s", (a.tenant,))
        f.rep_of = {str(c): str(r) for c, r in cur.fetchall()}
    return f


def owner(sql: str, *args):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0] if cur.description else None


def allowed_by_the_table(scope: str, user: dict, clinic: str, rep: str | None) -> bool:
    """The rule each scope stands for, written from the module doc and from the user's own facts."""
    if scope == "all":
        return True
    if scope == "clinic":
        return user["clinic"] == clinic
    if scope == "assigned":
        return clinic in user["assigned"]
    if scope == "self":
        return rep == user["id"]
    return False


# one operation for each (resource, action): prepare rows as the owner, return what to run as the user. Each `run` returns True when the operation took effect.
def make_row(table_sql: str, *args) -> str:
    return str(owner(table_sql, *args))


def prepare(world, resource: str, action: str, clinic_label: str):
    t, c = world.a.tenant, world.a.clinics[clinic_label]
    rep = world.rep_of[c]
    tag = uuid.uuid4().hex

    def order_row():
        return make_row("insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by) values (%s, %s, %s, 'owner') returning id", t, c, "ord_" + uuid.uuid4().hex[:24])

    def invoice_row(status="draft", total=0, method=None):
        paid_at = "now()" if status == "paid" else "null"
        return make_row("insert into billing_invoice (tenant_id, clinic_id, currency, status, total_minor, pay_method, paid_at, idempotency_key, request_hash, created_by) "
                        "values (%%s, %%s, 'USD', %s, %%s, %%s, %s, %%s, 'h', 'owner') returning id" % ("'%s'" % status, paid_at), t, c, total, method, tag)

    def payment_row():
        return make_row("insert into billing_payment (tenant_id, clinic_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency) "
                        "values (%s, %s, %s, 'h', 'p', 100, 'USD') returning id", t, c, tag)

    def accrual(inv):
        owner("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) values (%s, %s, %s, %s, 'accrual', 50, 1000, 500, 'USD', 'accrual') returning id",
              t, inv, c, rep)

    if resource == "orders":
        if action == "read":
            row = order_row()
            return lambda conn: conn.fetchval("select count(*) from fulfillment_order where id = $1::uuid", row)
        if action == "create":
            return lambda conn: conn.fetchval("insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by) values ($1::uuid, $2::uuid, $3, 'probe') returning 1",
                                              t, c, "ord_" + uuid.uuid4().hex[:24])
        row = order_row()
        return lambda conn: conn.fetchval("update fulfillment_order set status = 'cancelled' where id = $1::uuid returning 1", row)
    if resource == "invoices":
        if action == "read":
            row = invoice_row()
            return lambda conn: conn.fetchval("select count(*) from billing_invoice where id = $1::uuid", row)
        if action == "create":
            return lambda conn: conn.fetchval("insert into billing_invoice (tenant_id, clinic_id, currency, idempotency_key, request_hash, created_by) "
                                              "values ($1::uuid, $2::uuid, 'USD', $3, 'h', 'probe') returning 1", t, c, tag)
        if action == "update":
            row = invoice_row()
            return lambda conn: conn.fetchval("update billing_invoice set status = 'void', voided_at = now() where id = $1::uuid returning 1", row)
        if action == "settle":
            row = invoice_row("open", 1000)
            return lambda conn: conn.fetchval("update billing_invoice set status = 'paid', pay_method = 'wallet', paid_at = now() where id = $1::uuid returning 1", row)
        row = invoice_row("paid", 1000, "wallet")
        return lambda conn: conn.fetchval("update billing_invoice set refunded_minor = 100 where id = $1::uuid returning 1", row)
    if resource == "payments":
        if action == "read":
            row = payment_row()
            return lambda conn: conn.fetchval("select count(*) from billing_payment where id = $1::uuid", row)
        if action == "create":
            return lambda conn: conn.fetchval("insert into billing_payment (tenant_id, clinic_id, idempotency_key, request_hash, provider_idempotency_key, amount_minor, currency) "
                                              "values ($1::uuid, $2::uuid, $3, 'h', 'p', 100, 'USD') returning 1", t, c, tag)
        row = payment_row()
        return lambda conn: conn.fetchval("update billing_payment set status = 'processing' where id = $1::uuid returning 1", row)
    inv = invoice_row("paid", 1000, "wallet")
    if action == "read":
        accrual(inv)
        return lambda conn: conn.fetchval("select count(*) from commission where invoice_id = $1::uuid", inv)
    if action == "create":
        return lambda conn: conn.fetchval("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) "
                                          "values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'accrual', 50, 1000, 500, 'USD', 'accrual') returning 1", t, inv, c, rep)
    accrual(inv)
    return lambda conn: conn.fetchval("insert into commission (tenant_id, invoice_id, clinic_id, rep_user_id, kind, amount_minor, basis_minor, rate_bp, currency, ref) "
                                      "values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'reversal', -1, 1, 500, 'USD', 'refund:1') returning 1", t, inv, c, rep)


async def attempt(rig, world, label: str, run) -> bool:
    """Run one operation as one portal user on its own transaction; True when it took effect (a read counts a visible row)."""
    user = world.users[label]
    try:
        async with rig.db.session(world.a.tenant, user["role"], user["id"]) as conn:
            got = await run(conn)
    except (asyncpg.InsufficientPrivilegeError, asyncpg.RestrictViolationError, asyncpg.CheckViolationError, asyncpg.exceptions.RaiseError):
        return False
    return bool(got)


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("resource,action", OPERATIONS)
def test_every_user_resource_and_action_is_allowed_or_denied_by_the_database_as_the_table_says(world, label, resource, action):
    user = world.users[label]
    scope = matrix.scope(user["role"], resource, action)
    runs = {clinic: prepare(world, resource, action, clinic) for clinic in CLINICS}

    async def go(rig):
        return {clinic: await attempt(rig, world, label, run) for clinic, run in runs.items()}
    got = with_portal(go, size=2)
    for clinic, took_effect in got.items():
        c = world.a.clinics[clinic]
        expected = allowed_by_the_table(scope, user, c, world.rep_of[c])
        assert took_effect == expected, "%s (%s) %s %s on %s: scope %s, the database said %s" % (label, user["role"], action, resource, clinic, scope, took_effect)
    # a control inside the same test: a cell that grants something must have been allowed for at least one clinic, so a build that refused everything cannot pass
    assert any(got.values()) == (scope != "none")


def test_the_cells_with_no_operation_are_none_for_every_role_in_the_databases_own_copy_of_the_table(world):
    rows = owner("select count(*) from access_matrix where scope <> 'none' and (resource, action) in (%s)" % ", ".join("('%s', '%s')" % c for c in sorted(matrix.NO_OPERATION)))
    assert rows == 0


def test_the_databases_copy_of_the_matrix_is_the_codes_row_for_row(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select role, resource, action, scope from access_matrix")
        rows = {(r, res, a): s for r, res, a, s in cur.fetchall()}
    assert rows == {c: matrix.scope(*c) for c in matrix.cells()}


# ---- the policies that are not one per cell --------------------------------------------------------------------------------------------------

def test_with_no_role_set_every_portal_table_answers_nothing_and_accepts_nothing_while_payments_keep_the_older_tenant_wide_behaviour(world):
    world_rows = {}
    for table, make in (("fulfillment_order", lambda: prepare(world, "orders", "read", "c1")), ("billing_invoice", lambda: prepare(world, "invoices", "read", "c1")),
                        ("commission", lambda: prepare(world, "commissions", "read", "c1")), ("billing_payment", lambda: prepare(world, "payments", "read", "c1"))):
        make()
        world_rows[table] = owner("select count(*) from %s" % table)

    async def go(rig):
        seen = {}
        for table in world_rows:
            async with rig.db.tenant(world.a.tenant) as conn:                                # the tenant only, as every older module opens it
                seen[table] = await conn.fetchval("select count(*) from %s" % table)
        refused = []
        async with rig.db.tenant(world.a.tenant) as conn:
            try:
                await conn.execute("insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by) values ($1::uuid, $2::uuid, $3, 'x')", world.a.tenant,
                                   world.a.clinics["c1"], "ord_" + uuid.uuid4().hex[:24])
                refused.append(False)
            except asyncpg.InsufficientPrivilegeError:
                refused.append(True)
        return seen, refused
    seen, refused = with_portal(go, size=2)
    assert seen["fulfillment_order"] == seen["billing_invoice"] == seen["commission"] == 0 and refused == [True]
    assert seen["billing_payment"] == world_rows["billing_payment"] > 0            # the one exception, and why: the wallet engine uses billing_payment without a portal user


def test_a_user_of_another_tenant_sees_and_changes_nothing_of_this_tenants_rows_and_a_tenant_id_mismatch_sees_nothing(world):
    prepare(world, "invoices", "read", "c1")
    other_admin = world.b.users["admin"]

    async def go(rig):
        counts = {}
        async with rig.db.session(world.b.tenant, "admin", other_admin) as conn:
            for table in ("billing_invoice", "fulfillment_order", "billing_payment", "commission", "clinic", "portal_user", "sales_rep_assignment"):
                counts[table] = await conn.fetchval("select count(*) from %s" % table)
        async with rig.db.session(world.b.tenant, "admin", world.a.users["admin"]) as conn:        # an admin of tenant a, with tenant b in the session: no portal_user row is visible
            counts["mismatch_users"] = await conn.fetchval("select count(*) from portal_user where id = $1::uuid", world.a.users["admin"])
            counts["mismatch_invoices"] = await conn.fetchval("select count(*) from billing_invoice")
        return counts
    counts = with_portal(go, size=2)
    assert counts["billing_invoice"] == 0 and counts["fulfillment_order"] == 0 and counts["commission"] == 0 and counts["clinic"] == 3 and counts["portal_user"] == 6
    assert counts["mismatch_users"] == 0 and counts["mismatch_invoices"] == 0                    # tenant b's own admin sees its own 3 clinics and 6 users: the control


def test_a_user_who_is_not_in_the_table_or_has_a_role_it_has_no_row_for_gets_none_everywhere(world):
    prepare(world, "invoices", "read", "c1")

    async def go(rig):
        seen = []
        for role, user in (("viewer", world.a.users["admin"]), ("", world.a.users["admin"]), ("ADMIN", world.a.users["admin"]), ("admin", "00000000-0000-0000-0000-00000000dead")):
            try:
                async with rig.db.session(world.a.tenant, role or "x", user) as conn:
                    await conn.execute("select set_config('app.role', $1, true)", role)
                    seen.append(await conn.fetchval("select count(*) from billing_invoice"))
            except asyncpg.PostgresError:
                seen.append("error")
        async with rig.db.session(world.a.tenant, "admin", world.a.users["admin"]) as conn:            # the control: the real admin sees the rows
            seen.append(await conn.fetchval("select count(*) from billing_invoice"))
        return seen
    seen = with_portal(go, size=2)
    assert seen[:3] == [0, 0, 0] and seen[3] > 0 and seen[4] > 0           # an admin role with an unknown user id is still an admin by the settings: the settings are the boundary


def test_a_rep_cannot_assign_itself_a_clinic_a_clinic_user_cannot_read_other_users_and_nobody_but_an_admin_writes_clinics_or_users(world):
    async def go(rig):
        out = {}
        rep, clinic1 = world.users["rep2"], world.users["clinic1"]
        for name, (user, sql, args) in {
            "rep assigns itself c1": (rep, "insert into sales_rep_assignment (tenant_id, rep_user_id, clinic_id, commission_bp) values ($1::uuid, $2::uuid, $3::uuid, 10000)", (world.a.tenant, rep["id"], world.a.clinics["c1"])),
            "rep raises its rate": (rep, "update sales_rep_assignment set commission_bp = 10000 where rep_user_id = $1::uuid", (rep["id"],)),
            "clinic makes a clinic": (clinic1, "insert into clinic (tenant_id, code, name) values ($1::uuid, 'rogue-clinic', 'Rogue')", (world.a.tenant,)),
            "clinic promotes itself": (clinic1, "update portal_user set role = 'admin' where id = $1::uuid", (clinic1["id"],)),
        }.items():
            try:
                async with rig.db.session(world.a.tenant, user["role"], user["id"]) as conn:
                    status = await conn.execute(sql, *args)
                out[name] = status
            except asyncpg.PostgresError as exc:
                out[name] = type(exc).__name__
        async with rig.db.session(world.a.tenant, clinic1["role"], clinic1["id"]) as conn:
            out["clinic sees users"] = await conn.fetchval("select count(*) from portal_user")
            out["clinic sees clinics"] = await conn.fetchval("select count(*) from clinic")
        async with rig.db.session(world.a.tenant, rep["role"], rep["id"]) as conn:
            out["rep sees assignments"] = await conn.fetchval("select count(*) from sales_rep_assignment")
            out["rep sees clinics"] = await conn.fetchval("select count(*) from clinic")
        return out
    out = with_portal(go, size=2)
    assert out["rep assigns itself c1"] == "InsufficientPrivilegeError" and out["clinic makes a clinic"] == "InsufficientPrivilegeError"
    assert out["rep raises its rate"] == "UPDATE 0" and out["clinic promotes itself"] == "UPDATE 0"
    assert (out["clinic sees users"], out["clinic sees clinics"], out["rep sees assignments"], out["rep sees clinics"]) == (1, 1, 1, 1)
    assert owner("select commission_bp from sales_rep_assignment where rep_user_id = %s", world.users["rep2"]["id"]) == 250


def test_an_assignment_to_a_user_who_is_not_a_rep_is_refused_and_so_is_a_second_rep_for_one_clinic(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("insert into sales_rep_assignment (tenant_id, rep_user_id, clinic_id, commission_bp) values (%s, %s, %s, 100)",
                        (world.a.tenant, world.users["clinic1"]["id"], world.a.clinics["c3"]))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("insert into sales_rep_assignment (tenant_id, rep_user_id, clinic_id, commission_bp) values (%s, %s, %s, 100)",
                        (world.a.tenant, world.users["rep1"]["id"], world.a.clinics["c3"]))
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("insert into sales_rep_assignment (tenant_id, rep_user_id, clinic_id, commission_bp) values (%s, %s, %s, 10001)",
                        (world.a.tenant, world.users["rep2"]["id"], world.a.clinics["c1"]))


def test_the_request_role_cannot_change_the_matrix_or_the_credentials_and_the_helpers_are_not_a_way_around_it(world):
    async def go(rig):
        out = []
        admin = world.users["admin"]
        for sql in ("update access_matrix set scope = 'all' where role = 'clinic'", "insert into access_matrix (role, resource, action, scope) values ('clinic', 'orders', 'refund', 'all') on conflict do nothing",
                    "delete from access_matrix", "insert into portal_credential (tenant_id, user_id, key_hash) values ('%s', '%s', '%s')" % (world.a.tenant, admin["id"], "f" * 64),
                    "update portal_credential set revoked_at = null"):
            try:
                async with rig.db.session(world.a.tenant, admin["role"], admin["id"]) as conn:       # even an admin session: the grants are not the matrix's
                    await conn.execute(sql)
                out.append("allowed")
            except asyncpg.InsufficientPrivilegeError:
                out.append("denied")
        return out
    assert with_portal(go, size=2) == ["denied"] * 5
    assert owner("select scope from access_matrix where role = 'clinic' and resource = 'orders' and action = 'refund'") == "none"


def test_a_credential_resolves_to_its_user_and_a_revoked_or_unknown_one_to_nothing(world):
    async def go(rig):
        import hashlib
        found = []
        for raw in (world.a.keys["clinic1"], "pk_test_not_a_key", world.a.keys["rep2"]):
            async with rig.db.unscoped() as conn:
                row = await conn.fetchrow("select * from portal_resolve_credential($1)", hashlib.sha256(raw.encode()).hexdigest())
            found.append((row["role"], str(row["clinic_id"]) if row["clinic_id"] else None) if row else None)
        owner("update portal_credential set revoked_at = now() where user_id = %s", world.users["rep2"]["id"])
        async with rig.db.unscoped() as conn:
            revoked = await conn.fetchrow("select * from portal_resolve_credential($1)", hashlib.sha256(world.a.keys["rep2"].encode()).hexdigest())
        owner("update portal_credential set revoked_at = null where user_id = %s", world.users["rep2"]["id"])
        return found, revoked
    found, revoked = with_portal(go, size=2)
    assert found == [("clinic", world.a.clinics["c1"]), None, ("sales_rep", None)] and revoked is None


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("resource,action", sorted(matrix.NO_OPERATION))
def test_a_cell_no_operation_exists_for_is_none_for_this_user_in_the_database_and_in_the_code(world, label, resource, action):
    role = world.users[label]["role"]
    assert owner("select scope from access_matrix where role = %s and resource = %s and action = %s", role, resource, action) == "none"
    assert matrix.scope(role, resource, action) == "none"
