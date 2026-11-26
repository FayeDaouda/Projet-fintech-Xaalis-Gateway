# Test de charge — création d'intents de paiement

## Objectif (énoncé)

Scénario de 2 minutes : **200 créations d'intent par seconde**, **p95 < 150 ms** sur une
machine de développement, **0 erreur hors 429**. À défaut, analyser le goulot d'étranglement.

## Résultat final

| Critère | Objectif | Mesuré | |
|---|---|---|---|
| Débit soutenu sur 2 min | 200 créations/s | **200,2 créations/s** (22 837 requêtes) | ✅ |
| Erreurs (hors 429) | 0 | **0** | ✅ |
| Latence p95 | < 150 ms | **670 ms** (médiane 330 ms, p99 840 ms) | ❌ |

Fichiers : [rapport Locust HTML](load-test/locust-report.html),
[statistiques CSV](load-test/locust-stats.csv), [courbe latence/débit](load-test/latency-curve.txt).

L'objectif de débit est atteint sans erreur ; l'objectif de latence ne l'est pas **sur cette
machine**. L'analyse ci-dessous montre, mesures à l'appui, que la limite vient de la
capacité CPU de la machine de développement (virtualisation comprise) et non d'un défaut
de conception ; elle chiffre aussi ce qui reste à faire pour tenir le p95.

## Protocole

- **Machine** : MacBook Pro, Intel Core i9-9880H (8 cœurs, 16 threads), 16 Go ; Docker
  Desktop (VM Linux, 16 vCPU, 8 Go). **Toute** la pile tourne sur cette machine : API,
  PostgreSQL, Redis, worker, simulateurs, et le générateur de charge.
- **Scénario** ([`loadtest/locustfile.py`](../loadtest/locustfile.py)) : 200 utilisateurs
  virtuels, chacun à **1 création/s** (`constant_throughput`). Chaque requête porte une
  `Idempotency-Key` unique et un numéro de payeur aléatoire, comme un vrai client.
- **Règles respectées, pas contournées** : la limite XG-13 (100 requêtes/min par clé) reste
  active ; le scénario crée un pool de 200 marchands (60 requêtes/min par clé). Aucun 429.
- **Locust tourne dans un conteneur sur le réseau de la pile** (`http://api:8000`) : passer
  par la redirection de ports de Docker Desktop ajoute un proxy en espace utilisateur sur
  l'hôte. La montée en charge est exclue des statistiques (`--reset-stats`).
- **Configuration** : celle du dépôt (`docker compose up`), journaux `INFO` actifs, 4
  processus uvicorn, durabilité PostgreSQL intacte (`synchronous_commit = on`).

```bash
docker compose up -d --build
scripts/loadtest.sh 200 2m 200          # utilisateurs, durée, taille du pool de marchands
```

## Évolution des mesures

| Étape | Débit | Médiane | p95 | Erreurs |
|---|---|---|---|---|
| Ligne de base : 1 processus uvicorn ¹ | 96/s | 1 200 ms | 4 800 ms | 0 |
| 4 processus uvicorn ¹ | 193/s | 110 ms | 900 ms | 0 |
| + cache d'authentification, `now()` en base, journaux non bloquants | **200/s** | **330 ms** | **670 ms** | **0** |

¹ Locust lancé sur l'hôte macOS (avant le passage au conteneur sur le réseau de la pile).

Les mesures sur cette machine varient d'une exécution à l'autre (± 100 ms sur la médiane) :
chaque conclusion ci-dessous repose sur des **A/B consécutifs** dans le même état.

## Analyse du goulot d'étranglement

### 1. Un seul processus Python ne peut utiliser qu'un cœur

Ligne de base : le conteneur API plafonne à **100 % de CPU** (un cœur), PostgreSQL à 30 %,
Redis à 2 %. Le GIL limite un processus à un cœur ; au-delà de ~96 req/s les requêtes font
la queue (p95 4,8 s). **Correction** : plusieurs processus uvicorn (`WEB_CONCURRENCY`, 4 par
défaut). Deux conséquences traitées : métriques Prometheus en mode multiprocessus (sinon
`/metrics` ne montre qu'un processus) et budget de connexions PostgreSQL (`max_connections`).
**Effet : débit × 2.**

### 2. Coût d'une création : 11 ms, sans point chaud

Profil `cProfile` dans le conteneur (300 créations séquentielles) : **11,2 ms par requête,
dont 8,4 ms de CPU**. Le temps est réparti entre FastAPI (résolution des dépendances),
SQLAlchemy ORM (unité de travail, cache de requêtes) et asyncpg ; aucune fonction ne domine.
Une requête HTTP réelle mesurée seule avec `curl` sur le réseau Docker : **11,6 ms**.

Capacité d'un processus (même expérience, concurrence croissante) :

| Concurrence | Débit | Médiane |
|---|---|---|
| 1 | 81 req/s | 11 ms |
| 10 | 122 req/s | 77 ms |

Un processus plafonne à **~120 créations/s**, et au-delà la latence suit la loi de Little
(10 / 122 ≈ 82 ms). Quatre processus offrent une capacité théorique de ~480/s, **à
condition de disposer de 4 cœurs libres**.

### 3. La base de données n'est pas le goulot

`pg_stat_statements` sous charge : chaque instruction s'exécute en ~1 ms (INSERT intent
1,05 ms, INSERT idempotence 0,98 ms, historique 0,59 ms, outbox 0,37 ms) : ~3,5 ms de SQL
par création. Un COMMIT durable (`fsync`) coûte 0,86 ms, un aller-retour SQL 0,19 ms.

