"""Sprint 5.3 platform user-administration API contract."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.session import get_db_session
from app.deps import get_admin_user_service
from app.main import app
from app.models.audit import AuditEvent
from app.models.password_recovery import PasswordRecoveryToken
from app.models.refresh_token import RefreshToken
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.user import User
from app.repositories.audit_repo import AuditRepository
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository
from app.services.admin_user_service import AdminUserService
from app.services.password_recovery import PasswordRecoveryKernel
from tests._helpers import create_org, create_verified_user, login, switch_user

pytestmark = pytest.mark.asyncio


async def _make_admin(client: AsyncClient, *, superuser: bool = False) -> User:
    email = f"admin-{uuid4().hex}@agrovix.dev"
    admin = await create_verified_user(email)
    async with _session() as session:
        stored = await session.get(User, admin.id)
        assert stored is not None
        if superuser:
            stored.is_superuser = True
        else:
            role = (
                await session.execute(select(Role).where(Role.name == "platform_admin"))
            ).scalar_one()
            session.add(RoleAssignment(user_id=stored.id, role_id=role.id))
        await session.commit()
    await switch_user(client, email)
    return admin


def _session():
    from app.db import session as db

    return db.AsyncSessionLocal()


async def _audit(action: str, target_id: UUID) -> AuditEvent:
    async with _session() as session:
        return (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action == action, AuditEvent.entity_id == str(target_id))
                    .order_by(AuditEvent.created_at.desc())
                )
            )
            .scalars()
            .first()
        )


async def _seed_credentials(target: User, client: AsyncClient) -> tuple[UUID, UUID, str, str]:
    credentials = await login(client, target.email)
    async with _session() as session:
        refresh = (
            (
                await session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == target.id, RefreshToken.is_revoked.is_(False))
                    .order_by(RefreshToken.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert refresh is not None
        users = UserRepository(session)
        recovery = PasswordRecoveryTokenRepository(session)
        issued = await PasswordRecoveryKernel(user_repo=users, token_repo=recovery).issue(
            user_id=target.id
        )
        assert issued is not None
        await session.commit()
        return (
            refresh.id,
            issued[1].id,
            credentials[get_settings().cookie_access_name],
            credentials[get_settings().cookie_refresh_name],
        )


async def test_authorization_precedes_target_lookup_and_owner_is_not_admin(
    client: AsyncClient,
) -> None:
    target_id = uuid4()
    assert (await client.get(f"/api/v1/admin/users/{target_id}")).status_code == 401
    assert (
        await client.post(
            f"/api/v1/admin/users/{target_id}/disable", json={"reason": "unauthorized"}
        )
    ).status_code == 401

    ordinary = await create_verified_user(f"ordinary-{uuid4().hex}@agrovix.dev")
    await switch_user(client, ordinary.email)
    assert (await client.get(f"/api/v1/admin/users/{target_id}")).status_code == 403
    assert (
        await client.post(f"/api/v1/admin/users/{target_id}/disable", json={"reason": "forbidden"})
    ).status_code == 403

    owner = await create_verified_user(f"owner-{uuid4().hex}@agrovix.dev")
    await switch_user(client, owner.email)
    await create_org(client)
    assert (await client.get(f"/api/v1/admin/users/{target_id}")).status_code == 403


async def test_platform_permission_and_superuser_compatibility_allow_inspection(
    client: AsyncClient,
) -> None:
    target = await create_verified_user(f"target-{uuid4().hex}@agrovix.dev")
    admin = await _make_admin(client)
    response = await client.get(f"/api/v1/admin/users/{target.id}")
    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "email",
        "full_name",
        "is_active",
        "is_verified",
        "is_superuser",
        "created_at",
        "updated_at",
    }

    self_response = await client.get(f"/api/v1/admin/users/{admin.id}")
    assert self_response.status_code == 200
    async with _session() as session:
        assert (
            not (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "admin.user.view")
                )
            )
            .scalars()
            .all()
        )

    await _make_admin(client, superuser=True)
    assert (await client.get(f"/api/v1/admin/users/{target.id}")).status_code == 200


async def test_disabled_visible_but_deleted_and_missing_are_not(client: AsyncClient) -> None:
    disabled = await create_verified_user(f"disabled-{uuid4().hex}@agrovix.dev")
    deleted = await create_verified_user(f"deleted-{uuid4().hex}@agrovix.dev")
    async with _session() as session:
        disabled_row = await session.get(User, disabled.id)
        deleted_row = await session.get(User, deleted.id)
        assert disabled_row is not None and deleted_row is not None
        disabled_row.is_active = False
        deleted_row.is_active = False
        deleted_row.deleted_at = datetime.now(UTC)
        await session.commit()

    await _make_admin(client)
    response = await client.get(f"/api/v1/admin/users/{disabled.id}")
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert (await client.get(f"/api/v1/admin/users/{deleted.id}")).status_code == 404
    assert (await client.get(f"/api/v1/admin/users/{uuid4()}")).status_code == 404
    assert (
        await client.post(
            f"/api/v1/admin/users/{deleted.id}/enable", json={"reason": "hidden target"}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/admin/users/{uuid4()}/enable", json={"reason": "missing target"}
        )
    ).status_code == 404


async def test_self_target_contract(client: AsyncClient) -> None:
    admin = await _make_admin(client)
    assert (
        await client.post(f"/api/v1/admin/users/{admin.id}/disable", json={"reason": "unsafe"})
    ).status_code == 409
    assert (
        await client.post(
            f"/api/v1/admin/users/{admin.id}/sessions/revoke", json={"reason": "unsafe"}
        )
    ).status_code == 409
    assert (
        await client.post(f"/api/v1/admin/users/{admin.id}/enable", json={"reason": "keep active"})
    ).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}, {"reason": "ok", "x": 1}],
)
async def test_mutation_reason_validation(client: AsyncClient, payload: dict | None) -> None:
    target = await create_verified_user(f"validation-{uuid4().hex}@agrovix.dev")
    await _make_admin(client)
    kwargs = {} if payload is None else {"json": payload}
    response = await client.post(f"/api/v1/admin/users/{target.id}/disable", **kwargs)
    assert response.status_code == 422


async def test_disable_is_atomic_idempotent_and_enable_does_not_restore_credentials(
    client: AsyncClient,
) -> None:
    target = await create_verified_user(f"credentials-{uuid4().hex}@agrovix.dev")
    refresh_id, recovery_id, old_access, _ = await _seed_credentials(target, client)
    admin = await _make_admin(client)

    response = await client.post(
        f"/api/v1/admin/users/{target.id}/disable",
        json={"reason": "  Security investigation  "},
        headers={"x-request-id": "admin-disable-test", "user-agent": "admin-test-agent"},
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    audit = await _audit("admin.user.disable", target.id)
    assert audit.actor_id == admin.id
    assert audit.organization_id is None and audit.farm_id is None
    assert audit.ip_address is not None
    assert audit.user_agent == "admin-test-agent"
    assert audit.request_id == "admin-disable-test"
    assert audit.metadata_json == {
        "reason": "Security investigation",
        "previous_is_active": True,
        "resulting_is_active": False,
        "invalidated_recovery_tokens": 1,
        "revoked_sessions": 1,
        "idempotent": False,
    }

    repeat = await client.post(
        f"/api/v1/admin/users/{target.id}/disable", json={"reason": "repeat"}
    )
    assert repeat.status_code == 200
    repeat_audit = await _audit("admin.user.disable", target.id)
    assert repeat_audit.metadata_json["idempotent"] is True

    enabled = await client.post(
        f"/api/v1/admin/users/{target.id}/enable", json={"reason": "  cleared  "}
    )
    assert enabled.status_code == 200
    assert enabled.json()["is_active"] is True
    first_enable_audit = await _audit("admin.user.enable", target.id)
    assert first_enable_audit.metadata_json == {
        "reason": "cleared",
        "previous_is_active": False,
        "resulting_is_active": True,
        "idempotent": False,
    }
    repeated_enable = await client.post(
        f"/api/v1/admin/users/{target.id}/enable", json={"reason": "still cleared"}
    )
    assert repeated_enable.status_code == 200
    enable_audit = await _audit("admin.user.enable", target.id)
    assert enable_audit.metadata_json["idempotent"] is True

    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401
    fresh_login = await client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": "Sprint0ne!2026"},
    )
    assert fresh_login.status_code == 200
    fresh_access = fresh_login.cookies.get(get_settings().cookie_access_name)
    assert fresh_access is not None
    assert decode_token(fresh_access, expected_type="access")["sv"] == 1

    async with _session() as session:
        refresh = await session.get(RefreshToken, refresh_id)
        recovery = await session.get(PasswordRecoveryToken, recovery_id)
        assert refresh is not None and refresh.is_revoked
        assert recovery is not None and recovery.invalidated_at is not None


async def test_session_revoke_returns_count_audits_and_does_not_touch_recovery(
    client: AsyncClient,
) -> None:
    target = await create_verified_user(f"revoke-{uuid4().hex}@agrovix.dev")
    _, recovery_id, old_access, old_refresh = await _seed_credentials(target, client)
    await login(client, target.email)
    admin = await _make_admin(client)
    response = await client.post(
        f"/api/v1/admin/users/{target.id}/sessions/revoke",
        json={"reason": "  Device compromise  "},
    )
    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 2
    assert set(response.json()["user"]) == {
        "id",
        "email",
        "full_name",
        "is_active",
        "is_verified",
        "is_superuser",
        "created_at",
        "updated_at",
    }
    audit = await _audit("admin.user.sessions.revoke", target.id)
    assert audit.actor_id == admin.id
    assert audit.metadata_json == {"reason": "Device compromise", "revoked_sessions": 2}
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    ).status_code == 401
    fresh_login = await client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": "Sprint0ne!2026"},
    )
    assert fresh_login.status_code == 200
    fresh_access = fresh_login.cookies.get(get_settings().cookie_access_name)
    assert fresh_access is not None
    assert decode_token(fresh_access, expected_type="access")["sv"] == 1
    async with _session() as session:
        recovery = await session.get(PasswordRecoveryToken, recovery_id)
        assert recovery is not None and recovery.invalidated_at is None


class _FailingAuditRepository(AuditRepository):
    async def record(self, **kwargs):
        raise RuntimeError("injected audit failure")


def _failing_admin_service(
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserService:
    return AdminUserService(
        user_repo=UserRepository(session),
        recovery_repo=PasswordRecoveryTokenRepository(session),
        refresh_repo=RefreshTokenRepository(session),
        audit_repo=_FailingAuditRepository(session),
    )


async def test_audit_failure_rolls_back_user_and_credentials(client: AsyncClient) -> None:
    target = await create_verified_user(f"rollback-{uuid4().hex}@agrovix.dev")
    refresh_id, recovery_id, _, _ = await _seed_credentials(target, client)
    await _make_admin(client)
    app.dependency_overrides[get_admin_user_service] = _failing_admin_service
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            await client.post(
                f"/api/v1/admin/users/{target.id}/disable", json={"reason": "rollback"}
            )
    finally:
        app.dependency_overrides.pop(get_admin_user_service, None)

    async with _session() as session:
        user = await session.get(User, target.id)
        refresh = await session.get(RefreshToken, refresh_id)
        recovery = await session.get(PasswordRecoveryToken, recovery_id)
        assert user is not None and user.is_active and user.session_version == 0
        assert refresh is not None and not refresh.is_revoked
        assert recovery is not None and recovery.invalidated_at is None
        assert (
            not (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "admin.user.disable",
                        AuditEvent.entity_id == str(target.id),
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_service_rejects_repositories_from_mixed_sessions() -> None:
    async with _session() as first_session, _session() as second_session:
        with pytest.raises(ValueError, match="must share one transaction"):
            AdminUserService(
                user_repo=UserRepository(first_session),
                recovery_repo=PasswordRecoveryTokenRepository(first_session),
                refresh_repo=RefreshTokenRepository(second_session),
                audit_repo=AuditRepository(first_session),
            )


async def test_response_and_audit_do_not_serialize_secrets(client: AsyncClient) -> None:
    target = await create_verified_user(f"secrets-{uuid4().hex}@agrovix.dev")
    await _make_admin(client)
    response = await client.post(
        f"/api/v1/admin/users/{target.id}/disable", json={"reason": "policy"}
    )
    serialized = str(response.json()).lower()
    audit = await _audit("admin.user.disable", target.id)
    serialized += str(audit.metadata_json).lower()
    for forbidden in (
        "password",
        "token_hash",
        "raw_token",
        "refresh_token",
        "jwt",
        "cookie",
        "authorization",
    ):
        assert forbidden not in serialized


async def test_directory_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/users")
    assert response.status_code == 401


async def test_directory_openapi_exposes_only_frozen_query_parameters() -> None:
    operation = app.openapi()["paths"]["/api/v1/admin/users"]["get"]
    query_parameters = {
        parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "query"
    }
    assert query_parameters == {"search", "status", "verified", "limit", "offset"}


async def test_directory_authorization_precedes_repository_query(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = await create_verified_user(f"directory-ordinary-{uuid4().hex}@agrovix.dev")
    await switch_user(client, ordinary.email)
    calls = 0

    async def forbidden_query(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("directory query executed before platform authorization")

    monkeypatch.setattr(UserRepository, "search_admin_directory", forbidden_query)
    response = await client.get("/api/v1/admin/users")
    assert response.status_code == 403
    assert calls == 0


async def test_directory_organization_owner_is_not_platform_admin(client: AsyncClient) -> None:
    owner = await create_verified_user(f"directory-owner-{uuid4().hex}@agrovix.dev")
    await switch_user(client, owner.email)
    await create_org(client)
    assert (await client.get("/api/v1/admin/users")).status_code == 403


@pytest.mark.parametrize("superuser", [False, True])
async def test_directory_allows_effective_platform_admin(
    client: AsyncClient, superuser: bool
) -> None:
    admin = await _make_admin(client, superuser=superuser)
    response = await client.get("/api/v1/admin/users", params={"search": admin.email})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == str(admin.id)


async def test_directory_visibility_status_and_verification_filters(client: AsyncClient) -> None:
    marker = uuid4().hex
    active = await create_verified_user(f"directory-active-{marker}@agrovix.dev")
    disabled = await create_verified_user(f"directory-disabled-{marker}@agrovix.dev")
    unverified = await create_verified_user(f"directory-unverified-{marker}@agrovix.dev")
    deleted = await create_verified_user(f"directory-deleted-{marker}@agrovix.dev")
    async with _session() as session:
        disabled_row = await session.get(User, disabled.id)
        unverified_row = await session.get(User, unverified.id)
        deleted_row = await session.get(User, deleted.id)
        assert disabled_row is not None and unverified_row is not None and deleted_row is not None
        disabled_row.is_active = False
        unverified_row.is_verified = False
        unverified_row.verified_at = None
        deleted_row.is_active = False
        deleted_row.deleted_at = datetime.now(UTC)
        await session.commit()

    await _make_admin(client)
    page = (await client.get("/api/v1/admin/users", params={"search": marker, "limit": 200})).json()
    assert page["total"] == 3
    assert {item["id"] for item in page["items"]} == {
        str(active.id),
        str(disabled.id),
        str(unverified.id),
    }

    active_page = (
        await client.get("/api/v1/admin/users", params={"search": marker, "status": "active"})
    ).json()
    assert {item["id"] for item in active_page["items"]} == {
        str(active.id),
        str(unverified.id),
    }
    assert active_page["total"] == 2

    disabled_page = (
        await client.get("/api/v1/admin/users", params={"search": marker, "status": "disabled"})
    ).json()
    assert disabled_page["total"] == 1
    assert disabled_page["items"][0]["id"] == str(disabled.id)

    verified_page = (
        await client.get("/api/v1/admin/users", params={"search": marker, "verified": "true"})
    ).json()
    assert {item["id"] for item in verified_page["items"]} == {
        str(active.id),
        str(disabled.id),
    }
    assert verified_page["total"] == 2

    combined = (
        await client.get(
            "/api/v1/admin/users",
            params={"search": marker, "status": "active", "verified": "false"},
        )
    ).json()
    assert combined["total"] == 1
    assert combined["items"][0]["id"] == str(unverified.id)


async def test_directory_search_is_normalized_case_insensitive_and_wildcard_safe(
    client: AsyncClient,
) -> None:
    marker = uuid4().hex
    normalized = await create_verified_user(f"normalized-{marker}@agrovix.dev")
    percent = await create_verified_user(f"percent-{marker}@agrovix.dev")
    underscore = await create_verified_user(f"underscore-{marker}@agrovix.dev")
    backslash = await create_verified_user(f"backslash-{marker}@agrovix.dev")
    decoy = await create_verified_user(f"decoy-{marker}@agrovix.dev")
    async with _session() as session:
        names = {
            normalized.id: f"Mixed CASE {marker}",
            percent.id: f"Literal%{marker}",
            underscore.id: f"Literal_{marker}",
            backslash.id: f"Literal\\{marker}",
            decoy.id: f"LiteralX{marker}",
        }
        for user_id, full_name in names.items():
            row = await session.get(User, user_id)
            assert row is not None
            row.full_name = full_name
        await session.commit()

    await _make_admin(client)
    normalized_page = (
        await client.get(
            "/api/v1/admin/users", params={"search": f"  mixed case {marker.upper()}  "}
        )
    ).json()
    assert normalized_page["total"] == 1
    assert normalized_page["items"][0]["id"] == str(normalized.id)

    for literal, expected_id in (("%", percent.id), ("_", underscore.id), ("\\", backslash.id)):
        page = (
            await client.get("/api/v1/admin/users", params={"search": f"Literal{literal}{marker}"})
        ).json()
        assert page["total"] == 1
        assert page["items"][0]["id"] == str(expected_id)

    blank_page = (
        await client.get("/api/v1/admin/users", params={"search": "   ", "limit": 200})
    ).json()
    assert blank_page["total"] >= 6
    no_match = (
        await client.get("/api/v1/admin/users", params={"search": f"missing-{uuid4().hex}"})
    ).json()
    assert no_match == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_directory_deterministic_order_and_offset_pagination(client: AsyncClient) -> None:
    marker = uuid4().hex
    users = [
        await create_verified_user(f"directory-page-{marker}-{index}@agrovix.dev")
        for index in range(4)
    ]
    tied_created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    async with _session() as session:
        for user in users:
            row = await session.get(User, user.id)
            assert row is not None
            row.created_at = tied_created_at
        await session.commit()

    await _make_admin(client)
    expected = [str(user.id) for user in sorted(users, key=lambda user: user.id, reverse=True)]
    first = (
        await client.get("/api/v1/admin/users", params={"search": marker, "limit": 2, "offset": 0})
    ).json()
    second = (
        await client.get("/api/v1/admin/users", params={"search": marker, "limit": 2, "offset": 2})
    ).json()
    assert first["total"] == 4
    assert first["limit"] == 2 and first["offset"] == 0
    assert second["total"] == 4
    assert second["limit"] == 2 and second["offset"] == 2
    actual = [item["id"] for item in first["items"] + second["items"]]
    assert actual == expected
    assert len(actual) == len(set(actual)) == 4


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"status": "unknown"}, 422),
        ({"verified": "unknown"}, 422),
        ({"limit": 0}, 422),
        ({"limit": 201}, 422),
        ({"offset": -1}, 422),
        ({"search": "x" * 256}, 422),
    ],
)
async def test_directory_query_validation(
    client: AsyncClient, params: dict[str, object], expected_status: int
) -> None:
    await _make_admin(client)
    assert (await client.get("/api/v1/admin/users", params=params)).status_code == expected_status


async def test_directory_response_is_secret_safe_and_read_is_not_audited(
    client: AsyncClient,
) -> None:
    target = await create_verified_user(f"directory-secrets-{uuid4().hex}@agrovix.dev")
    await _make_admin(client)
    async with _session() as session:
        before = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.entity_type == "user", AuditEvent.entity_id == str(target.id))
            )
            or 0
        )

    response = await client.get("/api/v1/admin/users", params={"search": target.email})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items", "total", "limit", "offset"}
    assert set(payload["items"][0]) == {
        "id",
        "email",
        "full_name",
        "is_active",
        "is_verified",
        "is_superuser",
        "created_at",
        "updated_at",
    }
    serialized = str(payload).lower()
    for forbidden in (
        "password",
        "token",
        "jwt",
        "cookie",
        "authorization",
        "membership",
        "role",
        "ip_address",
        "user_agent",
    ):
        assert forbidden not in serialized

    async with _session() as session:
        after = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.entity_type == "user", AuditEvent.entity_id == str(target.id))
            )
            or 0
        )
    assert after == before
