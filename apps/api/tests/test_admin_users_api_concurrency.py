"""Independent-session PostgreSQL races for Sprint 5.3 administration."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import decode_token, hash_password, verify_password
from app.db import session as db
from app.email.log_sender import LogEmailSender
from app.models.password_recovery import PasswordRecoveryToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_repo import AuditRepository
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository
from app.repositories.verification_repo import VerificationTokenRepository
from app.services.admin_user_service import AdminUserService
from app.services.auth_service import AuthService
from app.services.password_recovery import PasswordRecoveryKernel, PasswordRecoveryService

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        "postgresql" not in os.environ.get("DATABASE_URL", ""),
        reason="Requires real PostgreSQL row locking.",
    ),
]


@pytest_asyncio.fixture(autouse=True)
async def _ensure_engine(_engine):
    yield


def _auth(session) -> AuthService:
    return AuthService(
        user_repo=UserRepository(session),
        refresh_repo=RefreshTokenRepository(session),
        verification_repo=VerificationTokenRepository(session),
        email_sender=LogEmailSender(),
        rate_limiter=InMemoryRateLimiter(),
    )


def _recovery(session) -> PasswordRecoveryService:
    users = UserRepository(session)
    tokens = PasswordRecoveryTokenRepository(session)
    return PasswordRecoveryService(
        kernel=PasswordRecoveryKernel(user_repo=users, token_repo=tokens),
        user_repo=users,
        token_repo=tokens,
        refresh_repo=RefreshTokenRepository(session),
        audit_repo=AuditRepository(session),
        rate_limiter=InMemoryRateLimiter(),
        email_sender=LogEmailSender(),
    )


def _admin(session) -> AdminUserService:
    return AdminUserService(
        user_repo=UserRepository(session),
        recovery_repo=PasswordRecoveryTokenRepository(session),
        refresh_repo=RefreshTokenRepository(session),
        audit_repo=AuditRepository(session),
    )


async def _seed() -> tuple[UUID, UUID, str, str, str, str]:
    password = "Original-Password!2026"
    async with db.AsyncSessionLocal() as session:
        actor = User(
            email=f"race-admin-{uuid4().hex}@agrovix.dev",
            hashed_password=hash_password(password),
            is_active=True,
            is_verified=True,
            is_superuser=True,
        )
        target = User(
            email=f"race-target-{uuid4().hex}@agrovix.dev",
            hashed_password=hash_password(password),
            is_active=True,
            is_verified=True,
        )
        session.add_all([actor, target])
        await session.flush()
        issued = await _recovery(session).kernel.issue(user_id=target.id)
        assert issued is not None
        raw_recovery, _ = issued
        _, pair = await _auth(session).login(email=target.email, password=password)
        await session.commit()
        return actor.id, target.id, target.email, password, raw_recovery, pair.refresh_token


async def _pid(session) -> int:
    result = await session.scalar(select(func.pg_backend_pid()))
    assert result is not None
    return int(result)


async def _wait_user_lock(contender_pid: int, controller_pid: int) -> tuple[int, ...]:
    statement = text("""
        SELECT wait_event_type, pg_blocking_pids(pid) AS blocking_pids
        FROM pg_stat_activity WHERE pid = :pid
        """)
    async with db.AsyncSessionLocal() as observer:
        while True:
            row = (await observer.execute(statement, {"pid": contender_pid})).one()
            # Each contending security operation resolves immutable token
            # identity, then locks the user before any credential row. A
            # blocked backend therefore cannot have reached a dependent-row
            # lock yet. Require the backend holding the winning transaction to
            # be an actual direct blocker; unrelated database locks cannot
            # satisfy this proof.
            blocking_pids = tuple(int(pid) for pid in row.blocking_pids)
            if row.wait_event_type == "Lock" and blocking_pids and controller_pid in blocking_pids:
                return blocking_pids
            await asyncio.sleep(0.01)


async def _contend(
    operation: Callable[[object], Awaitable[object]], session, ready: asyncio.Future[int]
) -> tuple[str, object | None]:
    ready.set_result(await _pid(session))
    try:
        result = await operation(session)
        await session.commit()
        return "success", result
    except HTTPException as exc:
        await asyncio.wait_for(session.rollback(), timeout=3)
        return f"http-{exc.status_code}", None


async def _close(session) -> list[BaseException]:
    errors: list[BaseException] = []
    if session.in_transaction():
        try:
            await asyncio.wait_for(session.rollback(), timeout=3)
        except BaseException as exc:
            errors.append(exc)
    try:
        await asyncio.wait_for(session.close(), timeout=3)
    except BaseException as exc:
        errors.append(exc)
    return errors


async def _drain(task: asyncio.Task) -> list[BaseException]:
    errors: list[BaseException] = []
    if not task.done():
        task.cancel()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=3,
        )
    except BaseException as exc:
        errors.append(exc)
    else:
        errors.extend(
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        )
    return errors


async def _race(
    winner: Callable[[object], Awaitable[object]],
    loser: Callable[[object], Awaitable[object]],
) -> tuple[object, tuple[str, object | None]]:
    controller = db.AsyncSessionLocal()
    contender = db.AsyncSessionLocal()
    task: asyncio.Task | None = None
    try:
        controller_pid = await _pid(controller)
        winner_result = await winner(controller)
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_contend(loser, contender, ready))
        contender_pid = await asyncio.wait_for(ready, timeout=5)
        # A loaded full-suite worker can take several seconds to schedule the
        # observer even though PostgreSQL has already blocked the contender.
        # The database lock state remains the proof of contention; this bound
        # only prevents an unhealthy test run from waiting indefinitely.
        blocking_pids = await asyncio.wait_for(
            _wait_user_lock(contender_pid, controller_pid), timeout=15
        )
        assert controller_pid in blocking_pids
        assert not task.done()
        await controller.commit()
        loser_result = await asyncio.wait_for(task, timeout=10)
        return winner_result, loser_result
    finally:
        errors: list[BaseException] = []
        if task is not None:
            errors.extend(await _drain(task))
        errors.extend(await _close(controller))
        errors.extend(await _close(contender))
        assert not errors, errors


async def _disable(session, actor_id: UUID, target_id: UUID):
    actor = await UserRepository(session).get_by_id(actor_id)
    assert actor is not None
    return await _admin(session).disable(
        actor=actor,
        target_id=target_id,
        reason="race test",
        request_ctx={},
    )


async def _active_refresh_count(user_id: UUID) -> int:
    async with db.AsyncSessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
            )
            or 0
        )


@pytest.mark.parametrize("disable_first", [True, False])
async def test_disable_vs_login_proves_both_winner_orders(disable_first: bool) -> None:
    actor_id, target_id, email, password, _, _ = await _seed()

    async def disable(session):
        return await _disable(session, actor_id, target_id)

    async def login(session):
        return await _auth(session).login(email=email, password=password)

    _, loser = await _race(disable, login) if disable_first else await _race(login, disable)
    assert loser[0] == ("http-403" if disable_first else "success")
    async with db.AsyncSessionLocal() as observer:
        target = await observer.get(User, target_id)
        assert target is not None and not target.is_active
    assert await _active_refresh_count(target_id) == 0


@pytest.mark.parametrize("disable_first", [True, False])
async def test_disable_vs_refresh_proves_both_winner_orders(disable_first: bool) -> None:
    actor_id, target_id, _, _, _, refresh_token = await _seed()

    async def disable(session):
        return await _disable(session, actor_id, target_id)

    async def refresh(session):
        return await _auth(session).refresh(refresh_token=refresh_token)

    _, loser = await _race(disable, refresh) if disable_first else await _race(refresh, disable)
    assert loser[0] == ("http-401" if disable_first else "success")
    assert await _active_refresh_count(target_id) == 0


@pytest.mark.parametrize("disable_first", [True, False])
async def test_disable_vs_recovery_reset_proves_both_winner_orders(disable_first: bool) -> None:
    actor_id, target_id, _, password, raw_recovery, _ = await _seed()
    replacement = "Replacement-Password!2026"

    async def disable(session):
        return await _disable(session, actor_id, target_id)

    async def reset(session):
        return await _recovery(session).reset_password(
            raw_token=raw_recovery,
            new_password=replacement,
            request_ctx={},
        )

    _, loser = await _race(disable, reset) if disable_first else await _race(reset, disable)
    assert loser[0] == ("http-400" if disable_first else "success")
    async with db.AsyncSessionLocal() as observer:
        target = await observer.get(User, target_id)
        token = await observer.scalar(
            select(PasswordRecoveryToken).where(PasswordRecoveryToken.user_id == target_id)
        )
        assert target is not None and not target.is_active
        assert target.hashed_password is not None
        assert verify_password(password if disable_first else replacement, target.hashed_password)
        assert token is not None
        if disable_first:
            assert token.consumed_at is None and token.invalidated_at is not None
        else:
            assert token.consumed_at is not None
    assert await _active_refresh_count(target_id) == 0


@pytest.mark.parametrize("revoke_first", [True, False])
async def test_revoke_sessions_vs_refresh_proves_both_winner_orders(revoke_first: bool) -> None:
    actor_id, target_id, _, _, _, refresh_token = await _seed()

    async def revoke(session):
        actor = await UserRepository(session).get_by_id(actor_id)
        assert actor is not None
        return await _admin(session).revoke_sessions(
            actor=actor,
            target_id=target_id,
            reason="race test",
            request_ctx={},
        )

    async def refresh(session):
        return await _auth(session).refresh(refresh_token=refresh_token)

    winner, loser = await _race(revoke, refresh) if revoke_first else await _race(refresh, revoke)
    assert loser[0] == ("http-401" if revoke_first else "success")
    assert await _active_refresh_count(target_id) == 0
    async with db.AsyncSessionLocal() as observer:
        target = await observer.get(User, target_id)
        assert target is not None and target.session_version == 1
    if not revoke_first:
        assert decode_token(winner.access_token, expected_type="access")["sv"] == 0


async def test_concurrent_session_revocations_serialize_generation_increments() -> None:
    actor_id, target_id, _, _, _, _ = await _seed()

    async def revoke(session):
        actor = await UserRepository(session).get_by_id(actor_id)
        assert actor is not None
        return await _admin(session).revoke_sessions(
            actor=actor,
            target_id=target_id,
            reason="concurrent revoke",
            request_ctx={},
        )

    _, loser = await _race(revoke, revoke)
    assert loser[0] == "success"
    async with db.AsyncSessionLocal() as observer:
        target = await observer.get(User, target_id)
        assert target is not None and target.session_version == 2
    assert await _active_refresh_count(target_id) == 0
