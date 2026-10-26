set -e
d=$(find /usr/local/lib/node_modules/n8n/node_modules/.pnpm -maxdepth 1 -type d -name 'cron@*' | head -1)
echo "cron package: $d"
cd "$d/node_modules/cron"
node /data/cron_probe.js "$PWD"
