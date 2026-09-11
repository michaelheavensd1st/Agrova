"""Refresh-token repository."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


@dataclass(frozen=True)
class RefreshTokenIdentity:
    id: uuid.UUID
    user_id: uuid.UUID


class RefreshTokenRepository:
    """Persistence for :class:`RefreshToken` aggregates."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        record = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def resolve_identity_by_hash(self, token_hash: str) -> RefreshTokenIdentity | None:
        stmt = select(RefreshToken.id, RefreshToken.user_id).where(
            RefreshToken.token_hash == token_hash
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return RefreshTokenIdentity(id=row.id, user_id=row.user_id)

    async def get_by_id_for_update(
        self, *, token_id: uuid.UUID, user_id: uuid.UUID
    ) -> RefreshToken | None:
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active_for_user_for_update(self, user_id: uuid.UUID) -> list[RefreshToken]:
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
            .order_by(RefreshToken.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def revoke_rows(self, rows: list[RefreshToken]) -> int:
        now = datetime.now(UTC)
        for row in rows:
            row.is_revoked = True
            row.revoked_at = now
        await self.session.flush()
        return len(rows)

    async def revoke_by_hash(self, token_hash: str) -> None:
        now = datetime.now(UTC)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .values(is_revoked=True, revoked_at=now)
        )
        await self.session.execute(stmt)

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        now = datetime.now(UTC)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now)
        )
        await self.session.execute(stmt)


__all__ = ["RefreshTokenIdentity", "RefreshTokenRepository"]
