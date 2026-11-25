# ADR 0006 — Limitation de débit : fenêtre glissante exacte dans Redis

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : XG-13, OWASP API4

## Contexte

XG-13 : 100 requêtes par minute et par clé ; 20 créations d'intent par minute et par
numéro de payeur ; au-delà, 429 avec `Retry-After`. Plusieurs instances de l'API doivent
partager les compteurs.

Une première implémentation en **GCRA** a été écartée après qu'un test l'a mise en défaut :
GCRA (comme un seau à jetons) recharge en continu, donc sur 60 s glissantes il laisse passer
jusqu'à environ 2 × la limite (rafale initiale + recharge). 101 requêtes envoyées en un peu
plus de 0,6 s passaient toutes. Une **fenêtre fixe** a le même défaut à la frontière de
deux minutes.

## Décision

**Fenêtre glissante exacte** : un *sorted set* Redis par compteur, contenant l'horodatage
de chaque requête acceptée. Un script Lua, exécuté atomiquement :

1. lit l'heure de Redis (`TIME`), commune à toutes les instances de l'API ;
2. purge les entrées plus vieilles que la période ;
3. si le nombre restant atteint la limite : refuse, et calcule `Retry-After` à partir de la
   plus ancienne entrée (le moment où une place se libère) ;
4. sinon : ajoute la requête et renouvelle l'expiration de la clé.

Compteurs : `key:{key_id}` (100/min, appliqué dès l'authentification) et
`payer:{sha256(numéro)[:24]}` (20/min, à la création d'intent). Le numéro est **haché**
dans la clé Redis : aucune donnée personnelle en clair dans le cache (XG-03).

**Panne de Redis** : on laisse passer (*fail-open*) et on journalise. Une passerelle de
paiement à l'arrêt coûte plus cher qu'un débit temporairement non borné ; les protections
métier (XG-14, idempotence, verrous) restent actives car elles vivent dans PostgreSQL.

## Conséquences

**Positives**

- Garantie stricte : jamais plus de N requêtes sur **n'importe quelles** 60 secondes,
  vérifiée par un test de 30 requêtes concurrentes pour une limite de 10.
- Atomique (Lua) : pas de condition de course entre lecture et écriture du compteur.

**Négatives / risques**

- Mémoire : au plus N entrées par compteur (100), soit quelques kilo-octets par clé active.
- *Fail-open* : pendant une panne de Redis, XG-13 n'est plus appliquée (alerte à prévoir).
- Le test de charge (200 créations/s) doit répartir les requêtes sur plusieurs clés et
  payeurs ; les 429 y sont comptés à part, comme l'autorise l'énoncé.
