"""Release 6.0.5 password-recovery persistence/security kernel.

This module intentionally has no HTTP, email, audit, password-mutation,
cookie, or session-revocation behavior. It owns opaque-token creation and
the frozen user-first persistence locking protocol only.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status

from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimiter
from app.core.security import hash_password, verify_password
from app.email.base import EmailMessage, EmailSender
from app.models.password_recovery import PasswordRecoveryToken
from app.models.user import User
from app.repositories.audit_repo import AuditRepository
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository

logger = logging.getLogger("app.password_recovery")


def generate_recovery_token() -> str:
    """Return a URL-safe token backed by 32 CSPRNG bytes (256 bits)."""
    return secrets.token_urlsafe(32)


def hash_recovery_token(raw_token: str) -> str:
    """Return the canonical lowercase SHA-256 hexadecimal digest."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class PasswordRecoveryKernel:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        token_repo: PasswordRecoveryTokenRepository,
    ) -> None:
        if user_repo.session is not token_repo.session:
            raise ValueError("Password-recovery repositories must share one transaction.")
        self.user_repo = user_repo
        self.token_repo = token_repo
        self.settings = get_settings()

    async def issue(
        self, *, user_id: uuid.UUID, now: datetime | None = None
    ) -> tuple[str, PasswordRecoveryToken] | None:
        """Issue the newest outstanding token under the user-first lock."""
        user = await self.user_repo.get_by_id_for_update(user_id)
        if user is None:
            return None
        issued_at = _utc(now)

        outstanding = await self.token_repo.list_outstanding_for_user_for_update(user.id)
        await self.token_repo.invalidate_rows(outstanding, invalidated_at=issued_at)

        raw_token = generate_recovery_token()
        row = await self.token_repo.create(
            user_id=user.id,
            token_hash=hash_recovery_token(raw_token),
            created_at=issued_at,
            expires_at=issued_at
            + timedelta(minutes=self.settings.password_recovery_token_expire_minutes),
        )
        return raw_token, row

    async def consume(
        self, *, raw_token: str, now: datetime | None = None
    ) -> PasswordRecoveryToken | None:
        """Consume one valid token, locking its user before its row.

        Invalid, expired, invalidated, or previously consumed credentials
        return ``None`` and leave persistence unchanged.
        """
        locked = await self.lock_valid(raw_token=raw_token, now=now)
        if locked is None:
            return None
        _, row = locked
        return await self.token_repo.mark_consumed(row, consumed_at=_utc(now))

    async def lock_valid(
        self, *, raw_token: str, now: datetime | None = None
    ) -> tuple[User, PasswordRecoveryToken] | None:
        """Resolve and lock a usable token without consuming it."""
        token_hash = hash_recovery_token(raw_token)
        identity = await self.token_repo.resolve_identity_by_hash(token_hash)
        if identity is None:
            return None

        user = await self.user_repo.get_by_id_for_update(identity.user_id)
        if user is None:
            return None
        row = await self.token_repo.get_by_id_for_update(
            token_id=identity.id,
            user_id=user.id,
        )
        if row is None or row.token_hash != token_hash:
            return None
        if row.consumed_at is not None or row.invalidated_at is not None:
            return None
        consumed_at = _utc(now)
        if _utc(row.expires_at) <= consumed_at:
            return None
        return user, row


@dataclass(frozen=True)
class RecoveryDelivery:
    to: str
    raw_token: str = field(repr=False)


async def deliver_recovery_email(
    *, email_sender: EmailSender, settings: Settings, delivery: RecoveryDelivery
) -> None:
    """Deliver recovery mail without retaining request-scoped persistence objects."""
    reset_url = f"{settings.web_app_url.rstrip('/')}/reset-password" f"?token={delivery.raw_token}"
    await email_sender.send(
        EmailMessage(
            to=delivery.to,
            subject="Reset your Agrovix AgOS password",
            text_body=(
                "A password reset was requested for your Agrovix AgOS account.\n\n"
                f"Open this link to reset your password:\n{reset_url}\n\n"
                "If you did not request this, you can ignore this email.\n"
                "This link expires in "
                f"{settings.password_recovery_token_expire_minutes} minutes.\n"
            ),
            template="auth.password_recovery",
            context={"reset_url": reset_url},
        )
    )


