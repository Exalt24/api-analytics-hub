"""The connector contract.

Deliberately small and boring: fetch a window, normalize, hand back canonical
rows. Adding a platform means writing one class and registering it, never
touching the scheduler, the storage layer or the dashboard.

WHY THE CONTRACT IS SHAPED THIS WAY. The claim "easy to add connectors later"
only holds if the canonical vocabulary is decided before the SECOND connector
lands. Otherwise the first platform's field names leak into the core, the second
platform reveals it, and by the third it is expensive to unpick. So a connector
is not allowed to return its own field names at all: it returns
`CanonicalPoint`, and translation is its whole job.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator, Mapping, Sequence


class ConnectorError(Exception):
    """Base for failures a connector can explain to the sync runner."""

    #: Whether re-running the same window later could succeed. A 429 or a 503 is
    #: retryable; a revoked token or a malformed mapping is not, and retrying it
    #: just burns quota while looking like progress.
    retryable = False
    code = "connector_error"


class RateLimited(ConnectorError):
    retryable = True
    code = "rate_limited"

    def __init__(self, retry_after_seconds: float | None = None, detail: str = ""):
        super().__init__(detail or "rate limited")
        self.retry_after_seconds = retry_after_seconds


class AuthExpired(ConnectorError):
    """The credential needs refreshing. Retryable, but only after a refresh."""

    retryable = True
    code = "auth_expired"


class AuthRevoked(ConnectorError):
    """The user removed the app. Retrying cannot fix this, a human must reconnect."""

    retryable = False
    code = "auth_revoked"


class ProtectedDataDenied(ConnectorError):
    """The platform serves the endpoint but withholds a class of field.

    Distinct from AuthRevoked on purpose. Shopify grants scopes at install and
    protected customer data separately, per app, so a connector can hold
    read_orders and still be refused the Order object. Retrying is pointless and
    reconnecting is pointless; a declaration has to be completed by the app
    developer. Recorded as its own code so a dashboard can say which of those two
    humans has to act.

    Not retryable, but a caller MAY choose to continue without the affected
    metric, which is what the Shopify connector does.
    """

    retryable = False
    code = "protected_data_denied"


class UpstreamBroken(ConnectorError):
    """The platform returned something we cannot parse. Not retryable silently:
    a shape change must surface, because the alternative is charting nulls."""

    retryable = False
    code = "upstream_broken"


@dataclass(frozen=True)
class CanonicalPoint:
    """One fact, already translated out of the vendor's vocabulary.

    `observed_on` is the date the PLATFORM attributes the value to, never the
    date we fetched. A sync that runs late must not create a phantom flat day,
    and a backfill must land on the day it belongs to.
    """

    metric_key: str
    observed_on: date
    value_numeric: int
    currency: str | None = None
    raw: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        # Money without a currency is the defect this catches: two platforms both
        # saying "cost" while one means EUR is worse than them using different
        # words, because it charts without complaint.
        if self.metric_key in MONEY_METRICS and not self.currency:
            raise UpstreamBroken(f"{self.metric_key} is money and needs a currency")
        if self.currency and self.metric_key not in MONEY_METRICS:
            raise UpstreamBroken(f"{self.metric_key} is not money but carries a currency")
        if not isinstance(self.value_numeric, int):
            # Floats are rejected at the boundary rather than rounded quietly.
            raise UpstreamBroken(
                f"{self.metric_key} must be an integer in minor units, got "
                f"{type(self.value_numeric).__name__}"
            )


MONEY_METRICS = frozenset({"gross_sales", "refunds", "net_sales", "avg_order_value"})


@dataclass
class FetchResult:
    """What one page of a fetch produced, plus how to resume."""

    points: Sequence[CanonicalPoint] = field(default_factory=list)
    #: Opaque continuation token. Stored on the sync run so a rate limit halfway
    #: through a window resumes rather than discarding completed work.
    cursor: str | None = None
    api_calls: int = 0
    throttle_waits: int = 0


class Connector(abc.ABC):
    """Implement this and register it. Nothing else changes."""

    #: Stable identifier stored on platform_connection.platform.
    platform: str

    #: Which canonical metrics this connector can produce. The registry uses it to
    #: reject a dashboard asking a platform for a metric it cannot supply, rather
    #: than returning an empty chart that looks like zero.
    supported_metrics: frozenset[str]

    @abc.abstractmethod
    async def fetch(
        self,
        *,
        window_start: date,
        window_end: date,
        cursor: str | None = None,
    ) -> AsyncIterator[FetchResult]:
        """Yield pages of canonical points for the window.

        Implementations MUST:
          * pace themselves from what the platform reports about remaining
            budget, never from a hardcoded constant (see shopify.py for why);
          * raise RateLimited rather than sleeping unboundedly, so the runner
            owns the retry policy and it is visible in one place;
          * return a cursor with every page, so a failure is resumable.
        """
        raise NotImplementedError

    async def verify(self) -> str:
        """Cheapest possible authenticated call, returning a human label.

        Used at connect time so a bad credential fails in front of the person who
        just pasted it, rather than six hours later inside a scheduled job.
        """
        raise NotImplementedError
