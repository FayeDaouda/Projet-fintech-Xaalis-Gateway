#!/usr/bin/env bash
# Teste le contrat OpenAPI de la pile en cours d'exécution (docker compose up) avec Schemathesis.
# Usage : scripts/schemathesis.sh [URL_DE_BASE] [MAX_EXEMPLES_PAR_OPÉRATION]
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
MAX_EXAMPLES="${2:-50}"
ADMIN_EMAIL="${XAALIS_SEED_ADMIN_EMAIL:-admin@xaalis.example}"
ADMIN_PASSWORD="${XAALIS_SEED_ADMIN_PASSWORD:-dev-only-admin-password}"

json_field() { python3 -c "import sys, json; print(json.load(sys.stdin)[\"$1\"])"; }

ADMIN_TOKEN=$(curl -sf "$BASE_URL/v1/admin/auth/token" -H 'Content-Type: application/json' \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASSWORD\"}" | json_field access_token)
SECRET_KEY=$(curl -sf "$BASE_URL/v1/admin/merchants" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name": "Schemathesis"}' | json_field secret_key)

export XAALIS_ST_ADMIN_TOKEN="$ADMIN_TOKEN" XAALIS_ST_SECRET_KEY="$SECRET_KEY"
export SCHEMATHESIS_HOOKS="tests.contract.schemathesis_hooks"

uv run schemathesis run "$BASE_URL/openapi.json" \
  --checks all \
  --max-examples "$MAX_EXAMPLES" \
  --report junit --report-dir schemathesis-report \
  "${@:3}"
