# ADR 0007 — Routage, disjoncteur et bascule sans double débit

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : lot 3, XG-06, XG-07

## Contexte

Trois opérateurs aux comportements différents (latence, taux d'échec, doublons, pannes 503).
L'énoncé impose :

- routage par préfixe, le moins cher d'abord (XG-06), puis le meilleur taux de succès sur 5 min ;
- un disjoncteur par opérateur : ouvert après 5 échecs consécutifs, semi-ouvert après 30 s ;
- une bascule vers un autre opérateur éligible quand le disjoncteur est ouvert, **sans jamais
  débiter deux fois le payeur** ;
- des callbacks vérifiés, dédupliqués, traités de façon idempotente.

Le risque central est le **double débit**. Après une demande à l'opérateur A, trois issues
sont possibles : A a refusé, A a accepté, ou **on ne sait pas** (timeout après envoi, 500,
coupure). Basculer vers B dans le troisième cas peut débiter deux fois.

Une première conception prévoyait, après un timeout, d'interroger le statut de la charge
chez A et de basculer s'il répondait « inconnue ». Elle a été **rejetée** : un opérateur
lent peut répondre « inconnue » pendant que la demande est encore en cours de traitement,
puis débiter quelques millisecondes plus tard.

## Décision

### Routage (à la confirmation)

`plan_route` (fonction pure) classe les opérateurs éligibles par coût (XG-06), puis par taux
de succès des 5 dernières minutes (en ‰, compteurs par minute dans Redis), puis par nom
(déterminisme). Les opérateurs au disjoncteur ouvert passent en **dernier** sans être retirés :
si tous sont ouverts, l'intent garde un ordre de repli et sera tenté à leur réouverture.
L'état est lu **avant** d'ouvrir la transaction : aucune entrée-sortie sous verrou.

### Disjoncteur (partagé)

État dans Redis, une clé par opérateur, transitions en **scripts Lua atomiques**, horloge
de Redis. En semi-ouvert, un **seul** appel d'essai à la fois (`probe_until`) ; son succès
referme, son échec rouvre. Une réponse 4xx (refus sur le fond) ne compte pas comme une panne.
Panne de Redis : *fail-open*.

### Envoi et bascule (worker)

La commande `internal.operator.charge`, écrite dans l'outbox à la confirmation, est relevée
par bail. Pour chaque opérateur du plan, dans l'ordre :

| Réponse de l'opérateur | Débit possible ? | Action |
|---|---|---|
| Disjoncteur ouvert (aucun envoi) | non | opérateur suivant |
| Connexion refusée, 502, 503, 429 | non | échec compté, opérateur suivant |
| 4xx | non | opérateur suivant |
| 2xx avec référence | oui | on attend le callback |
| Timeout de lecture, 500, 504, coupure | **peut-être** | **épinglage** : reprise plus tard auprès de **cet opérateur uniquement** |

Deux mécanismes rendent l'épinglage sûr :

1. **Idempotence chez l'opérateur** : chaque demande porte la référence de l'intent ; un
   rejeu auprès du même opérateur renvoie la charge existante au lieu d'en créer une seconde.
2. **Opérateur courant fixé avant l'appel** (`select_operator`, historisé `operator_failover`) :
   un callback rapide de l'opérateur choisi trouve l'intent déjà associé à lui ; un callback
   d'un autre opérateur déclenche une alerte `operator_mismatch`.

Si l'opérateur épinglé ne revient jamais, l'intent expire au bout de 15 minutes (XG-07) : le
paiement est perdu, mais le payeur n'est **jamais** débité deux fois.

### Callbacks

Un adaptateur par format (JSON signé en en-tête, formulaire signé en champ, JSON signé et
horodaté), vérification de signature en temps constant avant toute lecture du contenu, puis
un format interne unique. Dans une seule transaction : journal du callback (unicité
`(operator, operator_reference)` : le doublon de Teranga Money n'a aucun effet), verrou de
l'intent, transition, écritures du grand livre avec la commission figée à la création.
Tout callback correctement signé reçoit 200 ; les anomalies (tardif, opérateur ou montant
incohérent, intent inconnu) deviennent des **alertes de rapprochement** (lot 6).

## Conséquences

**Positives**

- Le double débit est exclu par construction ; un test le prouve en simulant la perte de la
  réponse d'un opérateur qui a bel et bien débité.
- La bascule est immédiate pour les pannes franches (503, disjoncteur), le cas de la démo.
- État partagé : plusieurs workers et instances d'API voient les mêmes disjoncteurs.

**Négatives / risques**

- Disponibilité sacrifiée dans le cas ambigu : un intent épinglé à un opérateur durablement
  muet expire au lieu de basculer. C'est un choix assumé (exactitude avant disponibilité).
- La sûreté repose sur l'idempotence de l'opérateur par référence ; un opérateur réel qui
  ne la garantirait pas imposerait une réconciliation par relevé (lot 6) avant tout rejeu.
