#!/usr/bin/env bash
# Démonstration du scénario imposé par l'énoncé (vidéo de 5 minutes), sur la pile locale.
#
#   1. paiement réussi suivi en SSE          4. webhook en dead letter puis rejoué
#   2. panne de Ndiaga Cash et bascule       5. rapprochement avec écarts
#   3. remboursement partiel                 6. règlement T+1 puis virement
#
# Prérequis : la pile lancée avec des relances de webhooks rapides (sinon la dead letter
# n'arrive qu'après ≈ 24 h, comme en production) :
#
#   XAALIS_WEBHOOK_RETRY_SCHEDULE_SECONDS='[2,2,2,2,2,2,2]' docker compose up -d --build
#
# En terminal interactif, le script marque une pause avant chaque étape (Entrée pour continuer).
set -euo pipefail

API="${XAALIS_API:-http://localhost:8000}"
SIM="${XAALIS_SIM:-http://localhost:8100}"
SHOP="${XAALIS_SHOP:-http://localhost:8200}"
ADMIN_EMAIL="${XAALIS_SEED_ADMIN_EMAIL:-admin@xaalis.example}"
ADMIN_PASSWORD="${XAALIS_SEED_ADMIN_PASSWORD:-dev-only-admin-password}"
TODAY="$(date -u +%F)"
TOMORROW="$(python3 -c 'import datetime as d; print(d.date.today() + d.timedelta(days=1))')"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
pause() { if [ -t 0 ]; then read -r -p "   ⏎ étape suivante… " _; fi; }
json() { python3 -c "import sys, json; b = json.load(sys.stdin); print($1)"; }
idem() { python3 -c 'import uuid; print(uuid.uuid4())'; }
db() { docker compose exec -T db psql -U xaalis -d xaalis -tAc "$1"; }

# Requête signée HMAC (remboursement, virement) : HMAC-SHA-256(sk, "timestamp.corps").
signed_post() {
  local path="$1" body="$2" ts sig
  ts="$(date +%s)"
  sig="$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$SK" -hex | sed 's/^.* //')"
  curl -sf "$API$path" -H "Authorization: Bearer $SK" -H "Idempotency-Key: $(idem)" \
    -H 'Content-Type: application/json' -H "X-Xaalis-Timestamp: $ts" \
    -H "X-Xaalis-Signature: $sig" -d "$body"
}

pay() {  # pay <téléphone> <montant> : crée puis confirme, affiche l'identifiant
  local intent
  intent="$(curl -sf "$API/v1/payment-intents" -H "Authorization: Bearer $SK" \
    -H "Idempotency-Key: $(idem)" -H 'Content-Type: application/json' \
    -d "{\"amount\": $2, \"currency\": \"XOF\", \"payer_phone\": \"$1\"}" | json 'b["id"]')"
  curl -sf -o /dev/null -X POST "$API/v1/payment-intents/$intent/confirm" \
    -H "Authorization: Bearer $SK" -H "Idempotency-Key: $(idem)"
  echo "$intent"
}

wait_status() {  # wait_status <intent> <statut> : attend au plus 15 s, sinon s'arrête en le disant
  local current=""
  for _ in $(seq 1 30); do
    current="$(curl -sf "$API/v1/payment-intents/$1" -H "Authorization: Bearer $SK" | json 'b["status"]')"
    [ "$current" = "$2" ] && return 0
    sleep 0.5
  done
  echo "   ✗ $1 est « $current » au lieu de « $2 » : démonstration interrompue" >&2
  exit 1
}

show_intent() {
  curl -sf "$API/v1/payment-intents/$1" -H "Authorization: Bearer $SK" |
    json '"%s : %s via %s, %s XOF (commission %s, net %s), payeur %s, remboursé %s" % (b["id"], b["status"], b["operator"], b["amount"], b["fee"], b["net"], b["payer_phone"], b["amount_refunded"])'
}

# --------------------------------------------------------------------------- préparation
bold "0. Préparation"
curl -sf "$API/readyz" | json '"API prête : " + str(b["checks"])' | sed 's/^/   /'
ADMIN="$(curl -sf "$API/v1/admin/auth/token" -H 'Content-Type: application/json' \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASSWORD\"}" | json 'b["access_token"]')"
# Un marchand neuf à chaque démonstration : rejouable à volonté, sans dépendre des logs du seed
# (les clés ne sont affichées qu'une fois, la base n'en garde que l'empreinte).
MERCHANT="$(curl -sf "$API/v1/admin/merchants" -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' -d '{"name": "Boutique Démo Vidéo"}')"
SK="$(echo "$MERCHANT" | json 'b["secret_key"]')"
PK="$(echo "$MERCHANT" | json 'b["publishable_key"]')"
ENDPOINT_SECRET="$(curl -sf "$API/v1/webhook-endpoints" -H "Authorization: Bearer $SK" \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://merchant-demo:8200/webhooks", "events": ["payment.succeeded", "payment.failed", "payment.partially_refunded", "refund.succeeded", "payout.paid"]}' |
  json 'b["secret"]')"
