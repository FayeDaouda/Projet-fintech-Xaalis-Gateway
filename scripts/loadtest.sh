#!/usr/bin/env bash
# Test de charge Locust contre la pile en cours d'exécution (docker compose up).
#
# Locust tourne dans un conteneur, sur le réseau Docker de la pile, et vise http://api:8000 :
# sur macOS, passer par la redirection de ports de Docker Desktop fausse la mesure (proxy en
# espace utilisateur sur l'hôte). Voir docs/load-test.md.
#
# Usage : scripts/loadtest.sh [UTILISATEURS=200] [DURÉE=2m] [MARCHANDS=200]
set -euo pipefail
USERS="${1:-200}"
DURATION="${2:-2m}"
MERCHANTS="${3:-200}"
NETWORK="${XAALIS_LT_NETWORK:-xaalis_default}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/loadtest/results"

docker run --rm --network "$NETWORK" \
  -v "$ROOT/loadtest:/mnt/locust" \
  -e XAALIS_LT_MERCHANTS="$MERCHANTS" \
  locustio/locust:2.47.0 \
  -f /mnt/locust/locustfile.py --headless \
  --host http://api:8000 --users "$USERS" --spawn-rate 50 --run-time "$DURATION" --reset-stats \
  --csv /mnt/locust/results/run --html /mnt/locust/results/report.html --only-summary
