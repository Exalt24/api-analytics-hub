#!/bin/sh
# Runs inside a Debian container (see docs/deploy.md): installs systemd, creates the paths the units name, then
# `systemd-analyze verify` every unit. verify fails on an unknown directive, a missing ExecStart binary or a bad timer.
set -e
apt-get update -qq >/dev/null
apt-get install -y -qq systemd >/dev/null
useradd --system hub 2>/dev/null || true
mkdir -p /opt/hub/.venv/bin /opt/hub/scripts /etc/hub /var/backups/hub /var/lib/hub
printf '#!/bin/sh\n' > /opt/hub/.venv/bin/python && chmod +x /opt/hub/.venv/bin/python
: > /opt/hub/scripts/db_backup.py
: > /etc/hub/hub.env
cp /deploy/hub-backup.service /deploy/hub-backup.timer /deploy/hub-backup-check.service /deploy/hub-backup-check.timer /etc/systemd/system/
cd /etc/systemd/system
chmod 644 hub-backup.service hub-backup.timer hub-backup-check.service hub-backup-check.timer   # a Windows mount marks them executable
# BREAK=1 points one ExecStart at a binary that does not exist, to show verify really fails on a bad unit
[ "${BREAK:-}" = 1 ] && sed -i 's#/opt/hub/.venv/bin/python#/opt/hub/.venv/bin/nope#' hub-backup.service
systemd-analyze verify hub-backup.service hub-backup.timer hub-backup-check.service hub-backup-check.timer
echo "UNITS OK"
systemd-analyze calendar '*-*-* 02:30:00' | grep -E "Normalized|Next elapse"
