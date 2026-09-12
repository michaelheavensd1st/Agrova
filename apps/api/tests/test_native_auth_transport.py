"""Regression coverage for the explicit native bearer-token transport."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User

NATIVE_HEADERS = {"X-Agrovix-Auth-Transport": "bearer"}
PASSWORD = "NativeTransport!2026"


async def _create_verified_user(email: str) -> User:
    from app.db import session as _db

    async with _db.AsyncSessionLocal() as session:
        user = User(
            email=email,
            hashed_password=hash_password(PASSWORD),
            full_name="Native User",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        return user


@pytest.mark.asyncio
async def test_browser_login_keeps_tokens_out_of_json_and_sets_cookies(
    client: AsyncClient,
) -> None:
    email = f"browser-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)

    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 200, response.text
    assert "access_token" not in response.json()
    assert "refresh_token" not in response.json()
    settings = get_settings()
    assert response.cookies.get(settings.cookie_access_name)
    assert response.cookies.get(settings.cookie_refresh_name)


@pytest.mark.asyncio
async def test_native_login_returns_token_pair_without_auth_cookies(client: AsyncClient) -> None:
    email = f"native-login-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=NATIVE_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"
    settings = get_settings()
    assert response.cookies.get(settings.cookie_access_name) is None
    assert response.cookies.get(settings.cookie_refresh_name) is None


@pytest.mark.asyncio
async def test_native_refresh_rotates_body_token_and_returns_new_pair(client: AsyncClient) -> None:
    email = f"native-refresh-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=NATIVE_HEADERS,
    )
    original = login.json()

    refreshed = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
        headers=NATIVE_HEADERS,
    )

    assert refreshed.status_code == 200, refreshed.text
    replacement = refreshed.json()
    assert replacement["access_token"]
    assert replacement["refresh_token"] != original["refresh_token"]
    settings = get_settings()
    assert refreshed.cookies.get(settings.cookie_access_name) is None
    assert refreshed.cookies.get(settings.cookie_refresh_name) is None
    replay = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
        headers=NATIVE_HEADERS,
    )
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_browser_refresh_rotates_cookies_without_exposing_tokens(client: AsyncClient) -> None:
    email = f"browser-refresh-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    settings = get_settings()
    original_refresh = login.cookies.get(settings.cookie_refresh_name)
    assert original_refresh

    refreshed = await client.post("/api/v1/auth/refresh", json={})

    assert refreshed.status_code == 200, refreshed.text
    assert "access_token" not in refreshed.json()
    assert "refresh_token" not in refreshed.json()
    assert refreshed.cookies.get(settings.cookie_access_name)
    replacement_refresh = refreshed.cookies.get(settings.cookie_refresh_name)
    assert replacement_refresh
    assert replacement_refresh != original_refresh


@pytest.mark.asyncio
async def test_native_refresh_never_falls_back_to_browser_cookie(client: AsyncClient) -> None:
    email = f"native-cookie-reject-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    settings = get_settings()
    original_refresh = login.cookies.get(settings.cookie_refresh_name)
    assert original_refresh

    rejected = await client.post("/api/v1/auth/refresh", json={}, headers=NATIVE_HEADERS)

    assert rejected.status_code == 401
    assert "access_token" not in rejected.json()
    assert "refresh_token" not in rejected.json()
    # The rejected bearer request must not consume the browser cookie.
    accepted = await client.post("/api/v1/auth/refresh", json={})
    assert accepted.status_code == 200, accepted.text
    assert accepted.cookies.get(settings.cookie_refresh_name) != original_refresh


@pytest.mark.asyncio
async def test_native_logout_revokes_body_refresh_without_cookies(client: AsyncClient) -> None:
    email = f"native-logout-{uuid4().hex[:8]}@agrovix.dev"
    await _create_verified_user(email)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=NATIVE_HEADERS,
    )
    refresh_token = login.json()["refresh_token"]
    client.cookies.clear()

    logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

    assert logout.status_code == 200, logout.text
    rejected = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
        headers=NATIVE_HEADERS,
    )
    assert rejected.status_code == 401
