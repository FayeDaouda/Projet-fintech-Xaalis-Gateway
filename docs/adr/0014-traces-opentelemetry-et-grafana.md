# ADR 0014 — Traces OpenTelemetry à travers l'outbox, Grafana provisionné

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : bonus « traces OpenTelemetry et tableau de bord Grafana provisionné
  dans le compose », observabilité (lot transverse), XG-03

## Contexte

Les logs JSON portent un `request_id` propagé jusqu'au worker et aux opérateurs, et
Prometheus expose les métriques métier. Il manque la vue **causale** : pour un paiement
lent ou en échec, savoir où le temps et l'erreur se trouvent, à travers cinq processus
(api, worker, risk, operators, merchant-demo) et deux frontières asynchrones (l'outbox).

## Décision

### Traces

- **SDK OpenTelemetry**, export **OTLP gRPC** vers **Jaeger** (all-in-one, interface sur
  :16686). Instrumentation automatique : FastAPI (spans serveur), httpx (appels opérateurs,
  webhooks, callbacks), gRPC (client et serveur du scoring), SQLAlchemy et Redis.
- **Propagation à travers l'outbox** : `enqueue()` écrit le `traceparent` W3C courant
  **dans la même transaction** que l'événement (colonne `outbox_messages.traceparent`,
  recopiée dans `webhook_events`). Le worker reprend la trace (`continue_trace`, span
  `CONSUMER`) pour le débit opérateur et la livraison du webhook. Une seule trace relie ainsi
  le clic « Payer » de la console, l'API, le worker, le scoring gRPC, l'opérateur, son
  callback et la réception du webhook par le marchand (45 spans, 5 services, vérifié sur la
  pile).
- Écarté : mettre le contexte dans le `payload` (il deviendrait visible dans le corps des
  webhooks envoyés aux marchands) ; des « liens » de span au lieu d'une filiation (la trace
  serait éclatée en morceaux à recoller à la main).
- **Logs ↔ traces** : chaque log émis dans un span porte `trace_id` et `span_id`.
- **Coût maîtrisé** : export par lots dans un thread (une panne de Jaeger ne ralentit
  aucun paiement, testé avec un collecteur injoignable) ; échantillonnage réglable
  (`XAALIS_OTEL_SAMPLE_PERMILLE`, entier) ; sondes et `/metrics` exclues des traces.
  Désactivé par défaut hors compose (tests, développement local) : l'API OpenTelemetry est
  alors un no-op.
- XG-03 : les spans SQL portent le texte des requêtes, **jamais leurs paramètres** ; les
  numéros ne figurent dans aucun attribut.

### Métriques et tableau de bord

- **Prometheus** collecte l'API (tous les processus uvicorn, mode multiprocessus) et le
  **worker**, qui expose désormais ses propres métriques (scoring gRPC, replis) sur un port
  interne.
- **Grafana** est entièrement provisionné : sources de données (Prometheus, Jaeger) et
  tableau de bord « Xaalis — exploitation », page d'accueil, lecture anonyme. Rien à cliquer.
- Le tableau de bord est **du code** : `scripts/build_dashboard.py` génère le JSON, revu
  comme le reste ; la CI vérifie que le JSON versionné est à jour. Panneaux : débit et p95 par
  route (seuil 150 ms de l'énoncé), erreurs 5xx, intents par statut, disjoncteurs, callbacks,
  écarts de rapprochement, file et dead letter des webhooks, décisions et replis du scoring,
  durée des appels gRPC face à la deadline.

## Conséquences

- Trois conteneurs de plus (Jaeger, Prometheus, Grafana), images épinglées. Aucun service
  applicatif n'en dépend pour démarrer : sans eux, seules les traces et les courbes manquent.
- Le test de charge de [docs/load-test.md](../load-test.md) a été mesuré avant les traces ;
  pour comparer à l'identique : `XAALIS_OTEL_ENABLED=false`.
- En production : un OpenTelemetry Collector entre les services et le stockage (Tempo ou
  Jaeger), un échantillonnage en queue (garder toutes les traces en erreur), Grafana derrière
  une authentification.