class PasswordRecoveryService:
    """Recovery API orchestration with caller-owned transaction boundaries."""

    def __init__(
        self,
        *,
        kernel: PasswordRecoveryKernel,
        user_repo: UserRepository,
        token_repo: PasswordRecoveryTokenRepository,
        refresh_repo: RefreshTokenRepository,
        audit_repo: AuditRepository,
        rate_limiter: RateLimiter,
        email_sender: EmailSender,
    ) -> None:
        sessions = {
            id(user_repo.session),
            id(token_repo.session),
            id(refresh_repo.session),
            id(audit_repo.session),
        }
        if len(sessions) != 1:
            raise ValueError("Password-recovery repositories must share one transaction.")
        self.kernel = kernel
        self.user_repo = user_repo
        self.token_repo = token_repo
        self.refresh_repo = refresh_repo
        self.audit_repo = audit_repo
        self.rate_limiter = rate_limiter
        self.email_sender = email_sender
        self.settings = get_settings()

    async def prepare_request(
        self, *, email: str, ip_address: str | None, request_ctx: dict
    ) -> RecoveryDelivery | None:
        normalized = email.strip().lower()
        await self._enforce_request_rate_limit(email=normalized, ip_address=ip_address)
        user = await self.user_repo.get_by_email(normalized)
        if user is None or not user.is_active or user.hashed_password is None:
            logger.info("auth.recovery.request.suppressed", extra={"outcome": "ineligible"})
            return None

        # Eligibility is authorization-sensitive state. Re-read it after
        # acquiring the same user security-root lock used by issuance,
        # login, refresh, reset, and (later) account administration.
        user = await self.user_repo.get_by_id_for_update(user.id)
        if user is None or not user.is_active or user.hashed_password is None:
            logger.info("auth.recovery.request.suppressed", extra={"outcome": "ineligible"})
            return None

        issued = await self.kernel.issue(user_id=user.id)
        if issued is None:
            logger.info("auth.recovery.request.suppressed", extra={"outcome": "ineligible"})
            return None
        raw_token, _ = issued
        await self.audit_repo.record(
            actor_id=None,
            action="auth.recovery.request",
            entity_type="user",
            entity_id=str(user.id),
            ip_address=request_ctx.get("ip_address"),
            user_agent=request_ctx.get("user_agent"),
            request_id=request_ctx.get("request_id"),
            metadata={"channel": "email"},
        )
        return RecoveryDelivery(to=user.email, raw_token=raw_token)

    async def reset_password(self, *, raw_token: str, new_password: str, request_ctx: dict) -> User:
        locked = await self.kernel.lock_valid(raw_token=raw_token)
        if locked is None:
            self._log_rejected("invalid_token")
            raise self._invalid_token()
        user, token = locked
        if not user.is_active or user.hashed_password is None:
            self._log_rejected("ineligible")
            raise self._invalid_token()
        if verify_password(new_password, user.hashed_password):
            self._log_rejected("password_reuse")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="New password must differ from the current password.",
            )

        await self.user_repo.set_password_hash(user, hash_password(new_password))
        await self.user_repo.increment_session_version(user)
        now = datetime.now(UTC)
        await self.token_repo.mark_consumed(token, consumed_at=now)
        outstanding = await self.token_repo.list_outstanding_for_user_for_update(user.id)
        await self.token_repo.invalidate_rows(outstanding, invalidated_at=now)
        refresh_tokens = await self.refresh_repo.list_active_for_user_for_update(user.id)
        revoked_count = await self.refresh_repo.revoke_rows(refresh_tokens)

        audit_kwargs = {
            "actor_id": user.id,
            "entity_type": "user",
            "entity_id": str(user.id),
            "ip_address": request_ctx.get("ip_address"),
            "user_agent": request_ctx.get("user_agent"),
            "request_id": request_ctx.get("request_id"),
        }
        await self.audit_repo.record(
            action="auth.recovery.complete",
            metadata={"recovery_token_id": str(token.id)},
            **audit_kwargs,
        )
        await self.audit_repo.record(action="auth.password.change", **audit_kwargs)
        await self.audit_repo.record(
            action="auth.sessions.revoke",
            metadata={"revoked_count": revoked_count},
            **audit_kwargs,
        )
        return user

    async def _enforce_request_rate_limit(self, *, email: str, ip_address: str | None) -> None:
        window = self.settings.password_recovery_request_window_seconds
        # Reject an already-blocked network source before touching a caller-
        # supplied email key. Accepted requests still consume both dimensions.
        if ip_address:
            allowed, retry = await self.rate_limiter.hit(
                key=f"password-recovery:ip:{ip_address}",
                limit=self.settings.password_recovery_request_max_per_ip_hour,
                window_seconds=window,
            )
            if not allowed:
                raise self._too_many_requests(retry, window)
        allowed, retry = await self.rate_limiter.hit(
            key=f"password-recovery:email:{email}",
            limit=self.settings.password_recovery_request_max_per_email_hour,
            window_seconds=window,
        )
        if not allowed:
            raise self._too_many_requests(retry, window)

    @staticmethod
    def _invalid_token() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired recovery token.",
        )

    @staticmethod
    def _too_many_requests(retry_after: int, window_seconds: int) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many recovery requests. Please try again later.",
            headers={"Retry-After": str(min(max(retry_after, 1), window_seconds))},
        )

    @staticmethod
    def _log_rejected(reason: str) -> None:
        logger.info("auth.recovery.reset.rejected", extra={"reason": reason})


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "PasswordRecoveryKernel",
    "PasswordRecoveryService",
    "RecoveryDelivery",
    "deliver_recovery_email",
    "generate_recovery_token",
    "hash_recovery_token",
]
