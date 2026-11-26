# ADR 0008 — Remboursements : comptes débités, verrous et ordre des contrôles

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : XG-08, XG-09, XG-10, XG-11, XG-16

## Contexte

Un remboursement rend au payeur tout ou partie du montant. L'énoncé impose : intent
`succeeded` ou `partially_refunded` et moins de 30 jours depuis le succès (XG-08) ; jamais
plus que le montant initial, même en concurrence (XG-09) ; commission non remboursée
(XG-10) ; refus si `merchant_pending + merchant_available` est insuffisant (XG-11).

L'exigence de test « 50 × 1 000 FCFA sur un paiement de 10 000, le marchand disposant **par
ailleurs** d'un solde suffisant » implique qu'un remboursement total (10 000) puisse puiser
au-delà du net de ce paiement (9 820) : les 180 de commission viennent des **autres** fonds
du marchand. Le solde du marchand est donc traité comme une réserve commune.

## Décision

1. **Comptes débités** : `merchant_pending` d'abord (les fonds les plus récents, pas encore
   engagés), puis `merchant_available` ; crédit de `operator_clearing:{opérateur}`, qui
   reverse au payeur. `xaalis_revenue` n'est jamais touché (XG-10).
2. **Verrous**, dans l'ordre de l'ADR 0002 : environnement marchand, puis intent, tous deux
   en `SELECT … FOR NO KEY UPDATE` (voir l'addendum de l'ADR 0002).
3. **Ordre des contrôles** sous verrous :
   1. l'état permet-il un remboursement ? — question posée à la table de transitions, seule
      autorité (409 `invalid_transition`) ;
   2. montant > 0, et ≤ reste remboursable (409 `refund_exceeds_amount`) ;
   3. moins de 30 jours depuis `succeeded_at`, horloge de la base (409 `refund_window_expired`) ;
   4. solde suffisant (409 `insufficient_balance`).
4. **Effets, dans une seule transaction** : écritures du grand livre (référence `re_…`),
   ligne `refunds` (contrainte `from_pending + from_available = amount`), transition
   `refund_partial` / `refund_full` qui incrémente `amount_refunded` (borné en base par
   `CHECK amount_refunded ≤ amount`), événements `refund.succeeded` et
   `payment.(partially_)refunded` dans l'outbox.
5. **Requête signée HMAC et Idempotency-Key obligatoire** : un rejeu ne rembourse pas deux fois.
6. **Règlement T+1 (lot 6)** : il ne déplace jamais plus que le solde `pending` réellement
   restant ; aucun compte marchand ne devient négatif.

## Conséquences

**Positives**

- XG-09 est garantie trois fois : verrou applicatif, contrôle sous verrou, contrainte en base.
- Le test des 50 remboursements concurrents accepte exactement 10 demandes, de façon stable.

**Négatives / risques**

- Le verrou par marchand sérialise ses remboursements et virements : acceptable, ces
  opérations sont rares au regard des encaissements, qui ne prennent pas ce verrou.
- Le remboursement est synchrone et réputé réussi ; un appel de remboursement réel à
  l'opérateur passerait par l'outbox, comme les débits (limite connue).
