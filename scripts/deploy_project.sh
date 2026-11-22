#!/usr/bin/env bash
# Deploy an explicitly configured independent gateway; never infer project paths.
set -Eeuo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 2 ]]; then
  echo "Usage: $0 PRIVATE_ENV_FILE COMPOSE_PROJECT" >&2
  exit 2
fi
[[ -f "$1" ]] || { echo 'Private environment file missing' >&2; exit 2; }
docker compose --env-file "$1" --project-name "$2" \
  -f "$root/deploy/compose.tcp.yaml" up -d --wait --wait-timeout 120
