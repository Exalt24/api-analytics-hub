# LIFESPAN: permanent
"""Back up the hub database, prove a restore, and alert when either stops.

    python scripts/db_backup.py backup --dir /var/backups/hub --keep 14
    python scripts/db_backup.py prove  --dir /var/backups/hub
    python scripts/db_backup.py check  --dir /var/backups/hub --state /var/lib/hub/backup_alert.json

DATABASE_URL names the database (postgresql://user:pass@host:port/db). By default pg_dump, pg_restore and psql run on this machine. To run them inside a container instead, set
PG_CMD_PREFIX, for example `docker compose exec -T db`; the connection then uses the container's own socket with the user and database from DATABASE_URL.

backup: pg_dump in the custom format to <dir>/hub-<UTC time>.dump plus a manifest (sha256, size, row counts just before and after), then prunes to --keep dumps.
prove:  restores the newest dump into a throwaway database, compares every table's row count with the manifest, drops the throwaway database and records the result in <dir>/restore_proof.json.
check:  tells a Slack incoming webhook when no backup exists, the newest is too old, or no restore has been proven lately or the last one failed. It reuses the sync alert policy, so a standing
        problem alerts once and again after --repeat-seconds, and a recovery sends one message.
Exit 0: fine (or a repeat inside the quiet window). Exit 1: a problem exists (backup/prove: the step failed; check: something needs attention). Exit 2: the tool itself could not run, or the alert could not be delivered.
The password is passed in the child's environment, never on a command line, and the webhook URL is read from the environment and never printed.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backup import (DEFAULT_MAX_BACKUP_AGE, DEFAULT_MAX_PROOF_AGE, Manifest, Proof, backup_problems,  # noqa: E402
                        count_mismatches, describe_backup, plan_prune, sha256_of)
from app.health import decide, load_state, save_state  # noqa: E402

log = logging.getLogger("hub.backup")
PROOF_FILE = "restore_proof.json"
DUMP_RE = re.compile(r"^hub-\d{8}T\d{6}Z\.dump$")


class ToolError(RuntimeError):
    """A pg tool failed. The message never carries the password or the dump contents."""


def parse_dsn(dsn: str) -> dict[str, str]:
    u = urlsplit(dsn)
    if u.scheme not in ("postgresql", "postgres") or not u.path.strip("/"):
        raise ToolError("DATABASE_URL must look like postgresql://user:pass@host:port/db")
    return {"user": unquote(u.username or ""), "password": unquote(u.password or ""), "host": u.hostname or "localhost", "port": str(u.port or 5432), "db": unquote(u.path.lstrip("/"))}


def _argv(tool: str, dsn: dict[str, str], db: str | None = None) -> tuple[list[str], dict[str, str]]:
    prefix = shlex.split(os.environ.get("PG_CMD_PREFIX", ""))
    conn = ["-U", dsn["user"]] if prefix else ["-h", dsn["host"], "-p", dsn["port"], "-U", dsn["user"]]
    if db is not None:
        conn += ["-d", db]
    env = dict(os.environ)
    if dsn["password"]:
        env["PGPASSWORD"] = dsn["password"]
    return prefix + [tool] + conn, env


def run_tool(tool: str, dsn: dict[str, str], extra: list[str], db: str | None = None, stdin=None, stdout=None, timeout: int = 1800) -> str:
    argv, env = _argv(tool, dsn, db)
    try:
        r = subprocess.run(argv + extra, env=env, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        raise ToolError("%s was not found; install the PostgreSQL client tools or set PG_CMD_PREFIX" % tool) from None
    except subprocess.TimeoutExpired:
        raise ToolError("%s timed out after %ds" % (tool, timeout)) from None
    if r.returncode != 0:
        raise ToolError("%s failed (exit %d): %s" % (tool, r.returncode, r.stderr.decode("utf-8", "replace").strip()[:300]))
    return r.stdout.decode("utf-8", "replace") if stdout is None else ""


def psql(dsn: dict[str, str], db: str, sql: str) -> list[str]:
    out = run_tool("psql", dsn, ["-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql], db=db)
    return [line for line in out.splitlines() if line.strip()]


def table_counts(dsn: dict[str, str], db: str) -> dict[str, int]:
    """Exact row counts for every ordinary table in public, in one statement so the counts share a snapshot."""
    names = psql(dsn, db, "select tablename from pg_tables where schemaname = 'public' order by 1")
    if not names:
        return {}
    union = " union all ".join("select '%s', count(*) from public.\"%s\"" % (n.replace("'", "''"), n.replace('"', '""')) for n in names)
    out = {}
    for line in psql(dsn, db, union):
        name, _, n = line.rpartition("|")
        out[name] = int(n)
    return out


def newest_manifest(directory: Path) -> Manifest | None:
    found = []
    for m in directory.glob("hub-*.dump.json"):
        try:
            found.append(Manifest.from_json(m.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            log.warning("unreadable manifest %s", m.name)
    return max(found, key=lambda x: x.created_at) if found else None


def do_backup(dsn: dict[str, str], directory: Path, keep: int, now: float) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target = directory / ("hub-%s.dump" % stamp)
    partial = target.with_suffix(".dump.partial")
    before = table_counts(dsn, dsn["db"])
    try:
        with open(partial, "wb") as fh:
            run_tool("pg_dump", dsn, ["--format=custom", "--no-owner"], db=dsn["db"], stdout=fh)
        if partial.stat().st_size == 0:
            raise ToolError("pg_dump wrote an empty file")
        after = table_counts(dsn, dsn["db"])
        os.replace(partial, target)         # a dump is visible under its final name only when complete
    finally:
        partial.unlink(missing_ok=True)
    manifest = Manifest(target.name, now, sha256_of(target), target.stat().st_size, before, after)
    Path(str(target) + ".json").write_text(manifest.to_json(), encoding="utf-8")
    dumps = [(p.name, p.stat().st_mtime) for p in directory.glob("hub-*.dump") if DUMP_RE.match(p.name)]
    for name in plan_prune(dumps, keep):
        (directory / name).unlink(missing_ok=True)
        (directory / (name + ".json")).unlink(missing_ok=True)
    log.info("backup written", extra={"event": "backup_ok", "findings": "%s %d bytes %d tables" % (target.name, manifest.size, len(after))})
    return 0


def do_prove(dsn: dict[str, str], directory: Path, now: float) -> int:
    manifest = newest_manifest(directory)
    if manifest is None:
        raise ToolError("no backup with a manifest in %s" % directory)
    dump = directory / manifest.file
    scratch = "hub_restore_proof_%s" % uuid.uuid4().hex[:8]
    detail, ok = "", False
    try:
        if not dump.is_file():
            detail = "the dump file named by the manifest is missing"
        elif sha256_of(dump) != manifest.sha256:
            detail = "the dump does not match its recorded checksum (changed or damaged since it was written)"
        else:
            psql(dsn, "postgres", 'create database "%s"' % scratch)
            try:
                with open(dump, "rb") as fh:
                    run_tool("pg_restore", dsn, ["--no-owner", "--exit-on-error"], db=scratch, stdin=fh, stdout=subprocess.DEVNULL)
                problems = count_mismatches(manifest, table_counts(dsn, scratch))
                ok, detail = (not problems), ("; ".join(problems) if problems else "restored %d tables, every row count inside the dump's bracket" % len(manifest.after))
            finally:
                psql(dsn, "postgres", 'drop database if exists "%s" with (force)' % scratch)
    except ToolError as exc:
        detail = str(exc)
    (directory / PROOF_FILE).write_text(Proof(manifest.file, now, ok, detail).to_json(), encoding="utf-8")
    log.log(logging.INFO if ok else logging.ERROR, detail, extra={"event": "restore_proof_ok" if ok else "restore_proof_failed"})
    return 0 if ok else 1


def load_proof(directory: Path) -> Proof | None:
    try:
        return Proof.from_json((directory / PROOF_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def post_slack(url: str, text: str, timeout: float = 10.0) -> None:
    import httpx
    try:
        resp = httpx.post(url, json={"text": text}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError("slack post failed: %s" % type(exc).__name__) from None
    if resp.status_code >= 300:
        raise RuntimeError("slack post failed: HTTP %d" % resp.status_code)


def do_check(directory: Path, args, now: float) -> int:
    manifest = newest_manifest(directory)
    proof = load_proof(directory)
    # A proof of an OLDER dump does not vouch for the newest one only if that older dump is gone; a proof is about restoring at all, so it stays valid while it is recent.
    problems = backup_problems(now, manifest, proof, args.max_backup_age, args.max_proof_age)
    log.info(describe_backup(problems), extra={"event": "backup_check", "findings": len(problems)})
    state = load_state(args.state) if args.state else {}
    message, new_state = decide(state, problems, now, args.repeat_seconds, describer=describe_backup)
    if message and os.environ.get("SLACK_WEBHOOK_URL"):
        try:
            post_slack(os.environ["SLACK_WEBHOOK_URL"], message)
        except RuntimeError as exc:
            log.error(str(exc), extra={"event": "alert_failed"})
            return 2                       # state is NOT saved, so the next run tries again
    if args.state:
        save_state(args.state, new_state)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "prove", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--dir", required=True, help="where dumps, manifests and the restore proof live")
        if name == "backup":
            sp.add_argument("--keep", type=int, default=14, help="how many dumps to keep (at least 1)")
        if name == "check":
            sp.add_argument("--state", help="JSON file remembering what was already alerted")
            sp.add_argument("--repeat-seconds", type=int, default=6 * 3600)
            sp.add_argument("--max-backup-age", type=int, default=DEFAULT_MAX_BACKUP_AGE)
            sp.add_argument("--max-proof-age", type=int, default=DEFAULT_MAX_PROOF_AGE)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory, now = Path(args.dir), time.time()
    try:
        if args.cmd == "check":
            return do_check(directory, args, now)
        dsn = parse_dsn(os.environ.get("DATABASE_URL", ""))
        return do_backup(dsn, directory, args.keep, now) if args.cmd == "backup" else do_prove(dsn, directory, now)
    except ToolError as exc:
        log.error(str(exc), extra={"event": "backup_tool_failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
