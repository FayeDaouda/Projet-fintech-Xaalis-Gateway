# ADR 0002 — Concurrence : READ COMMITTED et verrous pessimistes explicites

- **Statut** : accepté
- **Date** : 2026-11-25
- **Règles concernées** : XG-07, XG-09, XG-11, XG-14, idempotence (lot 2), callbacks (lot 3)

## Contexte

Plusieurs scénarios mettent des transactions concurrentes en compétition sur les mêmes données :

| Scénario | Risque sans contrôle |
|---|---|
| 50 remboursements simultanés de 1 000 sur un paiement de 10 000 (XG-09) | chacun lit « 0 remboursé » et tous passent |
| Remboursements sur plusieurs paiements d'un même marchand (XG-11) | le solde est vérifié par chacun avant le débit de l'autre |
| 20 requêtes simultanées avec la même `Idempotency-Key` | plusieurs intents créés |
| Callback opérateur, expiration à 15 min et annulation sur le même intent | deux transitions concurrentes depuis le même état |
| Callback Teranga Money envoyé deux fois | double crédit |
| Plus de 3 intents `processing` pour un même payeur (XG-14) | comptage concurrent |

Trois stratégies ont été comparées :

1. **SERIALIZABLE + reprise sur erreur 40001** : correct par construction, mais sur un point
   chaud (50 remboursements d'un même paiement) presque toutes les transactions échouent et
   sont rejouées : tempête de reprises, latence imprévisible, et chaque reprise doit
   rester sans effet de bord.
2. **Verrou optimiste (colonne `version`)** : efficace quand les conflits sont rares, mais
   le même problème de reprises apparaît précisément dans les cas que l'on doit prouver.
3. **READ COMMITTED + verrous pessimistes explicites** (`SELECT … FOR UPDATE`) sur la racine
   d'agrégat, plus des contraintes d'unicité en dernier rempart.

## Décision

Niveau d'isolation **READ COMMITTED** (défaut PostgreSQL), avec des verrous explicites et
un **ordre de verrouillage global unique**.

### Règles par cas d'usage

- **Transition d'un intent** : `SELECT … FROM payment_intents WHERE id = :id FOR UPDATE`,
  puis application de la table de transitions, puis écriture de l'événement
  (`payment_events`), du ledger et de l'outbox dans **la même transaction**. Le second
  arrivé voit l'état déjà modifié : sa transition est refusée (409 pour une requête
  marchand ; enregistrée sans effet pour un callback tardif, XG-07).
- **Remboursement** (XG-09, XG-11) : verrou de l'environnement marchand puis de l'intent.
  Sous ces deux verrous, on agrège les remboursements existants et le solde
  `pending + available`, puis on insère le remboursement et ses écritures. Les 50 requêtes
  sont sérialisées : exactement 10 réussissent, les 40 autres reçoivent 409.
- **Virement et règlement** : verrou de l'environnement marchand (même ordre).
- **Encaissement d'un paiement** : ne prend pas le verrou marchand, car un crédit ne peut
  pas violer XG-11 ; cela évite de sérialiser tous les paiements d'un marchand.
- **Limite de 3 intents `processing` par payeur** (XG-14) : le payeur n'est pas une ligne,
  on prend `pg_advisory_xact_lock(hashtextextended('payer:' || phone, 0))`, libéré
  automatiquement au commit, puis on compte.
- **Idempotence** : l'enregistrement de la clé (`UNIQUE (merchant_env_id, key)`) est inséré
  **dans la même transaction que l'effet**. PostgreSQL fait attendre un
  `INSERT … ON CONFLICT DO NOTHING` concurrent sur la même clé jusqu'à la fin de la
  première transaction : si elle valide, le second relit et rejoue la réponse stockée ; si
  elle échoue, le second exécute normalement. Aucun état « en cours » à gérer.
- **Callbacks dupliqués** : `UNIQUE (operator, operator_reference)` + `ON CONFLICT DO NOTHING`.

### Ordre de verrouillage (prévention des interblocages)

```
merchant_environment  →  payment_intent  →  refund / payout
```

Chaque cas d'usage est implémenté dans une seule fonction de service qui respecte cet
ordre ; aucune route ne prend de verrou elle-même.

### Garde-fous

- `lock_timeout = 2 s` et `statement_timeout = 5 s` par connexion : une file d'attente
  anormale échoue vite (réponse 503 `lock_timeout` avec `Retry-After`) au lieu d'épuiser
  le pool de connexions.
- Les appels réseau (opérateurs, webhooks) ne sont **jamais** faits pendant qu'un verrou
  est détenu : ils passent par l'outbox et le worker (voir ADR 0003).

## Conséquences

**Positives**

- Comportement déterministe et facile à prouver par des tests de concurrence sur PostgreSQL.
- Pas de boucle de reprise ni de tempête de réessais sur les points chauds.
- Les contraintes d'unicité restent un filet de sécurité si un chemin de code oubliait un verrou.

**Négatives / risques**

- Débit limité sur une même ligne chaude (un intent, un marchand) : acceptable, car la
  contention naturelle porte sur un seul agrégat à la fois et les opérations concernées
  (remboursement, virement) sont rares comparées aux créations d'intent.
- L'ordre de verrouillage est une discipline : un écart peut provoquer un interblocage
  (PostgreSQL le détecte et annule l'une des transactions avec 40P01, traité en 503).
- Les verrous consultatifs par payeur reposent sur un hachage 64 bits : une collision ne
  fait que sérialiser deux payeurs, sans incidence sur l'exactitude.
