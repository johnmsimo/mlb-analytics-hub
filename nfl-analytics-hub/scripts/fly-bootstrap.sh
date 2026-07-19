#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${FLY_APP:-nfl-analytics-hub-johnmsimo}"
REGION="${FLY_REGION:-ewr}"

command -v flyctl >/dev/null || { echo "Install flyctl first: https://fly.io/docs/flyctl/install/"; exit 1; }

if ! flyctl apps list --json | grep -q "\"Name\":\"${APP_NAME}\""; then
  flyctl apps create "$APP_NAME"
fi

# Persistent volume for the web machine's /app/data (tracker picks, odds
# snapshot, SQLite fallback). fly.toml mounts it; deploy fails without it.
if ! flyctl volumes list -a "$APP_NAME" --json 2>/dev/null | grep -q '"name":"nfl_data"'; then
  flyctl volumes create nfl_data -a "$APP_NAME" --region "$REGION" --size 1 --yes
fi

printf '\nCreate or attach PostgreSQL, then set DATABASE_URL.\n'
printf 'Create or attach Redis, then set REDIS_URL.\n\n'
printf 'Required secrets example:\n'
printf '  flyctl secrets set -a %s SECRET_KEY="$(python -c '\''import secrets; print(secrets.token_urlsafe(48))'\'')" DATABASE_URL="..." REDIS_URL="..." NWS_USER_AGENT="nfl-analytics-hub/2.2 you@example.com"\n' "$APP_NAME"
printf '\nAfter secrets are configured:\n  flyctl deploy -a %s\n' "$APP_NAME"
