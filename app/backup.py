"""Database backups that are only counted once a restore has been proven, and the alert for when they stop.

A backup nobody has restored is a hope, so the policy has two clocks: how old the newest dump is, and how old the last successful restore of a dump is. The pure parts live here (manifest, row
count bracket, pruning, the problems an operator is told about) so they are testable without Postgres; scripts/db_backup.py does the pg_dump, the scratch restore and the alert.

ROW COUNTS. A dump is one snapshot but counting rows is a separate query, so a table that takes writes during the dump can differ by a few rows from any single count. The manifest keeps the
count taken just BEFORE and just AFTER the dump; a restored table passes when its count lies between the two. That is exact on a quiet database and honest on a busy one: a table that lost or gained
rows outside the bracket, or is missing, fails.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.health import Problem

#: A daily cron with room for one missed run.
DEFAULT_MAX_BACKUP_AGE = 36 * 3600
#: The restore proof is weekly, with room for one missed run.
DEFAULT_MAX_PROOF_AGE = 9 * 86400

KIND_NEVER = "never_backed_up"
KIND_STALE = "backup_stale"
KIND_UNPROVEN = "restore_unproven"
KIND_FAILED = "restore_failed"
# The last nightly run (backup or prove) ended in an error and no later dump exists. Found under a real systemd 2026-10-30: OnFailure started the
# check, but the check only judged the age of the newest dump and the proof, so the first failed night said "healthy" and stayed silent for
# about a day and a half.
KIND_RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class Manifest:
    file: str
    created_at: float
    sha256: str
    size: int
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"file": self.file, "created_at": self.created_at, "sha256": self.sha256, "size": self.size, "before": self.before, "after": self.after}, indent=1, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "Manifest":
        d = json.loads(text)
        return Manifest(str(d["file"]), float(d["created_at"]), str(d["sha256"]), int(d["size"]),
                        {str(k): int(v) for k, v in d.get("before", {}).items()}, {str(k): int(v) for k, v in d.get("after", {}).items()})


@dataclass(frozen=True)
class Proof:
    """The result of the last attempt to restore a dump into a scratch database."""
    backup_file: str
    proved_at: float
    ok: bool
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps({"backup_file": self.backup_file, "proved_at": self.proved_at, "ok": self.ok, "detail": self.detail}, indent=1, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "Proof":
        d = json.loads(text)
        return Proof(str(d["backup_file"]), float(d["proved_at"]), bool(d["ok"]), str(d.get("detail", "")))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_mismatches(manifest: Manifest, restored: dict[str, int]) -> list[str]:
    """Every table the manifest knows must exist in the restore with a count inside the before/after bracket. A table that exists only in the restore is reported too."""
    out: list[str] = []
    for table in sorted(set(manifest.before) | set(manifest.after)):
        if table not in restored:
            out.append("%s is missing from the restore" % table)
            continue
        lo_hi = [c[table] for c in (manifest.before, manifest.after) if table in c]
        lo, hi = min(lo_hi), max(lo_hi)
        if not lo <= restored[table] <= hi:
            out.append("%s restored %d rows, the dump held %s" % (table, restored[table], lo if lo == hi else "%d to %d" % (lo, hi)))
    for table in sorted(set(restored) - set(manifest.before) - set(manifest.after)):
        out.append("%s is in the restore but not in the manifest" % table)
    return out


def plan_prune(dumps: list[tuple[str, float]], keep: int) -> list[str]:
    """Names to delete: everything but the `keep` newest. Never deletes the newest, and keep below 1 is treated as 1."""
    keep = max(1, keep)
    newest_first = sorted(dumps, key=lambda d: (-d[1], d[0]))
    return [name for name, _ in newest_first[keep:]]


def backup_problems(now: float, manifest: Manifest | None, proof: Proof | None,
                    max_backup_age: int = DEFAULT_MAX_BACKUP_AGE, max_proof_age: int = DEFAULT_MAX_PROOF_AGE,
                    last_run: dict | None = None) -> list[Problem]:
    def p(kind: str, age: float) -> Problem:
        return Problem("database", "postgres", kind, max(0, int(age)))

    if manifest is None:
        return [p(KIND_NEVER, 0)]
    out: list[Problem] = []
    if now - manifest.created_at > max_backup_age:
        out.append(p(KIND_STALE, now - manifest.created_at))
    if proof is None:
        out.append(p(KIND_UNPROVEN, now - manifest.created_at))
    elif not proof.ok:
        out.append(p(KIND_FAILED, now - proof.proved_at))
    elif now - proof.proved_at > max_proof_age:
        out.append(p(KIND_UNPROVEN, now - proof.proved_at))
    # a failed run newer than the newest dump: the dump is old news and tonight's run did not replace it
    if last_run and last_run.get("ok") is False and float(last_run.get("at", 0)) > manifest.created_at:
        out.append(p(KIND_RUN_FAILED, now - float(last_run["at"])))
    return out


_WORDS = {
    KIND_NEVER: "no backup has ever been written",
    KIND_STALE: "the newest backup is too old",
    KIND_UNPROVEN: "no restore has been proven recently",
    KIND_FAILED: "the last restore test FAILED",
    KIND_RUN_FAILED: "the last nightly backup run FAILED",
}


def describe_backup(problems: list[Problem]) -> str:
    if not problems:
        return "The database has a recent backup and a recent proven restore."
    lines = ["%d backup problem(s):" % len(problems)]
    for pr in problems:
        age = "" if pr.kind == KIND_NEVER else " (%s)" % _age(pr.age_seconds)
        lines.append("- %s%s" % (_WORDS.get(pr.kind, pr.kind), age))
    return "\n".join(lines)


def _age(seconds: int) -> str:
    if seconds >= 86400:
        return "%.1f days" % (seconds / 86400)
    if seconds >= 3600:
        return "%.1f hours" % (seconds / 3600)
    return "%d minutes" % max(1, seconds // 60)