### 4. Hypothèses testées et réfutées (A/B consécutifs)

| Hypothèse | Expérience | Résultat | Verdict |
|---|---|---|---|
| `NOTIFY` (flux SSE) sérialise les COMMIT | `NOTIFY` désactivé (copie de mesure) | médiane 430 → 380 ms | ❌ marginal |
| Écriture des journaux bloquante | niveau `INFO` vs `WARNING` | 420 vs 410 ms | ❌ (après passage aux journaux non bloquants) |
| COMMIT durable (`fsync`) | `synchronous_commit = off` (diagnostic, rétabli) | 450 ms | ❌ |
| Limitation de débit Redis | activée vs désactivée, 50/s | 120 vs 110 ms | ❌ |
| Proxy de ports de Docker Desktop | Locust sur l'hôte vs dans le réseau Docker | inchangé | ❌ |
| Connexions mal réparties entre processus | CPU par processus | équilibré (± 3 %) | ❌ |
| Trop peu de processus | 4, 6 puis 8 processus | médiane ~400 ms dans les trois cas | ❌ |

### 5. Conclusion : la machine entière est saturée, virtualisation comprise

Pendant le test à 200/s, sur l'hôte macOS : **7 % de temps utilisateur, 44 % de temps
système** (l'hyperviseur de Docker Desktop) et 50 % inactif sur 16 threads logiques. Sur un
processeur à hyperthreading, cela correspond à des cœurs physiques proches de la saturation.
Dans la VM : API 286 %, PostgreSQL 218 %, worker 30 %, plus le générateur de charge.

Chaque création effectue une douzaine d'allers-retours SQL, deux appels Redis et un échange
HTTP : environ trente opérations réseau, dont chaque appel système est surtaxé par la
virtualisation (d'où PostgreSQL à 2 cœurs pour seulement ~3,5 ms de SQL par transaction).
Ajouter des processus ne fait que déplacer la contention sur les mêmes 8 cœurs : c'est
pourquoi la latence est restée invariante de 4 à 8 processus.

## Optimisations retenues (toutes testées)

| Optimisation | Raison | Vérification |
|---|---|---|
| 4 processus uvicorn + métriques Prometheus multiprocessus | Un processus = un cœur (GIL) | débit × 2 ; `/metrics` agrégé |
| Cache des clés authentifiées (5 s, par processus) | Une session et une connexion de moins par requête | tests : expiration d'une clé en grâce respectée même en cache |
| `created_at` / `expires_at` calculés par PostgreSQL, relus par `RETURNING` | Un aller-retour `SELECT now()` de moins | migration 0008 ; test XG-07 (15 min) |
| Journaux non bloquants (`QueueHandler` + thread d'écriture) | Un tube stdout saturé ne gèle plus la boucle asyncio | tests de masquage des numéros (XG-03) |
| Dépendance `get_resources` asynchrone | Évite un passage par le pool de threads à chaque requête | — |

## Ce qu'il resterait à faire pour tenir p95 < 150 ms

Par gain attendu décroissant :

1. **Matériel de mesure représentatif** : PostgreSQL et le générateur de charge sur des
   machines distinctes, sur Linux sans hyperviseur. La moitié du CPU de l'hôte part ici
   en temps système de virtualisation.
2. **Moins d'allers-retours par création** : `NOTIFY` émis par un trigger sur
   `payment_events`, insertions en SQLAlchemy Core (sans l'unité de travail ORM), fusion de
   la réservation et de l'enregistrement de la réponse d'idempotence. Gain estimé : 30 à
   40 % du CPU applicatif par requête.
3. **Mise à l'échelle horizontale** : l'API est sans état (tout l'état partagé est dans
   PostgreSQL et Redis) ; deux instances sur deux machines doublent la capacité.
4. **PgBouncer** en mode transaction, pour mutualiser les connexions entre processus.

Garanties **non négociables** pendant ces mesures : durabilité des COMMIT, limitation de
débit XG-13, idempotence et transactions identiques à la production. Aucun réglage de
diagnostic (`synchronous_commit = off`, `NOTIFY` désactivé, `pg_stat_statements`) n'a été
conservé dans le dépôt.
