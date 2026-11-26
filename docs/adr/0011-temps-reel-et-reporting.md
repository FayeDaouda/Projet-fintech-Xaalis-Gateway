# ADR 0011 — Temps réel par LISTEN/NOTIFY, reporting GraphQL avec DataLoader

- **Statut** : accepté
- **Date** : 2026-11-27
- **Règles concernées** : lot 7, OWASP API4

## Contexte

La page de paiement doit voir chaque changement d'état d'un intent (SSE, clé publique).
Le tableau de bord marchand interroge intents, volumes, taux de succès et revenus (GraphQL
en lecture seule, sans N+1).

## Décision

### SSE

- Source : **`NOTIFY xaalis_payment_events`** émis dans la transaction de chaque changement
  d'état. `NOTIFY` est **transactionnel** : délivré au COMMIT seulement, jamais pour une
  transaction annulée — même garantie que l'outbox, sans composant supplémentaire. Redis
  pub/sub a été écarté : publier après le COMMIT réintroduit la double écriture (ADR 0003).
- Une connexion `LISTEN` par processus d'API redistribue aux flux ouverts (files bornées) ;
  reconnexion automatique avec délai croissant.
- Le contenu de la notification est minimal (identifiants) ; le flux relit `payment_events`.
  On s'abonne **avant** de lire l'historique, puis on déduplique par identifiant : aucun
  changement ne tombe entre les deux. `Last-Event-ID` reprend après une coupure.
- Fermeture dans un état terminal ou après 20 min ; commentaire de maintien toutes les 15 s ;
  404 avant ouverture si l'intent n'appartient pas à la clé (XG-12).

### GraphQL

- Strawberry, **aucune mutation**, POST uniquement, clé secrète, données confinées au
  marchand et à l'environnement.
- **DataLoader** pour les remboursements et l'historique : une requête par type pour toute
  la page. Un test compte les requêtes SQL pour 2 puis 10 intents : le nombre est identique.
- Une session par résolveur : Strawberry résout les champs en parallèle et une
  `AsyncSession` ne supporte pas l'usage concurrent.
- Scalaire `Amount` 64 bits (le `Int` GraphQL plafonne à 2³¹ − 1) ; taux en ‰ entiers.
- Garde-fous : profondeur ≤ 6, alias ≤ 15, jetons ≤ 2 000 ; période d'agrégation ≤ 366 jours.

## Conséquences

- Une connexion PostgreSQL de plus par processus d'API pour l'écoute.
- Un abonné trop lent perd des notifications mais rattrape depuis la base : aucun état perdu.
- Les agrégats sont calculés à la demande ; un volume important justifierait une vue
  matérialisée rafraîchie par le worker.
