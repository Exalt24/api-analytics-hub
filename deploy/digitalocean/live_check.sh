#!/usr/bin/env bash
# Prove deploy/digitalocean/ on a real DigitalOcean account and leave nothing behind.
#
#   export DIGITALOCEAN_TOKEN=...        # in the shell only, never in a file of this repo (docs/secrets.md)
#   export ALERT_EMAIL=you@example.com
#   bash live_check.sh --plan-only       # init, validate and a real plan; creates nothing, costs nothing
#   bash live_check.sh                   # apply, wait for first boot, check, destroy, confirm the account is empty
#
# A full run costs a few cents: the smallest sensible droplet (s-1vcpu-1gb, billed per second), a 1 GB volume and
# weekly backups, for about ten minutes. It copies the Terraform to a temp folder so no state file lands in the repo,
# and an exit trap destroys whatever it created even when a step fails or you press Ctrl-C.
# What it checks is printed as it goes and saved to ./live_check_evidence.txt (no token, no key, no address of a person).
set -u

PLAN_ONLY=0
[ "${1:-}" = "--plan-only" ] && PLAN_ONLY=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API="https://api.digitalocean.com/v2"
SIZE="${SIZE:-s-1vcpu-1gb}"
PY=""
for cand in python3 python; do
  # On Windows `python3` can be a Store stub that exists but does not run, so ask each one to do real work.
  if command -v "$cand" >/dev/null && "$cand" -c "import json" >/dev/null 2>&1; then PY="$cand"; break; fi
done

die() { echo "STOP: $*" >&2; exit 2; }
[ -n "${DIGITALOCEAN_TOKEN:-}" ] || die "set DIGITALOCEAN_TOKEN in the environment"
[ -n "${ALERT_EMAIL:-}" ] || die "set ALERT_EMAIL (the alert rules need an address)"
for tool in terraform curl ssh ssh-keygen; do command -v "$tool" >/dev/null || die "$tool is not installed"; done
[ -n "$PY" ] || die "python is needed to read the API's JSON"

api() { curl -s -m 30 -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" "$@"; }
pyj() { "$PY" -c "import sys,json; d=json.load(sys.stdin); $1"; }

WORK="$(mktemp -d)"
EVID="$PWD/live_check_evidence.txt"
KEY_ID=""
CREATED=0
note() { echo "$*" | tee -a "$EVID"; }

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  cd "$WORK" 2>/dev/null || exit "$code"
  if [ "$CREATED" = 1 ]; then
    echo "cleanup: terraform destroy"
    terraform destroy -input=false -auto-approve -var-file=run.tfvars >/dev/null 2>&1 || echo "WARNING: destroy failed, look at the account by hand"
  fi
  if [ -n "$KEY_ID" ]; then
    api -o /dev/null -X DELETE "$API/account/keys/$KEY_ID"
  fi
  if [ "$PLAN_ONLY" = 0 ] && [ "$CREATED" = 1 ]; then
    local left=0 ep n
    for ep in droplets volumes firewalls account/keys monitoring/alerts snapshots; do
      n=$(api "$API/$ep" | pyj 'print(sum(len(v) for k,v in d.items() if isinstance(v, list)))' 2>/dev/null || echo "?")
      note "account after destroy: $ep = $n"
      [ "$n" = "0" ] || left=1
    done
    [ "$left" = 0 ] && note "RESULT: account is empty" || note "RESULT: SOMETHING IS LEFT, check the lines above and delete it"
  fi
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT INT TERM

: > "$EVID"
note "live check of deploy/digitalocean, $(date -u +%Y-%m-%dT%H:%MZ), $(terraform version | head -1), size $SIZE"
cp "$HERE"/*.tf "$HERE"/cloud-init.yaml "$HERE"/.terraform.lock.hcl "$WORK"/ || die "could not copy the Terraform files"
cd "$WORK" || die "no work folder"

# Refuse to run next to resources we did not make: the final emptiness check would be meaningless.
pre=$(api "$API/droplets" | pyj 'print(len(d.get("droplets", [])))' 2>/dev/null || echo "?")
[ "$pre" = "0" ] || die "the account already has $pre droplet(s) (or the token is wrong); this check expects an empty account"

if [ "$PLAN_ONLY" = 1 ]; then
  FP="00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00"
else
  ssh-keygen -t ed25519 -N "" -C "hub-live-check" -f key -q || die "ssh-keygen failed"
  KEY_JSON=$(api -X POST -H "Content-Type: application/json" \
    -d "{\"name\":\"hub-live-check\",\"public_key\":\"$(cat key.pub)\"}" "$API/account/keys")
  KEY_ID=$(echo "$KEY_JSON" | pyj 'print(d["ssh_key"]["id"])') || die "could not upload the throwaway SSH key"
  FP=$(echo "$KEY_JSON" | pyj 'print(d["ssh_key"]["fingerprint"])')
fi
MYIP=$(curl -s -m 15 https://api.ipify.org)
[ -n "$MYIP" ] || die "could not find this machine's public address"

cat > run.tfvars <<EOF
size                 = "$SIZE"
data_volume_gb       = 1
ssh_key_fingerprints = ["$FP"]
admin_cidrs          = ["$MYIP/32"]
alert_emails         = ["$ALERT_EMAIL"]
EOF

terraform init -input=false >/dev/null || die "terraform init failed"
terraform validate >/dev/null || die "terraform validate failed"
terraform plan -input=false -var-file=run.tfvars -out=tf.plan | sed 's/\x1b\[[0-9;]*m//g' | grep -E "^Plan:" | tee -a "$EVID"
if [ "$PLAN_ONLY" = 1 ]; then note "plan only: nothing was created"; exit 0; fi

CREATED=1
terraform apply -input=false tf.plan >/dev/null || die "terraform apply failed"
IP=$(terraform output -raw droplet_ipv4)
ID=$(terraform state show digitalocean_droplet.hub | grep -E '^\s+id ' | head -1 | tr -dc 0-9)
SSH="ssh -i key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes root@$IP"

state=""
for _ in $(seq 1 60); do
  state=$($SSH "cloud-init status" 2>/dev/null | tr -d '\n')
  case "$state" in *done*|*error*) break;; esac
  sleep 8
done
note "first boot: ${state:-no answer over SSH}"

$SSH 'echo "os: $(. /etc/os-release; echo $PRETTY_NAME)"
for s in docker caddy fail2ban unattended-upgrades droplet-agent; do echo "$s: $(systemctl is-active $s)"; done
echo "hub.env: mode $(stat -c %a /etc/hub/hub.env), $(grep -vc "^#" /etc/hub/hub.env) non-comment lines"
echo "/etc/hub mode: $(stat -c %a /etc/hub)"
sshd -T | grep -i "^passwordauthentication"
echo "backup volume: $(readlink -f /var/backups/hub)"' 2>/dev/null | tee -a "$EVID"

for p in 22 80 443 5432 6379; do
  if timeout 4 bash -c "echo > /dev/tcp/$IP/$p" 2>/dev/null; then note "port $p from outside: open"; else note "port $p from outside: closed or filtered"; fi
done
api "$API/droplets/$ID/backups/policy" | pyj 'p=d["policy"]; print("backup policy:", p["backup_policy"])' | tee -a "$EVID"
api "$API/monitoring/alerts" | pyj '[print("alert:", a["type"], a["compare"], a["value"], a["window"]) for a in d["policies"]]' | tee -a "$EVID"
terraform plan -input=false -var-file=run.tfvars -detailed-exitcode >/dev/null 2>&1
note "plan after apply, exit code (0 = no changes): $?"
note "done; the exit trap destroys everything now"
exit 0
