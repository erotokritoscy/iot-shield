#!/bin/sh
# Rebuild the dashboard from this checkout and restart it:  sh scripts/deploy.sh
set -e
cd "$(dirname "$0")/.."

docker compose build --no-cache dashboard-web
docker compose up -d --force-recreate dashboard-web
