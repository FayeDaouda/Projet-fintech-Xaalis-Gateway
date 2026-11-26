# ADR 0009 — Webhooks : livraison signée, relances, dead letter, rejeu

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : lot 5, OWASP API7 (SSRF)

## Contexte

Le marchand choisit ses URL et ses événements. Chaque envoi est signé avec un secret propre
à l'endpoint, porte un `event_id` unique, est relancé 8 fois sur ≈ 24 h avec backoff
exponentiel et jitter, puis passe en dead letter ; on peut le rejouer. L'ordre n'est pas garanti.

## Décision

- **Chaîne** : outbox (même transaction que le fait métier, ADR 0003) → *fan-out* en une
  transaction (un `webhook_event` qui garde l'`evt_…`, une `webhook_delivery` par endpoint
  abonné) → livreur qui réserve les livraisons dues par bail (`SKIP LOCKED`), envoie hors
  transaction, puis enregistre le résultat.
- **Signature** : `X-Xaalis-Signature: t=<ts>,v1=<HMAC-SHA-256(secret, "ts.corps")>`, avec
  `X-Xaalis-Event-Id`, `X-Xaalis-Event-Type` et `X-Request-Id`. Le corps est reconstruit de
  façon déterministe : identique à chaque tentative ; seule la signature est horodatée à neuf.
- **Secrets** : chiffrés au repos (Fernet) avec une clé d'environnement, car il faut les
  relire pour signer ; affichés une seule fois, jamais stockés dans `idempotency_keys`.
- **Calendrier** : 7 délais entre 8 tentatives — 1 min, 5 min, 30 min, 2 h, 4 h, 7 h, 10 h
  (≈ 23,6 h) — chacun modulé de ±20 % (jitter en entiers) pour étaler les rafales. Il est
  configurable (`XAALIS_WEBHOOK_RETRY_SCHEDULE_SECONDS`), ce qui permet une démo en secondes.
  Un calendrier explicite a été préféré à `base × 2ⁿ` : la durée totale exigée (≈ 24 h) est
  garantie par construction et lisible d'un coup d'œil.
- **Succès** = réponse 2xx dans les 5 s. Redirections non suivies.
- **Dead letter** après la 8e tentative ; `POST /v1/webhook-events/{id}/replay` remet
  toutes les livraisons de l'événement en attente, compteur à zéro.
- **SSRF** : en live, HTTPS obligatoire, IP privées, réservées, locales et noms internes
  refusés à l'enregistrement, **et** adresses résolues revérifiées à chaque envoi (DNS rebinding).

## Conséquences

- Livraison « au moins une fois » : le marchand **doit** dédupliquer par `event_id` et ne
  pas présumer de l'ordre (guide dans le README, implémenté par le marchand de démo).
- La table `webhook_deliveries` donne l'état de chaque envoi (`GET /v1/webhook-events/{id}`)
  et alimente la métrique de profondeur de file.
