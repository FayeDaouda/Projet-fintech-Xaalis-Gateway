# ADR 0010 — Traitement des relevés avec Polars

- **Statut** : accepté
- **Date** : 2026-11-27
- **Règles concernées** : lot 6, XG-01, XG-15

## Contexte

Chaque nuit, chaque opérateur publie un relevé CSV de la veille, volontairement piégé :
ligne manquante, ligne en trop, montant différent, doublon. Il faut le comparer au grand
livre et classer les écarts. L'énoncé impose pandas ou Polars, au choix justifié.

## Décision

**Polars** (lecture CSV, regroupement, jointure externe, classement par expressions).

| Critère | Polars | pandas |
|---|---|---|
| Types | stricts : `Int64` sans valeur manquante implicite en flottant | une colonne entière avec un vide devient `float64` |
| Montant mal formé (« 12.5 ») | `cast(Int64, strict=True)` lève une erreur | conversion silencieuse en flottant, arrondi possible |
| Jointure externe + nulls | `how="full"`, nulls typés, pas d'index implicite | `merge(how="outer")`, entiers promus en flottants |
| Performance | moteur Rust multithread, lecture paresseuse possible | mono-thread, plus gourmand en mémoire |

Le critère décisif est **XG-01** : avec pandas, une jointure externe transforme les montants
entiers en `float64` dès qu'il manque une valeur (`NaN`), ce qui introduirait des flottants
dans le domaine. Avec Polars, le relevé est lu entièrement en texte puis le montant est
converti strictement en `Int64` : un relevé invalide **échoue** avec une raison lisible
(`runs[].error`) plutôt que d'être arrondi.

Classement (une ligne par référence opérateur, dans cet ordre de priorité) :
`missing_in_ledger` (absent du grand livre), `missing_in_statement` (absent du relevé),
`duplicate` (plusieurs fois dans le relevé), `amount_mismatch`, sinon `matched`.

Le côté grand livre est la liste des captures (`payment_capture`) du **jour comptable de
Dakar** (XG-15), bornes calculées par `xaalis.core.clock`. La dernière exécution terminée
d'un couple (date, opérateur) fait foi ; seules ses lignes `matched` autorisent le
règlement T+1.

## Conséquences

- Moteur pur (`reconciliation/engine.py`), testé sans base ni réseau.
- Dépendance lourde (~30 Mo) dans l'image, acceptable pour un traitement par lots.
- Un callback tardif (XG-07) — payé chez l'opérateur, intent expiré, rien au grand livre —
  ressort naturellement en `missing_in_ledger`, en plus de son alerte de rapprochement.
