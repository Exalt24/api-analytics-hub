# LIFESPAN: permanent
"""Rank what an MSP automation backlog is ordered by, from the ledgers the PSA and RMM syncs write.

    python scripts/msp_backlog.py analyze tickets.jsonl devices.jsonl [--as-of 2026-11-29T12:00:00Z] [--window-days 30] [--horizon-hours 4] [--top 10] [--format markdown|json] [--out report.md]
    python scripts/msp_backlog.py sample --seed 7 --out sample.jsonl [--as-of 2026-11-29T12:00:00Z]

`analyze` reads one or more ledgers (`sync_connectwise.py --ledger`, `sync_autotask.py --ledger`, `sync_ninjaone.py --ledger`), merges them, and prints three rankings
(app/msp/analysis.py): the ticket and alert DRIVERS (what repeats, what is noisy), the SLA RISK of the work open at --as-of, and the technician TOUCH TIME per ticket category.
The window is the --window-days days that end with the day of --as-of (30 by default); --start and --end (both, a half open window) replace it. --as-of defaults to the time the
oldest ledger was written, so the same files always give the same report. It reads files only and calls no vendor.

`sample` writes a GENERATED ledger (made up from the seed: no client, no real data) so the rankings can be seen without a tenant.

Exit 0: done. Exit 2: it could not run, with the reason on stderr (a missing or damaged ledger, a window that is empty, a time with no zone).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.msp.analysis import DEFAULT_HORIZON_HOURS, DEFAULT_TOP, AnalysisError, build_report, render_markdown  # noqa: E402
from app.msp.ledger import LedgerError, merge_ledgers, read_ledger, write_ledger  # noqa: E402
from app.msp.sample import generate_world  # noqa: E402

DEFAULT_SAMPLE_AS_OF = "2026-11-29T12:00:00Z"


def parse_when(text: str) -> datetime:
    if not text.endswith("Z"):
        raise argparse.ArgumentTypeError("a time must be UTC with a trailing Z, such as 2026-11-29T12:00:00Z")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise argparse.ArgumentTypeError("not a date and time: %r" % text) from None


def analyze(args: argparse.Namespace) -> int:
    try:
        ledger = merge_ledgers(*[read_ledger(p) for p in args.ledgers])
        as_of = args.as_of or ledger.generated_at
        if as_of is None:
            raise AnalysisError("no --as-of given and the ledgers carry no time")
        if (args.start is None) != (args.end is None):
            raise AnalysisError("give --start and --end together")
        if args.start is not None:
            start, end = args.start, args.end
        else:
            if isinstance(args.window_days, bool) or args.window_days < 1:
                raise AnalysisError("--window-days must be at least 1")
            end = as_of.astimezone(timezone.utc).date() + timedelta(days=1)
            start = end - timedelta(days=args.window_days)
        report = build_report(ledger, as_of=as_of, window_start=start, window_end=end, horizon_hours=args.horizon_hours, top=args.top)
    except (OSError, LedgerError, AnalysisError, ValueError) as exc:
        print("no report: %s" % exc, file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.format == "json" else render_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print("report written to %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


def sample(args: argparse.Namespace) -> int:
    ledger = generate_world(args.seed, as_of=args.as_of)
    count = write_ledger(args.out, ledger.records(), generated_at=args.as_of)
    print("sample ledger: %d generated records written to %s" % (count, args.out))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze", help="rank drivers, SLA risk and touch time from ledgers")
    a.add_argument("ledgers", nargs="+", help="ledger files written by the sync scripts")
    a.add_argument("--as-of", type=parse_when, default=None, help="the instant the open work is judged at, UTC with a trailing Z (default: when the oldest ledger was written)")
    a.add_argument("--window-days", type=int, default=30, help="how many days the window holds, ending with the day of --as-of (default 30)")
    a.add_argument("--start", type=date.fromisoformat, default=None, help="first day of the window, with --end")
    a.add_argument("--end", type=date.fromisoformat, default=None, help="first day NOT in the window, with --start")
    a.add_argument("--horizon-hours", type=int, default=DEFAULT_HORIZON_HOURS, help="a goal this many hours away or nearer is at risk (default %d)" % DEFAULT_HORIZON_HOURS)
    a.add_argument("--top", type=int, default=DEFAULT_TOP, help="rows in each ranking (default %d)" % DEFAULT_TOP)
    a.add_argument("--format", choices=("markdown", "json"), default="markdown")
    a.add_argument("--out", help="write the report to this file instead of the screen")
    a.set_defaults(run=analyze)
    s = sub.add_parser("sample", help="write a generated ledger")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--as-of", type=parse_when, default=parse_when(DEFAULT_SAMPLE_AS_OF))
    s.set_defaults(run=sample)
    args = p.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
