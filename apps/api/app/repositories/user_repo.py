"""User repository."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, user_id: uuid.UUID) -> User | None:
        """Lock one active-or-disabled, non-deleted user as a security root.

        Credential mutation services must acquire this lock before locking
        any dependent recovery-token or refresh-token row.
        """
        stmt = (
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email_for_update(self, email: str) -> User | None:
        stmt = (
            select(User)
            .where(User.email == email.strip().lower(), User.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def search_admin_directory(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        """Return a filtered page of non-deleted platform users.

        Count and page queries deliberately share one predicate list. Search
        input is LIKE-escaped so user-provided wildcard characters remain
        literal rather than broadening directory visibility.
        """
        predicates = [User.deleted_at.is_(None)]
        if search is not None:
            pattern = f"%{_escape_like(search)}%"
            predicates.append(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.full_name.ilike(pattern, escape="\\"),
                )
            )
        if is_active is not None:
            predicates.append(User.is_active.is_(is_active))
        if is_verified is not None:
            predicates.append(User.is_verified.is_(is_verified))

        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(User).where(*predicates)
                )
            ).scalar_one()
        )
        rows = list(
            (
                await self.session.execute(
                    select(User)
                    .where(*predicates)
                    .order_by(User.created_at.desc(), User.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def create(
        self,
        *,
        email: str,
        hashed_password: str | None,
        full_name: str | None = None,
    ) -> User:
        user = User(
            email=email.lower(),
            hashed_password=hashed_password,
            full_name=full_name,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def mark_verified(self, user: User) -> User:
        user.is_verified = True
        user.verified_at = datetime.now(UTC)
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def set_password_hash(self, user: User, hashed_password: str) -> User:
        user.hashed_password = hashed_password
        self.session.add(user)
        await self.session.flush()
        return user

    async def increment_session_version(self, user: User) -> int:
        """Invalidate every access token issued for the user's current generation.

        Credential-mutation services acquire the user row lock before calling
        this method, so concurrent security operations serialize on one
        authoritative, database-backed generation.
        """
        user.session_version += 1
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user.session_version

    async def set_active(self, user: User, *, is_active: bool) -> User:
        user.is_active = is_active
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def soft_delete(self, user: User) -> None:
        user.deleted_at = datetime.now(UTC)
        user.is_active = False
        self.session.add(user)
        await self.session.flush()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
