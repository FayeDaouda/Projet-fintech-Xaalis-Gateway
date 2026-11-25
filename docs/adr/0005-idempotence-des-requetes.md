# ADR 0005 — Idempotence des requêtes POST, en base, dans la transaction de l'effet

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : lot 2 (idempotence), XG-09, XG-11

## Contexte

Les réseaux mobiles sont instables : un marchand qui ne reçoit pas la réponse rejoue sa
requête. Exigences de l'énoncé :

- même clé + même corps → réponse initiale **à l'octet près**, en-tête `Idempotent-Replayed: true` ;
- même clé + corps différent → 422 ;
- deux requêtes simultanées avec la même clé → **un seul effet** ;
- clés conservées 24 h, **en base**, pas en mémoire.

Une approche classique (Redis `SET NX` + état « en cours ») oblige à gérer les clés
orphelines quand le traitement plante, et sépare la clé de l'effet : la clé peut être
enregistrée alors que l'effet a été annulé, ou l'inverse.

## Décision

La clé est stockée dans `idempotency_keys` (clé primaire `(merchant_id, livemode, key)`)
et **réservée dans la même transaction que l'effet** :

```
BEGIN
  INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <= now() RETURNING key
    ├─ ligne renvoyée → clé neuve (ou expirée, recyclée) : exécuter l'effet,
    │                   enregistrer statut + corps (octets exacts), COMMIT
    └─ rien → clé vivante : relire la réponse stockée
              ├─ empreinte différente → 422 idempotency_key_reused
              └─ sinon → rejouer les octets + Idempotent-Replayed: true
COMMIT
```

- **Concurrence** : si une autre transaction a réservé la clé sans avoir validé,
  PostgreSQL fait attendre l'`INSERT` concurrent jusqu'à son COMMIT (puis rejeu) ou son
  ROLLBACK (puis exécution normale). 20 requêtes simultanées → 1 effet, 19 rejeux.
- **Échec** : une erreur métier (409…) annule la transaction, réservation comprise. La
  clé n'est pas « brûlée » : un nouvel essai réévalue la requête. Seules les réponses de
  succès sont figées.
- **Empreinte** : SHA-256 de `méthode + chemin + corps JSON canonisé` (clés triées, sans
  espaces). Deux corps sémantiquement identiques sont « le même corps » ; réutiliser une
  clé sur un autre endpoint est aussi un 422.
- **Portée** : par marchand et par environnement (XG-12) : deux marchands peuvent utiliser
  la même clé sans interférence.
- **Obligatoire** (400 `idempotency_key_required`) sur la création et la confirmation
  d'intent, le remboursement et le virement ; facultative sur les autres POST.
- **Exception volontaire** : la création de marchand et la rotation de clé renvoient un
  secret en clair ; leur réponse n'est **jamais** stockée dans `idempotency_keys`.
- **Rétention** : 24 h ; une clé expirée est recyclée par l'`INSERT` lui-même, et le worker
  purge les clés expirées toutes les 10 minutes.

## Conséquences

**Positives**

- Exactement un effet, prouvé par un test de 20 requêtes concurrentes sur PostgreSQL.
- Aucun état « en cours » ni nettoyage de verrous orphelins.
- La réponse rejouée est l'octet exact de la première (stockée en `bytea`).

**Négatives / risques**

- Une écriture de plus par requête POST : coût mesuré au test de charge.
- Les requêtes concurrentes attendent la première : leur latence inclut celle de l'effet
  (borné par `lock_timeout`).
