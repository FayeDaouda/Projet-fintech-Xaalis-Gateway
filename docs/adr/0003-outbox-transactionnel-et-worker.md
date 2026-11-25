# ADR 0003 — File de tâches : outbox transactionnel et worker asyncio

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : lots 2, 3 et 5 ; « un webhook ne doit jamais partir pour un paiement
  dont la transaction a été annulée »

## Contexte

Un changement d'état doit déclencher des effets **hors de la base** : appel à l'opérateur
après une confirmation, webhook au marchand après un succès. Écrire en base puis publier
dans une file (Redis, RabbitMQ) pose le problème de la **double écriture** :

- publier avant le COMMIT : si la transaction échoue ensuite, un webhook part pour un
  paiement qui n'existe pas ;
- publier après le COMMIT : si le processus s'arrête entre les deux, l'événement est perdu.

Options comparées : Celery / Dramatiq / ARQ (file externe), et outbox transactionnel.

## Décision

**Outbox transactionnel** dans PostgreSQL, consommé par un **worker asyncio** maison.

1. Le service métier insère une ligne dans `outbox_messages` **dans la même transaction**
   que le changement d'état (`payment_events`, ledger). Atomicité garantie par PostgreSQL :
   pas de COMMIT, pas d'événement.
2. Le worker relève les messages non publiés par lots, avec
   `SELECT … FOR UPDATE SKIP LOCKED` : plusieurs workers se partagent le travail sans
   attente ni double traitement.
3. Deux familles de sujets :
   - `payment.*`, `refund.*`, `payout.*` : événements livrables aux marchands (lot 5) ;
   - `internal.*` (ex. `internal.operator.charge`) : commandes internes, jamais livrées.
4. Le contenu d'un événement est la **vue publique** de l'objet (`PaymentIntentView`) : le
   webhook et l'API ne peuvent pas diverger, et le numéro du payeur y est toujours masqué.
5. Le `request_id` de la requête d'origine est stocké avec le message : il est propagé
   jusqu'au worker et aux appels opérateurs (en-tête `X-Request-Id`).

Le worker exécute aussi les tâches périodiques (expiration à 15 min, purge des clés
d'idempotence, puis rapprochement et règlement) sous forme de boucles asyncio qui
s'arrêtent proprement sur SIGTERM.

## Conséquences

**Positives**

- Garantie « au moins une fois » sans infrastructure supplémentaire ; aucun événement
  fantôme, aucun événement perdu.
- Un seul système à opérer et à sauvegarder (PostgreSQL), et une seule source de vérité.
- Testable avec la même base que le reste : on vérifie dans un test qu'une confirmation
  refusée (409) n'écrit **aucun** message.

**Négatives / risques**

- Livraison « au moins une fois » : un message peut être publié deux fois (arrêt du worker
  après l'envoi, avant le marquage). Les consommateurs doivent être idempotents : `event_id`
  unique pour les marchands, référence opérateur unique pour les débits.
- Latence de quelques centaines de millisecondes (intervalle de relève) ; acceptable pour
  des notifications asynchrones. `LISTEN/NOTIFY` pourra réveiller le worker plus tôt.
- La table grossit : les messages publiés devront être purgés ou archivés.
