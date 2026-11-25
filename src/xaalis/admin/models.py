"""Comptes administrateurs de la plateforme."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("id LIKE 'adm\\_%'", name="id_prefix"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # format PHC Argon2id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
