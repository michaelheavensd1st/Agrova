"""Security contract for the create_admin CLI transaction."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cli import _create_admin
from app.core.security import create_token, hash_password, verify_password
from app.db import session as db
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.user import User
from app.repositories.role_repo import RoleAssignmentRepository
from app.repositories.user_repo import UserRepository

pytestmark = pytest.mark.asyncio

OLD_PASSWORD = "Create-Admin-Old!2026"
NEW_PASSWORD = "Create-Admin-New!2026"


async def _existing_user(*, is_active: bool = True, is_superuser: bool = False) -> User:
    async with db.AsyncSessionLocal() as session:
        user = User(
            email=f"create-admin-{uuid4().hex}@agrovix.dev",
            hashed_password=hash_password(OLD_PASSWORD),
            is_active=is_active,
            is_verified=True,
            is_superuser=is_superuser,
        )
        session.add(user)
        await session.commit()
        return user


def _access(user: User) -> str:
    token, _ = create_token(
        subject=user.id,
        token_type="access",
        extra_claims={"email": user.email, "sv": user.session_version},
    )
    return token


async def _stored(user_id) -> User:
    async with db.AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


async def test_existing_user_promotion_invalidates_old_access_token(client: AsyncClient) -> None:
    user = await _existing_user()
    old_access = _access(user)
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 200

    await _create_admin(user.email, NEW_PASSWORD)

    stored = await _stored(user.id)
    assert stored.is_superuser
    assert stored.session_version == 1
    assert verify_password(NEW_PASSWORD, stored.hashed_password)
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401


async def test_existing_inactive_user_stays_revoked_after_reactivation(
    client: AsyncClient,
) -> None:
    user = await _existing_user(is_active=False)
    old_access = _access(user)
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401

    await _create_admin(user.email, NEW_PASSWORD)

    stored = await _stored(user.id)
    assert stored.is_active and stored.is_verified and stored.is_superuser
    assert stored.session_version == 1
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401


async def test_already_correct_admin_rerun_is_security_noop() -> None:
    user = await _existing_user(is_superuser=True)
    async with db.AsyncSessionLocal() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "platform_admin"))
        ).scalar_one()
        session.add(RoleAssignment(user_id=user.id, role_id=role.id, granted_by_id=user.id))
        await session.commit()

    await _create_admin(user.email, OLD_PASSWORD)
    first = await _stored(user.id)
    await _create_admin(user.email, OLD_PASSWORD)
    second = await _stored(user.id)

    assert first.session_version == second.session_version == 0
    assert first.hashed_password == second.hashed_password


async def test_failure_rolls_back_existing_user_security_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _existing_user(is_active=False)
    original_increment = UserRepository.increment_session_version

    async def fail_after_increment(repo: UserRepository, target: User):
        await original_increment(repo, target)
        raise RuntimeError("forced failure after generation increment")

    monkeypatch.setattr(UserRepository, "increment_session_version", fail_after_increment)
    with pytest.raises(RuntimeError, match="forced failure after generation increment"):
        await _create_admin(user.email, NEW_PASSWORD)

    stored = await _stored(user.id)
    assert not stored.is_active
    assert not stored.is_superuser
    assert stored.session_version == 0
    assert verify_password(OLD_PASSWORD, stored.hashed_password)
    async with db.AsyncSessionLocal() as session:
        assignments = await RoleAssignmentRepository(session).list_for_user(user.id)
        assert not assignments


async def test_new_admin_starts_at_generation_zero() -> None:
    email = f"new-admin-{uuid4().hex}@agrovix.dev"
    await _create_admin(email, NEW_PASSWORD)

    async with db.AsyncSessionLocal() as session:
        stored = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert stored.is_active and stored.is_verified and stored.is_superuser
        assert stored.session_version == 0
        assert verify_password(NEW_PASSWORD, stored.hashed_password)


async def test_existing_user_uses_security_root_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _existing_user()
    original = UserRepository.get_by_email_for_update
    locked_emails: list[str] = []

    async def observe_lock(repo: UserRepository, email: str):
        locked_emails.append(email)
        return await original(repo, email)

    monkeypatch.setattr(UserRepository, "get_by_email_for_update", observe_lock)
    await _create_admin(user.email, NEW_PASSWORD)

    assert locked_emails == [user.email]
