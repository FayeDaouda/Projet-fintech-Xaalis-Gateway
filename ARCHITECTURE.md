# Architecture

> Document vivant : complété à chaque lot. Version actuelle : lots 1 à 7.

## Processus

```
                 ┌──────────────┐  webhooks signés   ┌───────────────┐
  marchand ────▶ │     api      │ ─ ─ ─ ─ ─ ─ ─ ─ ─▶ │ merchant-demo │
  (REST/GraphQL/ │  FastAPI     │                    └───────────────┘
   SSE)          └──────┬───────┘                            ▲
                        │ même transaction :                 │
                        │ état + ledger + outbox             │
                        ▼                                    │
                 ┌──────────────┐   relais outbox     ┌──────┴───────┐
                 │ PostgreSQL 16│ ◀────────────────── │    worker    │
                 └──────────────┘                     └──────┬───────┘
                        ▲                                    │ appels sortants
                        │ callbacks opérateurs               ▼
                 ┌──────┴───────┐                     ┌──────────────┐
                 │     api      │ ◀────────────────── │  operators   │
                 └──────────────┘                     └──────────────┘
                 worker ──gRPC, deadline 150 ms──▶ risk (scoring, interne, ADR 0013)
                 Redis : limitation de débit, état des disjoncteurs
```

## Paquets

| Paquet | Rôle | Dépend de |
|---|---|---|
| `xaalis.config` | Configuration typée lue depuis l'environnement | — |
| `xaalis.observability` | Logs JSON structurés, métriques Prometheus | — |
| `xaalis.infra` | Moteur SQLAlchemy async, base ORM, sonde Docker | `config` |
| `xaalis.core` | Domaine pur : montants (XG-01), commission (XG-04/05), identifiants, opérateurs, signatures HMAC | — |
| `xaalis.ledger` | Grand livre : plan de comptes, lignes, schémas d'écriture, comptabilisation, soldes, santé | `core`, `infra` |
| `xaalis.merchants` | Marchands, environnements test/live, clés d'API (empreintes), rotation | `core`, `infra` |
| `xaalis.admin` | Comptes administrateurs (Argon2id), jetons JWT | `core`, `config` |
| `xaalis.payments` | Intents : machine à états (table unique), cycle de vie, historique, vue publique | `core`, `routing`, `outbox` |
| `xaalis.routing` | Catalogue, plan de routage (coût, taux de succès, disjoncteurs), santé partagée dans Redis | `core` |
| `xaalis.operators` | Client sortant, dispatcher (bascule, épinglage), adaptateurs et traitement des callbacks | `payments`, `ledger`, `outbox`, `routing` |
| `xaalis.refunds` | Remboursements (XG-08 à XG-11) : verrous, contrôles, écritures | `payments`, `ledger`, `outbox` |
| `xaalis.webhooks` | Endpoints (secrets chiffrés), SSRF, diffusion, livraison signée, relances, rejeu | `outbox`, `core` |
| `xaalis.reconciliation` | Relevés (Polars), moteur de rapprochement pur, exécutions et rapports | `core`, `ledger` |
| `xaalis.settlement` | Règlement T+1 (jour de Dakar, rapproché sans écart) et virements | `reconciliation`, `ledger` |
| `xaalis.realtime` | Diffuseur LISTEN/NOTIFY et flux SSE avec rattrapage | `payments` |
| `xaalis.reporting` | Schéma GraphQL lecture seule, DataLoaders, agrégats | `payments`, `refunds` |
| `xaalis.outbox` | Outbox transactionnel (événements et commandes internes) | `infra` |
| `xaalis.idempotency` | Clés d'idempotence en base (ADR 0005) | `infra` |
| `xaalis.seed` | Jeu de démonstration idempotent | `admin`, `merchants` |
| `xaalis.api` | Application FastAPI, middleware, erreurs RFC 9457, authentification, routes | tous |
| `xaalis.worker` | Boucle des tâches de fond | `infra`, domaines |
| `xaalis.risk` | Client gRPC du scoring (deadline 150 ms) et politique de repli ; code généré dans `risk/v1` | `observability` |
| `xaalis_risk` | Microservice gRPC de scoring : règles pures, serveur `grpc.aio`, santé gRPC (ADR 0013) | `xaalis.risk.v1`, `core` |
| `xaalis_sim` | Simulateurs (opérateurs, marchand de démo et sa console web — ADR 0012), hors périmètre de couverture | `observability` |

Règle de dépendance : les modules de domaine (`core`, `ledger`, `merchants`, puis `payments`…)
ne dépendent ni de FastAPI ni de la couche HTTP ; seule `xaalis.api` les assemble.
`xaalis.core` ne dépend de rien : c'est le code que l'on teste sans base de données.

## Cycle d'une requête d'écriture (lot 2)

```
POST /v1/payment-intents/{id}/confirm
  │ authentification (empreinte de clé) ─ limite de débit (Redis, fenêtre glissante)
  ▼
BEGIN ─ réservation Idempotency-Key (INSERT … ON CONFLICT : attend un concurrent)
  │     SELECT … FOR UPDATE de l'intent ─ table de transitions (409 sinon)
  │     verrou consultatif du payeur ─ XG-14 (≤ 3 en cours)
  │     UPDATE intent ─ INSERT payment_events ─ INSERT outbox (payment.processing,
  │     internal.operator.charge) ─ stockage de la réponse (octets exacts)
COMMIT ─ 202
          … le worker relève l'outbox et appelle l'opérateur (lot 3), hors de tout verrou
```

## Paiement chez l'opérateur (lot 3)

```
worker : outbox (bail) ─▶ plan de routage ─▶ disjoncteur ouvert ? ─▶ opérateur suivant
                                │
                                ▼
            select_operator (opérateur courant fixé AVANT l'appel, historisé)
                                │
              POST /{op}/charges (référence = id de l'intent, dédupliquée par l'opérateur)
          ┌─────────────┬───────┴──────────┬───────────────────────────┐
       2xx accepté   503 / refus     4xx refus            timeout / 500 (ambigu)
          │          → suivant       → suivant            → ÉPINGLÉ : reprise chez lui seul
          ▼
 opérateur ── callback signé ──▶ POST /v1/operators/{op}/callback
                                   signature ─ unicité (op, référence) ─ verrou intent
                                   transition ─ grand livre ─ outbox (payment.succeeded)
```

## Grand livre (lot 4)

```
recipes.payment_capture(...)  ─┐   lignes équilibrées (pur, testé par Hypothesis)
recipes.settlement(...)       ─┤
recipes.refund(...)           ─┼─▶ service.post(session, kind, reference, lines)
recipes.payout(...)           ─┘        │  validate_lines()      ← 1er contrôle (Python)
                                        │  INSERT … ON CONFLICT  ← idempotence (kind, reference)
                                        ▼
                           PostgreSQL : sceau, équilibre différé au COMMIT,
                           immutabilité, FK composites test/live  ← autorité finale (XG016)
```
