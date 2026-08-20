#!/usr/bin/env python
"""Manage Shopify URL redirects through the Admin API.

WHY THIS IS A REAL TASK AND NOT BUSYWORK. Technical SEO on a catalogue store is
mostly redirects: a product is renamed, a collection is merged, a supplier's part
number changes, and every inbound link and every indexed URL breaks. Doing that by
hand in the admin is fine for three and impossible for three hundred, which is what
a catalogue migration actually looks like.

THREE THINGS THIS REFUSES TO DO, each because the careless version breaks a store:

  1. A REDIRECT LOOP. `/a` to `/b` while `/b` to `/a` already exists takes the page
     out of the index entirely and no error is raised at creation time. Checked
     before every write.
  2. A SELF-REDIRECT. `/a` to `/a` is accepted by the API and serves an infinite
     loop to a crawler.
  3. A SILENT OVERWRITE. Shopify rejects a duplicate path with a userErrors entry
     rather than an exception, so a script that only checks HTTP status reports
     success while changing nothing. Existing paths are read first and reported as
     skipped rather than failed, because a redirect that already points where you
     want is not a problem.

Redirects are also the one SEO change that must be REVERSIBLE, so every write is
logged with the id needed to undo it.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

SECRETS = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")
API = "2026-07"

LIST_REDIRECTS = """
query Redirects($first: Int!, $after: String) {
  urlRedirects(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id path target }
  }
}
"""

CREATE_REDIRECT = """
mutation Create($redirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $redirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""

DELETE_REDIRECT = """
mutation Delete($id: ID!) {
  urlRedirectDelete(id: $id) { deletedRedirectId userErrors { field message } }
}
"""


def creds() -> tuple[str, str]:
    text = io.open(SECRETS, encoding="utf-8").read()
    return (re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip(),
            re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip())


async def gql(client, shop, token, query, variables=None):
    r = await client.post(
        "https://%s/admin/api/%s/graphql.json" % (shop, API),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        content=json.dumps({"query": query, "variables": variables or {}}),
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("GraphQL errors: %s" % body["errors"][:2])
    return body["data"]


def normalise(path: str) -> str:
    """Compare paths the way Shopify stores them: leading slash, no trailing one."""
    p = "/" + path.strip().strip("/")
    return p.lower()


async def fetch_all(client, shop, token) -> list[dict]:
    out, after = [], None
    while True:
        data = await gql(client, shop, token, LIST_REDIRECTS,
                         {"first": 250, "after": after})
        block = data["urlRedirects"]
        out.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            return out
        after = block["pageInfo"]["endCursor"]


def check(pairs: list[tuple[str, str]], existing: list[dict]) -> list[tuple[str, str, str]]:
    """Return (path, target, reason) for every pair that must NOT be written."""
    by_path = {normalise(r["path"]): normalise(r["target"]) for r in existing}
    refused = []
    for path, target in pairs:
        p, t = normalise(path), normalise(target)
        if p == t:
            refused.append((path, target, "self-redirect, serves an infinite loop"))
            continue
        if by_path.get(t) == p:
            refused.append((path, target,
                            "loop: %s already redirects back to %s" % (t, p)))
            continue
        if p in by_path:
            refused.append((path, target,
                            "path already redirects to %s" % by_path[p]))
    return refused


async def main() -> int:
    ap = argparse.ArgumentParser(description="Create Shopify URL redirects safely.")
    ap.add_argument("--pair", action="append", default=[],
                    help="from:to, repeatable")
    ap.add_argument("--list", action="store_true", help="list existing redirects")
    ap.add_argument("--delete", help="delete by id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    shop, token = creds()
    async with httpx.AsyncClient(timeout=60.0) as client:
        existing = await fetch_all(client, shop, token)
        print("existing redirects:", len(existing))
        if args.list:
            for r in existing[:40]:
                print("  %-40s -> %-40s %s" % (r["path"], r["target"], r["id"]))
            return 0

        if args.delete:
            data = await gql(client, shop, token, DELETE_REDIRECT, {"id": args.delete})
            res = data["urlRedirectDelete"]
            print("deleted:", res.get("deletedRedirectId") or res.get("userErrors"))
            return 0

        pairs = []
        for raw in args.pair:
            if ":" not in raw:
                print("skipping malformed pair:", raw)
                continue
            a, b = raw.split(":", 1)
            pairs.append((a, b))

        if not pairs:
            print("nothing to do")
            return 0

        refused = check(pairs, existing)
        refused_paths = {normalise(p) for p, _, _ in refused}
        for path, target, reason in refused:
            print("REFUSED %-30s -> %-30s %s" % (path, target, reason))

        for path, target in pairs:
            if normalise(path) in refused_paths:
                continue
            if args.dry_run:
                print("would create %-30s -> %s" % (path, target))
                continue
            data = await gql(client, shop, token, CREATE_REDIRECT,
                             {"redirect": {"path": path, "target": target}})
            res = data["urlRedirectCreate"]
            if res.get("userErrors"):
                print("FAILED  %-30s %s" % (path, res["userErrors"]))
                continue
            r = res["urlRedirect"]
            # The id is printed because a redirect is the one SEO change that has
            # to be reversible, and reversing it needs this.
            print("created %-30s -> %-30s %s" % (r["path"], r["target"], r["id"]))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
