# ADR 0013 — Scoring de risque dans un microservice gRPC, deadline 150 ms et repli

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : bonus du lot 7, XG-01, XG-03, XG-07, ADR 0007

## Contexte

L'énoncé propose en bonus de déporter le scoring de risque dans un microservice gRPC
interne, appelé avec une **deadline de 150 ms**, et demande de **justifier la politique de
repli** en cas d'indisponibilité. Trois questions : où l'appeler, que faire d'un « refus »,
que faire quand le service ne répond pas.

## Décision

### Contrat et service

- Contrat [`proto/xaalis/risk/v1/risk.proto`](../../proto/xaalis/risk/v1/risk.proto) :
  `RiskScoring.Score(ScoreRequest) → ScoreResponse`. Tout est entier (XG-01) : montant en
  XOF, score en **pour mille** (0 à 1000), décision `APPROVE`, `REVIEW` ou `DECLINE`, et des
  codes « raison » stables.
- Code Python généré par `scripts/generate_grpc.py` avec des stubs typés (mypy-protobuf) :
  mypy `--strict` couvre aussi les appels gRPC. Le code généré est versionné (l'image n'a pas
  besoin de `grpcio-tools`) et la CI vérifie qu'il correspond au `.proto`.
- Service `risk` (`python -m xaalis_risk`) : `grpc.aio`, protocole de santé gRPC standard
  (sonde Docker), arrêt propre sur SIGTERM, **aucun port publié** (seul le worker l'appelle).
- Règles explicables plutôt qu'un modèle opaque (tableau dans `xaalis_risk/scoring.py`) :
  liste noire, paliers de montant, vélocité du payeur sur 10 min, nuit à Dakar, montant
  rond. ≥ 700 ‰ refus, ≥ 400 ‰ revue. Un marchand refusé peut se voir expliquer pourquoi.
- Le service ne garde ni ne journalise de numéro en clair : une empreinte SHA-256 suffit à
  la vélocité et à la liste noire (XG-03).

### Où l'appeler : dans le worker, avant le premier débit

L'appel est fait par le **dispatcher**, une seule fois par paiement, juste avant le premier
envoi à l'opérateur, et **hors transaction** (aucun appel réseau sous verrou, ADR 0002).

Écarté : l'appeler dans `POST /confirm`. La latence du scoring s'ajouterait à chaque
confirmation, et un refus y prendrait la forme d'un nouveau code d'erreur, donc d'un
changement de contrat. Dans le worker, le paiement est déjà asynchrone : 150 ms de plus
ne se voient pas.

### Que faire d'un refus

`processing → failed`, cause `risk_declined` (événement `RISK_DECLINED`). C'est **la même
arête** que le refus d'un opérateur : la machine à états de l'énoncé n'a ni état ni arête de
plus (un test le vérifie). Aucun débit n'a été demandé, rien n'est écrit au grand livre, le
marchand reçoit `payment.failed` et voit `risk` dans l'intent (score, décision, source).
`REVIEW` laisse passer le paiement en le signalant.

### Politique de repli (service indisponible ou deadline dépassée)

| Montant | Repli | Justification |
|---|---|---|
| ≤ 100 000 XOF (`XAALIS_RISK_FAIL_OPEN_MAX_AMOUNT`) | **on laisse passer**, tracé `source = fallback` | Perte possible bornée par le plafond ; le payeur valide de toute façon le débit sur son téléphone (code secret) ; l'encaissement reste disponible pendant une panne du scoring |
| > 100 000 XOF | **le paiement attend** : le worker réessaie (backoff 1 s → 30 s) | Un gros montant mérite d'être évalué ; si la panne dure, l'expiration à 15 min (XG-07) clôt l'intent **sans qu'aucun débit n'ait été demandé** |

Écartés : *fail-closed* total (une panne du scoring arrêterait tout l'encaissement) et
*fail-open* total (une panne, ou une lenteur provoquée, désactiverait l'anti-fraude pour
tous les montants). Chaque repli incrémente `xaalis_risk_assessments_total{source="fallback"}`
et produit un log `risk_fallback`, de quoi alerter.

## Conséquences

- Un test démarre un vrai serveur gRPC ralenti à 400 ms et vérifie que l'appel rend la main
  en moins de 350 ms avec le bon repli ; un autre, sans serveur, vérifie le repli
  « indisponible ». Le parcours complet (refus, attente puis reprise, accord) est testé sur
  PostgreSQL.
- La vélocité est en mémoire : suffisant pour une instance. À plusieurs, les compteurs iraient
  dans Redis (même technique que l'ADR 0006).
- Démonstration : payer depuis le **77 000 00 00** (liste noire par défaut) montre un refus ;
  `docker compose stop risk` montre le repli ; `XAALIS_RISK_LATENCY_MS=400` montre la deadline.
