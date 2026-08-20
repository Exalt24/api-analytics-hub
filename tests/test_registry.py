"""Registry tests: the "adding a connector changes nothing else" claim, tested.

The load-bearing test here is `test_a_new_connector_needs_no_core_changes`. It
registers a fabricated platform with a different auth kind, a different rate-limit
style and a different metric set, then exercises every generic path against it. If
Shopify's assumptions had leaked into the core, that test is where it shows.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import CanonicalPoint, Connector, FetchResult
from app.connectors.registry import (
    AuthKind,
    ConnectorRegistry,
    ConnectorSpec,
    CredentialField,
    MetricNotSupported,
    RateLimitStyle,
    UnknownPlatform,
    registry,
)


def test_shopify_is_registered_with_its_real_constraints():
    spec = registry.get("shopify")
    assert spec.rate_limit_style is RateLimitStyle.QUERY_COST_BUCKET
    assert spec.auth_kind is AuthKind.STATIC_TOKEN
    assert spec.max_history_days == 60, "the 60-day wall must be declared, not remembered"
    assert "gross_sales" in spec.supported_metrics


def test_asking_a_platform_for_a_metric_it_cannot_produce_RAISES():
    """Not an empty series. An empty chart is indistinguishable from a real zero,
    and a client reads it as "no sales" rather than "nobody asked"."""
    with pytest.raises(MetricNotSupported):
        registry.assert_supports("shopify", "followers")
    # And the supported one passes, so the guard is not simply refusing everything.
    registry.assert_supports("shopify", "orders")


def test_an_unknown_platform_names_what_is_registered():
    with pytest.raises(UnknownPlatform) as exc:
        registry.get("tiktok")
    assert "shopify" in str(exc.value), "the error should say what IS available"


def test_secrets_are_declared_and_redaction_is_centralised():
    assert registry.secret_keys("shopify") == frozenset({"access_token"})
    out = registry.redact("shopify", {"shop_domain": "x.myshopify.com", "access_token": "shpat_secret"})
    assert out["access_token"] == "***"
    assert out["shop_domain"] == "x.myshopify.com", "non-secret fields stay readable"


def test_building_without_all_credentials_fails_before_any_network_call():
    with pytest.raises(ValueError) as exc:
        registry.build("shopify", shop_domain="x.myshopify.com")
    assert "access_token" in str(exc.value)


def test_platforms_supporting_lets_a_dashboard_render_only_answerable_cards():
    assert "shopify" in registry.platforms_supporting("orders")
    assert registry.platforms_supporting("followers") == []


# ---------------------------------------------------------------- the real test

class FakeSocialConnector(Connector):
    """A deliberately UNLIKE-Shopify platform: OAuth with refresh, request-count
    limiting, a point-in-time gauge, and no backfill at all."""

    platform = "fakegram"
    supported_metrics = frozenset({"followers"})

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id

    async def fetch(self, *, window_start, window_end, cursor=None):
        yield FetchResult(points=[CanonicalPoint("followers", date(2026, 8, 1), 1234)])

    async def verify(self) -> str:
        return "fakegram account"


def test_a_new_connector_needs_no_core_changes():
    """Register a platform that differs from Shopify on every axis and drive every
    generic path. Anything Shopify-specific that leaked into the core fails here.
    """
    r = ConnectorRegistry()
    r.register(
        ConnectorSpec(
            platform="fakegram",
            label="Fakegram",
            auth_kind=AuthKind.OAUTH_REFRESH,
            rate_limit_style=RateLimitStyle.REQUEST_COUNT,
            supported_metrics=FakeSocialConnector.supported_metrics,
            credential_fields=(
                CredentialField("client_id", "Client ID", secret=False),
                CredentialField("client_secret", "Client secret"),
                CredentialField("refresh_token", "Refresh token"),
            ),
            factory=FakeSocialConnector,
            max_history_days=None,
            supports_backfill=False,
            notes="only ever returns the current value, so history comes from snapshots",
        )
    )

    assert r.platforms() == ["fakegram"]
    r.assert_supports("fakegram", "followers")
    with pytest.raises(MetricNotSupported):
        r.assert_supports("fakegram", "gross_sales")

    # Credentials, redaction and construction all work with no Shopify knowledge.
    assert r.secret_keys("fakegram") == frozenset({"client_secret", "refresh_token"})
    redacted = r.redact("fakegram", {"client_id": "abc", "client_secret": "s", "refresh_token": "t"})
    assert redacted == {"client_id": "abc", "client_secret": "***", "refresh_token": "***"}

    built = r.build("fakegram", client_id="abc", client_secret="s", refresh_token="t")
    assert isinstance(built, FakeSocialConnector)

    # A platform with no history must be declarable, since that is exactly why the
    # snapshot table exists.
    assert r.max_backfill_days("fakegram") is None
    assert r.get("fakegram").supports_backfill is False


def test_registering_the_same_platform_twice_is_refused():
    r = ConnectorRegistry()
    spec = ConnectorSpec(
        platform="dup", label="Dup", auth_kind=AuthKind.STATIC_TOKEN,
        rate_limit_style=RateLimitStyle.REQUEST_COUNT,
        supported_metrics=frozenset({"orders"}),
        credential_fields=(), factory=lambda: None,
    )
    r.register(spec)
    with pytest.raises(ValueError):
        r.register(spec)


def test_a_connector_supporting_no_metrics_is_refused_at_registration():
    """Otherwise it presents as a platform with a permanently empty dashboard."""
    r = ConnectorRegistry()
    with pytest.raises(ValueError) as exc:
        r.register(ConnectorSpec(
            platform="empty", label="Empty", auth_kind=AuthKind.STATIC_TOKEN,
            rate_limit_style=RateLimitStyle.REQUEST_COUNT,
            supported_metrics=frozenset(), credential_fields=(), factory=lambda: None,
        ))
    assert "no supported metrics" in str(exc.value)
