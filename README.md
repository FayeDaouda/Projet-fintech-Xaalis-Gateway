# Xaalis Gateway

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

**Rotation** : `POST /v1/api-keys/rotate` renvoie une nouvelle clé secrète ; l'ancienne reste
valide 24 h.

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

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — modules et dépendances
- [docs/adr/](docs/adr/) — décisions d'architecture
- [AI_USAGE.md](AI_USAGE.md) — déclaration d'usage des assistants IA

## Limites connues

À compléter au fil du projet.
