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

Ouvrir http://localhost:8200, puis **Simulateur opérateurs → Mode présentation** (callbacks
en 1,5 s, sans aléa : le tournage est reproductible). « Nouveau marchand », en bas du menu,
remet les chiffres à zéro entre deux prises.

| Temps | Écran | Geste | À dire |
|---|---|---|---|
| 0:00 | Terminal + `/docs` | `docker compose ps`, contrat OpenAPI | Une API pour trois opérateurs ; tout démarre avec une commande |
| 0:30 | **Boutique** | 2 articles, puce « 78 · Go Pay », Payer | Chronologie et flux SSE en direct (clé publique) ; reçu : commission (XG-04), numéro masqué (XG-03), webhook vérifié |
| 1:15 | **Simulateur** puis **Boutique** | Panne Ndiaga (2 min), puis payer avec « 77 » | Ndiaga est le moins cher (XG-06) mais en panne : badge « bascule », Go Pay ; jamais de double débit (ADR 0007) |
| 2:00 | **Paiements** | Ouvrir le premier paiement, rembourser 3 000 | Requête signée HMAC et idempotente ; XG-08 à XG-11 ; historique « Remb. partiel » |
| 2:40 | **Webhooks** | Activer la panne, payer dans la boutique, revenir | 8 points rouges puis « Dead letter » ; désactiver la panne, « Rejouer » → « Traité » |
| 3:30 | **Boutique** puis **Rapprochement** | 5 ou 6 paiements, puis « Lancer le rapprochement » | Les anomalies injectées sont toutes cochées : 4 types d'écarts détectés ; export CSV |
| 4:15 | **Rapprochement** puis **Vue d'ensemble** | « Régler le jour même » (409), « Simuler J+1 », puis « Virer » | Jours de Dakar (XG-15) ; seules les lignes saines sont réglées ; grand livre à somme nulle (XG-16) |
| 4:35 | **Bonus** | Boutique : puce « 77 000 00 00 · signalé » ; Grafana (:3000) ; une trace dans Jaeger (:16686) | Scoring gRPC avant tout débit (deadline 150 ms, repli justifié) ; une seule trace traverse 5 services, outbox comprise |
| 4:50 | Conclusion | CI verte, `docs/adr/` | 670+ tests sur PostgreSQL réel, Schemathesis sans écart, 14 ADR |

Le rapprochement porte sur tous les paiements du jour : avec un seul ou deux paiements, les
anomalies du relevé piégé peuvent tous les toucher et rien n'est réglé à J+1. D'où les
quelques paiements supplémentaires avant l'étape 5.

## Option B — par le script

| Temps | Étape | À montrer | À dire |
|---|---|---|---|
| 0:00 | Introduction | `docker compose ps` (7 services sains), `/docs` | Une API pour trois opérateurs ; grand livre en partie double ; tout démarre avec une commande |
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
