"""Seeded property test: random messy batches over several runs against an independent plain Python model of the rules.

The input varies every dimension that matters together, run after run: exact duplicates, a newer revision of a key, an older copy arriving late, the same
version with two different values, nulls, text where a number belongs, impossible dates, a column renamed away, non object records, and the whole of an
earlier run replayed. After every run:

  * nothing is lost: records read = landed + skipped (same bytes seen before) + quarantined at bronze, and silver's bronze rows = inserted + updated +
    unchanged + stale + duplicate in batch + quarantined;
  * silver equals the model key for key (value, currency, tenant and the revision it holds);
  * silver has one row per business key, and the counts of each outcome match the model's;
and after the last run gold's fact has one row per source, day and metric and its totals equal silver's.

The model restates the rules from the docstring of lakehouse/silver.py, not the code. A seed that fails prints itself: rerun it with SEEDS=<n>.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from pyspark.sql import functions as F

from lakehouse import config, gold, quality, silver
from lakehouse.bronze import classify_records
from lakehouse.tests.helpers import day, land, outcome_sum

SEEDS = [int(s) for s in os.environ.get("SEEDS", "101,202,303").split(",")]
NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
TENANTS = [str(uuid.uuid5(NS, f"t{i}")) for i in range(2)]
CONNS = [str(uuid.uuid5(NS, f"c{i}")) for i in range(3)]
METRICS = ["orders", "net_sales", "ad_spend"]
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
INT_RE = re.compile(r"^-?[0-9]{1,18}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
REQUIRED = ["tenant_id", "connection_id", "metric_key", "observed_on", "value_numeric", "synced_at"]


# ---------------------------------------------------------------- the generator


def good_row(rng: random.Random) -> dict:
    conn = rng.choice(CONNS)
    metric = rng.choice(METRICS)
    n = rng.randint(0, 7)
    hour = rng.randint(0, 23)
    return {"tenant_id": TENANTS[CONNS.index(conn) % 2], "connection_id": conn, "metric_key": metric, "observed_on": day(n), "value_numeric": rng.randint(0, 9000),
            "currency": "USD" if metric != "orders" else None, "synced_at": f"{day(n + 1)}T{hour:02d}:{rng.choice([0, 15, 30]):02d}:00+00:00"}


def messy_batch(rng: random.Random, earlier: list) -> list:
    out: list = []
    for _ in range(rng.randint(14, 30)):
        kind = rng.choices(["good", "dup", "newer", "older", "tie", "null", "text", "baddate", "renamed", "notobject", "badts", "blankcur"],
                           [30, 8, 10, 8, 4, 4, 5, 3, 4, 3, 3, 3])[0]
        row = good_row(rng)
        if kind == "dup" and out:
            out.append(dict(rng.choice([r for r in out if isinstance(r, dict)] or [row])))
            continue
        if kind in ("newer", "older", "tie"):
            pool = [r for r in earlier + out if isinstance(r, dict) and parse(r) is not None]
            if pool:
                base = dict(rng.choice(pool))
                ts = datetime.fromisoformat(str(base["synced_at"]).replace("Z", "+00:00"))
                if kind == "tie":
                    base["value_numeric"] = int(text(base["value_numeric"])) + 1  # the same revision with a different value
                else:
                    base["synced_at"] = (ts + timedelta(hours=7 if kind == "newer" else -7)).isoformat()
                    base["value_numeric"] = rng.randint(0, 9000)
                out.append(base)
                continue
        if kind == "null":
            row[rng.choice(REQUIRED)] = None
        elif kind == "text":
            row["value_numeric"] = rng.choice(["12.5", "abc", "", 4.5, "1e3", " 7", True])
        elif kind == "baddate":
            row["observed_on"] = rng.choice(["2026-13-45", "yesterday", "2026-02-30", "26-10-01"])
        elif kind == "renamed":
            row["value"] = row.pop("value_numeric")
        elif kind == "notobject":
            out.append(rng.choice(["not json {", "[1, 2]", 42, None, '"text"']))
            continue
        elif kind == "badts":
            row["synced_at"] = rng.choice(["garbage", "not-a-time", "2026-10-05T05:0:00+00:00", "2026-10-05"])
        elif kind == "blankcur":
            row["currency"] = "  "
        out.append(row)
    if earlier and rng.random() < 0.5:  # a replay of part of an earlier run
        out.extend(rng.sample(earlier, min(len(earlier), rng.randint(1, 5))))
    rng.shuffle(out)
    return out


# ---------------------------------------------------------------- the model


def text(v):
    return None if v is None else v if isinstance(v, str) else json.dumps(v)


def present(v) -> bool:
    t = text(v)
    return t is not None and t.strip() != ""


def parse(rec: dict):
    """The contract of silver_daily_figure restated: returns (key, values, version) for a readable record, None for one that must be quarantined."""
    for k in REQUIRED:
        if not present(rec.get(k)):
            return None
    t = {k: text(rec.get(k)) for k in REQUIRED + ["currency"]}
    if not (UUID_RE.match(t["tenant_id"]) and UUID_RE.match(t["connection_id"]) and INT_RE.match(t["value_numeric"]) and DATE_RE.match(t["observed_on"])
            and TS_RE.match(t["synced_at"])):
        return None
    try:
        date.fromisoformat(t["observed_on"])
        version = datetime.fromisoformat(t["synced_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    currency = t["currency"] if present(rec.get("currency")) else None
    key = (t["connection_id"].lower(), t["metric_key"], t["observed_on"])
    return key, (t["tenant_id"].lower(), int(t["value_numeric"]), currency), version.astimezone(timezone.utc)


def canonical(rec) -> tuple[str, str] | None:
    good, _ = classify_records([rec])
    if not good:
        return None
    payload = good[0][0]
    return payload, hashlib.sha256(payload.encode()).hexdigest()


class Model:
    def __init__(self):
        self.seen: set[str] = set()
        self.state: dict = {}

    def run(self, batch: list) -> tuple[dict, dict]:
        bronze = {"read": len(batch), "landed": 0, "skipped_known": 0, "quarantined": 0}
        landed: list[tuple[str, dict]] = []
        in_batch: set[str] = set()
        for rec in batch:
            c = canonical(rec)
            if c is None:
                bronze["quarantined"] += 1
                continue
            payload, digest = c
            if digest in self.seen or digest in in_batch:
                bronze["skipped_known"] += 1
                continue
            in_batch.add(digest)
            landed.append((digest, json.loads(payload)))
        bronze["landed"] = len(landed)
        self.seen |= in_batch
        out = dict.fromkeys(("read", "inserted", "updated", "unchanged", "stale", "duplicate_in_batch", "quarantined"), 0)
        out["read"] = len(landed)
        groups: dict = {}
        for digest, rec in landed:
            parsed = parse(rec)
            if parsed is None:
                out["quarantined"] += 1
                continue
            key, values, version = parsed
            groups.setdefault(key, []).append((version, digest, values))
        for key, rows in groups.items():
            rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
            out["duplicate_in_batch"] += len(rows) - 1
            version, _, values = rows[0]
            held = self.state.get(key)
            if held is None:
                out["inserted"] += 1
                self.state[key] = (values, version)
            elif version < held[1]:
                out["stale"] += 1
            elif values == held[0]:
                out["unchanged"] += 1
            else:
                out["updated"] += 1
                self.state[key] = (values, version)
        return bronze, out


# ---------------------------------------------------------------- the test


@pytest.mark.parametrize("seed", SEEDS)
def test_random_messy_runs_lose_nothing_and_match_the_model(lh, seed):
    for spec in silver.SPECS.values():
        silver.ensure_silver(lh, spec)
    rng = random.Random(seed)
    model = Model()
    earlier: list = []
    for step in range(3):
        batch = messy_batch(rng, earlier)
        exp_bronze, exp_silver = model.run(batch)
        got_bronze = land(lh, batch, run_id=f"b{step}")
        got_silver = silver.run_silver(lh, silver.SPECS["silver_daily_figure"], f"s{step}")
        where = f"seed {seed} step {step}"
        for k in ("read", "landed", "skipped_known", "quarantined"):
            assert got_bronze[k] == exp_bronze[k], (where, "bronze", k, got_bronze, exp_bronze)
        assert got_bronze["read"] == got_bronze["landed"] + got_bronze["skipped_known"] + got_bronze["quarantined"], where
        assert got_silver["read"] == outcome_sum(got_silver), (where, got_silver)
        for k, v in exp_silver.items():
            assert got_silver[k] == v, (where, "silver", k, got_silver, exp_silver)
        rows = lh.read("silver_daily_figure").collect()
        held = {(r["connection_id"], r["metric_key"], str(r["observed_on"])): ((r["tenant_id"], r["value_numeric"], r["currency"]), r["source_version"]) for r in rows}
        assert len(held) == len(rows), (where, "one row per business key")
        assert {k: v[0] for k, v in held.items()} == {k: v[0] for k, v in model.state.items()}, where
        assert {k: v[1].astimezone(timezone.utc) for k, v in held.items()} == {k: v[1] for k, v in model.state.items()}, where
        earlier.extend(r for r in batch if isinstance(r, dict))

    # gold: the fact keeps the grain and its totals equal silver's
    lh.register_all()
    for name in ("gold_fact_daily_figure",):
        gold.build_gold_table(lh, name)
    fact = lh.read("gold_fact_daily_figure")
    assert fact.count() == fact.select("source_key", "date_key", "metric_key").distinct().count() == lh.count("silver_daily_figure")
    assert fact.agg(F.sum("figure_value")).collect()[0][0] == lh.read("silver_daily_figure").agg(F.sum("value_numeric")).collect()[0][0]
    flag = quality.check_reconciliation(lh)
    assert flag["status"] == "PASS", flag
    assert quality.check_unique_key(lh, "silver_daily_figure", ["connection_id", "metric_key", "observed_on"])["status"] == "PASS"
    assert lh.count(config.SILVER_QUARANTINE) >= 0
