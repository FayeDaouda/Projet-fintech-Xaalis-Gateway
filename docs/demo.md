# Vidéo de démonstration — guide de tournage (5 minutes)

Scénario imposé : paiement réussi suivi en SSE, panne de Ndiaga Cash et bascule,
remboursement partiel, webhook en dead letter puis rejoué, rapprochement avec écarts,
règlement T+1. Deux façons de le dérouler : dans la **console web** (option A, la plus
parlante à l'écran) ou par [`scripts/demo.sh`](../scripts/demo.sh) (option B), qui marque une
pause avant chaque étape : vous commentez, puis Entrée.

## Préparation (hors caméra)

```bash
docker compose down -v                                   # base neuve : chiffres lisibles
XAALIS_WEBHOOK_RETRY_SCHEDULE_SECONDS='[2,2,2,2,2,2,2]' docker compose up -d --build
```

Les relances accélérées ne servent qu'à la démo (en production : 8 tentatives sur ≈ 24 h).
Le script crée son propre marchand : il est rejouable autant de fois que nécessaire.

À ouvrir dans le navigateur, pour l'introduction : http://localhost:8000/docs (contrat
OpenAPI), et éventuellement http://localhost:8000/metrics.

## Option A — dans la console web (recommandée pour la vidéo)

Ouvrir http://localhost:8200 (le **Centre de contrôle** s'affiche), puis **Simulateur
opérateurs → Mode présentation** (callbacks en 1,5 s, sans aléa : tournage reproductible).
« Nouveau marchand », en bas du menu, remet les chiffres à zéro entre deux prises ; la
palette <kbd>⌘K</kbd> donne accès à tous les écrans et aux actions de démonstration. Chaque
écran commence par une présentation (« Ce que vous voyez », « Sous le capot ») qui sert de
support visuel.

| Temps | Écran | Geste | Message |
|---|---|---|---|
| 0:00 | Centre de contrôle | Recharger : écran de démarrage | Une API pour trois opérateurs ; tout démarre avec une commande |
| 0:25 | Centre de contrôle | « Rafale × 5 » | La carte suit chaque paiement : API, outbox, worker, anti-fraude, opérateur, callback, grand livre, webhook |
| 0:55 | Boutique | 2 articles, puce « 78 », Payer | Routage annoncé ; suivi SSE avec la clé publique ; reçu : commission (XG-04), numéro masqué (XG-03) |
| 1:35 | Simulateur → Boutique | Panne Ndiaga, payer en « 77 », Rétablir | Bascule sur refus certain uniquement, jamais de double débit (ADR 0007) |
| 2:10 | Paiements | Rembourser 3 000 | Signé HMAC, idempotent (XG-08 à XG-11) ; écritures et trace du paiement |
| 2:40 | Webhooks | Panne 503, paiement de démo (⌘K), Rejouer | 8 tentatives, dead letter, rejeu ; outbox transactionnel |
| 3:20 | Rapprochement & T+1 | Lancer, régler le jour même, J+1 | Anomalies injectées toutes détectées ; T+1 en heure de Dakar (XG-15) |
| 3:55 | Grand livre | Montrer débit = crédit et Σ = 0 | Partie double garantie par PostgreSQL (XG-16, ADR 0001) |
| 4:15 | Centre de contrôle, Grafana, Jaeger | « Paiement suspect » | Bonus : anti-fraude gRPC 150 ms (ADR 0013), traces et tableau de bord (ADR 0014) |
| 4:40 | GitHub Actions | CI verte | 680+ tests sur PostgreSQL réel, Schemathesis sans écart, 14 ADR |

La rafale de 0:25 fournit assez de paiements pour que le règlement J+1 de 3:20 en libère une
partie (les anomalies du relevé piégé n'en touchent que quelques-uns).

## Option B — par le script

| Temps | Étape | À montrer | À dire |
|---|---|---|---|
| 0:00 | Introduction | `docker compose ps` (10 services sains), `/docs` | Une API pour trois opérateurs ; grand livre en partie double ; tout démarre avec une commande |
| 0:30 | **1. SSE** | Les événements arrivent à l'écran au fil de l'eau | Flux SSE par clé publique ; `LISTEN/NOTIFY` transactionnel ; commission 180 (1,8 %, XG-04), numéro masqué (XG-03) |
| 1:15 | **2. Bascule** | `operator: gopay` pour un numéro en 77, historique `operator_failover` | Routage au moins cher (XG-06) ; disjoncteur ; jamais de double débit : bascule seulement sur refus certain (ADR 0007) |
| 2:00 | **3. Remboursement** | `partially_refunded`, solde | Requête signée HMAC + idempotente ; XG-08 à XG-11 ; 50 remboursements concurrents → exactement 10 (test) |
| 2:40 | **4. Dead letter** | Compteur de tentatives jusqu'à 8, puis `delivered` après rejeu | Outbox transactionnel ; signature `t=…,v1=…` ; 8 tentatives avec jitter ; le marchand déduplique par `event_id` |
| 3:30 | **5. Rapprochement** | Anomalies injectées et écarts détectés (4 types) | Relevé CSV piégé, Polars, `Int64` strict ; rapport exportable en CSV |
| 4:15 | **6. T+1** | Refus le jour même (409), règlement à J+1 des seules lignes saines, virement | Jours comptables de Dakar (XG-15) ; grand livre équilibré, somme nulle (XG-16) |
| 4:45 | Conclusion | CI verte, `docs/adr/`, Grafana | 670+ tests sur PostgreSQL réel, Schemathesis sans écart, 14 ADR |

## Après le tournage

Mettre en ligne en YouTube « non répertorié » (ou Drive), puis renseigner le lien dans le
[README](../README.md#vidéo-de-démonstration).
