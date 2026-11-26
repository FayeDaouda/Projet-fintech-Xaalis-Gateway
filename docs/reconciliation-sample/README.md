# Échantillon de rapprochement

Produit le 2026-11-26 par la vraie pile (`docker compose up`) : 6 paiements Go Pay réussis,
relevé publié par le simulateur, rapprochement lancé par `POST /v1/admin/reconciliations`.

| Fichier | Contenu |
|---|---|
| [`gopay-statement-2026-11-26.csv`](gopay-statement-2026-11-26.csv) | Relevé de Go Pay tel que publié par le simulateur (`GET /gopay/statements/2026-11-26.csv`) |
| [`injected-anomalies-2026-11-26.json`](injected-anomalies-2026-11-26.json) | Les anomalies que le simulateur y a **volontairement** injectées (`GET /statements/2026-11-26/anomalies.json`) |
| [`report-2026-11-26.json`](report-2026-11-26.json) | Rapport d'écarts produit par la passerelle (`GET /v1/admin/reconciliations/2026-11-26`) |
| [`report-2026-11-26.csv`](report-2026-11-26.csv) | Le même rapport exporté en CSV (`?format=csv`) |

## Lecture

Le relevé contient 7 lignes pour 6 paiements réels. Les quatre anomalies injectées sont
toutes retrouvées, chacune avec son type :

| Anomalie injectée | Effet dans le relevé | Écart détecté |
|---|---|---|
| `missing` | un paiement réel absent du relevé | `missing_in_statement` (montant au grand livre, aucun au relevé) |
| `extra` | une ligne `GO-FAKE…` sans paiement correspondant | `missing_in_ledger` |
| `amount` | un montant majoré de 500 XOF | `amount_mismatch` (15 500 au relevé, 15 000 au grand livre) |
| `duplicate` | une ligne présente deux fois | `duplicate` (`occurrences = 2`) |

Les 3 autres paiements sont rapprochés sans écart : seuls eux ont été réglés à J+1
(`merchant_pending → merchant_available`, 14 665 XOF nets), les 3 paiements en écart
restent en attente jusqu'à résolution.

## Rejouer avec un relevé fourni

```bash
curl -X POST "localhost:8000/v1/admin/reconciliations?date=2026-11-26&operator=gopay" \
     -H "Authorization: Bearer $ADMIN_JWT" -H "Content-Type: text/csv" \
     --data-binary @gopay-statement-2026-11-26.csv
```

Colonnes reconnues : `operator_reference` et `amount` (obligatoires), `merchant_reference`,
`currency` (doit valoir XOF), `status` (seules les lignes `succeeded` comptent), `operator`
(relevé consolidé). Un montant non entier fait échouer le relevé, avec la raison dans
`runs[].error`.
