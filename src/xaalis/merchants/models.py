"""Tables des marchands.

Un marchand possède exactement deux environnements (``livemode`` faux puis vrai).
La ligne ``merchant_environments`` sert aussi de **verrou** pour les opérations qui
dépendent du solde (remboursement, virement) : voir ADR 0002.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class Merchant(Base):
    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint("id LIKE 'm\\_%'", name="id_prefix"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MerchantEnvironment(Base):
    __tablename__ = "merchant_environments"

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), primary_key=True
    )
    livemode: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """Clé d'API d'un environnement. Seule l'empreinte est stockée (ADR 0004).

    Une clé est valide tant que ``revoked_at`` est vide et que ``expires_at`` est vide ou
    dans le futur. La rotation fixe ``expires_at`` de l'ancienne clé à +24 h.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("id LIKE 'key\\_%'", name="id_prefix"),
        CheckConstraint("kind IN ('publishable', 'secret')", name="kind"),
        CheckConstraint("octet_length(fingerprint) = 32", name="fingerprint_length"),
        ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
        Index("ix_api_keys_merchant_id_livemode", "merchant_id", "livemode"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(40))
    livemode: Mapped[bool] = mapped_column(Boolean)
    kind: Mapped[str] = mapped_column(String(16))
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    redacted: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
