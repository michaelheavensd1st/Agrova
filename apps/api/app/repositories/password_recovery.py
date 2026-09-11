"""Persistence and ordered row-locking for password recovery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.password_recovery import PasswordRecoveryToken


@dataclass(frozen=True)
class PasswordRecoveryTokenIdentity:
    id: uuid.UUID
    user_id: uuid.UUID


class PasswordRecoveryTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve_identity_by_hash(
        self, token_hash: str
    ) -> PasswordRecoveryTokenIdentity | None:
        """Probe immutable identity without taking a row lock.

        Callers use the returned user id to acquire the user lock first.
        They must then call :meth:`get_by_id_for_update` and revalidate the
        hash while both the user and token locks are held.
        """
        stmt = select(PasswordRecoveryToken.id, PasswordRecoveryToken.user_id).where(
            PasswordRecoveryToken.token_hash == token_hash
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return PasswordRecoveryTokenIdentity(id=row.id, user_id=row.user_id)

    async def list_outstanding_for_user_for_update(
        self, user_id: uuid.UUID
    ) -> list[PasswordRecoveryToken]:
        stmt = (
            select(PasswordRecoveryToken)
            .where(
                PasswordRecoveryToken.user_id == user_id,
                PasswordRecoveryToken.consumed_at.is_(None),
                PasswordRecoveryToken.invalidated_at.is_(None),
            )
            .order_by(PasswordRecoveryToken.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id_for_update(
        self, *, token_id: uuid.UUID, user_id: uuid.UUID
    ) -> PasswordRecoveryToken | None:
        stmt = (
            select(PasswordRecoveryToken)
            .where(
                PasswordRecoveryToken.id == token_id,
                PasswordRecoveryToken.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> PasswordRecoveryToken:
        row = PasswordRecoveryToken(
            user_id=user_id,
            token_hash=token_hash,
            created_at=created_at,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def invalidate_rows(
        self, rows: list[PasswordRecoveryToken], *, invalidated_at: datetime
    ) -> None:
        for row in rows:
            row.invalidated_at = invalidated_at
            self.session.add(row)
        await self.session.flush()

    async def mark_consumed(
        self, row: PasswordRecoveryToken, *, consumed_at: datetime
    ) -> PasswordRecoveryToken:
        row.consumed_at = consumed_at
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = ["PasswordRecoveryTokenIdentity", "PasswordRecoveryTokenRepository"]
