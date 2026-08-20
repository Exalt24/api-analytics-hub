#!/usr/bin/env python
"""Run the OAuth install and capture an OFFLINE access token.

Starts a local callback listener, prints the install URL, waits for the redirect,
verifies it, exchanges the code, and writes the token to the secrets file.

Deliberately NOT using `shopify store auth`, which stores an ONLINE token: 24
hours, dies on logout, useless for a scheduled sync. This flow omits
`grant_options[]=per-user`, which is what makes Shopify issue an offline token.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.shopify_oauth import OAuthError, ShopifyOAuth
from app.connectors.shopify_scopes import dev_store_scope_string

SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_app.txt")
TOKEN_OUT = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")
PORT = 8765
STORE = "dac-dev-store.myshopify.com"


def read_creds() -> dict:
    out = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    for required in ("CLIENT_ID", "CLIENT_SECRET"):
        if not out.get(required):
            raise SystemExit(f"{required} missing from {SECRETS}")
    return out


class Captured:
    params: dict | None = None
    error: str | None = None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return
        Captured.params = {
            k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h2>Received. You can close this tab.</h2>"
            b"<p>Back to the terminal for the result.</p>"
        )

    def log_message(self, *args):
        # The callback query string contains the code. Keeping it out of stdout
        # matters: a terminal scrollback is a place secrets get pasted from.
        pass


async def main() -> int:
    creds = read_creds()
    scopes = dev_store_scope_string()

    oauth = ShopifyOAuth(
        client_id=creds["CLIENT_ID"],
        client_secret=creds["CLIENT_SECRET"],
        redirect_uri=f"http://localhost:{PORT}/callback",
        scopes=scopes,
    )

    url, _state = oauth.authorize_url(STORE)

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"listening on http://localhost:{PORT}/callback")
    print(f"scopes requested: {len(scopes.split(','))}")
    print()
    print("OPEN THIS AND CLICK INSTALL:")
    print()
    print(url)
    print()

    for _ in range(600):  # up to ~5 minutes
        if Captured.params is not None:
            break
        await asyncio.sleep(0.5)
    server.shutdown()

    if Captured.params is None:
        print("timed out waiting for the callback")
        return 1

    if "error" in Captured.params:
        print("Shopify returned an error:", Captured.params)
        return 1

    try:
        shop, tokens = await oauth.exchange(Captured.params)
    except OAuthError as exc:
        print("callback rejected:", exc)
        return 1

    TOKEN_OUT.write_text(
        "Shopify OFFLINE access token (dac-dev-store)\n"
        "Obtained via the app's own OAuth flow, NOT `shopify store auth` (which\n"
        "issues a 24-hour ONLINE token). Offline because grant_options[] was omitted.\n\n"
        f"SHOP={shop}\n"
        f"ACCESS_TOKEN={tokens.access_token}\n"
        f"EXPIRES_AT={tokens.expires_at or 'never'}\n"
        f"REFRESH_TOKEN={tokens.refresh_token or 'none'}\n",
        encoding="utf-8",
    )

    print("SUCCESS")
    print(f"  shop        : {shop}")
    print(f"  token       : {tokens.access_token[:12]}... ({len(tokens.access_token)} chars)")
    print(f"  expires_at  : {tokens.expires_at or 'never (non-expiring offline token)'}")
    print(f"  refreshable : {tokens.is_refreshable}")
    print(f"  written to  : {TOKEN_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
