"""Where the live scripts find their secret files, and that no file names a path on anyone's machine.

The scripts that talk to a real store read a token file. They used to carry the author's own
path; now each one asks `app/secret_files.py`, which reads an environment variable, then
`.secrets/` inside the repository, and otherwise says plainly which variable to set.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.secret_files import DEV_APP, DEV_TOKEN, SECRETS_DIR, STOREFRONT_TOKEN, SecretFile


def test_the_environment_variable_names_the_file(monkeypatch, tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("SHOP=x.myshopify.com\nACCESS_TOKEN=abc\n", encoding="utf-8")
    monkeypatch.setenv("SHOPIFY_DEV_TOKEN_FILE", str(token))
    assert DEV_TOKEN.path() == token
    assert "ACCESS_TOKEN=abc" in DEV_TOKEN.read()


def test_with_no_variable_the_default_is_inside_the_repository(monkeypatch):
    monkeypatch.delenv("SHOPIFY_DEV_TOKEN_FILE", raising=False)
    assert DEV_TOKEN.path() == SECRETS_DIR / "shopify_dev_token.txt"
    assert SECRETS_DIR == ROOT / ".secrets"


def test_a_missing_file_is_a_plain_error_naming_the_variable_and_no_other_place(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOPIFY_DEV_TOKEN_FILE", str(tmp_path / "absent.txt"))
    with pytest.raises(SystemExit) as caught:
        DEV_TOKEN.read()
    message = str(caught.value)
    assert "SHOPIFY_DEV_TOKEN_FILE" in message and ".secrets/shopify_dev_token.txt" in message
    assert str(tmp_path) not in message and "Operations" not in message


def test_each_secret_has_its_own_variable():
    assert len({f.env for f in (DEV_TOKEN, DEV_APP, STOREFRONT_TOKEN)}) == 3


def test_a_written_file_lands_where_the_variable_says_and_creates_its_folder(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "out.txt"
    monkeypatch.setenv("SHOPIFY_STOREFRONT_TOKEN_FILE", str(target))
    STOREFRONT_TOKEN.write("SHOP=x\nSTOREFRONT_TOKEN=y\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8").endswith("STOREFRONT_TOKEN=y\n")


def test_the_dataclass_takes_any_variable_name(monkeypatch, tmp_path):
    other = SecretFile("SOME_OTHER_FILE", "other.txt", "anything")
    monkeypatch.setenv("SOME_OTHER_FILE", str(tmp_path / "o.txt"))
    assert other.path() == tmp_path / "o.txt"


#: A drive path or a home folder in a source, doc or config file is someone's machine.
MACHINE_PATH = re.compile(r"\b[A-Za-z]:[\\/](?:Users|Projects|Program Files|Windows)|[\\/]Operations[\\/]\.secrets|/home/[a-z]+/")
SKIP_DIRS = {".venv", "web", ".git", "node_modules", ".next", ".pytest_cache", "__pycache__", "test-runs", ".secrets"}


def tracked_text_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".toml", ".yml", ".yaml", ".sql", ".cjs", ".txt"}:
            if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
                yield path


def test_no_source_doc_or_config_file_names_a_path_on_anyones_machine():
    offenders = []
    for path in tracked_text_files():
        if path.name == "test_secret_files.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if MACHINE_PATH.search(line):
                offenders.append("%s:%d" % (path.relative_to(ROOT).as_posix(), number))
    assert offenders == []
