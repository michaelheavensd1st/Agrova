"""Platform user-administration orchestration and locking."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status

from app.models.user import User
from app.repositories.audit_repo import AuditRepository
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository


class AdminUserService:
    """Read the platform directory and mutate security state when requested."""

    def __init__(
        self,
        *,
        user_repo: UserRepository,
        recovery_repo: PasswordRecoveryTokenRepository,
        refresh_repo: RefreshTokenRepository,
        audit_repo: AuditRepository,
    ) -> None:
        sessions = {
            id(user_repo.session),
            id(recovery_repo.session),
            id(refresh_repo.session),
            id(audit_repo.session),
        }
        if len(sessions) != 1:
            raise ValueError("Administration repositories must share one transaction.")
        self.user_repo = user_repo
        self.recovery_repo = recovery_repo
        self.refresh_repo = refresh_repo
        self.audit_repo = audit_repo

    async def list_directory(
        self,
        *,
        search: str | None,
        is_active: bool | None,
        is_verified: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        normalized_search = search.strip() if search is not None else None
        if not normalized_search:
            normalized_search = None
        return await self.user_repo.search_admin_directory(
            search=normalized_search,
            is_active=is_active,
            is_verified=is_verified,
            limit=limit,
            offset=offset,
        )

    async def inspect(self, *, target_id: uuid.UUID) -> User:
        target = await self.user_repo.get_by_id(target_id)
        if target is None:
            raise self._not_found()
        return target

    async def disable(
        self,
        *,
        actor: User,
        target_id: uuid.UUID,
        reason: str,
        request_ctx: dict,
    ) -> User:
        target = await self._lock_target(target_id)
        if target.id == actor.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Platform administrators cannot disable their own account.",
            )

        previous_is_active = target.is_active
        await self.user_repo.set_active(target, is_active=False)
        if previous_is_active:
            await self.user_repo.increment_session_version(target)

        invalidated_at = datetime.now(UTC)
        recovery_tokens = await self.recovery_repo.list_outstanding_for_user_for_update(target.id)
        await self.recovery_repo.invalidate_rows(
            recovery_tokens,
            invalidated_at=invalidated_at,
        )
        refresh_tokens = await self.refresh_repo.list_active_for_user_for_update(target.id)
        revoked_sessions = await self.refresh_repo.revoke_rows(refresh_tokens)

        await self._audit(
            actor=actor,
            target=target,
            action="admin.user.disable",
            reason=reason,
            request_ctx=request_ctx,
            metadata={
                "previous_is_active": previous_is_active,
                "resulting_is_active": False,
                "invalidated_recovery_tokens": len(recovery_tokens),
                "revoked_sessions": revoked_sessions,
                "idempotent": not previous_is_active,
            },
        )
        return target

    async def enable(
        self,
        *,
        actor: User,
        target_id: uuid.UUID,
        reason: str,
        request_ctx: dict,
    ) -> User:
        target = await self._lock_target(target_id)
        previous_is_active = target.is_active
        await self.user_repo.set_active(target, is_active=True)
        await self._audit(
            actor=actor,
            target=target,
            action="admin.user.enable",
            reason=reason,
            request_ctx=request_ctx,
            metadata={
                "previous_is_active": previous_is_active,
                "resulting_is_active": True,
                "idempotent": previous_is_active,
            },
        )
        return target

    async def revoke_sessions(
        self,
        *,
        actor: User,
        target_id: uuid.UUID,
        reason: str,
        request_ctx: dict,
    ) -> tuple[User, int]:
        target = await self._lock_target(target_id)
        if target.id == actor.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Platform administrators cannot revoke all of their own sessions.",
            )

        await self.user_repo.increment_session_version(target)
        refresh_tokens = await self.refresh_repo.list_active_for_user_for_update(target.id)
        revoked_sessions = await self.refresh_repo.revoke_rows(refresh_tokens)
        await self._audit(
            actor=actor,
            target=target,
            action="admin.user.sessions.revoke",
            reason=reason,
            request_ctx=request_ctx,
            metadata={"revoked_sessions": revoked_sessions},
        )
        return target, revoked_sessions

    async def _lock_target(self, target_id: uuid.UUID) -> User:
        target = await self.user_repo.get_by_id_for_update(target_id)
        if target is None:
            raise self._not_found()
        return target

    async def _audit(
        self,
        *,
        actor: User,
        target: User,
        action: str,
        reason: str,
        request_ctx: dict,
        metadata: dict,
    ) -> None:
        await self.audit_repo.record(
            actor_id=actor.id,
            action=action,
            entity_type="user",
            entity_id=str(target.id),
            organization_id=None,
            farm_id=None,
            ip_address=request_ctx.get("ip_address"),
            user_agent=(request_ctx.get("user_agent") or "")[:512] or None,
            request_id=request_ctx.get("request_id"),
            metadata={"reason": reason, **metadata},
        )

    @staticmethod
    def _not_found() -> HTTPException:
        return HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")


__all__ = ["AdminUserService"]
