"""Where the live scripts read and write their local secret files.

No script carries a path from anyone's machine. Each file is named by an environment
variable; with the variable unset it is looked for under `.secrets/` in this repository
(which `.gitignore` keeps out of git). A file that is not there is a plain error naming the
variable, never a traceback and never a guess at another location.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / ".secrets"


@dataclass(frozen=True)
class SecretFile:
    env: str
    name: str
    holds: str

    def path(self) -> Path:
        override = os.environ.get(self.env)
        return Path(override) if override else SECRETS_DIR / self.name

    def read(self) -> str:
        try:
            return self.path().read_text(encoding="utf-8")
        except OSError:
            raise SystemExit(
                "cannot read the file for %s: set %s to a file that holds %s, or create .secrets/%s in "
                "the repository (it is never committed)" % (self.name, self.env, self.holds, self.name)
            ) from None

    def write(self, text: str, encoding: str = "utf-8") -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)


DEV_TOKEN = SecretFile(
    "SHOPIFY_DEV_TOKEN_FILE", "shopify_dev_token.txt",
    "two lines, SHOP=<store>.myshopify.com and ACCESS_TOKEN=<an Admin API token for it>")
DEV_APP = SecretFile(
    "SHOPIFY_DEV_APP_FILE", "shopify_dev_app.txt",
    "CLIENT_ID=<the app's client id> and CLIENT_SECRET=<its client secret>")
STOREFRONT_TOKEN = SecretFile(
    "SHOPIFY_STOREFRONT_TOKEN_FILE", "shopify_storefront_token.txt",
    "SHOP=<store>.myshopify.com and STOREFRONT_TOKEN=<a Storefront API token>")
