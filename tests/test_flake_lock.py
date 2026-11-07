"""flake.lock must agree with the inputs written in flake.nix.

`nix flake check --no-update-lock-file` says the same thing, but it needs Nix. This test reads the two files as text and JSON, so it runs on any machine and in the
Python job of CI, and it fails when someone edits an input in flake.nix (a new branch, a different repository, an added or removed input) without running `nix flake lock`.

It supports what flake.nix uses: `inputs.<name>.url = "github:<owner>/<repo>[/<ref>]";`. A different style, or a url scheme it does not know, fails the test with the reason
instead of passing on nothing; teach `parse_url` the new shape in the same change.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INPUT_RE = re.compile(r'inputs\.([A-Za-z0-9_-]+)\.url\s*=\s*"([^"]*)"\s*;')
URL_KEY_RE = re.compile(r"\burl\s*=")


def parse_inputs(flake_nix: str) -> dict[str, str]:
    """{input name: url} for every `inputs.<name>.url = "...";` line, or a ValueError when the file uses a shape this does not read."""
    code = "\n".join(line.split("#", 1)[0] for line in flake_nix.splitlines())
    found = dict(INPUT_RE.findall(code))
    if not found:
        raise ValueError("flake.nix has no `inputs.<name>.url = \"...\";` line this test can read")
    if len(URL_KEY_RE.findall(code)) != len(INPUT_RE.findall(code)):
        raise ValueError("flake.nix has a `url =` that is not in the `inputs.<name>.url = \"...\";` form, so part of it would go unchecked")
    return found


def parse_url(url: str) -> dict[str, str]:
    """`github:owner/repo[/ref]` as the fields flake.lock keeps under `original`."""
    m = re.fullmatch(r"github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/([^?#]+))?", url)
    if not m:
        raise ValueError(f"url {url!r} is not github:<owner>/<repo>[/<ref>], the only form this test reads")
    owner, repo, ref = m.groups()
    out = {"type": "github", "owner": owner, "repo": repo}
    if ref:
        out["ref"] = ref
    return out


def drift(flake_nix: str, lock_text: str) -> list[str]:
    """What is wrong between the two files; an empty list when the lock matches."""
    problems: list[str] = []
    try:
        wanted = parse_inputs(flake_nix)
    except ValueError as exc:
        return [str(exc)]
    lock = json.loads(lock_text)
    if lock.get("version") != 7:
        problems.append(f"flake.lock version is {lock.get('version')!r}, expected 7")
    nodes = lock["nodes"]
    root_inputs = nodes[lock["root"]].get("inputs", {})
    for name in sorted(set(wanted) - set(root_inputs)):
        problems.append(f"input {name!r} is in flake.nix and not in flake.lock: run `nix flake lock`")
    for name in sorted(set(root_inputs) - set(wanted)):
        problems.append(f"input {name!r} is in flake.lock and not in flake.nix: run `nix flake lock`")
    for name, url in wanted.items():
        target = root_inputs.get(name)
        if not isinstance(target, str):
            continue                                    # a follows list or a missing input; reported above or not ours to judge
        node = nodes[target]
        try:
            expected = parse_url(url)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if node.get("original") != expected:
            problems.append(f"input {name!r}: flake.nix says {expected}, flake.lock was made from {node.get('original')}: run `nix flake lock`")
        locked = node.get("locked", {})
        for key in ("rev", "narHash"):
            if not locked.get(key):
                problems.append(f"input {name!r}: flake.lock has no locked {key}")
        if (locked.get("owner"), locked.get("repo")) != (expected["owner"], expected["repo"]):
            problems.append(f"input {name!r}: locked source {locked.get('owner')}/{locked.get('repo')} is not {expected['owner']}/{expected['repo']}")
    reachable = {lock["root"]} | {t for t in root_inputs.values() if isinstance(t, str)}
    for name in sorted(set(nodes) - reachable):
        problems.append(f"flake.lock holds a node {name!r} that no input reaches")
    return problems


def test_flake_lock_matches_flake_nix():
    assert drift((ROOT / "flake.nix").read_text(encoding="utf-8"), (ROOT / "flake.lock").read_text(encoding="utf-8")) == []


def test_every_test_file_the_fast_command_names_exists():
    text = (ROOT / "flake.nix").read_text(encoding="utf-8")
    m = re.search(r'fastTestArgs\s*=\s*"([^"]+)"', text)
    assert m, "flake.nix has no fastTestArgs"
    names = m.group(1).split()
    assert names, "fastTestArgs is empty"
    missing = [n for n in names if not (ROOT / n).is_file()]
    assert missing == []


# The same check, shown to fail: each drift below is a way flake.nix and flake.lock come apart, and drift() must name it.

NIX = 'outputs = { self, nixpkgs }: {};\n  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";\n'


def lock(original: dict, locked: dict | None = None, extra_nodes: dict | None = None, extra_inputs: dict | None = None, version: int = 7) -> str:
    locked = locked if locked is not None else {"type": "github", "owner": original["owner"], "repo": original["repo"], "rev": "a" * 40, "narHash": "sha256-x"}
    nodes = {"nixpkgs": {"original": original, "locked": locked}, "root": {"inputs": {"nixpkgs": "nixpkgs", **(extra_inputs or {})}}}
    nodes.update(extra_nodes or {})
    return json.dumps({"nodes": nodes, "root": "root", "version": version})


GOOD_ORIGINAL = {"type": "github", "owner": "NixOS", "repo": "nixpkgs", "ref": "nixos-26.05"}


def test_a_matching_pair_has_no_drift():
    assert drift(NIX, lock(GOOD_ORIGINAL)) == []


@pytest.mark.parametrize(
    "nix,lock_text,fragment",
    [
        (NIX.replace("nixos-26.05", "nixos-26.11"), lock(GOOD_ORIGINAL), "flake.lock was made from"),
        (NIX.replace("NixOS/nixpkgs", "someone/nixpkgs"), lock(GOOD_ORIGINAL), "flake.lock was made from"),
        (NIX.replace("/nixos-26.05", ""), lock(GOOD_ORIGINAL), "flake.lock was made from"),
        (NIX + '  inputs.other.url = "github:a/b";\n', lock(GOOD_ORIGINAL), "'other' is in flake.nix and not in flake.lock"),
        (NIX.replace("nixpkgs.url", "nixpkgz.url"), lock(GOOD_ORIGINAL), "'nixpkgs' is in flake.lock and not in flake.nix"),
        (NIX, lock(GOOD_ORIGINAL, locked={"type": "github", "owner": "NixOS", "repo": "nixpkgs", "narHash": "sha256-x"}), "no locked rev"),
        (NIX, lock(GOOD_ORIGINAL, locked={"type": "github", "owner": "NixOS", "repo": "nixpkgs", "rev": "b" * 40}), "no locked narHash"),
        (NIX, lock(GOOD_ORIGINAL, locked={"type": "github", "owner": "evil", "repo": "nixpkgs", "rev": "b" * 40, "narHash": "sha256-x"}), "locked source evil/nixpkgs"),
        (NIX, lock(GOOD_ORIGINAL, extra_nodes={"orphan": {"original": GOOD_ORIGINAL}}), "no input reaches"),
        (NIX, lock(GOOD_ORIGINAL, version=6), "version is 6"),
    ],
)
def test_each_way_the_files_come_apart_is_named(nix, lock_text, fragment):
    found = drift(nix, lock_text)
    assert found, "drift went unnoticed"
    assert any(fragment in line for line in found), found


@pytest.mark.parametrize(
    "nix,fragment",
    [
        ("outputs = {};\n", "no `inputs.<name>.url"),
        ('inputs = { nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05"; };\n', "no `inputs.<name>.url"),
        (NIX + '  inputs.more = { url = "github:a/b"; };\n', "not in the `inputs.<name>.url"),
        ('inputs.nixpkgs.url = "git+https://example.com/x";\n', "only form this test reads"),
    ],
)
def test_a_flake_nix_it_cannot_read_fails_instead_of_passing(nix, fragment):
    found = drift(nix, lock(GOOD_ORIGINAL))
    assert found and fragment in found[0], found


def test_a_commented_out_input_is_not_an_input():
    nix = NIX + '  # inputs.old.url = "github:a/b";\n'
    assert drift(nix, lock(GOOD_ORIGINAL)) == []
