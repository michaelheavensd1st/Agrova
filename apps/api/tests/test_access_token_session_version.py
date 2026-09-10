"""Access-token generation validation and backward-compatibility contract."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.security import create_token, hash_password
from app.db import session as db
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _user(*, session_version: int = 0, is_superuser: bool = False) -> User:
    async with db.AsyncSessionLocal() as session:
        user = User(
            email=f"session-version-{uuid4().hex}@agrovix.dev",
            hashed_password=hash_password("Session-Version!2026"),
            is_active=True,
            is_verified=True,
            is_superuser=is_superuser,
            session_version=session_version,
        )
        session.add(user)
        await session.commit()
        return user


def _access(user: User, session_version: object = 0) -> str:
    token, _ = create_token(
        subject=user.id,
        token_type="access",
        extra_claims={"email": user.email, "sv": session_version},
    )
    return token


async def _me(client: AsyncClient, token: str):
    return await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})


async def test_matching_session_version_succeeds(client: AsyncClient) -> None:
    user = await _user(session_version=4)
    response = await _me(client, _access(user, 4))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("claim", [3, -1, "4", 4.0, True, None, {"generation": 4}])
async def test_invalid_session_version_is_generic_unauthorized(
    client: AsyncClient, claim: object
) -> None:
    user = await _user(session_version=4)
    response = await _me(client, _access(user, claim))
    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."


async def test_legacy_token_only_works_for_generation_zero(client: AsyncClient) -> None:
    user = await _user(session_version=0)
    legacy, _ = create_token(subject=user.id, token_type="access")
    assert (await _me(client, legacy)).status_code == 200

    async with db.AsyncSessionLocal() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.session_version += 1
        await session.commit()

    response = await _me(client, legacy)
    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."


async def test_increment_is_isolated_to_target_user_and_preserves_superuser(
    client: AsyncClient,
) -> None:
    target = await _user()
    unrelated = await _user(is_superuser=True)
    target_token = _access(target)
    unrelated_token = _access(unrelated)

    async with db.AsyncSessionLocal() as session:
        stored = await session.get(User, target.id)
        assert stored is not None
        stored.session_version += 1
        await session.commit()

    assert (await _me(client, target_token)).status_code == 401
    unrelated_response = await _me(client, unrelated_token)
    assert unrelated_response.status_code == 200, unrelated_response.text
    assert set(unrelated_response.json()["permissions"]) == {"*", "platform.admin"}
