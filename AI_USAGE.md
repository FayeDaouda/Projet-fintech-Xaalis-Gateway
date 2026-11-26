# Déclaration d'usage des assistants IA

Conformément à la section 10 de l'énoncé.

## Outils utilisés

| Outil | Usage |
|---|---|
| Claude Code (Anthropic) | Assistance à la conception (ADR), génération de code et de tests, revue |

## Parties concernées

| Partie | Nature de l'aide | Relu, compris et validé par moi |
|---|---|---|
| Squelette (uv, Docker, Alembic, CI) | Génération initiale | ☐ |
| ADR 0001 (ledger), ADR 0002 (verrouillage) | Rédaction assistée, choix discutés | ☐ |
| Lot 4 : migrations, triggers, module `ledger`, tests Hypothesis | Génération assistée | ☐ |
| Lot 1 : clés d'API, rotation, signature HMAC, Argon2id, seed, ADR 0004 | Génération assistée | ☐ |
| Lot 2 : intents, machine à états, idempotence, limites XG-13/14, outbox, ADR 0003/0005/0006 | Génération assistée | ☐ |
| Lot 3 : simulateur, disjoncteur, dispatcher et bascule, callbacks, ADR 0007 | Génération assistée | ☐ |
| Lot 5 et remboursements : webhooks, SSRF, dead letter, XG-08 à XG-11, ADR 0008/0009 | Génération assistée | ☐ |
| Lot 6 : relevés piégés, rapprochement Polars, T+1, virements, ADR 0010 | Génération assistée | ☐ |
| Lot 7 : SSE (LISTEN/NOTIFY), GraphQL Strawberry + DataLoader, ADR 0011 | Génération assistée | ☐ |
| Conformité du contrat : Schemathesis, limite de taille, OpenAPI exact | Génération assistée | ☐ |
| Test de charge Locust, profilage, optimisations, docs/load-test.md | Génération assistée, mesures réelles | ☐ |
| Console web du marchand de démo (BFF, interface, tests), ADR 0012 | Génération assistée, vérifiée dans le navigateur | ☐ |
| Bonus gRPC : contrat proto, microservice de scoring, deadline et repli, ADR 0013 | Génération assistée | ☐ |

## Propositions corrigées ou rejetées

> Au moins deux exemples exigés. À noter **au moment où cela arrive**, avec la raison.

> **Brouillon factuel à relire et à reformuler avec mes mots avant le rendu.**

1. **Limitation de débit (XG-13) : algorithme GCRA rejeté.**
   - *Proposition* : un limiteur GCRA (variante du seau à jetons) dans Redis.
   - *Problème constaté* : le test de la limite de 100 requêtes par minute a échoué :
     101 requêtes envoyées en 0,6 s passaient toutes. GCRA recharge en continu, donc sur
     60 s glissantes il laisse passer jusqu'à ≈ 2 × la limite.
   - *Correction* : fenêtre glissante exacte (sorted set Redis + script Lua atomique).
     Voir [ADR 0006](docs/adr/0006-limitation-de-debit.md).

2. **Verrouillage : `FOR UPDATE` remplacé par `FOR NO KEY UPDATE`.**
   - *Proposition* : verrouiller la ligne du marchand en `SELECT … FOR UPDATE` avant un
     remboursement.
   - *Problème constaté* : le test des 50 remboursements concurrents a donné 49 réponses
     503. L'insertion de la clé d'idempotence (clé étrangère) pose un verrou `KEY SHARE` sur
     la même ligne, incompatible avec `FOR UPDATE` : interblocage jusqu'au `lock_timeout`.
   - *Correction* : `FOR NO KEY UPDATE`, exclusif entre écrivains mais compatible avec les
     clés étrangères. Voir l'addendum de l'[ADR 0002](docs/adr/0002-concurrence-et-verrouillage.md).

3. **Bascule d'opérateur : « interroger puis basculer » rejeté.**
   - *Proposition* : après un timeout chez l'opérateur A, interroger le statut de la charge
     et basculer vers B s'il répondait « inconnue ».
   - *Problème* : un opérateur lent peut répondre « inconnue » pendant que la demande est
     encore en cours, puis débiter : risque de **double débit**.
   - *Correction* : une réponse ambiguë fige l'opérateur ; la bascule n'a lieu que sur un
     refus certain. Voir [ADR 0007](docs/adr/0007-routage-disjoncteur-et-bascule.md).
