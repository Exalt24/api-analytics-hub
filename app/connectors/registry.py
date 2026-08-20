"""The connector registry: what makes this a platform rather than a Shopify project.

WHY THIS FILE IS THE ACTUAL ASSET. A single connector is a script. The reusable
thing is the CONTRACT plus the machinery around it, so that adding a platform
means writing one class and registering it, and touching nothing else. That claim
is easy to make and only becomes true when the second connector lands, because
the second one is what reveals where the first one's assumptions leaked into the
core.

So this file exists to make the claim testable:

  * A connector declares which canonical metrics it can produce, and the registry
    REFUSES a request for a metric a platform cannot supply. Returning an empty
    series instead is the failure mode that matters: an empty chart is
    indistinguishable from a genuine zero, and a client reads it as "no sales"
    rather than "we never asked".

  * Credentials are described per platform (what fields, which are secret, how it
    authenticates) so the storage and redaction layers stay generic. The moment
    one platform's field names appear in the credential code, every future
    platform inherits them.

  * Capability is introspectable, so a dashboard can render only what a tenant's
    connected platforms can actually answer.

DESIGNED FOR THE PLATFORMS AHEAD, not just the one behind. The listing that
prompted this names Shopify, Meta/Instagram, TikTok, Amazon, Pinterest and
Google, and they differ in ways the contract has to absorb rather than special
case: cost-based rate limits versus request-based, cursor versus page-token
pagination, offline tokens versus short-lived ones with refresh, per-day metrics
versus point-in-time gauges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from .base import Connector


class AuthKind(str, Enum):
    """How a platform expects to be authenticated.

    Kept as an enum rather than free text because the credential UI, the refresh
    scheduler and the reconnect flow all branch on it, and a typo in a string
    would silently route a token through the wrong path.
    """

    #: A long-lived token pasted once. Shopify custom distribution, GitHub PATs.
    STATIC_TOKEN = "static_token"
    #: OAuth 2.0 with a refresh token. Meta, Google, TikTok, Pinterest.
    OAUTH_REFRESH = "oauth_refresh"
    #: Signed requests, no bearer token at all. Amazon SP-API's older sibling.
    REQUEST_SIGNING = "request_signing"
    #: A service account key file. Google's server-to-server flow.
    SERVICE_ACCOUNT = "service_account"


class RateLimitStyle(str, Enum):
    """The shape of the platform's limiter, which decides how pacing works.

    This is the field that most repays being explicit. A request-per-second
    limiter and a cost-based bucket need completely different pacing, and code
    that assumes one will quietly over- or under-throttle on the other.
    """

    #: Requests per window, usually with X-RateLimit-* headers. Most REST APIs.
    REQUEST_COUNT = "request_count"
    #: A points bucket charged by computed query cost. Shopify Admin GraphQL.
    QUERY_COST_BUCKET = "query_cost_bucket"
    #: A quota per day rather than per second. Google's Data APIs.
    DAILY_QUOTA = "daily_quota"
    #: Undocumented, discovered only by being throttled. More common than vendors admit.
    UNDOCUMENTED = "undocumented"


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    #: Secret fields are encrypted at rest with app/crypto.py and carried in a
    #: Secret wrapper whose repr is redacted and whose __format__ raises, because
    #: the realistic leak is an f-string in a log line rather than a broken
    #: cipher. This line used to make the same promise with nothing behind it.
    secret: bool = True
    #: Shown in a connect form so a human knows where to find the value.
    hint: str = ""


@dataclass(frozen=True)
class ConnectorSpec:
    """Everything the platform-agnostic layers need to know about a connector."""

    platform: str
    label: str
    auth_kind: AuthKind
    rate_limit_style: RateLimitStyle
    supported_metrics: frozenset[str]
    credential_fields: tuple[CredentialField, ...]
    #: Builds the connector from decrypted credentials. A factory rather than a
    #: class so a platform needing a session, a signer or a token manager can
    #: assemble it without the registry knowing how.
    factory: Callable[..., Connector]
    #: How far back the platform will serve, or None for unlimited. Shopify caps
    #: orders at 60 days without an approved scope, and a backfill request beyond
    #: a platform's reach must be refused rather than returning an empty range
    #: that charts as zero.
    max_history_days: int | None = None
    #: Whether the platform can serve history at all. Some only ever return the
    #: current value, which is precisely why the store keeps dated snapshots.
    supports_backfill: bool = True
    notes: str = ""


class UnknownPlatform(KeyError):
    pass


class MetricNotSupported(ValueError):
    """Asked a platform for something it cannot produce.

    Raised rather than returning nothing, because an empty series renders as a
    genuine zero and a client cannot tell the difference.
    """


@dataclass
class ConnectorRegistry:
    _specs: dict[str, ConnectorSpec] = field(default_factory=dict)

    def register(self, spec: ConnectorSpec) -> None:
        if spec.platform in self._specs:
            raise ValueError(f"{spec.platform} is already registered")
        if not spec.supported_metrics:
            # A connector that supports nothing is a configuration mistake that
            # would otherwise present as a platform with a permanently empty
            # dashboard.
            raise ValueError(f"{spec.platform} declares no supported metrics")
        self._specs[spec.platform] = spec

    def get(self, platform: str) -> ConnectorSpec:
        try:
            return self._specs[platform]
        except KeyError as exc:
            raise UnknownPlatform(
                f"no connector for {platform!r}; registered: {sorted(self._specs)}"
            ) from exc

    def platforms(self) -> list[str]:
        return sorted(self._specs)

    def build(self, platform: str, **credentials) -> Connector:
        spec = self.get(platform)
        missing = [f.key for f in spec.credential_fields if f.key not in credentials]
        if missing:
            raise ValueError(f"{platform} is missing credentials: {missing}")
        return spec.factory(**credentials)

    def assert_supports(self, platform: str, metric_key: str) -> None:
        spec = self.get(platform)
        if metric_key not in spec.supported_metrics:
            raise MetricNotSupported(
                f"{platform} cannot produce {metric_key!r}; it supports "
                f"{sorted(spec.supported_metrics)}"
            )

    def platforms_supporting(self, metric_key: str) -> list[str]:
        """Which connected platforms can answer this metric.

        Lets a dashboard render only what is answerable instead of showing a card
        that is permanently blank for one tenant.
        """
        return sorted(p for p, s in self._specs.items() if metric_key in s.supported_metrics)

    def max_backfill_days(self, platform: str) -> int | None:
        return self.get(platform).max_history_days

    def secret_keys(self, platform: str) -> frozenset[str]:
        """Credential keys that must never be logged or returned.

        Centralised so redaction is a property of the registry rather than
        something each platform's code remembers to do.
        """
        return frozenset(f.key for f in self.get(platform).credential_fields if f.secret)

    def redact(self, platform: str, credentials: dict) -> dict:
        secrets = self.secret_keys(platform)
        return {k: ("***" if k in secrets else v) for k, v in credentials.items()}


registry = ConnectorRegistry()


def _register_builtin() -> None:
    """Registered here rather than at import of each module, so the set of
    available platforms is one readable list instead of scattered side effects."""
    from .shopify import ShopifyConnector
    from .shopify_scopes import DEFAULT_ORDER_HISTORY_DAYS

    registry.register(
        ConnectorSpec(
            platform="shopify",
            label="Shopify",
            auth_kind=AuthKind.STATIC_TOKEN,
            rate_limit_style=RateLimitStyle.QUERY_COST_BUCKET,
            supported_metrics=ShopifyConnector.supported_metrics,
            credential_fields=(
                CredentialField("shop_domain", "Shop domain", secret=False,
                                hint="the *.myshopify.com domain, not a custom domain"),
                CredentialField("access_token", "Admin API access token"),
            ),
            factory=ShopifyConnector,
            max_history_days=DEFAULT_ORDER_HISTORY_DAYS,
            supports_backfill=True,
            notes=(
                "Orders beyond 60 days need the approval-gated read_all_orders scope. "
                "Pacing is read from extensions.cost.throttleStatus per response, so the "
                "connector adapts to standard (1000/50) and Plus (2000/100) buckets alike."
            ),
        )
    )


_register_builtin()
