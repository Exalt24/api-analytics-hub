"""Push an APPROVED proposal to the shops. Dry run unless told otherwise.

The rules, each pinned by a test and a mutation control:
  * Only an approved proposal syncs (or a failed one, which is an approved proposal with a shop still owing). A proposed,
    rejected or unknown proposal is REFUSED, in a dry run too.
  * One call per shop per proposal that has succeeded. The proposal keeps a status for each shop (pending, applied, failed,
    skipped); a retry skips what is applied, so a Shopify success followed by a Zoho failure is retried against Zoho only.
    A proposal that is already applied does nothing at all, however often it is run: a retry never applies twice.
  * The proposal row is locked for the whole run, so two sync processes cannot both be mid-way through the same proposal.
    (If the process dies between a vendor success and the commit, the retry sets the same absolute price again, which is harmless;
    that is the reason the adapters SET a price and never add to one.)
  * Before anything is sent the proposal is checked against the catalogue as it is NOW: the price must still be the one the
    proposal was made from (someone may have changed it since), and the proposed price must still be at or above the floor.
  * A shop that rejects the change (a 4xx other than 408 and 429) leaves that side `failed` with the vendor's message; one that
    stays busy (429, 5xx) leaves it `failed` with a note that it is retryable. Either way the proposal is `failed`, never `applied`.
  * `applied` means every shop the product is wired to has accepted the price. Only then does the catalogue take the new price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.db import Database

from . import proposals
from .adapters import AdapterResult, PriceAdapter, PriceTarget, VendorRejected, VendorTransient

SIDES = ("shopify", "zoho")


class NotApproved(Exception):
    """The proposal is not in a state that may be synced (proposed, rejected)."""


class StaleProposal(Exception):
    """The catalogue price or floor moved since the proposal was made."""


class NoSyncTarget(Exception):
    """The product has neither a Shopify variant nor a Zoho item, so there is nowhere to send the price."""


@dataclass
class SyncOutcome:
    proposal_id: str
    state: str
    dry_run: bool
    sides: dict[str, str] = field(default_factory=dict)       # shop -> pending, applied, failed, skipped
    messages: dict[str, str] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)  # what a real run would send (dry run only)
    already_applied: bool = False


async def sync_proposal(db: Database, tenant_id: str, proposal_id: str, adapters: Mapping[str, PriceAdapter], *,
                        apply: bool = False, actor: str = "price-watch sync") -> SyncOutcome:
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow(
            """select p.*, c.price_minor as catalogue_price, c.floor_minor as catalogue_floor, c.sku,
                      c.shopify_product_gid, c.shopify_variant_gid, c.zoho_item_id, c.zoho_item_name
                 from price_change_proposal p join catalogue_product c on c.id = p.catalogue_product_id
                where p.id = $1::uuid for update of p""", proposal_id)
        if row is None:
            raise proposals.ProposalNotFound(proposal_id)
        state = row["state"]
        sides = {"shopify": row["shopify_status"], "zoho": row["zoho_status"]}
        if state == "applied":
            return SyncOutcome(proposal_id, "applied", not apply, sides, already_applied=True)
        if state not in ("approved", "failed"):
            raise NotApproved("proposal %s is %s; only an approved proposal can be synced" % (proposal_id, state))
        if row["catalogue_price"] != row["current_price_minor"]:
            raise StaleProposal("the catalogue price is now %d, the proposal was made from %d" % (row["catalogue_price"], row["current_price_minor"]))
        if row["proposed_price_minor"] < row["catalogue_floor"]:
            raise StaleProposal("the floor is now %d, above the proposed %d" % (row["catalogue_floor"], row["proposed_price_minor"]))

        target = PriceTarget(row["sku"], row["shopify_product_gid"], row["shopify_variant_gid"], row["zoho_item_id"], row["zoho_item_name"])
        if not row["shopify_variant_gid"] and not row["zoho_item_id"]:
            raise NoSyncTarget("product %s has no Shopify variant or Zoho item configured" % row["sku"])

        price, currency, key = row["proposed_price_minor"], row["currency"].strip(), row["idempotency_key"]
        todo = [n for n in SIDES if sides[n] in ("pending", "failed")]

        if not apply:
            plan = [adapters[n].describe(target, price, currency) for n in todo if n in adapters and adapters[n].applies_to(target)]
            return SyncOutcome(proposal_id, state, True, sides, plan=plan)

        messages: dict[str, str] = {}
        for name in todo:
            adapter = adapters.get(name)
            if adapter is None or not adapter.applies_to(target):
                sides[name], messages[name] = "failed", "no %s adapter configured for this product" % name
                continue
            try:
                result: AdapterResult = await adapter.set_price(target, price, currency, key)
                sides[name], messages[name] = "applied", result.detail
            except VendorRejected as exc:
                sides[name], messages[name] = "failed", "rejected %s" % exc
            except VendorTransient as exc:
                sides[name], messages[name] = "failed", "transient, retry later: %s" % exc

        done = all(s in ("applied", "skipped") for s in sides.values())
        new_state = "applied" if done else "failed"
        cols: dict[str, Any] = {
            "shopify_status": sides["shopify"], "shopify_error": None if sides["shopify"] == "applied" else messages.get("shopify", row["shopify_error"]),
            "zoho_status": sides["zoho"], "zoho_error": None if sides["zoho"] == "applied" else messages.get("zoho", row["zoho_error"]),
            "sync_attempts": row["sync_attempts"] + 1}
        note = "; ".join("%s: %s" % (n, m) for n, m in messages.items()) or None
        await proposals.move(conn, tenant_id, proposal_id, new_state, actor, note, **cols)
        if done:
            await conn.execute("update catalogue_product set price_minor = $2 where id = $1::uuid", row["catalogue_product_id"], price)
        return SyncOutcome(proposal_id, new_state, False, sides, messages)
