"""The scope split, enforced rather than promised.

The app holds full write access so the seeder can create test orders on our own
dev store. That is deliberate and necessary: with read-only scopes there is
nothing to sync and no demo.

But the CONNECTOR must still never mutate, because the connector is the half that
would one day point at a client's live shop, and a read-only integration cannot
corrupt a merchant's data even if it has a bug. A comment saying so decays the
first time someone adds a convenience mutation. So it is asserted here, against
the source.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.connectors import shopify_scopes as sc

CONNECTOR_SRC = (ROOT / "app" / "connectors" / "shopify.py").read_text(encoding="utf-8")

# GraphQL mutations are the only way this connector could write. Matching the
# keyword at a statement position rather than anywhere in the file, so the word
# appearing in a comment does not trip it.
MUTATION_RE = re.compile(r"^\s*mutation\s+\w+|\bmutation\s*\(", re.MULTILINE)


def test_the_connector_contains_no_graphql_mutation():
    hits = MUTATION_RE.findall(CONNECTOR_SRC)
    assert not hits, (
        f"shopify.py contains what looks like a mutation ({hits[:2]}). The connector "
        "must stay read-only; put writes in the seeder."
    )


def test_the_connector_declares_only_read_scopes_in_its_queries():
    """A cheap proxy for the same property: the queries it sends are named reads."""
    # Every GraphQL document in the connector should start with `query`.
    docs = re.findall(r'^([A-Z_]+_QUERY|SHOP_QUERY)\s*=\s*"""?\s*\n?\s*(\w+)', CONNECTOR_SRC, re.M)
    for name, first_word in docs:
        assert first_word == "query", f"{name} starts with '{first_word}', expected 'query'"


def test_client_facing_scope_string_has_no_write_scopes():
    """What a real deployment would request. Write scopes here would be a breach
    of the read-only promise made to a merchant."""
    client_scopes = sc.scope_string().split(",")
    writes = [s for s in client_scopes if s.startswith("write_")]
    assert not writes, f"the client scope string leaked write scopes: {writes}"


def test_dev_store_scope_string_does_contain_write_scopes():
    """The counterweight. If this ever goes read-only again the seeder breaks and
    the failure looks like a Shopify permissions problem rather than our change.
    """
    dev_scopes = sc.dev_store_scope_string().split(",")
    assert any(s.startswith("write_") for s in dev_scopes), (
        "the dev-store scope string has no write scopes, so nothing can seed test orders"
    )
    assert "write_orders" in dev_scopes
    assert "write_draft_orders" in dev_scopes


def test_no_scope_is_requested_twice():
    """Shopify rejects a duplicated scope, and the read and write tuples overlap
    by construction risk since both are hand-maintained."""
    scopes = sc.dev_store_scope_string().split(",")
    dupes = {s for s in scopes if scopes.count(s) > 1}
    assert not dupes, f"duplicated scopes would be rejected at install: {dupes}"


def test_pii_scopes_stay_excluded():
    """Holding real customer PII in a portfolio project is a liability with no
    upside, so this is a deliberate omission that should not drift back."""
    scopes = set(sc.dev_store_scope_string().split(","))
    for banned in ("read_customers_pii", "write_customers_pii", "read_users"):
        assert banned not in scopes, f"{banned} should never be requested"


def test_read_all_orders_is_flagged_as_approval_gated():
    """It is requested, but it is not ours to grant. If the install fails this is
    the first thing to drop, so it must stay separately identifiable."""
    assert "read_all_orders" in sc.APPROVAL_REQUIRED_SCOPES
    assert "read_all_orders" not in sc.FULL_READ_SCOPES, (
        "keep it out of the base tuple so an install can be retried without it"
    )


def test_the_sixty_day_wall_is_encoded_not_just_documented():
    assert sc.DEFAULT_ORDER_HISTORY_DAYS == 60


def test_access_mode_is_offline():
    """Online tokens expire in 24h and are useless for a background sync."""
    assert sc.ACCESS_MODE == "offline"
