# ADR 0004 — Hachage des clés d'API (HMAC-SHA-256 + pepper) et des mots de passe (Argon2id)

- **Statut** : accepté
- **Date** : 2026-11-25
- **Règles concernées** : lot 1, OWASP API2 (authentification défaillante)

## Contexte

L'énoncé impose de ne stocker que l'empreinte de la clé secrète, « SHA-256 avec un pepper
lu depuis l'environnement, ou Argon2id : à justifier ». Deux secrets de natures très
différentes coexistent :

| | Clé d'API `sk_…` | Mot de passe administrateur |
|---|---|---|
| Choisi par | la machine (`secrets`) | un humain |
| Entropie | 32 caractères base62 ≈ **190 bits** | faible, souvent < 40 bits |
| Fréquence de vérification | **chaque requête** (objectif 200 req/s, p95 < 150 ms) | à la connexion |
| Recherche en base | par la clé elle-même | par l'e-mail |

Un hachage lent (Argon2id, bcrypt) sert à rendre coûteuse une attaque par dictionnaire
sur un secret **devinable**. Il n'apporte rien contre un secret de 190 bits, impossible
à énumérer, mais coûte ~50 ms de CPU par appel.

## Décision

### Clés d'API : HMAC-SHA-256 avec pepper

- Empreinte = `HMAC-SHA-256(pepper, clé)`, 32 octets, colonne `UNIQUE` indexée.
- Le **pepper** (`XAALIS_API_KEY_PEPPER`, 32 caractères minimum) vit dans l'environnement,
  jamais en base : une fuite de la base seule ne permet même pas de tester une clé candidate.
- Authentification = calcul de l'empreinte puis **une recherche indexée** (O(1)), suivie
  d'une comparaison `hmac.compare_digest` en temps constant. La recherche par égalité sur
  l'index ne fuit rien d'exploitable : l'attaquant ne contrôle que l'entrée du HMAC,
  pas l'empreinte comparée.
- Une forme masquée (`sk_test_…Ab12`) est stockée pour l'affichage ; la clé en clair
  n'est renvoyée qu'une fois, à la création ou à la rotation.
- **Rotation** : la nouvelle clé est active immédiatement, l'ancienne reçoit
  `expires_at = now() + 24 h`. Une seconde rotation avec l'ancienne clé ne prolonge
  jamais son échéance (`min`).

### Mots de passe administrateur : Argon2id

- `argon2-cffi`, paramètres par défaut (profil RFC 9106 *low memory* : t = 3, m = 64 Mio, p = 4).
- Le calcul est exécuté dans un thread (`asyncio.to_thread`) pour ne pas bloquer la boucle.
- E-mail inconnu : on vérifie quand même un hachage factice, pour que le temps de réponse
  ne révèle pas quels comptes existent ; la réponse 401 est identique dans les deux cas.

## Conséquences

**Positives**

- Coût d'authentification d'une clé : un HMAC (≈ 1 µs) et une lecture d'index, compatible
  avec l'objectif de charge.
- Une fuite de la base (sauvegarde, injection SQL) n'expose ni clé ni mot de passe exploitable.
- La rotation sans interruption permet au marchand de redéployer sans coupure.

**Négatives / risques**

- Changer le pepper invalide toutes les clés existantes. Une rotation du pepper exigerait
  de stocker un identifiant de version du pepper avec chaque empreinte (non implémenté).
- Le pepper est un secret de plus à gérer en production (gestionnaire de secrets).
- La vérification Argon2 (~50 ms) rend la route de connexion coûteuse : elle devra être
  soumise à une limite de débit stricte (lot sécurité, XG-13).
