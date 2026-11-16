"""A Playwright page from local Chromium or from a Browserbase session, behind one context manager.

`open_page("local")` starts Chromium with a DevTools port (the address is `cdp_url`, for tools that can attach). `open_page("browserbase", ...)` creates a session through the
official `browserbase` SDK, connects Playwright to its `connect_url` over CDP, and asks for the session to be released when the block ends, including when the block raised.
`base_url` points the SDK at the stand-in server in fake_browserbase.py for the tests; leave it out for the real service.

SESSIONS THAT DO NOT LOOK ALIKE. Every session picks its own user agent and viewport (`pick_fingerprint`, seeded for a test), and a `ProxyRotator` hands each new session the next
proxy in its list: round robin, a proxy that failed is skipped for a cool-down, and a list with every proxy cooling down is an error rather than a silent direct connection.
Locally the proxy goes to Chromium's launch options; on Browserbase it goes in the session's `proxies` setting, which the SDK documents. Typing at a human pace is formfill's
`typing_delay`. This is about not being the same browser a hundred times over, and it does nothing about a CAPTCHA, which stays a person's.

A page is handed out with one rule: nothing here submits anything. Submitting is formfill's `submit` flag, off unless the caller turns it on.
"""
from __future__ import annotations

import contextlib
import random
import socket
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from playwright.async_api import Page, async_playwright

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
]
VIEWPORTS = [(1280, 800), (1366, 768), (1440, 900), (1536, 864), (1920, 1080)]


class NoProxyAvailable(RuntimeError):
    pass


class ProxyRotator:
    def __init__(self, proxies: list[str], *, cooldown: float = 300.0, clock: Callable[[], float] = time.monotonic):
        if not proxies:
            raise ValueError("a rotator needs at least one proxy")
        self._proxies = list(proxies)
        self._cooldown = cooldown
        self._clock = clock
        self._next = 0
        self._bad_until: dict[str, float] = {}

    def pick(self) -> str:
        now = self._clock()
        for _ in range(len(self._proxies)):
            p = self._proxies[self._next % len(self._proxies)]
            self._next += 1
            if self._bad_until.get(p, 0.0) <= now:
                return p
        raise NoProxyAvailable("every proxy is cooling down after a failure")

    def failed(self, proxy: str) -> None:
        """The proxy refused or timed out: skip it until the cool-down is over."""
        self._bad_until[proxy] = self._clock() + self._cooldown


def pick_fingerprint(rng: random.Random | None = None) -> dict[str, Any]:
    r = rng or random
    w, h = r.choice(VIEWPORTS)
    return {"user_agent": r.choice(USER_AGENTS), "viewport": {"width": w, "height": h}}


@dataclass
class OpenPage:
    page: Page
    mode: str
    cdp_url: str
    session_id: str | None = None
    proxy: str | None = None
    fingerprint: dict[str, Any] | None = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proxy_option(url: str) -> dict[str, str]:
    """`http://user:pass@host:port` as the proxy object Playwright takes."""
    from urllib.parse import urlsplit

    u = urlsplit(url)
    out = {"server": "%s://%s:%d" % (u.scheme or "http", u.hostname, u.port or 80)}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out


@contextlib.asynccontextmanager
async def open_page(mode: str = "local", *, browserbase_api_key: str | None = None, browserbase_project_id: str | None = None,
                    browserbase_base_url: str | None = None, headless: bool = True, proxies: ProxyRotator | None = None,
                    rng: random.Random | None = None) -> AsyncIterator[OpenPage]:
    fp = pick_fingerprint(rng)
    proxy = proxies.pick() if proxies is not None else None
    async with async_playwright() as pw:
        if mode == "local":
            port = _free_port()
            # Chromium never sends a loopback address through a proxy unless told to, which would make a proxy test against a local page pass without using the proxy
            args = ["--remote-debugging-port=%d" % port, "--proxy-bypass-list=<-loopback>"] if proxy else ["--remote-debugging-port=%d" % port]
            kwargs: dict[str, Any] = {"headless": headless, "args": args}
            if proxy:
                kwargs["proxy"] = _proxy_option(proxy)
            browser = await pw.chromium.launch(**kwargs)
            try:
                ctx = await browser.new_context(user_agent=fp["user_agent"], viewport=fp["viewport"])
                page = await ctx.new_page()
                yield OpenPage(page, "local", "http://127.0.0.1:%d" % port, None, proxy, fp)
            finally:
                await browser.close()
            return
        if mode != "browserbase":
            raise ValueError("mode must be 'local' or 'browserbase'")
        if not browserbase_api_key:
            raise ValueError("a Browserbase API key is required")
        from browserbase import Browserbase

        kwargs = {"api_key": browserbase_api_key}
        if browserbase_base_url:
            kwargs["base_url"] = browserbase_base_url
        bb = Browserbase(**kwargs)
        create: dict[str, Any] = {}
        if browserbase_project_id:
            create["project_id"] = browserbase_project_id
        if proxy:
            create["proxies"] = [dict(_proxy_option(proxy), type="external")]
        session = bb.sessions.create(**create)
        try:
            browser = await pw.chromium.connect_over_cdp(session.connect_url)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                yield OpenPage(page, "browserbase", session.connect_url, session.id, proxy, fp)
            finally:
                await browser.close()
        finally:
            try:
                bb.sessions.update(session.id, status="REQUEST_RELEASE", **({"project_id": browserbase_project_id} if browserbase_project_id else {}))
            except Exception:  # the release is best effort: the service ends an idle session by itself
                pass
