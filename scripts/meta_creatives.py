# LIFESPAN: permanent
"""Pull the ad creatives of one Meta ad account into a CSV and a JSON file: copy, links, video and preview, per ad.

    META_AD_ACCOUNT_ID=<number> META_ACCESS_TOKEN=<token> \
        python scripts/meta_creatives.py --out-dir out [--campaign 123,456] [--status ACTIVE,PAUSED] [--include-deleted]
    python scripts/meta_creatives.py --stand-in --out-dir out

For every ad of the account (or of the campaigns named) it writes the ad id, name and status, the campaign, the ad copy
(primary text, headline, link description, call to action), the destination links, the image, the video id with its
source link, post link and length, the shareable ad preview link and the page post link when the creative has one. The
CSV holds one row per ad with many values in one cell joined by " | "; the JSON file keeps lists as lists and holds the
carousel cards. A cell that would start a spreadsheet formula (= + - @) gets a leading apostrophe in the CSV only.

`--stand-in` starts the local stand-in server (app/connectors/meta_creatives_fake.py) with a small sample account and
runs against that, which needs no token. The real run takes the credentials from the environment only, and
META_API_BASE_URL points it at another server. It has never been run against a live ad account.

Exit 0: the run succeeded. Exit 1: it ran and failed (nothing is written). Exit 2: it could not start, with the reason on
stderr. Exit 3: a rate limit stopped it; the files hold what was read and say so (`"complete": false`).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.connectors.base import ConnectorError  # noqa: E402
from app.connectors.meta import API_VERSION  # noqa: E402
from app.connectors.meta_creatives import COLUMNS, CreativeRun, MetaCreativesClient, to_row  # noqa: E402

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """A spreadsheet reads a cell that starts with = + - @ as a formula. Ad copy is somebody else's text, so it is quoted."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def write_csv(path: Path, run: CreativeRun) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for rec in run.records:
            writer.writerow({k: safe_cell(v) for k, v in to_row(rec).items()})


def write_json(path: Path, run: CreativeRun, *, account: str, source: str, generated_at: str) -> None:
    doc: dict[str, Any] = {
        "account": account, "source": source, "api_version": API_VERSION, "generated_at": generated_at,
        "complete": run.complete, "stopped_because": run.stopped_because, "stats": asdict(run.stats),
        "count": len(run.records), "creatives": [r.to_json() for r in run.records],
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _split(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


async def run_client(client: MetaCreativesClient, args: argparse.Namespace) -> CreativeRun:
    try:
        return await client.fetch_all(
            campaign_ids=_split(args.campaign), statuses=_split(args.status), include_deleted=args.include_deleted)
    finally:
        await client.aclose()


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", required=True, type=Path, help="folder for the CSV and JSON files (made if missing)")
    p.add_argument("--account", help="ad account id, with or without act_ (default: META_AD_ACCOUNT_ID)")
    p.add_argument("--campaign", action="append", help="campaign id, repeatable or comma separated; default every ad")
    p.add_argument("--status", action="append", help="effective_status values to read (default: all but DELETED)")
    p.add_argument("--include-deleted", action="store_true", help="also read ads whose status is DELETED")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--stand-in", action="store_true", help="run against the local stand-in server and its sample account")
    args = p.parse_args(argv)

    env = dict(os.environ)
    stand_in = None
    try:
        if args.stand_in:
            from app.connectors.meta_creatives_fake import FakeMetaCreatives, load_sample
            stand_in = FakeMetaCreatives(token="stand-in-token")
            stand_in.__enter__()
            account = args.account or "555001"
            load_sample(stand_in, account.replace("act_", ""))
            env.update({"META_AD_ACCOUNT_ID": account, "META_ACCESS_TOKEN": "stand-in-token", "META_API_BASE_URL": stand_in.url})
            source = "stand-in"
        else:
            if args.account:
                env["META_AD_ACCOUNT_ID"] = args.account
            source = "custom base address" if env.get("META_API_BASE_URL") else "graph.facebook.com"
        try:
            client = MetaCreativesClient.from_env(
                env, page_size=args.page_size, client=httpx.AsyncClient(timeout=30.0, trust_env=not args.stand_in))
            client._check_statuses(_split(args.status), args.include_deleted)
            for c in _split(args.campaign) or []:
                if not c.isdigit():
                    raise ValueError("campaign ids are numbers, got %r" % c)
        except ValueError as exc:
            print("no run: %s" % exc, file=sys.stderr)
            return 2
        try:
            result = asyncio.run(run_client(client, args))
        except ConnectorError as exc:
            print("the run failed (%s): %s" % (exc.code, exc), file=sys.stderr)
            return 1
        args.out_dir.mkdir(parents=True, exist_ok=True)
        stem = "meta_creatives_%s" % client.account_id
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_csv(args.out_dir / (stem + ".csv"), result)
        write_json(args.out_dir / (stem + ".json"), result, account=client.external_account, source=source, generated_at=stamp)
        notes = sum(1 for r in result.records if r.notes)
        print("%d ads, %d with a note, %d pages, %d batches, %d calls -> %s" % (
            len(result.records), notes, result.stats.pages, result.stats.batches, result.stats.api_calls,
            args.out_dir / (stem + ".csv")))
        if not result.complete:
            print("PARTIAL: %s" % result.stopped_because, file=sys.stderr)
            return 3
        return 0
    finally:
        if stand_in is not None:
            stand_in.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
