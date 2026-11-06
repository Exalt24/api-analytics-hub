"""The DigitalOcean setup in deploy/digitalocean/, checked as text so no account, token or network is needed.

`terraform validate` and a local plan with dummy values are run by hand (docs/digitalocean.md, output kept in docs/test-runs);
these tests pin the properties that must never quietly change, and each has a control that shows the check can fail:

  D1  SSH is reachable only from the admin ranges, never from the whole internet
  D2  the only ports open to the world are 80 and 443 (Postgres and the API are not listed at all)
  D3  the droplet has monitoring on and weekly backups on
  D4  CPU, memory and disk alerts exist and watch the droplet
  D5  cloud-init is valid YAML, carries no secret, and creates the secrets file empty and readable by root alone
  D6  the variable checks refuse an empty SSH key list, an open admin range and an empty alert list
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DIR = Path(__file__).resolve().parents[1] / "deploy" / "digitalocean"
WORLD = ("0.0.0.0/0", "::/0")


def _blocks(text: str, name: str) -> list[str]:
    """The bodies of every flat `name { ... }` block (these files nest nothing inside the blocks checked here)."""
    return re.findall(r"\b%s\s*\{(.*?)\n\s*\}" % re.escape(name), text, flags=re.S)


def _resource(text: str, kind: str, name: str) -> str:
    m = re.search(r'resource\s+"%s"\s+"%s"\s*\{(.*)' % (re.escape(kind), re.escape(name)), text, flags=re.S)
    assert m, f"resource {kind}.{name} not found"
    rest, depth, out = m.group(1), 1, []
    for ch in rest:
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            break
        out.append(ch)
    return "".join(out)


def open_to_world(main_tf: str) -> dict[str, bool]:
    """port -> True when an inbound rule on that port lists a whole-internet source, False when only named ranges."""
    ports: dict[str, bool] = {}
    for body in _blocks(_resource(main_tf, "digitalocean_firewall", "hub"), "inbound_rule"):
        port = re.search(r'port_range\s*=\s*"([^"]+)"', body).group(1)
        sources = re.search(r"source_addresses\s*=\s*(.+)", body).group(1)
        ports[port] = ports.get(port, False) or any(w in sources for w in WORLD)
    return ports


def droplet_flag(main_tf: str, flag: str) -> bool:
    m = re.search(r"\b%s\s*=\s*(true|false)" % flag, _resource(main_tf, "digitalocean_droplet", "hub"))
    return bool(m) and m.group(1) == "true"


def alert_types(main_tf: str) -> set[str]:
    found = set()
    for name in re.findall(r'resource\s+"digitalocean_monitor_alert"\s+"(\w+)"', main_tf):
        body = _resource(main_tf, "digitalocean_monitor_alert", name)
        if "digitalocean_droplet.hub.id" in body and re.search(r"enabled\s*=\s*true", body):
            found.add(re.search(r'type\s*=\s*"([^"]+)"', body).group(1))
    return found


def secret_lines(text: str) -> list[str]:
    """Lines that look like they carry a secret, ignoring comments and the word appearing in a sentence about secrets."""
    bad = []
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"(?i)(password|passwd|token|api[_-]?key)\s*[:=]\s*\S+", code) or re.search(r"BEGIN [A-Z ]*PRIVATE KEY|dop_v1_[0-9a-f]{20,}|://[^\s:@/]+:[^\s@/]+@", code):
            bad.append(line.strip())
    return bad


MAIN = (DIR / "main.tf").read_text(encoding="utf-8")
VARS = (DIR / "variables.tf").read_text(encoding="utf-8")
INIT = (DIR / "cloud-init.yaml").read_text(encoding="utf-8")


def test_d1_ssh_is_not_open_to_the_world():
    assert open_to_world(MAIN)["22"] is False


def test_d1_control_an_open_ssh_rule_is_caught():
    broken = MAIN.replace("source_addresses = var.admin_cidrs", 'source_addresses = ["0.0.0.0/0"]')
    assert broken != MAIN
    assert open_to_world(broken)["22"] is True


def test_d2_only_web_ports_are_public():
    assert {p for p, world in open_to_world(MAIN).items() if world} == {"80", "443"}


def test_d2_control_a_database_port_rule_is_caught():
    broken = MAIN.replace('port_range       = "443"', 'port_range       = "5432"')
    assert broken != MAIN
    assert {p for p, world in open_to_world(broken).items() if world} != {"80", "443"}


def test_d3_droplet_has_monitoring_and_backups():
    assert droplet_flag(MAIN, "monitoring") and droplet_flag(MAIN, "backups")


def test_d3_control_turning_either_off_is_caught():
    for flag in ("monitoring", "backups"):
        broken = re.sub(r"(\b%s\s*=\s*)true" % flag, r"\1false", MAIN, count=1)
        assert broken != MAIN
        assert not (droplet_flag(broken, "monitoring") and droplet_flag(broken, "backups"))


def test_d4_cpu_memory_and_disk_alerts_watch_the_droplet():
    assert alert_types(MAIN) == {
        "v1/insights/droplet/cpu",
        "v1/insights/droplet/memory_utilization_percent",
        "v1/insights/droplet/disk_utilization_percent",
    }


def test_d4_control_an_alert_not_attached_to_the_droplet_is_caught():
    broken = MAIN.replace("entities    = [digitalocean_droplet.hub.id]", "entities    = []", 1)
    assert broken != MAIN
    assert len(alert_types(broken)) == 2


def test_d5_cloud_init_is_valid_yaml_with_the_header():
    assert INIT.startswith("#cloud-config")
    # the two template placeholders are replaced before parsing, as Terraform does
    doc = yaml.safe_load(INIT.replace("${hostname}", "hub").replace("${backup_mount}", "/mnt/hub_backups"))
    assert doc["ssh_pwauth"] is False and "docker.io" in doc["packages"]


def test_d5_cloud_init_carries_no_secret():
    assert secret_lines(INIT) == []


def test_d5_control_a_planted_secret_is_caught():
    assert secret_lines(INIT + "\n  - export DB_PASSWORD=hunter2\n")
    assert secret_lines("token: dop_v1_" + "a" * 64)
    assert secret_lines("url: postgres://app:s3cret@db/hub")


def test_d5_secrets_file_is_created_empty_and_root_only():
    doc = yaml.safe_load(INIT.replace("${hostname}", "hub").replace("${backup_mount}", "/mnt/hub_backups"))
    env = next(f for f in doc["write_files"] if f["path"] == "/etc/hub/hub.env")
    assert env["permissions"] == "0600" and env["owner"] == "root:root"
    assert [l for l in env["content"].splitlines() if l.strip() and not l.lstrip().startswith("#")] == []


def test_d6_variable_checks_refuse_the_unsafe_inputs():
    assert 'contains(var.admin_cidrs, "0.0.0.0/0")' in VARS and 'contains(var.admin_cidrs, "::/0")' in VARS
    assert "length(var.ssh_key_fingerprints) > 0" in VARS
    assert "length(var.alert_emails) > 0" in VARS


def test_provider_token_is_never_in_the_folder():
    for path in DIR.iterdir():
        if path.is_file() and path.suffix in {".tf", ".yaml", ".hcl"}:
            assert secret_lines(path.read_text(encoding="utf-8")) == [], path.name
