# LIFESPAN: permanent
"""Write an invented carrier snapshot and its next day, for the change detection lab.

    python scripts/make_carrier_snapshot.py --out-dir <folder> [--n 400000] [--seed 20261024] [--change-rate 0.005]

Everything is made up. The names come from word lists and carry a serial number, the insurers are invented, and the USDOT numbers start at 90,000,000, a range this lab reserves for
itself (a lab convention, not a claim about which numbers are issued). MC numbers are 7 digits from 9,000,000. No real company or person is described. The same seed always writes the
same files.

It writes four files into the output folder:
  carriers_day1.csv   N carriers, the file shape app/changes.py loads
  carriers_day2.csv   the same carriers the next day, with about change_rate of them changed, a few new carriers, and some name edits that are NOT tracked and must raise nothing
  companies.csv       a made-up book of companies to match against (name, usdot_no, mc_no), numbers written in different shapes than the file
  carriers_day3.csv, carriers_day4.csv   only with --flappers: reinstate, then cancel again on the same filing id
  manifest.json       what changed and what a correct run must find, worked out here with its own plain Python so the database is not asked to mark its own homework

The USDOT and MC numbers are written in mixed shapes (zero padded, MC prefixed, bare, FF prefixed, blank) so the normalisers have to earn their keep at full size.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter, namedtuple
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.changes import FILE_COLUMNS  # noqa: E402

Row = namedtuple("Row", FILE_COLUMNS)

USDOT_BASE = 90_000_000
MC_BASE = 9_000_000

INSURERS = [
    "Zorlan Casualty", "Quillon Mutual", "Tamarind Peak Indemnity", "Brightvale Surety", "Orrin and Vey Assurance", "Halcyon Ford Underwriters",
    "Mistral Harbor Casualty", "Pennywhistle Re", "Ostrander Lane Mutual", "Kestrel Bay Indemnity", "Umbra Shore Assurance", "Wicklow Reach Casualty",
]
ADJECTIVES = ["Amber", "Brisk", "Cobalt", "Dune", "Ember", "Fallow", "Granite", "Hollow", "Indigo", "Juniper", "Kelp", "Lantern", "Marrow", "Nettle", "Opal", "Pewter"]
NOUNS = ["Ridge", "Crossing", "Meadow", "Harbor", "Summit", "Hollow", "Forge", "Prairie", "Canyon", "Delta", "Orchard", "Basin", "Quarry", "Mesa"]
KINDS = ["Freight", "Haulage", "Carriers", "Logistics", "Transport", "Trucking"]
FORMS = ["LLC", "Inc", "Co", "Ltd"]

CHANGE_MIX = (("insurer_changed", 0.40), ("cancellation_filed", 0.25), ("replacement_filed", 0.20), ("authority_status_changed", 0.15))


# ------------------------------------------------------------------ the oracle: the rules again, in plain Python

def norm_dot(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", re.sub(r"\.0+\s*$", "", raw or "")).lstrip("0")
    return digits or None


def norm_mc(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", re.sub(r"\.0+\s*$", "", raw or "")).lstrip("0")
    if not digits:
        return None
    m = re.match(r"\s*(FF|MX)", (raw or "").upper())
    return (m.group(1) if m else "MC") + digits


TRACKED = ("insurer", "policy_status", "authority_status", "authority_date", "filing_id", "filing_date", "cancel_date")


def clean_text(raw: str | None) -> str:
    """A cell as the database cleans it before hashing: inner whitespace collapsed, trimmed, blank as empty."""
    return re.sub(r"\s+", " ", raw or "").strip()


def clean_code(raw: str | None) -> str:
    return clean_text(raw).upper()


def normalise(row: Row) -> Row:
    return row._replace(legal_name=clean_text(row.legal_name), insurer=clean_code(row.insurer), policy_status=clean_code(row.policy_status),
                        authority_status=clean_code(row.authority_status), authority_date=clean_text(row.authority_date), filing_id=clean_text(row.filing_id),
                        filing_date=clean_text(row.filing_date), cancel_date=clean_text(row.cancel_date))


def expected_events(old: Row | None, new: Row) -> list[tuple[str, str]]:
    """The (kind, event_ref) pairs a correct run raises for one carrier going from `old` (None if unseen) to `new`, both read the way the database reads a cell."""
    new = normalise(new)
    if old is None:
        return [("new_entity", "")]
    old = normalise(old)
    if all(getattr(old, c) == getattr(new, c) for c in TRACKED):
        return []
    effective = new.cancel_date or new.filing_date
    out = []
    if new.insurer != old.insurer:
        out.append(("insurer_changed", new.filing_id))
    if new.policy_status == "CANCELLED" and (old.policy_status != "CANCELLED" or old.filing_id != new.filing_id):
        out.append(("cancellation_filed", "%s|%s" % (new.filing_id, effective)))
    if new.policy_status == "REPLACED" and (old.policy_status != "REPLACED" or old.filing_id != new.filing_id):
        out.append(("replacement_filed", "%s|%s" % (new.filing_id, effective)))
    if new.authority_status != old.authority_status:
        out.append(("authority_status_changed", "%s:%s" % (new.authority_status, (date.fromisoformat(new.authority_date) - date(1970, 1, 1)).days if new.authority_date else "")))
    return out


# ------------------------------------------------------------------ building the files

def _usdot_text(rng: random.Random, i: int) -> str:
    n = USDOT_BASE + i
    return "0%d" % n if rng.random() < 0.03 else str(n)


def _mc_text(rng: random.Random, i: int) -> str:
    roll = rng.random()
    n = MC_BASE + i
    if roll < 0.08:
        return ""
    if roll < 0.55:
        return "MC-%d" % n
    if roll < 0.75:
        return str(n)
    if roll < 0.87:
        return "00%d" % n
    if roll < 0.97:
        return "MC%d" % n
    return "FF-%d" % n


def _rand_date(rng: random.Random, start: date, end: date) -> str:
    return (start + timedelta(days=rng.randrange((end - start).days + 1))).isoformat()


def _carrier(rng: random.Random, i: int, day1: date) -> Row:
    name = "%s %s %s %07d %s" % (rng.choice(ADJECTIVES), rng.choice(NOUNS), rng.choice(KINDS), i, rng.choice(FORMS))
    roll = rng.random()
    policy = "ACTIVE" if roll < 0.92 else ("CANCELLED" if roll < 0.96 else "REPLACED")
    filing_date = _rand_date(rng, date(2024, 1, 1), day1)
    cancel = (date.fromisoformat(filing_date) + timedelta(days=30)).isoformat() if policy == "CANCELLED" else ""
    roll = rng.random()
    authority = "ACTIVE" if roll < 0.93 else ("INACTIVE" if roll < 0.96 else ("REVOKED" if roll < 0.99 else "PENDING"))
    return Row(_usdot_text(rng, i), _mc_text(rng, i), name, rng.choice(INSURERS), policy, authority, _rand_date(rng, date(2019, 1, 1), day1),
               "F%09d" % rng.randrange(10**9), filing_date, cancel)


def _change(rng: random.Random, row: Row, kind: str, day2: date) -> Row:
    """Apply one scenario. A scenario the row cannot take (a cancellation of a policy already cancelled) falls back to an insurer change."""
    if kind in ("cancellation_filed", "replacement_filed") and row.policy_status != "ACTIVE":
        kind = "insurer_changed"
    fid = "F%09d" % rng.randrange(10**9)
    if kind == "insurer_changed":
        other = rng.choice([x for x in INSURERS if x != row.insurer])
        return row._replace(insurer=other, policy_status="ACTIVE", filing_id=fid, filing_date=day2.isoformat(), cancel_date="")
    if kind == "cancellation_filed":
        return row._replace(policy_status="CANCELLED", filing_id=fid, filing_date=day2.isoformat(), cancel_date=(day2 + timedelta(days=30)).isoformat())
    if kind == "replacement_filed":
        insurer = row.insurer if rng.random() < 0.5 else rng.choice([x for x in INSURERS if x != row.insurer])
        return row._replace(policy_status="REPLACED", insurer=insurer, filing_id=fid, filing_date=day2.isoformat(), cancel_date="")
    other = rng.choice([x for x in ("ACTIVE", "INACTIVE", "REVOKED", "PENDING") if x != row.authority_status])
    return row._replace(authority_status=other, authority_date=day2.isoformat())


def _write(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(FILE_COLUMNS)
        w.writerows(rows)


def generate(out_dir, *, n: int = 400_000, seed: int = 20261024, change_rate: float = 0.005, new_rate: float = 0.0005, noise_rate: float = 0.002,
             company_rate: float = 0.01, flappers: int = 0, day1: date = date(2026, 10, 23)) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    day2 = day1 + timedelta(days=1)

    old = [_carrier(rng, i, day1) for i in range(n)]
    n_changed = round(n * change_rate)
    n_new = round(n * new_rate)
    n_noise = round(n * noise_rate)
    picked = rng.sample(range(n), n_changed + n_noise)
    changed_ix, noise_ix = picked[:n_changed], picked[n_changed:]

    kinds = [k for k, _ in CHANGE_MIX]
    weights = [w for _, w in CHANGE_MIX]
    new = list(old)
    scenario: Counter = Counter()
    for ix in changed_ix:
        kind = rng.choices(kinds, weights)[0]
        new[ix] = _change(rng, old[ix], kind, day2)
    for ix in noise_ix:
        new[ix] = old[ix]._replace(legal_name=old[ix].legal_name + " Holdings")     # an untracked column: the file changes, nothing else may
    added = [_carrier(rng, n + j, day2) for j in range(n_new)]
    day2_rows = new + added

    # The company book: a sample of carriers, written in a shape the file does not use, with some holding only one of the two numbers. Two carriers from the changed set are given to
    # two different companies each (one by USDOT, one by MC) to make a conflict.
    companies: list[tuple[str, str, str]] = []
    changed_with_mc = [ix for ix in changed_ix if norm_mc(old[ix].mc)][:2]
    reserved = set(changed_with_mc)
    sample = [ix for ix in rng.sample(range(n), round(n * company_rate)) if ix not in reserved]
    for k, ix in enumerate(sample):
        roll = rng.random()
        dot = "DOT %d" % (USDOT_BASE + ix)
        mc = "MC %07d" % (MC_BASE + ix) if norm_mc(old[ix].mc) else ""
        if roll < 0.3:
            mc = ""
        elif roll < 0.6:
            dot = ""
        if not dot and not mc:
            dot = "DOT %d" % (USDOT_BASE + ix)
        companies.append(("Lab Account %05d" % k, dot, mc))
    for k, ix in enumerate(changed_with_mc):
        companies.append(("Lab Conflict %d A" % (k + 1), "%d" % (USDOT_BASE + ix), ""))
        companies.append(("Lab Conflict %d B" % (k + 1), "", "MC-%d" % (MC_BASE + ix)))
    for j in range(0, n_new, 10):
        companies.append(("Lab New Account %04d" % j, "%d" % (USDOT_BASE + n + j), ""))

    # Shuffle the files so order carries no information, then write.
    day1_rows = list(old)
    rng.shuffle(day1_rows)
    rng.shuffle(day2_rows)
    _write(out / "carriers_day1.csv", day1_rows)
    _write(out / "carriers_day2.csv", day2_rows)
    with open(out / "companies.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(("name", "usdot_no", "mc_no"))
        w.writerows(companies)

    # Optional days 3 and 4 for carriers cancelled on day 2: reinstated on day 3, cancelled again on day 4 on the SAME filing id with a new cancel date. A correct run alerts on each
    # cancellation, so the manifest says two per carrier, counted from this scenario and not from the event key rule.
    flap_ix = [ix for ix in changed_ix if old[ix].policy_status == "ACTIVE" and new[ix].policy_status == "CANCELLED"][:flappers]
    if flap_ix:
        day4 = day2 + timedelta(days=2)
        d3, d4 = list(new), list(new)
        for ix in flap_ix:
            d3[ix] = new[ix]._replace(policy_status="ACTIVE", cancel_date="")
            d4[ix] = new[ix]._replace(cancel_date=(day4 + timedelta(days=30)).isoformat())
        for name, rows in (("carriers_day3.csv", d3 + added), ("carriers_day4.csv", d4 + added)):
            rng.shuffle(rows)
            _write(out / name, rows)

    # The expected answer, from the oracle above.
    by_dot: dict[str, list[int]] = {}
    by_mc: dict[str, list[int]] = {}
    for idx, (_n, dot, mc) in enumerate(companies):
        if norm_dot(dot):
            by_dot.setdefault(norm_dot(dot), []).append(idx)
        if norm_mc(mc):
            by_mc.setdefault(norm_mc(mc), []).append(idx)
    events: Counter = Counter()
    matched = conflict = unmatched = 0
    old_by_dot = {norm_dot(r.usdot): r for r in old}
    for r in day2_rows:
        for kind, _ref in expected_events(old_by_dot.get(norm_dot(r.usdot)), r):
            events[kind] += 1
            hits = set(by_dot.get(norm_dot(r.usdot), [])) | set(by_mc.get(norm_mc(r.mc), []))
            if not hits:
                unmatched += 1
            elif len(hits) == 1:
                matched += 1
            else:
                conflict += 1
    manifest = {
        "seed": seed, "carriers_day1": n, "carriers_day2": len(day2_rows), "change_rate": change_rate, "rows_changed_tracked": n_changed, "rows_new": n_new,
        "rows_name_only_edits": n_noise, "companies": len(companies), "snapshot_day1": day1.isoformat(), "snapshot_day2": day2.isoformat(),
        "expected_events_by_kind": {k: events[k] for k in sorted(events)}, "expected_events_total": sum(events.values()),
        "flap_carriers": len(flap_ix), "flap_usdots": [norm_dot(new[ix].usdot) for ix in flap_ix], "expected_flap_cancellations": 2 * len(flap_ix),
        "expected_matched": matched, "expected_conflict": conflict, "expected_unmatched": unmatched,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--n", type=int, default=400_000)
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--change-rate", type=float, default=0.005, help="share of carriers whose tracked columns change next day (an assumption, default 0.5 percent)")
    ap.add_argument("--new-rate", type=float, default=0.0005)
    ap.add_argument("--noise-rate", type=float, default=0.002)
    ap.add_argument("--company-rate", type=float, default=0.01)
    ap.add_argument("--flappers", type=int, default=0, help="carriers cancelled on day 2 that are reinstated on day 3 and cancelled again on day 4 (writes those two files)")
    a = ap.parse_args()
    manifest = generate(a.out_dir, n=a.n, seed=a.seed, change_rate=a.change_rate, new_rate=a.new_rate, noise_rate=a.noise_rate, company_rate=a.company_rate, flappers=a.flappers)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
