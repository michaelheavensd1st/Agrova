"""Persistent, single-use password-recovery credentials.

Only a SHA-256 digest is stored. Raw recovery tokens are transient
application values and never cross this persistence boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User

_SHA256_CHECK = (
    "length(token_hash) = 64 AND token_hash = lower(token_hash) AND "
    "length(replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "token_hash,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),"
    "'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')) = 0"
)


class PasswordRecoveryToken(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "password_recovery_tokens"
    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at",
            name="ck_password_recovery_expires_after_created",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR invalidated_at IS NULL",
            name="ck_password_recovery_terminal_state_exclusive",
        ),
        CheckConstraint(
            _SHA256_CHECK,
            name="ck_password_recovery_token_hash_sha256",
        ),
        Index("uq_password_recovery_token_hash", "token_hash", unique=True),
        Index("ix_password_recovery_tokens_expires_at", "expires_at"),
        Index(
            "uq_password_recovery_outstanding_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("consumed_at IS NULL AND invalidated_at IS NULL"),
            sqlite_where=text("consumed_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="password_recovery_tokens")


__all__ = ["PasswordRecoveryToken"]
