#!/usr/bin/env bash
# Rebuild and recreate a running deployment from the code checked out here, then check it came up.
#
#   scripts/deploy.sh              # on the server, from anywhere inside the checkout
#
# It does not touch git: update the checkout first (git pull, or however the server gets its
# code). What is on disk is what gets deployed, local edits included. In order:
#   1. shows the commit being deployed and any local changes riding along with it;
#   2. warns if .env lacks the SensiPro/Biology passwords (never prints them);
#   3. rebuilds and recreates EVERY service. All of them, because nginx in dashboard-web
#      resolves the APIs' addresses once at start: recreating an API alone leaves the
#      dashboard proxying to a container that no longer exists. Recreating (not restarting)
#      is also what makes the single-file bind mount of connections.yaml show the new file.
#      Never `down -v`: that deletes the database;
#   4. waits for both APIs and the dashboard's proxy;
#   5. checks the dashboard serves this checkout's web/public/samples/manifest.json (the ready
#      scenarios) and, if not, rebuilds dashboard-web with --no-cache and recreates it;
#   6. prints what the stack now serves.

# `sh deploy.sh` runs this under dash, which has no pipefail: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

say() { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

say "deploying $(git branch --show-current) at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
dirty=$(git status --porcelain --untracked-files=no)
[ -z "$dirty" ] || printf 'with these local changes:\n%s\n' "$dirty"

if [ -f .env ]; then
  for var in SENSIPRO_DB_PASSWORD BIOLOGY_DB_PASSWORD; do
    grep -qE "^${var}=.+" .env || echo "warning: ${var} is empty or missing in .env — that pull will fail"
  done
else
  echo "warning: no .env — compose defaults apply and the SensiPro/Biology pulls will fail"
fi

say "rebuilding and recreating every service (data is kept)"
docker compose up -d --build --force-recreate

say "waiting for the stack"
wait_for() { # name url
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null -m 5 "$2" && { echo "ok    $1"; return 0; }
    sleep 2
  done
  return 1
}
wait_for "read API      :8000" http://127.0.0.1:8000/health || fail "the read API did not come up: docker compose logs api"
wait_for "Pipeline API  :8100" http://127.0.0.1:8100/health || fail "the Pipeline API did not come up: docker compose logs pipeline-api"
if ! wait_for "dashboard     :8080 -> /pipeline" http://127.0.0.1:8080/pipeline/health; then
  echo "dashboard cannot reach the Pipeline API; restarting it once"
  docker compose restart dashboard-web
  wait_for "dashboard     :8080 -> /pipeline" http://127.0.0.1:8080/pipeline/health ||
    fail "the dashboard still cannot reach the Pipeline API: docker compose logs dashboard-web"
fi

say "checking the dashboard serves this checkout's samples and scenarios"
served_is_current() {
  curl -fsS -m 10 http://127.0.0.1:8080/samples/manifest.json | cmp -s - web/public/samples/manifest.json
}
if served_is_current; then
  echo "ok    manifest.json matches web/public/samples/"
else
  echo "the dashboard serves an older manifest.json; rebuilding it without the build cache"
  docker compose build --no-cache dashboard-web
  docker compose up -d --force-recreate dashboard-web
  wait_for "dashboard     :8080 -> /pipeline" http://127.0.0.1:8080/pipeline/health ||
    fail "the dashboard did not come back: docker compose logs dashboard-web"
  served_is_current || fail "the dashboard still serves an older manifest.json than web/public/samples/"
  echo "ok    manifest.json matches web/public/samples/"
fi

say "what is now deployed"
docker compose ps --format '{{.Service}}\t{{.Status}}'
echo
echo "connections (as the Pipeline API sees them):"
docker compose exec -T pipeline-api sh -c \
  "grep -nE '^  [a-z]+:|host:|port:' /app/config/connections.yaml"
echo
echo "ready scenarios served by the dashboard:"
curl -fsS http://127.0.0.1:8080/samples/manifest.json |
  docker compose exec -T pipeline-api python -c '
import json, sys
for s in json.load(sys.stdin)["scenarios"]:
    pulls = ", ".join("%s %s..%s" % (p["connection"], p["start"], p["end"]) for p in s.get("pulls", []))
    print("  %-22s %s" % (s["id"], pulls))
'

say "done — deployed $(git rev-parse --short HEAD)"
