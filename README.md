# Xaalis Gateway

[![CI](https://github.com/FayeDaouda/Projet-fintech-Xaalis-Gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/FayeDaouda/Projet-fintech-Xaalis-Gateway/actions/workflows/ci.yml)

Passerelle de paiement mobile money multi-opérateurs (Go Pay, Teranga Money, Ndiaga Cash) :
une seule API pour le marchand, routage et bascule entre opérateurs, grand livre en partie
double, webhooks signés, rapprochement quotidien et règlement T+1.

> Projet pédagogique : les opérateurs sont simulés, aucun argent réel ne circule.

## Démarrage en une commande

Prérequis : Docker (avec Compose v2).

```bash
docker compose up --build
```

Les migrations sont appliquées automatiquement par le service `migrate` avant le démarrage
de l'API et du worker. Aucun fichier `.env` n'est nécessaire en local.

| Service | Rôle | URL |
|---|---|---|
| `api` | API publique (REST, GraphQL, SSE) | http://localhost:8000 — docs : `/docs` |
| `worker` | Outbox, webhooks, expiration, rapprochement, règlement | — |
| `operators` | Simulateur des trois opérateurs | http://localhost:8100 |
| `merchant-demo` | Marchand de démonstration (réception des webhooks) | http://localhost:8200 |
| `db` | PostgreSQL 16 | `localhost:5432` |
| `redis` | Limitation de débit, état des disjoncteurs | `localhost:6379` |

## Comptes de démonstration

Créés automatiquement par le service `migrate` (idempotent : relancer ne recrée rien).

| Compte | Identifiants |
|---|---|
| Administrateur | `admin@xaalis.example` / `dev-only-admin-password` (surchargeable par `XAALIS_SEED_ADMIN_PASSWORD`) |
| Marchand « Boutique Démo Dakar » | clés de **test** affichées **une seule fois** au premier démarrage : `docker compose logs migrate \| grep seed_demo` |

Pour des clés de démo stables (vidéo, tests manuels), renseignez
`XAALIS_DEMO_TEST_PUBLISHABLE_KEY` et `XAALIS_DEMO_TEST_SECRET_KEY` dans `.env`
**avant** le premier démarrage. Aucune clé n'est versionnée dans le dépôt.

## Authentification

| Appelant | En-têtes | Obtention |
|---|---|---|
| Administrateur | `Authorization: Bearer <JWT>` | `POST /v1/admin/auth/token` `{"email", "password"}` |
| Marchand (serveur à serveur) | `Authorization: Bearer sk_test_…` | renvoyée une fois par `POST /v1/admin/merchants` |
| Page de paiement (navigateur) | `Authorization: Bearer pk_test_…` | idem (lecture seule) |
| Remboursement, virement | clé secrète **+** `X-Xaalis-Timestamp` **+** `X-Xaalis-Signature` | voir ci-dessous |

Les environnements test et live sont étanches : une clé `sk_test_…` ne voit jamais un objet
live (et inversement), et répond 404 plutôt que 403 pour ne rien révéler (XG-12).

**Signature des requêtes sensibles** : `HMAC-SHA-256(clé_secrète, f"{timestamp}.{corps_brut}")`
en hexadécimal, horodatage Unix en secondes, tolérance ±300 s. Le corps signé doit être
exactement celui envoyé, octet pour octet.

```python
import hashlib, hmac, json, time, httpx

secret = "sk_test_…"
body = json.dumps({"payment_intent": "pi_…", "amount": 1000}).encode()
ts = int(time.time())
signature = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
httpx.post(
    "http://localhost:8000/v1/refunds",
    content=body,
    headers={
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Idempotency-Key": "refund-cmd-42",
        "X-Xaalis-Timestamp": str(ts),
        "X-Xaalis-Signature": signature,
    },
)
```

## Conventions de l'API

- **Erreurs** : toujours `application/problem+json` (RFC 9457) avec un `code` stable :
  `invalid_transition` (409), `idempotency_key_reused` (422), `idempotency_key_required` (400),
  `payer_processing_limit` (409, XG-14), `intent_expired` (409), `rate_limited` (429 +
  `Retry-After`), `validation_error` (422), `not_found` (404)…
- **Idempotence** : en-tête `Idempotency-Key` obligatoire sur la création et la confirmation
  d'intent, le remboursement et le virement. Rejouer la même clé avec le même corps renvoie
  la réponse initiale à l'octet près avec `Idempotent-Replayed: true` ; un corps différent
  renvoie 422. Clés conservées 24 h ([ADR 0005](docs/adr/0005-idempotence-des-requetes.md)).
- **Pagination** : par curseur, `?limit=` (100 au maximum) et `?starting_after=<id>` ; la
  réponse contient `has_more` et `next_cursor`.
- **Montants** : entiers en XOF (pas de décimales). **Numéros** : `+2217XXXXXXXX` en entrée,
  toujours masqués en sortie (`+221 77 *** ** 12`).
- **Limites** (XG-13) : 100 requêtes/min par clé, 20 créations d'intent/min par payeur.

## Webhooks : guide du marchand

1. Enregistrer une URL : `POST /v1/webhook-endpoints` `{"url": "...", "events": ["payment.succeeded", "refund.succeeded"]}`.
   La réponse contient le **secret** (`whsec_…`), affiché une seule fois. En live : HTTPS
   obligatoire, adresses privées interdites.
2. **Vérifier chaque envoi** : `X-Xaalis-Signature: t=<ts>,v1=<hex>` où
   `hex = HMAC-SHA-256(secret, f"{ts}.{corps_brut}")`. Rejeter si la signature diffère
   (comparaison en temps constant) ou si `|maintenant − ts| > 300 s`.
3. **Répondre 2xx en moins de 5 s**, puis traiter de façon asynchrone. Sinon : 8 tentatives
   sur ≈ 24 h (1 min, 5 min, 30 min, 2 h, 4 h, 7 h, 10 h, ±20 %), puis dead letter ;
   `POST /v1/webhook-events/{id}/replay` renvoie un événement, `GET /v1/webhook-events/{id}`
   montre l'état des livraisons.
4. **Dédupliquer par `id`** (`evt_…`, aussi dans `X-Xaalis-Event-Id`) : la livraison est
   « au moins une fois ».
5. **L'ordre n'est pas garanti.** Un `payment.processing` peut arriver après le
   `payment.succeeded`. Règles à appliquer :
   - ne jamais faire régresser un statut : comparer le `created_at` de l'événement à celui
     du dernier traité pour le même objet et ignorer le plus ancien ;
   - les états finaux (`succeeded`, `failed`, `refunded`…) l'emportent sur les intermédiaires ;
   - en cas de doute, relire l'état de référence : `GET /v1/payment-intents/{id}`.

   Le service `merchant-demo` applique exactement ces règles ; ses événements reçus sont
   visibles sur http://localhost:8200/events (`POST /_fail` le met en panne pour la démo).

**Rotation** : `POST /v1/api-keys/rotate` renvoie une nouvelle clé secrète ; l'ancienne reste
valide 24 h.

## Simulateur d'opérateurs et chaos

Le service `operators` (port 8100) simule Go Pay (77, 78), Teranga Money (76, 70) et
Ndiaga Cash (75, 77) avec leurs latences, taux d'échec et formats de callback. Pour la
démonstration et les tests manuels :

```bash
# Panne de Ndiaga Cash pendant 2 minutes : les paiements 77 basculent vers Go Pay
curl -X POST localhost:8100/_chaos -H 'Content-Type: application/json' \
     -d '{"operator": "ndiaga", "outage_seconds": 120}'

# Autres réglages : taux d'échec, latence du callback (ms), doublons, réponse lente
curl -X POST localhost:8100/_chaos -H 'Content-Type: application/json' \
     -d '{"operator": "teranga", "failure_rate": 0.5, "latency_ms": [100, 200], "duplicate_rate": 1}'

curl localhost:8100/_chaos             # état courant
curl -X DELETE localhost:8100/_chaos   # retour au comportement nominal
```

Fenêtres de panne périodiques de Ndiaga Cash : `XAALIS_SIM_NDIAGA_OUTAGE_EVERY_SECONDS` et
`XAALIS_SIM_NDIAGA_OUTAGE_DURATION_SECONDS` (désactivées par défaut).

## Rapprochement, règlement T+1 et virements

```bash
# 1. Rapprocher une date (relevés récupérés auprès du simulateur), traité par le worker
curl -X POST localhost:8000/v1/admin/reconciliations -H "Authorization: Bearer $ADMIN_JWT" \
     -H 'Content-Type: application/json' -d '{"date": "2026-11-27"}'
# … ou rapprocher un relevé fourni (CSV brut)
curl -X POST "localhost:8000/v1/admin/reconciliations?date=2026-11-27&operator=gopay" \
     -H "Authorization: Bearer $ADMIN_JWT" -H 'Content-Type: text/csv' --data-binary @releve.csv

# 2. Rapport d'écarts, en JSON ou en CSV
curl "localhost:8000/v1/admin/reconciliations/2026-11-27" -H "Authorization: Bearer $ADMIN_JWT"
curl "localhost:8000/v1/admin/reconciliations/2026-11-27?format=csv" -H "Authorization: Bearer $ADMIN_JWT"

# 3. Règlement T+1 : seuls les paiements rapprochés sans écart passent en « available ».
#    Refusé le jour même (409 settlement_too_early). « as_of » simule la date courante
#    pour la démo, interdit en production.
curl -X POST localhost:8000/v1/admin/settlements -H "Authorization: Bearer $ADMIN_JWT" \
     -H 'Content-Type: application/json' -d '{"date": "2026-11-27", "as_of": "2026-11-28"}'
```

Chaque nuit, le worker rapproche la veille (heure de Dakar) puis règle ce qui est
rapproché. `POST /v1/payouts` (signé HMAC, `Idempotency-Key`) vire tout le solde
`available`. Exemple complet : [docs/reconciliation-sample/](docs/reconciliation-sample/).

## Temps réel et tableau de bord

**SSE** (page de paiement, clé publique) — un événement `status_changed` par changement
d'état ; reprise automatique après coupure grâce à `Last-Event-ID` :

```bash
curl -N localhost:8000/v1/payment-intents/pi_…/stream -H "Authorization: Bearer pk_test_…"
```

```js
// côté navigateur, avec une bibliothèque SSE autorisant l'en-tête Authorization
source.addEventListener("status_changed", (e) => afficher(JSON.parse(e.data).status));
```

**GraphQL** (`POST /graphql`, clé secrète, lecture seule ; GraphiQL hors production) :

```graphql
{
  paymentIntents(status: SUCCEEDED, operator: GOPAY, first: 20) {
    nodes { id amount payerPhone refunds { amount } history { toStatus } }
    hasNextPage endCursor
  }
  dailyVolumes(dateFrom: "2026-11-01", dateTo: "2026-11-30") { date operator succeeded volume }
  successRates(dateFrom: "2026-11-01", dateTo: "2026-11-30") { operator successRatePermille }
  revenue(dateFrom: "2026-11-01", dateTo: "2026-11-30") { gross fees refunded net }
}
```

## Variables d'environnement

Voir [`.env.example`](.env.example). Toutes sont préfixées par `XAALIS_`.

| Variable | Défaut (développement) | Rôle |
|---|---|---|
| `XAALIS_ENVIRONMENT` | `development` | `production` refuse les secrets `dev-only-*` |
| `XAALIS_DATABASE_URL` | PostgreSQL du compose | DSN `postgresql+asyncpg://…` |
| `XAALIS_REDIS_URL` | Redis du compose | |
| `XAALIS_API_KEY_PEPPER` | `dev-only-…` (≥ 32 car.) | Pepper du hachage des clés d'API |
| `XAALIS_JWT_SECRET` | `dev-only-…` (≥ 32 car.) | Signature des JWT administrateur (HS256) |
| `XAALIS_SEED_ADMIN_PASSWORD` | `dev-only-admin-password` | Mot de passe du compte admin de démo |
| `XAALIS_DEMO_TEST_*_KEY` | vide (générées) | Clés de test imposées pour le marchand de démo |
| `XAALIS_OPERATOR_SECRET_{GOPAY,TERANGA,NDIAGA}` | `dev-only-…` | Secrets de signature des callbacks, partagés avec le simulateur |
| `XAALIS_BREAKER_FAILURE_THRESHOLD` / `_COOLDOWN_SECONDS` | `5` / `30` | Disjoncteur par opérateur |
| `XAALIS_WEBHOOK_ENCRYPTION_KEY` | `dev-only-…` | Chiffrement des secrets d'endpoints au repos |
| `XAALIS_WEBHOOK_RETRY_SCHEDULE_SECONDS` | `[60,300,1800,7200,14400,25200,36000]` | Délais entre tentatives ; démo : `[2,2,2,2,2,2,2]` |
| `XAALIS_LOG_LEVEL` | `INFO` | |

## Développement

Prérequis : [uv](https://docs.astral.sh/uv/) et Docker (les tests démarrent PostgreSQL et
Redis via Testcontainers).

```bash
uv sync                      # environnement virtuel + dépendances verrouillées
uv run ruff check .          # lint
uv run ruff format --check . # format
uv run mypy                  # typage strict
uv run pytest --cov          # tests (PostgreSQL réel) + couverture ≥ 85 %
uv run pytest tests/unit     # tests unitaires seuls, sans Docker
```

## Contrat d'API (OpenAPI + Schemathesis)

Le contrat publié sur `/openapi.json` est versionné dans [`openapi.json`](openapi.json) ; la
CI échoue s'il n'est pas à jour (`uv run python scripts/export_openapi.py`). Il est vérifié
par Schemathesis avec **toutes** ses vérifications, contre la pile Docker réelle :

```bash
docker compose up -d --build
./scripts/schemathesis.sh http://localhost:8000 100   # JWT admin et clé marchand obtenus automatiquement
```

Le hook [`tests/contract/schemathesis_hooks.py`](tests/contract/schemathesis_hooks.py) se
comporte comme un client correct : bonne authentification par route, `Idempotency-Key`
unique, signature HMAC des remboursements et virements. Résultat : 0 erreur 500, 0 écart de
contrat (≈ 4 000 cas, phases Examples, Coverage, Fuzzing et Stateful), avec ou sans
limitation de débit.

Points de contrat notables :

- toute erreur est `application/problem+json`, y compris 400/413/422 du framework ;
- un nombre JSON entier comme `1420.0` est un entier (JSON Schema) : accepté et stocké en
  `int` exact ; `1420.5`, `"1420"` et `true` sont refusés ;
- les caractères non stockables en UTF-8/PostgreSQL (`\u0000`, demi-paires UTF-16) sont
  remplacés par `U+FFFD` à l'entrée, longueur préservée ;
- corps limités à 1 Mio (5 Mio pour `POST /v1/admin/reconciliations/statements`) : 413 ;
- une `Idempotency-Key` manquante vaut 400 **avant** toute authentification ou limite de débit.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — modules et dépendances
- [docs/adr/](docs/adr/) — décisions d'architecture
- [AI_USAGE.md](AI_USAGE.md) — déclaration d'usage des assistants IA

## Limites connues

À compléter au fil du projet.
