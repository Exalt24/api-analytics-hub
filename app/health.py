"""Reading sync health, and deciding when to alert on it.

The SQL (migrations/004_observability.sql) names the problems. This file reads them through a Database opened as the cross-tenant sync_worker role (never the request pool: app_request must not be able to become sync_worker), turns them into /metrics gauges, and holds the
alert policy as pure functions so it is testable without a database or a network.

ALERT POLICY. A problem is keyed by (connection, kind). The same set of problems alerts once, then again only after `repeat_seconds`; a changed set alerts at once; a set that
clears sends one recovery message. Without that, a 15 minute cron posts the same line 96 times a day and the channel gets muted, which is the same as having no alert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

#: Three cadences without a success. One missed run is normal (the lock skips, a rate limit waits); three in a row is a pattern.
DEFAULT_STALE_SECONDS = 21600


@dataclass(frozen=True)
class Problem:
    connection_id: str
    platform: str
    kind: str
    age_seconds: int

    @property
    def key(self) -> str:
        return "%s:%s" % (self.connection_id, self.kind)


async def read_problems(db, default_stale_seconds: int = DEFAULT_STALE_SECONDS) -> list[Problem]:
    async with db.unscoped() as conn:
        rows =await conn.fetch("select * from sync_problems($1)", default_stale_seconds)
    return sorted((Problem(str(r["connection_id"]), r["platform"], r["kind"], int(r["age_seconds"])) for r in rows), key=lambda p: (p.kind, p.connection_id))


async def read_gauges(db, window_seconds: int = 86400) -> list[tuple[str, dict[str, str], float]]:
    """Gauges for /metrics. Raises when the database cannot be read; the caller reports that as hub_scrape_ok 0 and omits these, never as zeros."""
    problems = await read_problems(db)
    async with db.unscoped() as conn:
        counts = await conn.fetch("select * from sync_run_counts($1)", window_seconds)
    out: list[tuple[str, dict[str, str], float]] = [("hub_sync_problems_total", {}, float(len(problems)))]
    by: dict[tuple[str, str], int] = {}
    for p in problems:
        by[(p.platform, p.kind)] = by.get((p.platform, p.kind), 0) + 1
    out += [("hub_sync_problems", {"platform": plat, "kind": kind}, float(n)) for (plat, kind), n in sorted(by.items())]
    out.append(("hub_sync_oldest_problem_age_seconds", {}, float(max((p.age_seconds for p in problems), default=0))))
    out += [("hub_sync_runs_24h", {"platform": r["platform"], "status": r["status"]}, float(r["runs"])) for r in counts]
    return out


def describe(problems: list[Problem]) -> str:
    if not problems:
        return "All active connections synced inside their window."
    lines = ["%d connection problem(s):" % len(problems)]
    for p in problems:
        lines.append("- %s %s on %s, %s" % (p.kind, p.connection_id[:8], p.platform, _age(p.age_seconds)))
    return "\n".join(lines)


def _age(seconds: int) -> str:
    if seconds >= 86400:
        return "%.1f days" % (seconds / 86400)
    if seconds >= 3600:
        return "%.1f hours" % (seconds / 3600)
    return "%d minutes" % max(1, seconds // 60)


def decide(state: dict, problems: list[Problem], now: float, repeat_seconds: int) -> tuple[str | None, dict]:
    """Returns (message or None, new state). State is {"keys": [...], "alerted_at": float}."""
    keys = sorted(p.key for p in problems)
    old = sorted(state.get("keys", []))
    if not keys:
        return ("Recovered. " + describe([]) if old else None), {"keys": [], "alerted_at": 0.0}
    if keys != old or now - float(state.get("alerted_at", 0.0)) >= repeat_seconds:
        return describe(problems), {"keys": keys, "alerted_at": now}
    return None, state


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
