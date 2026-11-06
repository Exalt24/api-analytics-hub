"""No secret reaches the repository, checked over every file git would commit (tracked, or new and not ignored).

What counts as a secret here: a DigitalOcean token, a private key block, a live Stripe key, a Shopify, Slack, GitHub or AWS
access token, and a database URL with a password that is not a loopback or placeholder host. The one credentialed URL the
repository documents (`analytics:analytics@127.0.0.1:5433`, the throwaway database in compose.yaml) is on loopback, so it passes
by rule and not by a list of files. A pattern that cannot fail proves nothing, so each one has a control below that plants a
sample and shows it is caught.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "digitalocean token": re.compile(r"dop_v1_[0-9a-f]{40,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "stripe live key": re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}"),
    "shopify token": re.compile(r"\bshp(?:at|ca|pa|ss)_[0-9a-f]{32}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{20,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"),
}
URL_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s:@/'\"]+):([^\s@/'\"<>{}$]{3,})@([^\s:/'\"?]+)")
# Hosts a credentialed URL may use: this machine, or a stand-in name in a test or a docstring.
SAFE_HOSTS = {"127.0.0.1", "localhost", "host", "h", "db", "hub-db", "example.com"}


def findings(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        for name, pat in PATTERNS.items():
            if pat.search(line):
                out.append(name)
        for m in URL_PASSWORD.finditer(line):
            if m.group(3) not in SAFE_HOSTS:
                out.append("database url with a password on host " + m.group(3))
    return out


def _files() -> list[Path]:
    """Tracked files plus new files git has not ignored: what the next commit could contain."""
    names = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, encoding="utf-8", check=True, timeout=60,
    ).stdout.splitlines()
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def test_nothing_in_the_repository_looks_like_a_secret():
    bad = {}
    scanned = 0
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary file (an image, a font, a pdf) holds no readable token
        scanned += 1
        found = findings(text)
        if found:
            bad[str(path.relative_to(ROOT))] = sorted(set(found))
    assert scanned > 100, f"only {scanned} files were scanned: the file listing is broken, not clean"
    assert bad == {}


def test_terraform_state_and_variable_files_are_ignored():
    """State files hold every value Terraform ever read, secrets included, so they must never be committable."""
    for name in ("deploy/digitalocean/terraform.tfstate", "deploy/digitalocean/terraform.tfvars", "deploy/digitalocean/.terraform/x"):
        ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, timeout=30).returncode == 0
        assert ignored, f"{name} is not ignored"


def test_control_each_pattern_catches_a_planted_sample():
    samples = {
        "digitalocean token": "token = dop_v1_" + "ab12" * 16,
        "private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "stripe live key": "key = sk_live_" + "A1b2C3d4" * 3,
        "shopify token": "shpat_" + "0123abcd" * 4,
        "slack token": "xoxb-" + "1234567890-abcdefghij" * 2,
        "aws access key": "AKIA" + "ABCDEFGHIJKLMNOP",
        "github token": "ghp_" + "a1B2c3D4e5" * 4,
    }
    assert set(samples) == set(PATTERNS)
    for name, sample in samples.items():
        assert name in findings(sample), name


def test_control_a_database_url_on_a_real_host_is_caught_and_loopback_is_not():
    # built from pieces so this file never holds a whole credentialed URL on a real host, which the scan above would flag
    scheme = "postgresql" + "://"
    assert findings(scheme + "app:" + "hunter22" + "@db.example.org:5432/hub")
    assert findings(scheme + "analytics:" + "analytics" + "@10.0.0.5:5433/analytics")
    assert findings(scheme + "analytics:analytics@127.0.0.1:5433/analytics") == []
    assert findings(scheme + "user:pass@host:port/db") == []
