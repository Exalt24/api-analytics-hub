#!/bin/sh
# Boots a real systemd as PID 1 in a Debian container, installs the four hub units with stand-in executables and
# proves what `systemd-analyze verify` cannot: the timer fires the service on its own schedule, the service runs as the
# `hub` user under ProtectSystem=strict, and a failed backup starts the alert check through OnFailure=.
# Needs Docker with Linux containers on a cgroup v2 host (Docker Desktop is). Nothing here touches a real database:
# the stand-in python records its arguments and exits 1 when a flag file exists. See docs/deploy.md.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME=hub-systemd-proof
IMAGE="${SYSTEMD_IMAGE:-jrei/systemd-debian:12}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup

docker run -d --name "$NAME" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /tmp "$IMAGE" >/dev/null || exit 2

# systemd needs a moment to finish booting; poll the real state, never a fixed sleep
i=0
while [ $i -lt 20 ]; do
  state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null | head -1)
  [ "$state" = running ] || [ "$state" = degraded ] && break
  i=$((i + 1)); sleep 1   # WAIT-OK: one second polls of the boot state, each pass prints nothing heavy
done
echo "1. systemd booted as PID 1: is-system-running = $state"
docker exec "$NAME" sh -c 'echo PID1=$(cat /proc/1/comm 2>/dev/null || readlink /proc/1/exe); systemctl --version | head -1'

# the stand-in for the venv python, written on the host with a quoted heredoc so nothing expands before it runs in the container
STANDIN="$HERE/.standin_python.tmp"
cat > "$STANDIN" <<'EOF'
#!/bin/sh
echo "$(date +%T) ran as $(id -un): $*" >> /var/lib/hub/ran.log
[ -e /var/lib/hub/fail ] && exit 1
exit 0
EOF
docker exec "$NAME" sh -c 'useradd --system hub 2>/dev/null || true; mkdir -p /opt/hub/.venv/bin /opt/hub/scripts /etc/hub /var/backups/hub /var/lib/hub'
(cd "$HERE" && docker cp "./.standin_python.tmp" "$NAME:/opt/hub/.venv/bin/python")
rm -f "$STANDIN"
docker exec "$NAME" sh -c '
set -e
chmod +x /opt/hub/.venv/bin/python
: > /opt/hub/scripts/db_backup.py
: > /etc/hub/hub.env
chown -R hub:hub /var/lib/hub /var/backups/hub
'
for u in hub-backup.service hub-backup.timer hub-backup-check.service hub-backup-check.timer; do
  (cd "$HERE" && docker cp "./$u" "$NAME:/etc/systemd/system/$u")
done
# a fast schedule on top of the shipped timer, so the run sees the timer itself fire: every 15 seconds, no random delay
docker exec "$NAME" sh -c '
mkdir -p /etc/systemd/system/hub-backup.timer.d
printf "[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:*:0/15\nRandomizedDelaySec=0\nPersistent=false\n" > /etc/systemd/system/hub-backup.timer.d/fast.conf
chmod 644 /etc/systemd/system/*.service /etc/systemd/system/*.timer
systemctl daemon-reload
systemctl enable --now hub-backup.timer hub-backup-check.timer 2>&1 | tail -2
'
echo "2. timers armed:"
docker exec "$NAME" sh -c 'systemctl list-timers --all --no-pager | grep -E "hub-backup|NEXT" | cut -c1-150'

echo "3. the timer fires the service on its own (watching ran.log for about 40 seconds):"
i=0
while [ $i -lt 8 ]; do
  n=$(docker exec "$NAME" sh -c 'grep -c " backup " /var/lib/hub/ran.log 2>/dev/null; true' | head -1)
  echo "   pass $i: backup runs recorded = $n"
  [ "$n" -ge 2 ] && break
  i=$((i + 1)); sleep 5   # WAIT-OK: five second polls of a counter that is printed on every pass
done
docker exec "$NAME" sh -c 'cat /var/lib/hub/ran.log | head -6'
docker exec "$NAME" sh -c 'systemctl show hub-backup.service -p Result -p ExecMainStatus -p User | tr "\n" " "; echo'

echo "4. a failed backup starts the alert check through OnFailure= (flag file makes the stand-in exit 1):"
docker exec "$NAME" sh -c 'systemctl stop hub-backup.timer; touch /var/lib/hub/fail; : > /var/lib/hub/ran.log; systemctl start hub-backup.service 2>&1 | tail -1; true'
sleep 2   # WAIT-OK: two seconds for the OnFailure unit to start
docker exec "$NAME" sh -c 'echo "   backup result: $(systemctl show hub-backup.service -p Result --value)"; echo "   ran.log:"; sed "s/^/     /" /var/lib/hub/ran.log; echo "   check unit ran: $(grep -c " check " /var/lib/hub/ran.log) time(s)"'

echo "5. ProtectSystem=strict really blocks a write outside ReadWritePaths:"
docker exec "$NAME" sh -c 'systemd-run --quiet --wait --collect -p User=hub -p ProtectSystem=strict -p ReadWritePaths=/var/lib/hub sh -c "touch /etc/should_fail 2>&1; echo exit=\$?; touch /var/lib/hub/ok && echo allowed_in_rw_path" 2>&1 | tail -3'
exit 0
