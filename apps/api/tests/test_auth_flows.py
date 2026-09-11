"""Sprint 1 — required auth negative + rotation tests."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.security import create_token, decode_token, hash_password
from app.models.user import User


async def _create_verified_user(email: str, password: str = "Sprint0ne!2026") -> User:
    from app.db import session as _db

    async with _db.AsyncSessionLocal() as session:
        user = User(
            email=email.lower(),
            hashed_password=hash_password(password),
            full_name="QA User",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        return user


@pytest.mark.asyncio
async def test_duplicate_email_registration(client: AsyncClient) -> None:
    email = f"dup-{uuid4().hex[:8]}@agrovix.dev"
    payload = {"email": email, "password": "Sprint0ne!2026", "full_name": "Dup"}
    r1 = await client.post("/api/v1/auth/register", json=payload)
    assert r1.status_code == 201, r1.text
    r2 = await client.post("/api/v1/auth/register", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_invalid_credentials(client: AsyncClient) -> None:
    email = f"bad-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert r.status_code == 401
    assert "invalid email or password" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_expired_access_token_rejected(client: AsyncClient) -> None:
    email = f"exp-{uuid4().hex[:8]}@agrovix.dev"
    user = await _create_verified_user(email)
    # Manually forge an expired access token — the codepath in decode_token
    # returns TokenExpiredError, which /auth/me maps to 401.
    expired, _ = create_token(
        subject=user.id, token_type="access", expires_delta=timedelta(seconds=-1)
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_token_rotation_and_revocation(client: AsyncClient) -> None:
    email = f"rot-{uuid4().hex[:8]}@agrovix.dev"
    password = "Sprint0ne!2026"
    await _create_verified_user(email, password=password)

    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    settings = get_settings()
    refresh_cookie = r.cookies.get(settings.cookie_refresh_name)
    assert refresh_cookie is not None, "login must set the refresh cookie"

    # First refresh — succeeds, rotates.
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_cookie})
    assert r1.status_code == 200, r1.text
    replacement_access = r1.cookies.get(settings.cookie_access_name)
    assert replacement_access is not None
    assert decode_token(replacement_access, expected_type="access")["sv"] == 0

    # Reuse of the ORIGINAL refresh — must now be revoked.
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_cookie})
    assert r2.status_code == 401
    assert (
        "expired refresh" in r2.json()["detail"].lower() or "invalid" in r2.json()["detail"].lower()
    )


@pytest.mark.asyncio
async def test_revoked_refresh_token_via_logout(client: AsyncClient) -> None:
    email = f"rev-{uuid4().hex[:8]}@agrovix.dev"
    password = "Sprint0ne!2026"
    await _create_verified_user(email, password=password)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    refresh = login.cookies.get(get_settings().cookie_refresh_name)

    lo = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert lo.status_code == 200

    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unauthorized_protected_route(client: AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
