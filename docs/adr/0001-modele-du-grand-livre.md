# ADR 0001 — Modèle du grand livre en partie double

- **Statut** : accepté
- **Date** : 2026-11-25
- **Règles concernées** : XG-01, XG-04, XG-05, XG-10, XG-12, XG-16 (lot 4)

## Contexte

Xaalis encaisse pour le compte de marchands, prélève une commission et leur reverse le
solde. Toute erreur d'argent (double crédit, solde négatif, écriture perdue) est un
incident grave, et un seul contre-exemple de déséquilibre annule le critère « ledger ».

Contraintes de l'énoncé :

- aucun solde stocké dans une colonne modifiable : un solde est une agrégation d'écritures ;
- chaque mouvement est une transaction comptable d'au moins deux écritures de somme nulle ;
- les écritures sont immuables, imposé **au niveau de la base** ; on corrige par extourne ;
- comptes minimum : `operator_clearing:{op}`, `merchant_pending:{m}`,
  `merchant_available:{m}`, `merchant_payout:{m}`, `xaalis_revenue` ;
- environnements test et live étanches, y compris pour les soldes (XG-12) ;
- montants entiers en XOF, jamais de float (XG-01).

## Décision

### 1. Trois tables

| Table | Rôle | Points clés |
|---|---|---|
| `ledger_accounts` | Plan de comptes | `code` lisible (`merchant_pending:m_…`), `kind`, `livemode`, unicité `(code, livemode)` |
| `ledger_transactions` | En-tête d'un mouvement | `kind` (`payment_capture`, `refund`, `settlement`, `payout`, `reversal`), `reference` métier (`pi_…`, `re_…`, `po_…`), unicité `(kind, reference)` |
| `ledger_entries` | Écritures | `amount BIGINT` signé, `CHECK (amount <> 0)`, `livemode` |

Les comptes globaux (`operator_clearing:gopay`, `xaalis_revenue`) existent **une fois par
environnement** : un paiement de test ne touche jamais un compte live.

### 2. Convention de signe : débit positif, crédit négatif

Une écriture porte un montant signé : `+n` = débit, `−n` = crédit. L'invariant XG-16
s'écrit alors simplement `SUM(amount) = 0`, par transaction comme sur tout le grand livre.

Le solde présenté d'un compte applique son **sens normal** :

| Nature | Comptes | Sens normal | Solde présenté |
|---|---|---|---|
| Actif | `operator_clearing:*` | débiteur | `SUM(amount)` |
| Passif (dû au marchand) | `merchant_pending`, `merchant_available`, `merchant_payout` | créditeur | `−SUM(amount)` |
| Produit | `xaalis_revenue` | créditeur | `−SUM(amount)` |

Le code applicatif ne manipule jamais un signe à la main : il construit les lignes avec
`debit(compte, n)` / `credit(compte, n)` où `n > 0`, ce qui supprime une classe entière
d'erreurs de signe.

### 3. Écritures types

| Événement | Écritures (XOF) |
|---|---|
| Paiement réussi de 10 000 (commission 180) | `operator_clearing:gopay` +10 000 · `merchant_pending:m` −9 820 · `xaalis_revenue` −180 |
| Règlement T+1 | `merchant_pending:m` +9 820 · `merchant_available:m` −9 820 |
| Virement (payout) | `merchant_available:m` +X · `merchant_payout:m` −X |
| Remboursement de R | `merchant_pending:m` (ou `available`) +R · `operator_clearing:op` −R — la commission n'est pas extournée (XG-10) |
| Correction | transaction `reversal` aux montants opposés, liée par `reverses_id` |

### 4. Garanties portées par PostgreSQL (dernière ligne de défense)

Toutes les violations lèvent le SQLSTATE applicatif **`XG016`** (du nom de la règle) :
le code et les tests les reconnaissent sans dépendre du texte du message.

1. **Immutabilité** : trigger `BEFORE UPDATE OR DELETE` (par ligne) et `BEFORE TRUNCATE`
   (par instruction) sur `ledger_accounts`, `ledger_transactions` et `ledger_entries`.
2. **Sceau** : un trigger `BEFORE INSERT` sur `ledger_entries` n'accepte une écriture que si
   son en-tête a été créé **dans la même transaction SQL** (colonne technique
   `created_xact_id xid8 DEFAULT pg_current_xact_id()`). Une transaction comptable validée
   est donc close : on ne peut plus lui ajouter d'écriture, même une paire équilibrée.
3. **Équilibre** : `CONSTRAINT TRIGGER … AFTER INSERT ON ledger_transactions DEFERRABLE
   INITIALLY DEFERRED`. Au `COMMIT`, il vérifie que la transaction comptable compte au moins
   deux écritures et que `SUM(amount) = 0`. Grâce au sceau, un contrôle par en-tête suffit à
   couvrir toutes les écritures (un par mouvement, pas un par écriture). Un mouvement
   déséquilibré fait **annuler la transaction SQL entière**, état métier compris.
4. **Étanchéité test/live** : clés étrangères composites
   `(account_id, livemode) → ledger_accounts(id, livemode)` et
   `(transaction_id, livemode) → ledger_transactions(id, livemode)`. Une écriture ne peut
   pas mélanger les environnements, même par erreur de code.
5. **Pas de double comptabilisation** : l'unicité `(kind, reference)` rend chaque
   comptabilisation idempotente (un callback dupliqué ne crédite pas deux fois).

Le module `xaalis.ledger` valide les mêmes règles en Python avant d'écrire (échec rapide,
message clair) ; la base reste l'autorité.

### 5. Calcul des soldes

Agrégation directe `SUM(amount)` par compte, servie par un index couvrant
`(account_id) INCLUDE (amount)` qui permet un *index-only scan*. Aucun instantané
matérialisé n'est introduit à ce stade : il ne le sera que si le test de charge montre que
l'agrégation est le goulot, et il devra alors être recalculable et vérifié par
`/v1/admin/ledger/health`.

### 6. Identifiants et montants

- En-têtes de transaction : ULID préfixé `ltx_`, triable dans le temps ;
- écritures : `BIGINT GENERATED ALWAYS AS IDENTITY`, internes, jamais exposées ;
- montants : `int` Python ↔ `BIGINT` PostgreSQL, devise implicite XOF (devise unique, XG-02).

## Conséquences

**Positives**

- L'invariant XG-16 est garanti par la base même en cas de bogue applicatif : aucun chemin
  de code ne peut valider un mouvement déséquilibré ou modifier l'historique.
- Piste d'audit complète : tout solde se justifie écriture par écriture.
- `GET /v1/admin/ledger/health` se réduit à des requêtes simples (somme globale, somme par
  transaction, transactions à moins de deux écritures).

**Négatives / risques**

- Le trigger différé s'exécute une fois par mouvement au commit (une requête indexée sur
  `transaction_id`) : coût marginal, à mesurer au test de charge.
- Le coût d'un solde croît avec le nombre d'écritures du compte ; l'index couvrant repousse
  la limite, un instantané reste l'option documentée.
- Un superutilisateur PostgreSQL peut désactiver les triggers. Mitigation envisagée : un rôle
  applicatif distinct du propriétaire du schéma, avec seulement `SELECT, INSERT` sur le
  grand livre (non retenu pour ce rendu, faute de temps ; noté dans les limites connues).
