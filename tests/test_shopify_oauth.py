"""OAuth install flow tests.

The two that matter most: that the URL never requests a per-user token (which
would silently give us a 24-hour token instead of an offline one, and the flow
would appear to work until the next morning), and that the shop domain is
validated, since the callback value is attacker-controlled and the code then makes
an HTTPS request to it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.shopify_oauth import (
    OAuthError,
    ShopifyOAuth,
    validate_shop_domain,
    verify_callback_hmac,
)

SECRET = "shpss_clientsecret"
SHOP = "dac-dev-store.myshopify.com"


def oauth() -> ShopifyOAuth:
    return ShopifyOAuth(
        client_id="cid",
        client_secret=SECRET,
        redirect_uri="http://localhost:8765/callback",
        scopes="read_orders,read_customers",
    )


def signed(params: dict) -> dict:
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params = dict(params)
    params["hmac"] = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return params


# ---------------------------------------------------------------- domain guard

@pytest.mark.parametrize("good", [
    "dac-dev-store.myshopify.com",
    "https://dac-dev-store.myshopify.com",
    "DAC-DEV-STORE.MYSHOPIFY.COM",
    "dac-dev-store.myshopify.com/",
])
def test_valid_domains_normalise(good):
    assert validate_shop_domain(good) == SHOP


@pytest.mark.parametrize("bad", [
    "",
    "evil.com",
    "evil.com/?x=.myshopify.com",
    "dac-dev-store.myshopify.com.evil.com",
    "myshopify.com",
    "-leadinghyphen.myshopify.com",
    "http://internal-metadata/latest/meta-data",
    "localhost",
])
def test_invalid_domains_are_REFUSED(bad):
    """The callback's shop value is attacker-controlled and the next thing the code
    does is POST to that host, so this is SSRF prevention, not tidiness."""
    with pytest.raises(OAuthError):
        validate_shop_domain(bad)


# ---------------------------------------------------------------- offline token

def test_the_authorize_url_does_NOT_request_a_per_user_token():
    """THE test. `grant_options[]=per-user` yields an ONLINE token that expires in
    24 hours. Its absence is what makes the token offline, and the flow would
    appear to work perfectly until the next morning if this regressed.
    """
    url, _ = oauth().authorize_url(SHOP)
    assert "grant_options" not in url, "requesting per-user would give a 24h online token"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["client_id"] == ["cid"]
    assert q["scope"] == ["read_orders,read_customers"]
    assert q["redirect_uri"] == ["http://localhost:8765/callback"]
    assert len(q["state"][0]) >= 32, "state must be long enough not to be guessable"


def test_the_authorize_url_targets_the_validated_shop():
    url, _ = oauth().authorize_url("HTTPS://DAC-DEV-STORE.MYSHOPIFY.COM/")
    assert url.startswith(f"https://{SHOP}/admin/oauth/authorize?")


def test_each_authorize_call_issues_a_distinct_state():
    o = oauth()
    _, s1 = o.authorize_url(SHOP)
    _, s2 = o.authorize_url(SHOP)
    assert s1 != s2


# ---------------------------------------------------------------- callback hmac

def test_a_correctly_signed_callback_verifies():
    verify_callback_hmac(signed({"shop": SHOP, "code": "abc", "state": "s"}), SECRET)


def test_an_unsigned_callback_is_refused():
    with pytest.raises(OAuthError):
        verify_callback_hmac({"shop": SHOP, "code": "abc"}, SECRET)


def test_a_tampered_callback_is_refused():
    params = signed({"shop": SHOP, "code": "abc", "state": "s"})
    params["code"] = "attacker-code"
    with pytest.raises(OAuthError):
        verify_callback_hmac(params, SECRET)


def test_a_non_ascii_hmac_is_refused_rather_than_raising():
    params = signed({"shop": SHOP, "code": "abc", "state": "s"})
    params["hmac"] = "é" * 64
    with pytest.raises(OAuthError):
        verify_callback_hmac(params, SECRET)


def test_the_signature_field_is_excluded_from_the_message():
    """Shopify's legacy `signature` param must not be part of the signed string."""
    base = {"shop": SHOP, "code": "abc", "state": "s"}
    params = signed(base)
    params["signature"] = "legacy-noise"
    verify_callback_hmac(params, SECRET)


# ---------------------------------------------------------------- state / CSRF

def test_an_unknown_state_is_refused_as_csrf():
    o = oauth()
    o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": "never-issued"})
    with pytest.raises(OAuthError, match="CSRF"):
        asyncio.run(o.exchange(params))


def test_a_state_cannot_be_replayed():
    """Single use. A replayable state lets a captured callback be resubmitted."""
    o = oauth()
    _, state = o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": state})

    def handler(req):
        return httpx.Response(200, json={"access_token": "shpat_offline"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shop, tokens = asyncio.run(o.exchange(params, client=client))
    assert shop == SHOP
    assert tokens.access_token == "shpat_offline"

    # Second use of the same state must fail.
    with pytest.raises(OAuthError):
        asyncio.run(o.exchange(params, client=client))


def test_an_expired_state_is_refused():
    o = oauth()
    o.state_ttl_seconds = 0.0
    _, state = o.authorize_url(SHOP)
    time.sleep(0.01)
    params = signed({"shop": SHOP, "code": "abc", "state": state})
    with pytest.raises(OAuthError, match="expired"):
        asyncio.run(o.exchange(params))


# ---------------------------------------------------------------- exchange

def test_a_non_expiring_offline_token_has_no_expiry():
    o = oauth()
    _, state = o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": state})
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"access_token": "shpat_x", "scope": "read_orders"})
    ))
    _, tokens = asyncio.run(o.exchange(params, client=client))
    assert tokens.expires_at is None
    assert tokens.refresh_token is None


def test_an_expiring_offline_token_gets_an_ABSOLUTE_expiry():
    """Under the newer regime an offline token carries expires_in plus a refresh
    token. Converting to an absolute instant here is the only moment we know what
    'now' was; a relative value is wrong as soon as it is persisted."""
    o = oauth()
    _, state = o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": state})
    before = time.time()
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "access_token": "shpat_x", "expires_in": 3600, "refresh_token": "shprt_y",
        })
    ))
    _, tokens = asyncio.run(o.exchange(params, client=client))
    assert tokens.refresh_token == "shprt_y"
    assert tokens.expires_at is not None
    assert before + 3500 < tokens.expires_at < before + 3700


def test_a_failed_exchange_raises_with_the_status():
    o = oauth()
    _, state = o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": state})
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(400, text="invalid_grant")
    ))
    with pytest.raises(OAuthError, match="400"):
        asyncio.run(o.exchange(params, client=client))


def test_a_token_response_with_no_access_token_raises():
    o = oauth()
    _, state = o.authorize_url(SHOP)
    params = signed({"shop": SHOP, "code": "abc", "state": state})
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"scope": "read_orders"})
    ))
    with pytest.raises(OAuthError, match="no access_token"):
        asyncio.run(o.exchange(params, client=client))


def test_a_callback_naming_a_different_shop_is_still_validated():
    """An attacker substituting their own shop must not sail through domain
    validation just because the rest of the callback is well-formed."""
    with pytest.raises(OAuthError):
        asyncio.run(oauth().exchange(signed({"shop": "evil.com", "code": "c", "state": "s"})))
