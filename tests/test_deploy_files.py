"""Static checks on deploy/: the Caddyfile and the four systemd units.

The runtime proof (caddy validate, a request through a running Caddy, systemd-analyze verify in a Debian container) is in
docs/deploy.md; these tests pin the properties that proof found, so a later edit cannot quietly undo them.
"""
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = ["hub-backup.service", "hub-backup.timer", "hub-backup-check.service", "hub-backup-check.timer"]


def read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def keys(text, key):
    """Every value of `key=` in a unit file (a oneshot service may repeat ExecStart)."""
    return [m.group(1).strip() for m in re.finditer(r"^%s=(.*)$" % re.escape(key), text, re.M)]


def test_every_unit_file_exists():
    for u in UNITS:
        assert (DEPLOY / u).is_file(), u


@pytest.mark.parametrize("name", ["hub-backup.service", "hub-backup-check.service"])
def test_services_run_as_the_hub_user_and_are_sandboxed(name):
    t = read(name)
    assert keys(t, "Type") == ["oneshot"]
    assert keys(t, "User") == ["hub"]
    assert keys(t, "NoNewPrivileges") == ["true"]
    assert keys(t, "ProtectSystem") == ["strict"]


def test_the_calendar_timer_catches_up_after_downtime():
    assert keys(read("hub-backup.timer"), "OnCalendar")
    assert keys(read("hub-backup.timer"), "Persistent") == ["true"]


def test_the_interval_timer_does_not_rely_on_persistent():
    # systemd documents Persistent= as having an effect only on OnCalendar= timers, so setting it here would be a promise nothing keeps.
    t = read("hub-backup-check.timer")
    assert not keys(t, "OnCalendar")
    assert not keys(t, "Persistent")
    assert keys(t, "OnBootSec") and keys(t, "OnUnitActiveSec")


def test_a_failed_backup_runs_the_alert_check_at_once():
    assert keys(read("hub-backup.service"), "OnFailure") == ["hub-backup-check.service"]
    assert (DEPLOY / "hub-backup-check.service").is_file()


def test_the_backup_unit_runs_backup_then_prove_in_that_order():
    cmds = keys(read("hub-backup.service"), "ExecStart")
    subcommands = [re.search(r"db_backup\.py (\w+)", c).group(1) for c in cmds]
    assert subcommands == ["backup", "prove"]


def test_unit_commands_use_flags_the_script_accepts():
    script = (DEPLOY.parent / "scripts" / "db_backup.py").read_text(encoding="utf-8")
    for name in ("hub-backup.service", "hub-backup-check.service"):
        for cmd in keys(read(name), "ExecStart"):
            sub = re.search(r"db_backup\.py (\w+)", cmd).group(1)
            assert 'add_parser' in script and sub in script
            for flag in re.findall(r"(--[a-z-]+)", cmd):
                assert 'add_argument("%s"' % flag in script, "%s: %s is not a db_backup.py option" % (name, flag)


def test_every_directory_a_unit_writes_is_allowed_by_its_sandbox():
    for name in ("hub-backup.service", "hub-backup-check.service"):
        t = read(name)
        rw = " ".join(keys(t, "ReadWritePaths"))
        ro = " ".join(keys(t, "ReadOnlyPaths"))
        for cmd in keys(t, "ExecStart"):
            m = re.search(r"--dir (\S+)", cmd)
            if m and "check" not in cmd:
                assert m.group(1) in rw, "%s writes %s outside ReadWritePaths" % (name, m.group(1))
            s = re.search(r"--state (\S+)", cmd)
            if s:
                assert str(Path(s.group(1)).parent).replace("\\", "/") in rw
        assert "/var/backups/hub" in rw + ro


def test_the_check_timer_interval_is_shorter_than_the_alert_repeat_window():
    interval = int(re.search(r"(\d+)min", keys(read("hub-backup-check.timer"), "OnUnitActiveSec")[0]).group(1))
    assert interval * 60 < 6 * 3600


def test_caddy_never_proxies_metrics_and_does_it_inside_a_handle():
    c = read("Caddyfile")
    block = re.search(r"@private path ([^\n]+)\n\s*handle @private \{\s*respond 404\s*\}", c)
    assert block, "the /metrics 404 must live inside `handle @private { respond 404 }`: a bare respond is ordered after handle and the catch-all answered it"
    assert "/metrics" in block.group(1) and "/metrics/*" in block.group(1)
    assert not re.search(r"^\s*respond @private", c, re.M)


def test_caddy_upstreams_and_site_come_from_the_environment_with_defaults():
    c = read("Caddyfile")
    for var, default in (("HUB_SITE", "hub.localhost"), ("HUB_API_UPSTREAM", "127.0.0.1:8000"), ("HUB_WEB_UPSTREAM", "127.0.0.1:3100")):
        assert "{$%s:%s}" % (var, default) in c


def test_caddy_sets_the_security_headers_and_hides_the_server_name():
    c = read("Caddyfile")
    for h in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "-Server"):
        assert h in c
