"""Shopify OAuth 2.0 install flow, yielding an OFFLINE access token.

WHY NOT `shopify store auth`. That CLI command stores an ONLINE access token,
which expires after 24 hours and dies when the user logs out. It exists for
interactive commands. A scheduled sync needs an OFFLINE token, which is granted to
the app rather than to a person, and that is what this implements.

The distinction is in the authorization URL: Shopify issues an offline token by
DEFAULT, and only issues an online one if `grant_options[]=per-user` is present.
So the correct move is to omit that parameter, and the safest way to guarantee it
stays omitted is to never add it, which a test pins.

FOUR THINGS THIS GETS RIGHT that a copied snippet usually does not:

  1. STATE IS COMPARED IN CONSTANT TIME and is single-use. A reused or guessable
     state is CSRF: an attacker gets your app installed on THEIR store and your
     callback happily stores the token.

  2. THE SHOP DOMAIN IS VALIDATED against Shopify's own rules before any redirect.
     The callback receives `shop` as a query parameter, and an unvalidated value is
     an open redirect and an SSRF vector, because the next thing the code does is
     make an HTTPS request to it.

  3. THE HMAC ON THE CALLBACK IS VERIFIED. Shopify signs the callback query string.
     Skipping it means accepting a forged callback, and it is skipped in most
     tutorials because the flow appears to work without it.

  4. THE TOKEN RESPONSE IS TREATED AS POSSIBLY EXPIRING. Under the newer regime an
     offline token can carry `expires_in` and a refresh token, so the exchange
     returns a TokenSet with an ABSOLUTE expiry rather than assuming permanence.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping

import httpx

from .tokens import TokenSet

#: Shopify's own rule for a shop domain. Anchored, so `evil.com/?x=.myshopify.com`
#: cannot pass, and length-bounded because the value ends up in a URL we fetch.
SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]\.myshopify\.com$", re.I)


class OAuthError(Exception):
    pass


def validate_shop_domain(shop: str) -> str:
    """Normalise and validate, or raise.

    Called before the authorize redirect AND again on the callback. The callback
    value is attacker-controlled, and the code goes on to POST to that host, so an
    unvalidated value is server-side request forgery, not merely untidy.
    """
    if not shop:
        raise OAuthError("no shop domain given")
    cleaned = shop.strip().lower()
    cleaned = cleaned.removeprefix("https://").removeprefix("http://").rstrip("/")
    if not SHOP_DOMAIN_RE.match(cleaned):
        raise OAuthError(f"not a valid myshopify.com domain: {shop!r}")
    return cleaned


def verify_callback_hmac(params: Mapping[str, str], client_secret: str) -> None:
    """Verify the signature Shopify puts on the callback query string.

    The rule: remove `hmac` (and the legacy `signature`), sort the rest, join as
    `k=v` with `&`, and HMAC-SHA256 with the client secret. Percent-encoding of the
    remaining values is preserved as received, which is why this takes the parsed
    mapping rather than re-encoding anything.
    """
    provided = params.get("hmac")
    if not provided:
        raise OAuthError("callback has no hmac")
    if not provided.isascii():
        # hmac.compare_digest raises TypeError on non-ascii str, which would turn a
        # forged callback into a 500 instead of a rejection.
        raise OAuthError("hmac is not ascii")

    message = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature")
    )
    expected = hmac.new(
        client_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise OAuthError("callback hmac mismatch")


@dataclass
class ShopifyOAuth:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    #: Issued states awaiting a callback, mapped to when they were issued. Single
    #: use: consumed on the first successful callback.
    _pending: dict[str, float] = field(default_factory=dict, repr=False)
    state_ttl_seconds: float = 600.0

    def authorize_url(self, shop: str) -> tuple[str, str]:
        """Build the install URL. Returns (url, state).

        NOTE the absence of `grant_options[]`. Adding it would request a per-user
        ONLINE token, which expires in 24 hours and is useless for a background
        sync. A test asserts it stays absent.
        """
        shop = validate_shop_domain(shop)
        state = secrets.token_urlsafe(32)
        self._pending[state] = time.time()

        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "scope": self.scopes,
            "redirect_uri": self.redirect_uri,
            "state": state,
        })
        return f"https://{shop}/admin/oauth/authorize?{query}", state

    def _consume_state(self, provided: str) -> None:
        if not provided:
            raise OAuthError("callback has no state")
        # Constant-time match against each pending state, then single-use removal.
        # A plain dict lookup would be a timing oracle on the state value.
        match = None
        for known in self._pending:
            if len(known) == len(provided) and hmac.compare_digest(known, provided):
                match = known
                break
        if match is None:
            raise OAuthError("unknown or already-used state (possible CSRF)")
        issued = self._pending.pop(match)
        if time.time() - issued > self.state_ttl_seconds:
            raise OAuthError("state expired")

    async def exchange(
        self, params: Mapping[str, str], client: httpx.AsyncClient | None = None
    ) -> tuple[str, TokenSet]:
        """Validate a callback and exchange the code. Returns (shop, TokenSet)."""
        shop = validate_shop_domain(params.get("shop", ""))
        verify_callback_hmac(params, self.client_secret)
        self._consume_state(params.get("state", ""))

        code = params.get("code")
        if not code:
            raise OAuthError("callback has no code")

        owned = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        try:
            r = await client.post(
                f"https://{shop}/admin/oauth/access_token",
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
            )
        finally:
            if owned:
                await client.aclose()

        if r.status_code != 200:
            raise OAuthError(f"token exchange failed: {r.status_code} {r.text[:200]}")

        payload = r.json()
        token = payload.get("access_token")
        if not token:
            raise OAuthError("token exchange returned no access_token")

        # An offline token MAY now carry an expiry and a refresh token. Convert the
        # relative expires_in to an ABSOLUTE instant here, at the only moment we
        # know what "now" was, because a relative value is wrong as soon as it is
        # persisted and read back.
        expires_in = payload.get("expires_in")
        return shop, TokenSet(
            access_token=token,
            expires_at=(time.time() + float(expires_in)) if expires_in else None,
            refresh_token=payload.get("refresh_token"),
        )
