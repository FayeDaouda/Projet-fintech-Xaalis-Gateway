"""Schémas d'écriture des faits métier (ADR 0001 §3).

Chaque fonction traduit un fait métier en lignes équilibrées. C'est le seul endroit
du code qui sait quels comptes sont touchés par un paiement, un règlement, etc.
"""

from __future__ import annotations

from xaalis.core.fees import FeeBreakdown
from xaalis.core.operators import Operator
from xaalis.ledger.accounts import AccountRef
from xaalis.ledger.lines import Line, credit, debit


def payment_capture(merchant_id: str, operator: Operator, breakdown: FeeBreakdown) -> list[Line]:
    """Paiement réussi : l'opérateur nous doit le montant brut, partagé entre marchand et Xaalis.

    Pour un montant de 100 XOF la commission minimale (100) absorbe tout : le net est nul
    et aucune écriture nulle n'est produite pour le marchand.
    """
    lines = [debit(AccountRef.operator_clearing(operator), breakdown.amount)]
    if breakdown.net > 0:
        lines.append(credit(AccountRef.merchant_pending(merchant_id), breakdown.net))
    lines.append(credit(AccountRef.revenue(), breakdown.fee))
    return lines


def settlement(merchant_id: str, amount: int) -> list[Line]:
    """Règlement T+1 : les fonds passent de « en attente » à « disponible »."""
    return [
        debit(AccountRef.merchant_pending(merchant_id), amount),
        credit(AccountRef.merchant_available(merchant_id), amount),
    ]


def refund(
    merchant_id: str, operator: Operator, *, from_pending: int, from_available: int
) -> list[Line]:
    """Remboursement : le marchand rend les fonds, l'opérateur les reverse au payeur.

    La commission n'est pas extournée (XG-10) : c'est le marchand qui la supporte.
    """
    lines: list[Line] = []
    if from_pending > 0:
        lines.append(debit(AccountRef.merchant_pending(merchant_id), from_pending))
    if from_available > 0:
        lines.append(debit(AccountRef.merchant_available(merchant_id), from_available))
    lines.append(credit(AccountRef.operator_clearing(operator), from_pending + from_available))
    return lines


def payout(merchant_id: str, amount: int) -> list[Line]:
    """Virement : le disponible du marchand lui est versé."""
    return [
        debit(AccountRef.merchant_available(merchant_id), amount),
        credit(AccountRef.merchant_payout(merchant_id), amount),
    ]
