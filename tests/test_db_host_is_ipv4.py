"""The compose Postgres is published on 127.0.0.1 only, so no file may reach it as `localhost`.

On Windows (measured 2026-10-30 on a Ryzen desktop) `localhost` resolves to ::1 first, nothing answers there, and every connection waits out its connect timeout (6 seconds for psycopg
with connect_timeout=6, longer for the tests' own settings). The database tests open hundreds of connections, so the whole suite sat at about 1 percent for over an hour; the same
connection to 127.0.0.1 takes 0.01 seconds. The class is "an address that depends on how the machine resolves localhost", so this scans every text file of the repo for it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".next", "dist", "build"}
TEXT = {".py", ".md", ".yml", ".yaml", ".sh", ".cjs", ".js", ".ts", ".tsx", ".json", ".toml", ".txt", ".sql", ".env", ".example"}
BAD = re.compile(r"localhost:5433")
THIS = Path(__file__).resolve()


def offenders(root: Path = ROOT) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(root).parts) or p.suffix.lower() not in TEXT or p.resolve() == THIS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if BAD.search(text):
            out.append(str(p.relative_to(root)))
    return sorted(out)


def test_no_file_reaches_the_compose_database_as_localhost():
    assert offenders() == [], "use 127.0.0.1:5433, the compose port is bound to IPv4 only"


def test_the_scan_finds_the_address_when_it_is_there(tmp_path):
    (tmp_path / "t.py").write_text('DSN = "postgresql://a:b@localhost:5433/analytics"\n', encoding="utf-8")
    (tmp_path / "ok.py").write_text('DSN = "postgresql://a:b@127.0.0.1:5433/analytics"\n', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skipped.js").write_text("localhost:5433", encoding="utf-8")
    assert offenders(tmp_path) == ["t.py"]


def test_the_compose_file_still_binds_ipv4_only():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:5433:5432"' in compose, "if the port is published on every interface, localhost may work again and this guard can be revisited"