# Le marchand recopie le secret de son endpoint dans sa configuration.
curl -sf -o /dev/null "$SHOP/_secrets" -H 'Content-Type: application/json' -d "{\"secret\": \"$ENDPOINT_SECRET\"}"
info "marchand « Boutique Démo Vidéo » $(echo "$MERCHANT" | json 'b["id"]'), clé ${SK:0:12}…"
info "webhooks → merchant-demo (secret ${ENDPOINT_SECRET:0:10}… partagé)"
curl -sf -o /dev/null -X DELETE "$SIM/_chaos"
for op in gopay teranga ndiaga; do  # démo lisible : callbacks en 1,5 s, sans échec aléatoire
  curl -sf -o /dev/null -X POST "$SIM/_chaos" -H 'Content-Type: application/json' \
    -d "{\"operator\": \"$op\", \"latency_ms\": [1500, 1500], \"failure_rate\": 0, \"duplicate_rate\": 0}"
done
curl -sf -o /dev/null -X POST "$SHOP/_fail" -H 'Content-Type: application/json' -d '{"enabled": false}'
pause

# --------------------------------------------------------------------- 1. SSE
bold "1. Paiement réussi, suivi en direct par SSE (clé publique)"
P1="$(curl -sf "$API/v1/payment-intents" -H "Authorization: Bearer $SK" -H "Idempotency-Key: $(idem)" \
  -H 'Content-Type: application/json' \
  -d '{"amount": 10000, "currency": "XOF", "payer_phone": "+221781234512", "metadata": {"order_id": "CMD-2026-0042"}}' | json 'b["id"]')"
