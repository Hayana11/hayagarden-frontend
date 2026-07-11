#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/app"

echo "[dash] installing frontend deps (dev included)..."
NODE_ENV=development npm ci --ignore-scripts

echo "[dash] building production bundle..."
npm run build

echo "[dash] reloading frontend.service (zero-downtime graceful reload)..."
sudo systemctl reload frontend.service

if [[ -f /opt/frontend/app/dist/index.html ]]; then
  echo "[dash] deployed: /opt/frontend/app/dist/index.html"
else
  echo "[dash] build finished but dist not found at /opt/frontend/app/dist" >&2
  exit 1
fi
