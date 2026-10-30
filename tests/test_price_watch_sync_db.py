"""Price watch: syncing approved proposals to the (fake) Shopify and Zoho, against real Postgres as the request role.

Everything a vendor would say comes from app/price_watch/fakes.py. These tests prove OUR rules: only an approved proposal syncs, a dry run changes
nothing, a retry never applies twice, a partial failure retries only the failed shop, and the audit trail records each move. They do not prove
that Shopify or Zoho accept the requests; no live account has been used.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price_watch_fixtures import DAY, DSN, add_tenant, needs_db, reset_schema, run, seed_catalogue, seed_listing_with_scan  # noqa: E402

pytestmark = needs_db

from app.db import Database  # noqa: E402
from app.price_watch import proposals, store  # noqa: E402
from app.price_watch.adapters import ShopifyPriceAdapter, ZohoInventoryAdapter, ZohoTokenSource  # noqa: E402
from app.price_watch.fakes import FakeShopify, FakeZoho  # noqa: E402
from app.price_watch.sync import NoSyncTarget, NotApproved, StaleProposal, sync_proposal  # noqa: E402


async def no_sleep(seconds: float) -> None:
    return None


class Rig:
    """One tenant with a queued proposal on KTB-750 (both shops), LMP-210 (Shopify only) and HMJ-NVY-M (both), fake vendors, and helpers."""

    def __init__(self):
        reset_schema()
        self.tenant = add_tenant("sync-tenant")
        self.shop, self.zoho = FakeShopify(), FakeZoho()
        self.db = None

    async def start(self):
        self.db = Database(DSN, max_size=4)
        await self.db.connect()
        self.ids = await seed_catalogue(self.db, self.tenant)
        await seed_listing_with_scan(self.db, self.tenant, self.ids["KTB-750"], competitor="Depot", url="http://depot.invalid/bottle", price_minor=4100, shipping_minor=695)
        await seed_listing_with_scan(self.db, self.tenant, self.ids["LMP-210"], competitor="Depot", url="http://depot.invalid/lamp", price_minor=2490, shipping_minor=None)
        async with self.db.tenant(str(self.tenant)) as conn:
            await store.make_report(conn, str(self.tenant), DAY)
        self.adapters = self.make_adapters()
        return self

    def make_adapters(self):
        sc, zc = self.shop.client(), self.zoho.client()
        tokens = ZohoTokenSource(zc, "https://" + self.zoho.accounts_host, self.zoho.client_id, self.zoho.client_secret, self.zoho.refresh_token, sleep=no_sleep)
        return {"shopify": ShopifyPriceAdapter(sc, self.shop.shop_domain, self.shop.token, sleep=no_sleep),
                "zoho": ZohoInventoryAdapter(zc, tokens, self.zoho.org, "https://" + self.zoho.api_host, sleep=no_sleep)}

    async def stop(self):
        await self.db.close()

    async def pid(self, sku):
        async with self.db.tenant(str(self.tenant)) as conn:
            return str([r for r in await proposals.list_proposals(conn) if r["sku"] == sku][0]["id"])

    async def approve(self, sku, who="dana@example.com"):
        pid = await self.pid(sku)
        async with self.db.tenant(str(self.tenant)) as conn:
            await proposals.approve(conn, str(self.tenant), pid, who)
        return pid

    async def row(self, pid):
        async with self.db.tenant(str(self.tenant)) as conn:
            return (await conn.fetchrow("select p.*, c.price_minor as catalogue_price from price_change_proposal p join catalogue_product c on c.id = p.catalogue_product_id where p.id = $1::uuid", pid),
                    await proposals.audit_trail(conn, pid))

    async def sync(self, pid, apply=True):
        return await sync_proposal(self.db, str(self.tenant), pid, self.adapters, apply=apply)


def scenario(coro_fn):
    """Run `coro_fn(rig)` against a fresh rig and always close its pool."""
    async def go():
        rig = await Rig().start()
        try:
            return await coro_fn(rig)
        finally:
            await rig.stop()
    return run(go())


# ---- the happy path --------------------------------------------------------------------------------------------------------------

def test_an_approved_proposal_is_sent_to_both_shops_and_the_catalogue_takes_the_new_price():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        out = await rig.sync(pid)
        row, trail = await rig.row(pid)
        return rig, out, row, trail
    rig, out, row, trail = scenario(go)
    assert (out.state, out.dry_run, out.sides) == ("applied", False, {"shopify": "applied", "zoho": "applied"})
    assert rig.shop.prices == {"gid://shopify/ProductVariant/2001": "47.95"} and rig.zoho.rates == {"4815000000010001": 47.95}
    assert row["state"] == "applied" and row["catalogue_price"] == 4795 and row["sync_attempts"] == 1 and row["shopify_error"] is None
    assert [(t["from_state"], t["to_state"]) for t in trail] == [(None, "proposed"), ("proposed", "approved"), ("approved", "applied")]
    assert trail[-1]["actor"] == "price-watch sync"


def test_a_product_wired_to_one_shop_only_touches_that_shop():
    async def go(rig):
        pid = await rig.approve("LMP-210")
        out = await rig.sync(pid)
        return rig, out
    rig, out = scenario(go)
    assert out.state == "applied" and out.sides == {"shopify": "applied", "zoho": "skipped"}
    assert rig.zoho.calls == [] and rig.shop.prices == {"gid://shopify/ProductVariant/2002": "33.40"}


def test_the_default_is_a_dry_run_that_sends_nothing_and_changes_nothing():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        out = await rig.sync(pid, apply=False)
        row, trail = await rig.row(pid)
        return rig, out, row, trail
    rig, out, row, trail = scenario(go)
    assert out.dry_run and out.state == "approved" and rig.shop.calls == [] and rig.zoho.calls == []
    assert [p["vendor"] for p in out.plan] == ["shopify", "zoho"]
    assert out.plan[0]["variables"]["variants"][0]["price"] == "47.95" and out.plan[1]["body"]["rate"] == 47.95
    assert row["state"] == "approved" and row["catalogue_price"] == 4995 and len(trail) == 2


# ---- unapproved, rejected, stale ---------------------------------------------------------------------------------------------------

def test_a_proposal_nobody_approved_is_refused_in_a_dry_run_and_for_real():
    async def go(rig):
        pid = await rig.pid("KTB-750")
        errors = []
        for apply in (False, True):
            with pytest.raises(NotApproved) as exc:
                await rig.sync(pid, apply=apply)
            errors.append(str(exc.value))
        return rig, errors, await rig.row(pid)
    rig, errors, (row, trail) = scenario(go)
    assert all("is proposed; only an approved proposal can be synced" in e for e in errors)
    assert rig.shop.calls == [] and rig.zoho.calls == [] and row["state"] == "proposed" and len(trail) == 1


def test_a_rejected_proposal_is_refused():
    async def go(rig):
        pid = await rig.pid("KTB-750")
        async with rig.db.tenant(str(rig.tenant)) as conn:
            await proposals.reject(conn, str(rig.tenant), pid, "dana@example.com", "no")
        with pytest.raises(NotApproved):
            await rig.sync(pid)
        return rig
    rig = scenario(go)
    assert rig.shop.calls == []


def test_an_unknown_proposal_is_refused():
    async def go(rig):
        with pytest.raises(proposals.ProposalNotFound):
            await rig.sync("00000000-0000-0000-0000-000000000000")
    scenario(go)


def test_a_price_that_moved_since_the_proposal_was_made_is_not_overwritten_blindly():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        async with rig.db.tenant(str(rig.tenant)) as conn:
            await conn.execute("update catalogue_product set price_minor = 4500 where sku = 'KTB-750'")
        with pytest.raises(StaleProposal, match="catalogue price is now 4500"):
            await rig.sync(pid)
        return rig
    assert scenario(go).shop.calls == []


def test_a_floor_raised_after_approval_stops_the_sync():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        async with rig.db.tenant(str(rig.tenant)) as conn:
            await conn.execute("update catalogue_product set floor_minor = 4900 where sku = 'KTB-750'")
        with pytest.raises(StaleProposal, match="floor is now 4900"):
            await rig.sync(pid)
        return rig
    assert scenario(go).zoho.calls == []


def test_a_product_wired_to_nothing_cannot_be_synced():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        async with rig.db.tenant(str(rig.tenant)) as conn:
            await conn.execute("update catalogue_product set zoho_item_id = null, zoho_item_name = null, shopify_product_gid = null, shopify_variant_gid = null where sku = 'KTB-750'")
        with pytest.raises(NoSyncTarget):
            await rig.sync(pid)
    scenario(go)


# ---- retries, rejections, partial failure -------------------------------------------------------------------------------------------

def test_429_then_success_still_ends_applied():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        rig.shop.script(429, 429)
        out = await rig.sync(pid)
        return rig, out
    rig, out = scenario(go)
    assert out.state == "applied" and len(rig.shop.calls) == 3


def test_shopify_401_marks_the_proposal_failed_with_the_vendors_message_and_the_other_shop_still_runs():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        rig.shop.script((401, {"errors": "[API] Invalid API key or access token"}))
        out = await rig.sync(pid)
        row, trail = await rig.row(pid)
        return rig, out, row, trail
    rig, out, row, trail = scenario(go)
    assert out.state == "failed" and out.sides == {"shopify": "failed", "zoho": "applied"}
    assert row["state"] == "failed" and "rejected 401" in row["shopify_error"] and "Invalid API key or access token" in row["shopify_error"]
    assert row["catalogue_price"] == 4995           # not applied everywhere, so the catalogue keeps the old price
    assert "shopify: rejected 401" in trail[-1]["note"] and trail[-1]["to_state"] == "failed"


def test_shopify_ok_zoho_fails_then_the_retry_calls_only_zoho():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        rig.zoho.script((200, {"access_token": "t", "expires_in": 3600}), (400, {"code": 2, "message": "Invalid value passed for rate"}))
        first = await rig.sync(pid)
        mid, _ = await rig.row(pid)
        shop_calls_after_first = len(rig.shop.calls)
        second = await rig.sync(pid)
        final, trail = await rig.row(pid)
        return rig, first, mid, shop_calls_after_first, second, final, trail
    rig, first, mid, shop_calls_after_first, second, final, trail = scenario(go)
    assert first.state == "failed" and first.sides == {"shopify": "applied", "zoho": "failed"}
    assert mid["shopify_status"] == "applied" and mid["zoho_status"] == "failed" and "Invalid value passed for rate" in mid["zoho_error"]
    assert second.state == "applied" and second.sides == {"shopify": "applied", "zoho": "applied"}
    assert shop_calls_after_first == 1 and len(rig.shop.calls) == 1      # Shopify was called once in total, never again
    assert final["zoho_error"] is None and final["sync_attempts"] == 2 and final["catalogue_price"] == 4795
    assert [t["to_state"] for t in trail] == ["proposed", "approved", "failed", "applied"]


def test_a_shop_that_stays_busy_leaves_a_retryable_failure_and_a_later_run_finishes_it():
    async def go(rig):
        pid = await rig.approve("LMP-210")
        rig.shop.script(503, 503, 503, 503)
        first = await rig.sync(pid)
        second = await rig.sync(pid)
        return first, second, (await rig.row(pid))[0]
    first, second, row = scenario(go)
    assert first.state == "failed" and "transient, retry later" in first.messages["shopify"]
    assert second.state == "applied" and row["shopify_status"] == "applied"


def test_running_an_applied_proposal_again_sends_nothing():
    async def go(rig):
        pid = await rig.approve("KTB-750")
        await rig.sync(pid)
        before = (len(rig.shop.calls), len(rig.zoho.calls))
        again = await rig.sync(pid)
        dry = await rig.sync(pid, apply=False)
        _, trail = await rig.row(pid)
        return rig, before, again, dry, trail
    rig, before, again, dry, trail = scenario(go)
    assert again.already_applied and dry.already_applied and again.state == "applied"
    assert (len(rig.shop.calls), len(rig.zoho.calls)) == before
    assert [t["to_state"] for t in trail].count("applied") == 1


def test_two_syncs_at_the_same_moment_apply_once():
    import asyncio

    async def go(rig):
        pid = await rig.approve("KTB-750")
        a, b = await asyncio.gather(rig.sync(pid), rig.sync(pid))
        return rig, a, b, (await rig.row(pid))[1]
    rig, a, b, trail = scenario(go)
    assert sorted([a.already_applied, b.already_applied]) == [False, True]
    assert len(rig.shop.calls) == 1 and [t["to_state"] for t in trail].count("applied") == 1


# ---- a seeded simulation: random failures over many rounds ------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(12))
def test_whatever_the_shops_do_each_side_succeeds_at_most_once_and_the_state_tells_the_truth(seed):
    """Dimensions varied together: which shops a product is wired to, a failure script for each shop (429, 5xx, 4xx, a 200 with userErrors, a
    refused token), how many sync rounds, and a dry run mixed in. Invariants: a shop that has accepted a price is never called with it again;
    the proposal is applied exactly when every wired shop accepted; the catalogue price changes exactly then; the audit trail has one row per
    state change and ends where the row ends; an applied proposal never moves again."""
    rng = random.Random(seed)

    async def go(rig):
        sku = rng.choice(["KTB-750", "LMP-210"])
        pid = await rig.approve(sku)
        wired = ["shopify", "zoho"] if sku == "KTB-750" else ["shopify"]
        bad = [503, 429, (400, {"errors": "bad"}), (200, {"data": {"productVariantsBulkUpdate": {"productVariants": None, "userErrors": [{"field": ["variants", "0", "price"], "message": "no"}]}}})]
        zbad = [500, 429, (400, {"code": 2, "message": "bad rate"}), (200, {"code": 1001, "message": "Rate is invalid"})]
        for _ in range(rng.randint(2, 5)):
            if rng.random() < 0.6:
                rig.shop.script(*[rng.choice(bad) for _ in range(rng.randint(1, 5))])
            if "zoho" in wired and rng.random() < 0.6:
                rig.zoho.script(*[rng.choice(zbad) for _ in range(rng.randint(1, 5))])
            if rng.random() < 0.3:
                await rig.sync(pid, apply=False)
            out = await rig.sync(pid)
            row, trail = await rig.row(pid)
            applied_sides = [s for s in wired if row[s + "_status"] == "applied"]
            assert (row["state"] == "applied") == (len(applied_sides) == len(wired)), (seed, row["state"], applied_sides)
            assert (row["catalogue_price"] == row["proposed_price_minor"]) == (row["state"] == "applied")
            assert trail[-1]["to_state"] == row["state"] and trail[0]["from_state"] is None
            assert all(t["actor"] for t in trail)
            if row["state"] == "applied":
                break
        return rig, row, trail
    rig, row, trail = scenario(go)
    # a shop that accepted a price accepted it once, however many rounds ran: after that the per shop status stops further calls
    assert rig.shop.accepted <= 1 and rig.zoho.accepted <= 1
    assert (row["shopify_status"] == "applied") == (rig.shop.accepted == 1)
    assert (row["zoho_status"] == "applied") == (rig.zoho.accepted == 1)
    states = [t["to_state"] for t in trail]
    assert states[:2] == ["proposed", "approved"]
    assert states.count("applied") <= 1 and (states[-1] != "applied" or states.index("applied") == len(states) - 1)
