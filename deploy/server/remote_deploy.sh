#!/usr/bin/env bash
set -euo pipefail

# This script runs on the target host after the repository has been copied.
# It keeps uploaded datasets outside the release copy and restarts only the
# OilCode service after a successful dependency/install step.

APP_ROOT="${OILCODE_APP_ROOT:-$HOME/oilcode}"
VENV="$APP_ROOT/.venv"
PORT="${OILCODE_PORT:-8000}"
DEPLOY_USER="${OILCODE_USER:-$(id -un)}"

cd "$APP_ROOT"
mkdir -p "$APP_ROOT/storage"

if ! python3 -m venv "$VENV" 2>/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip
  python3 -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/pip" install --upgrade -r requirements.txt

SERVICE_FILE="/tmp/oilcode.service.$$"
sed \
  -e "s#__APP_ROOT__#$APP_ROOT#g" \
  -e "s#__VENV__#$VENV#g" \
  -e "s#__PORT__#$PORT#g" \
  -e "s#__DEPLOY_USER__#$DEPLOY_USER#g" \
  deploy/server/oilcode.service.in > "$SERVICE_FILE"
sudo install -m 0644 "$SERVICE_FILE" /etc/systemd/system/oilcode.service
rm -f "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable oilcode.service
sudo systemctl restart oilcode.service

if command -v nginx >/dev/null 2>&1 && [ -f deploy/server/nginx.conf ]; then
  sudo install -m 0644 deploy/server/nginx.conf /etc/nginx/sites-available/oilcode
  sudo ln -sfn /etc/nginx/sites-available/oilcode /etc/nginx/sites-enabled/oilcode
  sudo nginx -t
  sudo systemctl enable nginx
  sudo systemctl reload nginx
fi

curl --fail --silent --show-error "http://127.0.0.1:${PORT}/api/health"
printf '\nOilCode deployment is healthy at %s\n' "$(date --iso-8601=seconds)"
