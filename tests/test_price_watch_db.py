"""Price watch against real Postgres, as the REQUEST role: tenant isolation, the review queue and its audit trail, the stored daily report.

The isolation tests connect through `Database`, which drops into app_request and refuses to start as a superuser. That matters: the first isolation run
of this project had tenant A reading tenant B's rows with every policy correct, because the test connected as the bootstrap superuser (see db.py).
Skips cleanly when Postgres is not running; like the other database suites it drops and recreates the `public` schema it points at.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price_watch_fixtures import DAY, DSN, add_tenant, needs_db, psycopg, reset_schema, run, seed_catalogue, seed_listing_with_scan  # noqa: E402

pytestmark = needs_db

from app.db import Database  # noqa: E402
from app.price_watch import proposals, store  # noqa: E402
from app.price_watch.scraper import ScrapedProduct  # noqa: E402

TABLES = ("catalogue_product", "competitor_listing", "competitor_price_observation", "shipping_rate", "price_watch_report", "price_change_proposal", "price_change_audit")


class World:
    pass


@pytest.fixture()
def world():
    """Two tenants with the same catalogue, one matched listing and a report each; tenant A also has a euro listing and one that needs review."""
    reset_schema()
    w = World()
    w.a, w.b = add_tenant("tenant-a"), add_tenant("tenant-b")

    async def build():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            w.ids = {}
            for t in (w.a, w.b):
                w.ids[t] = await seed_catalogue(db, t)
                await seed_listing_with_scan(db, t, w.ids[t]["KTB-750"], competitor="Depot", url="http://depot.invalid/bottle-%s" % t, price_minor=4100, shipping_minor=695)
            await seed_listing_with_scan(db, w.a, w.ids[w.a]["LMP-210"], competitor="Depot", url="http://depot.invalid/lamp", price_minor=2490, shipping_minor=None)
            await seed_listing_with_scan(db, w.a, w.ids[w.a]["HMJ-NVY-M"], competitor="Gearhouse", url="http://gear.invalid/jumper", price_minor=4950, shipping_minor=0)
            await seed_listing_with_scan(db, w.a, w.ids[w.a]["KTB-750"], competitor="Alpen", url="http://alpen.invalid/bottle", price_minor=3990, shipping_minor=690, currency="EUR")
            async with db.tenant(str(w.a)) as conn:
                lid = await store.add_listing(conn, str(w.a), competitor="Alpen", url="http://alpen.invalid/unsure")
                await conn.execute("update competitor_listing set match_status = 'needs_review', match_reason = 'title score 0.72' where id = $1::uuid", lid)
                for t in (w.a,):
                    await store.make_report(conn, str(t), DAY)
            async with db.tenant(str(w.b)) as conn:
                await store.make_report(conn, str(w.b), DAY)
        finally:
            await db.close()

    run(build())
    return w


def with_db(fn):
    """Run `fn(db)` with a connected request-role Database, closing it after."""
    async def go():
        db = Database(DSN, max_size=4)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()
    return run(go())


# ---- the migration -------------------------------------------------------------------------------------------------------

def test_the_migration_is_safe_to_run_twice(world):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute((ROOT / "migrations" / "011_price_watch.sql").read_text(encoding="utf-8"))


def test_every_price_watch_table_has_row_level_security_enabled_forced_and_a_policy(world):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(%s)", (list(TABLES),))
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute("select tablename, count(*) from pg_policies where schemaname = 'public' and tablename = any(%s) group by tablename", (list(TABLES),))
        policies = dict(cur.fetchall())
    assert set(rows) == set(TABLES)
    assert all(v == (True, True) for v in rows.values()), rows
    assert all(policies.get(t, 0) >= 1 for t in TABLES), policies


# ---- tenant isolation, negative, as the request role -------------------------------------------------------------------

def test_the_connection_really_is_the_request_role_and_not_a_superuser(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            return await conn.fetchrow("select current_user as who, r.rolsuper, r.rolbypassrls from pg_roles r where r.rolname = current_user")
    row = with_db(go)
    assert (row["who"], row["rolsuper"], row["rolbypassrls"]) == ("app_request", False, False)


def test_tenant_a_sees_none_of_tenant_bs_rows_in_any_table_and_all_of_its_own(world):
    async def go(db):
        out = {}
        for t in TABLES:
            async with db.tenant(str(world.a)) as conn:
                out[t] = (await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, str(world.b)),
                          await conn.fetchval("select count(*) from %s" % t),
                          await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, str(world.a)))
        return out
    got = with_db(go)
    for t, (of_b, visible, of_a) in got.items():
        assert of_b == 0, t
        assert visible == of_a, "%s: sees rows that are not its own" % t
    for t in ("catalogue_product", "competitor_listing", "competitor_price_observation", "shipping_rate", "price_watch_report", "price_change_proposal", "price_change_audit"):
        assert got[t][2] > 0, "%s has no rows for A, so the check above proved nothing" % t


def test_with_no_tenant_set_nothing_is_visible(world):
    async def go(db):
        async with db.unscoped() as conn:
            return [await conn.fetchval("select count(*) from %s" % t) for t in TABLES]
    assert with_db(go) == [0] * len(TABLES)


def test_tenant_a_cannot_write_a_row_for_tenant_b(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            with pytest.raises(asyncpg.PostgresError):
                await store.add_product(conn, str(world.b), sku="SNEAK", title="x", price_minor=100, currency="AUD", floor_minor=1)
        async with db.tenant(str(world.a)) as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("insert into price_change_audit (tenant_id, proposal_id, to_state, actor) values ($1::uuid, $2::uuid, 'approved', 'x')",
                                   str(world.b), str(uuid.uuid4()))
    with_db(go)


def test_tenant_a_cannot_change_or_move_tenant_bs_proposal(world):
    async def go(db):
        async with db.tenant(str(world.b)) as conn:
            pid = str((await proposals.list_proposals(conn, "proposed"))[0]["id"])
        async with db.tenant(str(world.a)) as conn:
            assert await conn.execute("update price_change_proposal set proposed_price_minor = 3800 where id = $1::uuid", pid) == "UPDATE 0"
            with pytest.raises(proposals.ProposalNotFound):
                await proposals.approve(conn, str(world.a), pid, "mallory")
            assert await proposals.audit_trail(conn, pid) == []
        async with db.tenant(str(world.b)) as conn:
            assert (await conn.fetchval("select state from price_change_proposal where id = $1::uuid", pid)) == "proposed"
    with_db(go)


def test_the_request_role_cannot_delete_anything_or_edit_the_audit_trail(world):
    async def go(db):
        for t in TABLES:
            async with db.tenant(str(world.a)) as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute("delete from %s" % t)
        async with db.tenant(str(world.a)) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("update price_change_audit set actor = 'someone else'")
    with_db(go)


# ---- constraints that stand behind the Python ----------------------------------------------------------------------------

def insert_proposal_sql(proposed, current=4995, floor=3800, state="proposed", by="null", shop="pending", zoho="pending"):
    return ("insert into price_change_proposal (tenant_id, catalogue_product_id, report_date, currency, current_price_minor, proposed_price_minor, floor_minor, competitor, "
            "competitor_landed_minor, shipping_source, idempotency_key, state, decided_by, shopify_status, zoho_status) "
            "values ($1::uuid, $2::uuid, current_date, 'AUD', %d, %d, %d, 'x', 1, 'stated', 'k-' || gen_random_uuid()::text, '%s', %s, '%s', '%s')"
            % (current, proposed, floor, state, by, shop, zoho))


def test_the_database_refuses_a_proposal_below_the_floor_above_the_price_or_applied_with_a_shop_owing(world):
    async def go(db):
        pid = world.ids[world.a]["HMJ-NVY-M"]       # no open proposal yet on this one
        async with db.tenant(str(world.a)) as conn:
            for sql in (insert_proposal_sql(3799),                                              # below the floor
                        insert_proposal_sql(4995),                                              # not a reduction
                        insert_proposal_sql(4000, state="approved"),                            # decided without an actor
                        insert_proposal_sql(4000, state="applied", by="'x'", shop="applied", zoho="failed")):   # applied with a shop owing
                with pytest.raises(asyncpg.CheckViolationError):
                    async with conn.transaction():          # a savepoint, so one refusal does not abort the rest
                        await conn.execute(sql, str(world.a), pid)
    with_db(go)


def test_only_one_open_proposal_per_product(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            line = (await store.make_report(conn, str(world.a), DAY))[0].best_by_product()[world.ids[world.a]["KTB-750"]]
            return await proposals.create_proposal(conn, str(world.a), line, DAY)
    assert with_db(go) is None       # the world fixture's report already queued one


# ---- the review queue and its audit trail ------------------------------------------------------------------------------------

def first_proposal(world, sku="KTB-750"):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            return [str(r["id"]) for r in await proposals.list_proposals(conn) if r["sku"] == sku][0]
    return with_db(go)


def test_approving_writes_an_audit_row_with_the_actor_and_the_time(world):
    pid = first_proposal(world)

    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            row = await proposals.approve(conn, str(world.a), pid, "dana@example.com", "checked the page")
            trail = await proposals.audit_trail(conn, pid)
        return row, trail
    row, trail = with_db(go)
    assert row["state"] == "approved" and row["decided_by"] == "dana@example.com" and row["decided_at"] is not None
    assert [(t["from_state"], t["to_state"], t["actor"]) for t in trail] == [(None, "proposed", "price-watch report"), ("proposed", "approved", "dana@example.com")]
    assert trail[1]["note"] == "checked the page" and trail[1]["at"] is not None


def test_rejecting_is_audited_too_and_a_rejected_proposal_goes_nowhere(world):
    pid = first_proposal(world)

    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            await proposals.reject(conn, str(world.a), pid, "dana@example.com", "wrong product")
        for fn in (proposals.approve, proposals.reject):
            async with db.tenant(str(world.a)) as conn:
                with pytest.raises(proposals.InvalidTransition):
                    await fn(conn, str(world.a), pid, "dana@example.com")
        async with db.tenant(str(world.a)) as conn:
            return await proposals.audit_trail(conn, pid)
    trail = with_db(go)
    assert [t["to_state"] for t in trail] == ["proposed", "rejected"]


def test_a_second_approval_is_refused_and_writes_nothing(world):
    pid = first_proposal(world)

    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            await proposals.approve(conn, str(world.a), pid, "dana@example.com")
        async with db.tenant(str(world.a)) as conn:
            with pytest.raises(proposals.InvalidTransition, match="is approved and cannot go to approved"):
                await proposals.approve(conn, str(world.a), pid, "someone.else@example.com")
        async with db.tenant(str(world.a)) as conn:
            return await proposals.audit_trail(conn, pid), (await conn.fetchrow("select decided_by from price_change_proposal where id = $1::uuid", pid))["decided_by"]
    trail, decided_by = with_db(go)
    assert len(trail) == 2 and decided_by == "dana@example.com"


def test_two_people_approving_at_the_same_moment_only_one_succeeds(world):
    pid = first_proposal(world)

    async def go(db):
        async def attempt(who):
            try:
                async with db.tenant(str(world.a)) as conn:
                    await proposals.approve(conn, str(world.a), pid, who)
                return "ok"
            except proposals.InvalidTransition:
                return "refused"
        results = await asyncio.gather(*(attempt("person-%d" % i) for i in range(4)))
        async with db.tenant(str(world.a)) as conn:
            return results, await proposals.audit_trail(conn, pid)
    results, trail = with_db(go)
    assert sorted(results) == ["ok", "refused", "refused", "refused"]
    assert [t["to_state"] for t in trail] == ["proposed", "approved"]


def test_an_actor_is_required_and_unknown_states_and_columns_are_refused(world):
    pid = first_proposal(world)

    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            with pytest.raises(ValueError, match="actor"):
                await proposals.approve(conn, str(world.a), pid, "   ")
            with pytest.raises(ValueError):
                await proposals.move(conn, str(world.a), pid, "teleported", "x")
            with pytest.raises(ValueError, match="may not be set"):
                await proposals.move(conn, str(world.a), pid, "approved", "x", proposed_price_minor=1)
    with_db(go)


def test_every_legal_move_is_in_the_table_and_the_terminal_states_have_none():
    assert proposals.TRANSITIONS["proposed"] == {"approved", "rejected"}
    assert proposals.TRANSITIONS["approved"] == {"applied", "failed"}
    assert proposals.TRANSITIONS["failed"] == {"applied", "failed"}
    assert proposals.TRANSITIONS["rejected"] == frozenset() == proposals.TRANSITIONS["applied"]


# ---- the stored daily report, seeded day ---------------------------------------------------------------------------------------

def test_the_seeded_day_gives_these_lines_this_markdown_and_these_proposals(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            rep = await conn.fetchrow("select * from price_watch_report where report_date = $1", DAY)
            props = await proposals.list_proposals(conn)
        return rep, props
    rep, props = with_db(go)
    assert (rep["lines"], rep["needs_review"]) == (3, 1)
    assert rep["markdown"].startswith("# Competitor price report, 2026-10-30")
    assert "Alpen (http://alpen.invalid/unsure): title score 0.72" in rep["markdown"]
    assert "currency EUR against our AUD" in rep["markdown"]                              # the euro listing is excluded, not compared
    assert "8.50 ESTIMATED" in rep["markdown"]                                            # the lamp's shipping is our estimate and says so
    assert rep["csv"].splitlines()[0].startswith("date,sku,title")
    got = {p["sku"]: (p["current_price_minor"], p["proposed_price_minor"], p["shipping_source"], p["state"]) for p in props}
    assert got == {"KTB-750": (4995, 4795, "stated", "proposed"),
                   "LMP-210": (3450, 3340, "estimated", "proposed"),
                   "HMJ-NVY-M": (7900, 5500, "stated", "proposed")}          # the jumper is held at its 55.00 floor, not matched to 49.50
    assert all(p["proposed_price_minor"] >= p["floor_minor"] for p in props)
    assert all(p["shopify_status"] == "pending" for p in props)
    assert {p["sku"]: p["zoho_status"] for p in props} == {"KTB-750": "pending", "LMP-210": "skipped", "HMJ-NVY-M": "pending"}


def test_running_the_report_again_replaces_the_stored_row_and_queues_nothing_new(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            report, created = await store.make_report(conn, str(world.a), DAY)
            n = await conn.fetchval("select count(*) from price_watch_report")
            p = await conn.fetchval("select count(*) from price_change_proposal")
        return report, created, n, p
    report, created, n, p = with_db(go)
    assert len(report.lines) == 3 and created == [] and n == 1 and p == 3


def test_a_report_for_another_day_does_not_compare_a_stale_scan(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            return (await store.make_report(conn, str(world.a), DAY + timedelta(days=2), create_proposals=False))[0]
    report = with_db(go)
    assert report.lines == [] and len(report.excluded) == 4 and all("not scanned" in e.reason for e in report.excluded)


def test_only_matched_and_confirmed_listings_reach_the_report(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            await conn.execute("update competitor_listing set match_status = 'needs_review', catalogue_product_id = null where url like '%/lamp'")
            return (await store.make_report(conn, str(world.a), DAY, create_proposals=False))[0]
    report = with_db(go)
    assert {l.sku for l in report.lines} == {"KTB-750", "HMJ-NVY-M"} and len(report.needs_review) == 2


# ---- scans and matching, without a browser --------------------------------------------------------------------------------------

def scraped(**kw):
    base = dict(url="http://x", title=None, sku=None, gtin=None, price_minor=4100, original_price_minor=None, currency="AUD", availability="in_stock",
                shipping_minor=None, shipping_text=None, problems=())
    base.update(kw)
    return ScrapedProduct(**base)


def test_a_scan_stores_the_observation_and_matches_by_gtin_then_a_rescan_replaces_that_days_row(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            lid = await store.add_listing(conn, str(world.a), competitor="New", url="http://new.invalid/p")
            first = await store.record_scan(conn, str(world.a), lid, scraped(title="Whatever", gtin="9300000001016", shipping_minor=500), DAY)
            again = await store.record_scan(conn, str(world.a), lid, scraped(title="Whatever", gtin="9300000001016", price_minor=3900), DAY)
            row = await conn.fetchrow("select * from competitor_listing where id = $1::uuid", lid)
            obs = await conn.fetch("select price_minor from competitor_price_observation where listing_id = $1::uuid", lid)
        return first, again, row, obs
    first, again, row, obs = with_db(go)
    assert first["status"] == again["status"] == "matched"
    assert str(row["catalogue_product_id"]) == world.ids[world.a]["KTB-750"] and row["match_method"] == "gtin"
    assert [o["price_minor"] for o in obs] == [3900]


def test_a_low_confidence_match_is_stored_as_needs_review_and_has_no_product(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            lid = await store.add_listing(conn, str(world.a), competitor="New", url="http://new.invalid/p2")
            outcome = await store.record_scan(conn, str(world.a), lid, scraped(title="Harbour Merino Wool Jumper Navy M"), DAY)
            row = await conn.fetchrow("select * from competitor_listing where id = $1::uuid", lid)
        return outcome, row
    outcome, row = with_db(go)
    assert outcome["status"] == "needs_review" and row["catalogue_product_id"] is None
    assert "HMJ-NVY-M" in row["match_reason"]


def test_a_near_miss_is_not_matched(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            lid = await store.add_listing(conn, str(world.a), competitor="New", url="http://new.invalid/p3")
            await store.record_scan(conn, str(world.a), lid, scraped(title="Kestrel Insulated Bottle 500ml"), DAY)
            return await conn.fetchrow("select match_status, catalogue_product_id from competitor_listing where id = $1::uuid", lid)
    row = with_db(go)
    assert row["match_status"] == "no_match" and row["catalogue_product_id"] is None


def test_a_persons_confirmation_survives_a_later_scan_and_puts_the_listing_in_the_report(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            lid = await store.add_listing(conn, str(world.a), competitor="New", url="http://new.invalid/p4")
            await store.record_scan(conn, str(world.a), lid, scraped(title="Harbour Merino Wool Jumper Navy M"), DAY)
            await store.confirm_match(conn, str(world.a), lid, world.ids[world.a]["HMJ-NVY-M"], "dana@example.com")
            outcome = await store.record_scan(conn, str(world.a), lid, scraped(title="Something else entirely"), DAY)
            row = await conn.fetchrow("select match_status, match_method, catalogue_product_id from competitor_listing where id = $1::uuid", lid)
            with pytest.raises(ValueError):
                await store.confirm_match(conn, str(world.a), lid, world.ids[world.a]["HMJ-NVY-M"], " ")
        return outcome, row
    outcome, row = with_db(go)
    assert outcome["status"] == "confirmed" and row["match_status"] == "confirmed" and row["match_method"] == "manual"


def test_a_page_that_failed_to_load_is_recorded_as_a_problem_with_no_price(world):
    async def go(db):
        async with db.tenant(str(world.a)) as conn:
            lid = await store.add_listing(conn, str(world.a), competitor="Dead", url="http://dead.invalid/p")
            outcome = await store.record_scan(conn, str(world.a), lid, scraped(price_minor=None, currency=None, problems=("HTTP 500",)), DAY)
            return outcome, await conn.fetchrow("select price_minor, problem from competitor_price_observation where listing_id = $1::uuid", lid)
    outcome, obs = with_db(go)
    assert outcome["status"] == "unchanged" and obs["price_minor"] is None and obs["problem"] == "HTTP 500"
