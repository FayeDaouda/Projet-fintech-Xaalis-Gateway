# Vidéo de démonstration — guide de tournage (5 minutes)

Scénario imposé : paiement réussi suivi en SSE, panne de Ndiaga Cash et bascule,
remboursement partiel, webhook en dead letter puis rejoué, rapprochement avec écarts,
règlement T+1. Tout est déroulé par [`scripts/demo.sh`](../scripts/demo.sh), qui marque une
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

## Déroulé conseillé

| Temps | Étape | À montrer | À dire |
|---|---|---|---|
| 0:00 | Introduction | `docker compose ps` (7 services sains), `/docs` | Une API pour trois opérateurs ; grand livre en partie double ; tout démarre avec une commande |
| 0:30 | **1. SSE** | Les événements arrivent à l'écran au fil de l'eau | Flux SSE par clé publique ; `LISTEN/NOTIFY` transactionnel ; commission 180 (1,8 %, XG-04), numéro masqué (XG-03) |
| 1:15 | **2. Bascule** | `operator: gopay` pour un numéro en 77, historique `operator_failover` | Routage au moins cher (XG-06) ; disjoncteur ; jamais de double débit : bascule seulement sur refus certain (ADR 0007) |
| 2:00 | **3. Remboursement** | `partially_refunded`, solde | Requête signée HMAC + idempotente ; XG-08 à XG-11 ; 50 remboursements concurrents → exactement 10 (test) |
| 2:40 | **4. Dead letter** | Compteur de tentatives jusqu'à 8, puis `delivered` après rejeu | Outbox transactionnel ; signature `t=…,v1=…` ; 8 tentatives avec jitter ; le marchand déduplique par `event_id` |
| 3:30 | **5. Rapprochement** | Anomalies injectées et écarts détectés (4 types) | Relevé CSV piégé, Polars, `Int64` strict ; rapport exportable en CSV |
| 4:15 | **6. T+1** | Refus le jour même (409), règlement à J+1 des seules lignes saines, virement | Jours comptables de Dakar (XG-15) ; grand livre équilibré, somme nulle (XG-16) |
| 4:45 | Conclusion | CI verte, `docs/adr/` | 600+ tests sur PostgreSQL réel, Schemathesis sans écart, ADR |

## Après le tournage

Mettre en ligne en YouTube « non répertorié » (ou Drive), puis renseigner le lien dans le
[README](../README.md#vidéo-de-démonstration).
