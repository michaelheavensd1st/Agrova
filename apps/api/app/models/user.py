"""User model.

Provider-agnostic account. Verification is required for password login;
SSO logins added later will short-circuit that check.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.audit import AuditEvent
    from app.models.invitation import Invitation
    from app.models.membership import FarmMembership, OrganizationMembership
    from app.models.password_recovery import PasswordRecoveryToken
    from app.models.refresh_token import RefreshToken
    from app.models.role_assignment import RoleAssignment
    from app.models.verification import EmailVerificationToken


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    session_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    verification_tokens: Mapped[list[EmailVerificationToken]] = relationship(
        "EmailVerificationToken", back_populates="user", cascade="all, delete-orphan"
    )
    password_recovery_tokens: Mapped[list[PasswordRecoveryToken]] = relationship(
        "PasswordRecoveryToken", back_populates="user", cascade="all, delete-orphan"
    )
    role_assignments: Mapped[list[RoleAssignment]] = relationship(
        "RoleAssignment",
        back_populates="user",
        foreign_keys="RoleAssignment.user_id",
        cascade="all, delete-orphan",
    )
    organization_memberships: Mapped[list[OrganizationMembership]] = relationship(
        "OrganizationMembership",
        back_populates="user",
        foreign_keys="OrganizationMembership.user_id",
        cascade="all, delete-orphan",
    )
    farm_memberships: Mapped[list[FarmMembership]] = relationship(
        "FarmMembership",
        back_populates="user",
        foreign_keys="FarmMembership.user_id",
        cascade="all, delete-orphan",
    )
    sent_invitations: Mapped[list[Invitation]] = relationship(
        "Invitation",
        back_populates="invited_by",
        foreign_keys="Invitation.invited_by_id",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="actor",
        foreign_keys="AuditEvent.actor_id",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r}>"
