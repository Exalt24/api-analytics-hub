"""Price watch: the Shopify and Zoho Inventory adapters, against the fake vendors of app/price_watch/fakes.py. No network, no waiting.

These prove the adapters send what the saved vendor pages describe and react to 429, 5xx and 4xx the way the brief says. They do NOT
prove the vendors accept those requests: the fakes were written from the same pages, and no live account has been used.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.price_watch.adapters import (  # noqa: E402
    SHOPIFY_API_VERSION, PriceTarget, ShopifyPriceAdapter, VendorRejected, VendorTransient, ZohoInventoryAdapter, ZohoTokenSource, with_retry)
from app.price_watch.fakes import FakeShopify, FakeZoho  # noqa: E402

TARGET = PriceTarget("KTB-750", "gid://shopify/Product/1001", "gid://shopify/ProductVariant/2001", "4815000000010001", "Kestrel Insulated Bottle 750ml")


def run(coro):
    return asyncio.run(coro)


class Sleeps:
    """Stands in for asyncio.sleep: records the waits and returns at once."""
    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def shopify(fake: FakeShopify | None = None, sleeps: Sleeps | None = None, **kw):
    fake = fake or FakeShopify()
    return fake, ShopifyPriceAdapter(fake.client(), fake.shop_domain, fake.token, sleep=sleeps or Sleeps(), **kw)


def zoho(fake: FakeZoho | None = None, sleeps: Sleeps | None = None):
    fake = fake or FakeZoho()
    sleeps = sleeps or Sleeps()
    client = fake.client()
    tokens = ZohoTokenSource(client, "https://" + fake.accounts_host, fake.client_id, fake.client_secret, fake.refresh_token, sleep=sleeps)
    return fake, ZohoInventoryAdapter(client, tokens, fake.org, "https://" + fake.api_host, sleep=sleeps), tokens


# ---- Shopify ---------------------------------------------------------------------------------------------------------------

def test_shopify_sends_the_documented_mutation_to_the_documented_endpoint():
    fake, adapter = shopify()
    result = run(adapter.set_price(TARGET, 4795, "AUD", "key-1"))
    assert result.vendor == "shopify" and fake.prices == {"gid://shopify/ProductVariant/2001": "47.95"}
    (call,) = fake.calls
    assert call["url"] == "https://demo-shop.myshopify.com/admin/api/%s/graphql.json" % SHOPIFY_API_VERSION
    assert SHOPIFY_API_VERSION == "2026-10"
    assert call["headers"]["x-shopify-access-token"] == fake.token
    assert "productVariantsBulkUpdate(productId: $productId, variants: $variants)" in call["body"]["query"]
    assert call["body"]["variables"] == {"productId": "gid://shopify/Product/1001", "variants": [{"id": "gid://shopify/ProductVariant/2001", "price": "47.95"}]}


def test_shopify_prices_are_decimal_strings_and_zero_decimal_currencies_are_not_scaled():
    fake, adapter = shopify()
    run(adapter.set_price(TARGET, 5, "AUD", "k"))
    run(adapter.set_price(TARGET, 4995, "JPY", "k"))
    assert [c["body"]["variables"]["variants"][0]["price"] for c in fake.calls] == ["0.05", "4995"]


def test_shopify_429_twice_then_success_waits_between_tries():
    fake, adapter = shopify()
    sleeps = Sleeps()
    adapter = ShopifyPriceAdapter(fake.client(), fake.shop_domain, fake.token, sleep=sleeps)
    fake.script((429, {"errors": "Throttled"}, {"Retry-After": "2"}), (429, {"errors": "Throttled"}))
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert len(fake.calls) == 3 and fake.prices
    assert sleeps.waits == [2.0, 1.0]       # the first wait is the vendor's Retry-After, the second is the backoff (0.5 * 2^1)


def test_shopify_throttled_inside_a_200_is_retried_too():
    fake, adapter = shopify()
    fake.script((200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}))
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert len(fake.calls) == 2


def test_shopify_gives_up_after_the_attempts_and_says_it_is_retryable():
    fake, adapter = shopify()
    fake.script(503, 503, 503, 503, 503)
    with pytest.raises(VendorTransient) as exc:
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert "gave up after 4 attempts" in str(exc.value) and len(fake.calls) == 4
    assert fake.prices == {}


def test_shopify_401_is_a_rejection_with_the_vendors_words_and_is_not_retried():
    fake, adapter = shopify()
    adapter = ShopifyPriceAdapter(fake.client(), fake.shop_domain, "wrong-token", sleep=Sleeps())
    with pytest.raises(VendorRejected) as exc:
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert exc.value.status == 401 and "Invalid API key or access token" in exc.value.message
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_shopify_other_4xx_are_rejections_not_retries(status):
    fake, adapter = shopify()
    fake.script((status, {"errors": "nope %d" % status}))
    with pytest.raises(VendorRejected) as exc:
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert exc.value.status == status and len(fake.calls) == 1


def test_shopify_408_is_retried_like_a_429():
    fake, adapter = shopify()
    fake.script(408)
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert len(fake.calls) == 2


def test_shopify_user_errors_in_a_200_are_a_rejection_with_the_message():
    fake, adapter = shopify()
    bad = PriceTarget("X", "gid://shopify/Product/1", "gid://shopify/ProductVariant/1")
    with pytest.raises(VendorRejected) as exc:
        run(adapter.set_price(bad, -5, "AUD", "k"))
    assert "Price must be greater than or equal to 0" in exc.value.message and "variants.0.price" in exc.value.message


def test_shopify_a_200_that_does_not_echo_the_new_price_is_not_recorded_as_success():
    fake, adapter = shopify()
    fake.script((200, {"data": {"productVariantsBulkUpdate": {"productVariants": [{"id": "gid://shopify/ProductVariant/2001", "price": "49.95"}], "userErrors": []}}}))
    with pytest.raises(VendorRejected, match="did not confirm"):
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))


def test_a_dropped_connection_is_transient():
    def boom(request):
        raise httpx.ConnectError("refused")
    adapter = ShopifyPriceAdapter(httpx.AsyncClient(transport=httpx.MockTransport(boom)), "demo-shop.myshopify.com", "t", sleep=Sleeps())
    with pytest.raises(VendorTransient, match="connection failed"):
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))


def test_shopify_needs_a_myshopify_domain_and_a_dry_run_description_has_no_token():
    with pytest.raises(ValueError):
        ShopifyPriceAdapter(httpx.AsyncClient(), "example.com", "t")
    fake, adapter = shopify()
    plan = adapter.describe(TARGET, 4795, "AUD")
    assert fake.calls == [] and plan["variables"]["variants"][0]["price"] == "47.95" and fake.token not in str(plan)


# ---- Zoho --------------------------------------------------------------------------------------------------------------------

def test_zoho_refreshes_a_token_then_puts_the_rate_the_documented_way():
    fake, adapter, tokens = zoho()
    result = run(adapter.set_price(TARGET, 4795, "AUD", "key-1"))
    assert result.vendor == "zoho" and fake.rates == {"4815000000010001": 47.95}
    token_call, put = fake.calls
    assert token_call["method"] == "POST" and token_call["url"].startswith("https://accounts.zoho.com.au/oauth/v2/token?")
    assert token_call["query"]["grant_type"] == "refresh_token" and token_call["query"]["refresh_token"] == fake.refresh_token
    assert put["method"] == "PUT"
    assert put["url"].startswith("https://www.zohoapis.com.au/inventory/v1/items/4815000000010001?")      # the .com.au data centre
    assert put["query"] == {"organization_id": "10234695"}
    assert put["headers"]["authorization"] == "Zoho-oauthtoken 1000.invented.access.1"
    assert put["body"] == {"name": "Kestrel Insulated Bottle 750ml", "rate": 47.95}


def test_zoho_reuses_the_token_until_it_is_about_to_expire():
    fake, adapter, tokens = zoho()
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    run(adapter.set_price(TARGET, 4700, "AUD", "k"))
    assert fake.tokens_issued == 1 and tokens.refreshes == 1
    now = [0.0]
    client = fake.client()
    clocked = ZohoTokenSource(client, "https://" + fake.accounts_host, fake.client_id, fake.client_secret, fake.refresh_token, clock=lambda: now[0])
    run(clocked.token())
    now[0] = 3539          # a token lives 3600 seconds and is renewed a minute early, so this is still inside
    run(clocked.token())
    assert clocked.refreshes == 1
    now[0] = 3540
    run(clocked.token())
    assert clocked.refreshes == 2


def test_zoho_429_then_success_backs_off():
    sleeps = Sleeps()
    fake, adapter, tokens = zoho(sleeps=sleeps)
    run(tokens.token())                                           # a real token first, so the scripted answers hit the item calls
    fake.script((429, {"code": 44, "message": "too many requests per minute"}), (429, {"code": 44, "message": "too many requests per minute"}))
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert fake.rates == {"4815000000010001": 47.95}
    assert sleeps.waits == [0.5, 1.0]                             # no Retry-After header, so base * 2^n


def test_zoho_expired_token_is_refreshed_once_and_the_call_goes_through():
    fake, adapter, tokens = zoho()
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    fake.expire_tokens()           # the vendor stops honouring the cached token before our clock thinks it has expired
    run(adapter.set_price(TARGET, 4700, "AUD", "k"))
    assert fake.tokens_issued == 2 and fake.rates["4815000000010001"] == 47.0


def test_zoho_persistent_401_is_rejected_after_one_refresh():
    fake = FakeZoho()
    original = fake._handle

    def items_always_401(request):
        if request.url.path.startswith("/inventory/"):
            fake.calls.append({"method": request.method, "url": str(request.url)})
            return httpx.Response(401, json={"code": 57, "message": "You are not authorized to perform this operation"})
        return original(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(items_always_401))
    tokens = ZohoTokenSource(client, "https://" + fake.accounts_host, fake.client_id, fake.client_secret, fake.refresh_token, sleep=Sleeps())
    adapter = ZohoInventoryAdapter(client, tokens, fake.org, "https://" + fake.api_host, sleep=Sleeps())
    with pytest.raises(VendorRejected) as exc:
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert exc.value.status == 401 and "not authorized" in exc.value.message
    puts = [c for c in fake.calls if c.get("method") == "PUT"]
    assert len(puts) == 2 and fake.tokens_issued == 2             # one try, one refresh, one more try, then it stops


def test_zoho_refused_refresh_in_a_200_is_a_rejection_not_a_missing_token():
    fake = FakeZoho(refresh_token="1000.the.right.one")
    client = fake.client()
    tokens = ZohoTokenSource(client, "https://" + fake.accounts_host, fake.client_id, fake.client_secret, "1000.a.wrong.one", sleep=Sleeps())
    adapter = ZohoInventoryAdapter(client, tokens, fake.org, "https://" + fake.api_host, sleep=Sleeps())
    with pytest.raises(VendorRejected, match="token refresh refused: invalid_code"):
        run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    assert fake.rates == {}


def test_zoho_a_nonzero_code_in_a_200_is_a_rejection():
    fake, adapter, _ = zoho()
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))              # token cached
    fake.script((200, {"code": 1001, "message": "Rate is invalid"}))
    with pytest.raises(VendorRejected, match="code 1001: Rate is invalid"):
        run(adapter.set_price(TARGET, 4700, "AUD", "k"))


def test_zoho_4xx_other_than_408_and_429_is_a_rejection_with_the_vendors_message():
    fake, adapter, _ = zoho()
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    fake.script((400, {"code": 2, "message": "Invalid value passed for rate"}))
    with pytest.raises(VendorRejected) as exc:
        run(adapter.set_price(TARGET, 4700, "AUD", "k"))
    assert exc.value.status == 400 and exc.value.message == "Invalid value passed for rate"


def test_zoho_5xx_exhaustion_is_transient():
    fake, adapter, _ = zoho()
    run(adapter.set_price(TARGET, 4795, "AUD", "k"))
    fake.script(500, 502, 503, 504)
    with pytest.raises(VendorTransient):
        run(adapter.set_price(TARGET, 4700, "AUD", "k"))


def test_zoho_from_env_picks_the_australian_domains_and_refuses_an_unknown_region():
    env = {"ZOHO_REGION": "com.au", "ZOHO_CLIENT_ID": "i", "ZOHO_CLIENT_SECRET": "s", "ZOHO_REFRESH_TOKEN": "r", "ZOHO_ORGANIZATION_ID": "99"}
    adapter = ZohoInventoryAdapter.from_env(httpx.AsyncClient(), env)
    assert adapter.describe(TARGET, 4795, "AUD")["url"] == "https://www.zohoapis.com.au/inventory/v1/items/4815000000010001"
    assert adapter._tokens._accounts == "https://accounts.zoho.com.au"
    with pytest.raises(ValueError, match="com.au"):
        ZohoInventoryAdapter.from_env(httpx.AsyncClient(), {**env, "ZOHO_REGION": "mars"})
    with pytest.raises(KeyError):
        ZohoInventoryAdapter.from_env(httpx.AsyncClient(), {"ZOHO_REGION": "com.au"})


def test_applies_to_needs_both_ids_of_a_shop():
    _, s = shopify()
    _, z, _ = zoho()
    assert s.applies_to(TARGET) and z.applies_to(TARGET)
    assert not s.applies_to(PriceTarget("X", "gid://shopify/Product/1", None))
    assert not z.applies_to(PriceTarget("X", zoho_item_id="1"))     # the update needs the item's name too


def test_with_retry_caps_the_wait():
    sleeps = Sleeps()
    calls = []

    async def op():
        calls.append(1)
        raise VendorTransient("busy", retry_after=9999)

    with pytest.raises(VendorTransient):
        run(with_retry(op, sleep=sleeps, max_attempts=3))
    assert len(calls) == 3 and sleeps.waits == [30.0, 30.0]