info "intent $P1 créé ; ouverture du flux SSE…"
curl -sN --max-time 12 "$API/v1/payment-intents/$P1/stream" -H "Authorization: Bearer $PK" |
  while IFS= read -r line; do
    case "$line" in data:*) printf '   [%s] SSE %s\n' "$(date +%T)" "$(echo "${line#data: }" | json '"%s → %s (%s)" % (b["previous_status"], b["status"], b["cause"])')";; esac
  done &
SSE=$!
sleep 1
info "[$(date +%T)] confirmation du paiement"
curl -sf -o /dev/null -X POST "$API/v1/payment-intents/$P1/confirm" -H "Authorization: Bearer $SK" -H "Idempotency-Key: $(idem)"
wait_status "$P1" succeeded
sleep 1; kill "$SSE" 2>/dev/null || true; wait "$SSE" 2>/dev/null || true
info "$(show_intent "$P1")"
pause

# ------------------------------------------------------------- 2. panne et bascule
bold "2. Panne de Ndiaga Cash, bascule vers Go Pay"
curl -sf "$SIM/_chaos" -X POST -H 'Content-Type: application/json' \
  -d '{"operator": "ndiaga", "outage_seconds": 120}' | json '"Ndiaga en panne : " + str(b["ndiaga"]["outage"])' | sed 's/^/   /'
info "numéro en 77 : Ndiaga (0,8 %) est normalement préféré à Go Pay (0,9 %)"
P2="$(pay "+221771234599" 5000)"
wait_status "$P2" succeeded
info "$(show_intent "$P2")"
curl -sf "$API/graphql" -H "Authorization: Bearer $SK" -H 'Content-Type: application/json' \
  -d "{\"query\": \"{ paymentIntent(id: \\\"$P2\\\") { history { fromStatus toStatus cause } } }\"}" |
  json '"historique : " + " ; ".join("%s → %s (%s)" % (h["fromStatus"], h["toStatus"], h["cause"]) for h in b["data"]["paymentIntent"]["history"])' | sed 's/^/   /'
# Fin de la panne seulement : les réglages déterministes de la démo sont conservés.
curl -sf -o /dev/null -X POST "$SIM/_chaos" -H 'Content-Type: application/json' -d '{"operator": "ndiaga", "outage_seconds": 0}'
pause

# ----------------------------------------------------------- 3. remboursement partiel
bold "3. Remboursement partiel de 3 000 XOF (requête signée HMAC)"
signed_post /v1/refunds "{\"payment_intent\": \"$P1\", \"amount\": 3000}" |
  json '"remboursement %s : %s, %s XOF" % (b["id"], b["status"], b["amount"])' | sed 's/^/   /'
info "$(show_intent "$P1")"
curl -sf "$API/v1/balance" -H "Authorization: Bearer $SK" |
  json '"solde : en attente %s, disponible %s, viré %s" % (b["pending"], b["available"], b["paid_out"])' | sed 's/^/   /'
pause

# ------------------------------------------------------- 4. dead letter et rejeu
bold "4. Webhook en dead letter, puis rejoué"
curl -sf -o /dev/null -X POST "$SHOP/_fail" -H 'Content-Type: application/json' -d '{"enabled": true}'
info "marchand de démo mis en panne (répond 503)"
P3="$(pay "+221781234577" 7000)"
wait_status "$P3" succeeded
EVT=""
for _ in $(seq 1 20); do
  EVT="$(db "SELECT id FROM webhook_events WHERE type = 'payment.succeeded' AND payload->>'id' = '$P3'")"
  [ -n "$EVT" ] && break; sleep 1
done
info "événement $EVT : relances en cours…"
for _ in $(seq 1 60); do
  STATE="$(curl -sf "$API/v1/webhook-events/$EVT" -H "Authorization: Bearer $SK" | json 'b["deliveries"][0]["status"] + " " + str(b["deliveries"][0]["attempts"])')"
  [ -t 1 ] && printf '\r   livraison : %-20s' "$STATE"
  [ "${STATE%% *}" = "dead_letter" ] && break; sleep 1
done
[ -t 1 ] && echo
info "livraison : $STATE (8 tentatives, toutes en 503)"
curl -sf -o /dev/null -X POST "$SHOP/_fail" -H 'Content-Type: application/json' -d '{"enabled": false}'
info "marchand rétabli ; rejeu :"
curl -sf -X POST "$API/v1/webhook-events/$EVT/replay" -H "Authorization: Bearer $SK" |
  json '"%s livraison replanifiée" % b["deliveries_rescheduled"]' | sed 's/^/   /'
sleep 3
curl -sf "$API/v1/webhook-events/$EVT" -H "Authorization: Bearer $SK" | json '"livraison : " + b["deliveries"][0]["status"]' | sed 's/^/   /'
curl -sf "$SHOP/events" | json '"le marchand a reçu et vérifié %s événements (signature HMAC)" % b["count"]' | sed 's/^/   /'
pause

# --------------------------------------------------------- 5. rapprochement
bold "5. Rapprochement du jour avec le relevé piégé de Go Pay"
info "activité de la journée : 6 paiements Go Pay supplémentaires…"
BATCH=""
for i in 1 2 3 4 5 6; do BATCH="$BATCH $(pay "+22178555000$i" $((i * 2500)))"; done
for intent in $BATCH; do wait_status "$intent" succeeded; done
curl -sf "$SIM/statements/$TODAY/anomalies.json" |
  json '"anomalies injectées par le simulateur : " + ", ".join(a["type"] for a in b["anomalies"] if a["operator"] == "gopay")' | sed 's/^/   /'
curl -sf "$API/v1/admin/reconciliations" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d "{\"date\": \"$TODAY\", \"operators\": [\"gopay\"]}" | json '"rapprochement demandé : %s" % b["runs"][0]["status"]' | sed 's/^/   /'
sleep 5
curl -sf "$API/v1/admin/reconciliations/$TODAY" -H "Authorization: Bearer $ADMIN" |
  json '"rapprochées sans écart : %s ; écarts : %s" % (b["matched"], b["summary"])' | sed 's/^/   /'
info "export CSV : $API/v1/admin/reconciliations/$TODAY?format=csv"
pause

# ------------------------------------------------------------- 6. T+1
bold "6. Règlement T+1, puis virement"
curl -s "$API/v1/admin/settlements" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d "{\"date\": \"$TODAY\"}" | json '"le jour même : %s (%s)" % (b["status"], b["code"])' | sed 's/^/   /'
curl -sf "$API/v1/admin/settlements" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d "{\"date\": \"$TODAY\", \"as_of\": \"$TOMORROW\"}" |
  json '"à J+1 : %s paiements réglés (%s XOF), %s en écart laissés en attente" % (b["settled"], b["settled_amount"], len(b["skipped_unreconciled"]))' | sed 's/^/   /'
curl -sf "$API/v1/balance" -H "Authorization: Bearer $SK" |
  json '"solde : en attente %s, disponible %s, viré %s" % (b["pending"], b["available"], b["paid_out"])' | sed 's/^/   /'
if PAYOUT="$(signed_post /v1/payouts '{}')"; then
  echo "$PAYOUT" | json '"virement %s : %s XOF %s" % (b["id"], b["amount"], b["status"])' | sed 's/^/   /'
else
  info "aucun fonds disponible à virer (409 insufficient_balance)"
fi
curl -sf "$API/v1/balance" -H "Authorization: Bearer $SK" |
  json '"solde : en attente %s, disponible %s, viré %s" % (b["pending"], b["available"], b["paid_out"])' | sed 's/^/   /'
curl -sf "$API/v1/admin/ledger/health" -H "Authorization: Bearer $ADMIN" |
  json '"grand livre équilibré : %s (%s transactions, %s écritures, somme %s)" % (b["balanced"], b["transactions_count"], b["entries_count"], b["totals"]["all"])' | sed 's/^/   /'
bold "Fin de la démonstration."
