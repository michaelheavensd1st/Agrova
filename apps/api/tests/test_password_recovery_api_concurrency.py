"""Independent-session PostgreSQL races for Sprint 5.2 recovery."""

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
from app.models.audit import AuditEvent
from app.models.password_recovery import PasswordRecoveryToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_repo import AuditRepository
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository
from app.repositories.verification_repo import VerificationTokenRepository
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


def _recovery_service(session) -> PasswordRecoveryService:
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


def _auth_service(session) -> AuthService:
    return AuthService(
        user_repo=UserRepository(session),
        refresh_repo=RefreshTokenRepository(session),
        verification_repo=VerificationTokenRepository(session),
        email_sender=LogEmailSender(),
        rate_limiter=InMemoryRateLimiter(),
    )


async def _seed() -> tuple[UUID, str, str]:
    password = "Original-Password!2026"
    async with db.AsyncSessionLocal() as session:
        user = User(
            email=f"recovery-race-api-{uuid4().hex}@agrovix.dev",
            hashed_password=hash_password(password),
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.flush()
        issued = await _recovery_service(session).kernel.issue(user_id=user.id)
        assert issued is not None
        raw_token, _ = issued
        await session.commit()
        return user.id, user.email, raw_token


async def _pid(session) -> int:
    value = await session.scalar(select(func.pg_backend_pid()))
    assert value is not None
    return int(value)


async def _wait_blocked(pids: set[int], *, timeout: float = 5) -> None:
    statement = text("""
        SELECT pid, state, wait_event_type, wait_event,
               pg_blocking_pids(pid) blocking_pids, query, query_start,
               backend_xid, backend_xmin
        FROM pg_stat_activity WHERE pid = ANY(CAST(:pids AS INTEGER[]))
        """)
    deadline = asyncio.get_running_loop().time() + timeout
    last_rows: list[dict[str, object]] = []

    def remaining(operation: str) -> float:
        seconds = deadline - asyncio.get_running_loop().time()
        if seconds <= 0:
            pytest.fail(
                "Timed out waiting for PostgreSQL user-row locks during "
                f"{operation}; expected_pids={sorted(pids)!r}, last_rows={last_rows!r}"
            )
        return seconds

    async with db.AsyncSessionLocal() as observer:
        while True:
            try:
                result = await asyncio.wait_for(
                    observer.execute(statement, {"pids": list(pids)}),
                    timeout=remaining("observer.execute"),
                )
            except TimeoutError:
                pytest.fail(
                    "Timed out waiting for PostgreSQL user-row locks during "
                    "observer.execute; "
                    f"expected_pids={sorted(pids)!r}, last_rows={last_rows!r}"
                )
            rows = result.all()
            last_rows = [dict(row._mapping) for row in rows]
            if len(rows) == len(pids) and all(
                row.wait_event_type == "Lock"
                and row.blocking_pids
                and "from users" in row.query.lower()
                and "for update" in row.query.lower()
                for row in rows
            ):
                return
            # PostgreSQL may cache cumulative-statistics values, including
            # pg_stat_activity query text, until the observer transaction ends.
            try:
                await asyncio.wait_for(
                    observer.rollback(),
                    timeout=remaining("observer.rollback"),
                )
            except TimeoutError:
                pytest.fail(
                    "Timed out waiting for PostgreSQL user-row locks during "
                    "observer.rollback; "
                    f"expected_pids={sorted(pids)!r}, last_rows={last_rows!r}"
                )
            await asyncio.sleep(min(0.01, remaining("poll delay")))


async def _contender(
    operation: Callable[[object], Awaitable[object]],
    session,
    ready: asyncio.Future[int],
) -> tuple[str, object | None]:
    ready.set_result(await _pid(session))
    try:
        result = await operation(session)
        await session.commit()
        return "success", result
    except HTTPException as exc:
        await asyncio.wait_for(session.rollback(), timeout=3)
        return f"http-{exc.status_code}", None


async def _bounded_session_cleanup(session) -> list[BaseException]:
    """Attempt independently bounded rollback and close for one owned session."""
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


async def _bounded_task_cleanup(tasks: list[asyncio.Task]) -> list[BaseException]:
    """Cancel and drain owned tasks without an unbounded failure path."""
    if not tasks:
        return []
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        return []
    except BaseException as first_error:
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        except BaseException as second_error:
            return [first_error, second_error]
        return [first_error]


async def _cleanup_owned_race_resources(
    *, controller, contenders: list, tasks: list[asyncio.Task]
) -> list[BaseException]:
    """Release every explicitly owned race resource, regardless of sibling failures."""
    errors = await _bounded_session_cleanup(controller)
    errors.extend(await _bounded_task_cleanup(tasks))
    for session in contenders:
        errors.extend(await _bounded_session_cleanup(session))
    return errors


async def _blocked_race(
    user_id: UUID,
    operations: list[Callable[[object], Awaitable[object]]],
) -> list[tuple[str, object | None]]:
    controller = db.AsyncSessionLocal()
    sessions = [db.AsyncSessionLocal() for _ in operations]
    tasks: list[asyncio.Task] = []
    try:
        assert await UserRepository(controller).get_by_id_for_update(user_id) is not None
        loop = asyncio.get_running_loop()
        pid_futures = [loop.create_future() for _ in operations]
        tasks = [
            asyncio.create_task(_contender(operation, session, pid_future))
            for operation, session, pid_future in zip(
                operations, sessions, pid_futures, strict=True
            )
        ]
        pids = set(await asyncio.wait_for(asyncio.gather(*pid_futures), timeout=5))
        assert len(pids) == len(operations)
        await _wait_blocked(pids)
        assert all(not task.done() for task in tasks)
        await controller.commit()
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        cleanup_errors = await _cleanup_owned_race_resources(
            controller=controller, contenders=sessions, tasks=tasks
        )
        assert not cleanup_errors, cleanup_errors


async def test_reset_vs_reset_has_exactly_one_winner() -> None:
    user_id, _, raw_token = await _seed()

    async def reset(session):
        return await _recovery_service(session).reset_password(
            raw_token=raw_token,
            new_password="Replacement-Password!2026",
            request_ctx={},
        )

    outcomes = await _blocked_race(user_id, [reset, reset])
    assert sorted(outcome for outcome, _ in outcomes) == ["http-400", "success"]


async def test_reset_wins_against_refresh_and_login() -> None:
    user_id, email, raw_token = await _seed()
    async with db.AsyncSessionLocal() as session:
        _, pair = await _auth_service(session).login(email=email, password="Original-Password!2026")
        await session.commit()

    controller = db.AsyncSessionLocal()
    refresh_session = db.AsyncSessionLocal()
    login_session = db.AsyncSessionLocal()
    tasks: list[asyncio.Task] = []
    try:
        await _recovery_service(controller).reset_password(
            raw_token=raw_token,
            new_password="Replacement-Password!2026",
            request_ctx={},
        )
        loop = asyncio.get_running_loop()
        refresh_pid = loop.create_future()
        login_pid = loop.create_future()

        async def refresh(session):
            return await _auth_service(session).refresh(refresh_token=pair.refresh_token)

        async def login(session):
            return await _auth_service(session).login(
                email=email, password="Original-Password!2026"
            )

        tasks = [
            asyncio.create_task(_contender(refresh, refresh_session, refresh_pid)),
            asyncio.create_task(_contender(login, login_session, login_pid)),
        ]
        pids = set(await asyncio.wait_for(asyncio.gather(refresh_pid, login_pid), timeout=5))
        await _wait_blocked(pids)
        await controller.commit()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert [outcome for outcome, _ in outcomes] == ["http-401", "http-401"]
    finally:
        cleanup_errors = await _cleanup_owned_race_resources(
            controller=controller,
            contenders=[refresh_session, login_session],
            tasks=tasks,
        )
        assert not cleanup_errors, cleanup_errors

    async with db.AsyncSessionLocal() as observer:
        user = await observer.get(User, user_id)
        assert user is not None and user.session_version == 1
    assert decode_token(pair.access_token, expected_type="access")["sv"] == 0


@pytest.mark.parametrize("operation_name", ["refresh", "login"])
async def test_refresh_or_login_winner_is_revoked_by_following_reset(operation_name: str) -> None:
    user_id, email, raw_token = await _seed()
    async with db.AsyncSessionLocal() as seed_session:
        _, original_pair = await _auth_service(seed_session).login(
            email=email, password="Original-Password!2026"
        )
        await seed_session.commit()

    controller = db.AsyncSessionLocal()
    reset_session = db.AsyncSessionLocal()
    task: asyncio.Task | None = None
    try:
        if operation_name == "refresh":
            winner_pair = await _auth_service(controller).refresh(
                refresh_token=original_pair.refresh_token
            )
        else:
            _, winner_pair = await _auth_service(controller).login(
                email=email, password="Original-Password!2026"
            )
        # Pin the winner transaction at the shared security-root boundary.
        # The auth operation already acquired this lock; the explicit re-read
        # makes the contested state deterministic for PostgreSQL observation.
        assert await UserRepository(controller).get_by_id_for_update(user_id) is not None
        assert controller.in_transaction()
        ready = asyncio.get_running_loop().create_future()

        async def reset(session):
            # Enter the shared user-lock boundary explicitly so observation
            # cannot race the reset service's immutable identity probe. The
            # service re-locks/re-reads the same user before token mutation.
            assert await UserRepository(session).get_by_id_for_update(user_id) is not None
            return await _recovery_service(session).reset_password(
                raw_token=raw_token,
                new_password="Replacement-Password!2026",
                request_ctx={},
            )

        task = asyncio.create_task(_contender(reset, reset_session, ready))
        reset_pid = await asyncio.wait_for(ready, timeout=5)
        await _wait_blocked({reset_pid})
        await controller.commit()
        assert (await asyncio.wait_for(task, timeout=10))[0] == "success"
    finally:
        cleanup_errors = await _cleanup_owned_race_resources(
            controller=controller,
            contenders=[reset_session],
            tasks=[] if task is None else [task],
        )
        assert not cleanup_errors, cleanup_errors

    async with db.AsyncSessionLocal() as observer:
        user = await observer.get(User, user_id)
        active = await observer.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
        )
        assert user is not None and user.session_version == 1
        assert active == 0
    assert decode_token(winner_pair.access_token, expected_type="access")["sv"] == 0


async def test_reset_rollback_restores_password_token_sessions_and_audits() -> None:
    user_id, email, raw_token = await _seed()
    async with db.AsyncSessionLocal() as session:
        await _auth_service(session).login(email=email, password="Original-Password!2026")
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        await _recovery_service(session).reset_password(
            raw_token=raw_token,
            new_password="Replacement-Password!2026",
            request_ctx={},
        )
        await session.rollback()

    async with db.AsyncSessionLocal() as observer:
        user = await observer.get(User, user_id)
        token = await observer.scalar(
            select(PasswordRecoveryToken).where(PasswordRecoveryToken.user_id == user_id)
        )
        active_refreshes = await observer.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
        )
        audit_count = await observer.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.entity_id == str(user_id))
        )
        assert user is not None and verify_password("Original-Password!2026", user.hashed_password)
        assert user.session_version == 0
        assert token is not None and token.consumed_at is None
        assert active_refreshes == 1
        assert audit_count == 0


async def test_cleanup_closes_every_session_when_rollbacks_fail() -> None:
    class FailingRollbackSession:
        def __init__(self) -> None:
            self.rollback_attempted = False
            self.close_attempted = False

        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            self.rollback_attempted = True
            raise RuntimeError("injected rollback failure")

        async def close(self) -> None:
            self.close_attempted = True

    controller = FailingRollbackSession()
    contender = FailingRollbackSession()
    errors = await asyncio.wait_for(
        _cleanup_owned_race_resources(
            controller=controller,
            contenders=[contender],
            tasks=[],
        ),
        timeout=8,
    )

    assert len(errors) == 2
    assert controller.rollback_attempted and controller.close_attempted
    assert contender.rollback_attempted and contender.close_attempted


async def test_wait_blocked_bounds_stalled_observer_execute(monkeypatch) -> None:
    class StalledExecuteObserver:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(db, "AsyncSessionLocal", StalledExecuteObserver)

    with pytest.raises(pytest.fail.Exception) as raised:
        await asyncio.wait_for(_wait_blocked({4321}, timeout=0.02), timeout=0.5)

    message = str(raised.value)
    assert "observer.execute" in message
    assert "expected_pids=[4321]" in message
    assert "last_rows=[]" in message


async def test_wait_blocked_bounds_stalled_observer_rollback(monkeypatch) -> None:
    class DiagnosticRow:
        def __init__(self) -> None:
            self.pid = 4321
            self.state = "active"
            self.wait_event_type = None
            self.wait_event = None
            self.blocking_pids = []
            self.query = "SELECT 1"
            self.query_start = None
            self.backend_xid = None
            self.backend_xmin = None
            self._mapping = vars(self)

    class Result:
        def all(self):
            return [DiagnosticRow()]

    class StalledRollbackObserver:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, *_args, **_kwargs):
            return Result()

        async def rollback(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(db, "AsyncSessionLocal", StalledRollbackObserver)

    with pytest.raises(pytest.fail.Exception) as raised:
        await asyncio.wait_for(_wait_blocked({4321}, timeout=0.02), timeout=0.5)

    message = str(raised.value)
    assert "observer.rollback" in message
    assert "'pid': 4321" in message
    assert "'query': 'SELECT 1'" in message


async def test_wait_blocked_rolls_back_before_resampling(monkeypatch) -> None:
    class DiagnosticRow:
        def __init__(self, *, blocked: bool) -> None:
            self.pid = 4321
            self.state = "active"
            self.wait_event_type = "Lock" if blocked else None
            self.wait_event = "transactionid" if blocked else None
            self.blocking_pids = [1234] if blocked else []
            self.query = "SELECT users.id FROM users FOR UPDATE" if blocked else "SELECT 1"
            self.query_start = None
            self.backend_xid = None
            self.backend_xmin = None
            self._mapping = vars(self)

    class Result:
        def __init__(self, row) -> None:
            self.row = row

        def all(self):
            return [self.row]

    class SnapshotObserver:
        def __init__(self) -> None:
            self.samples = 0
            self.rollbacks = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, *_args, **_kwargs):
            if self.samples:
                assert self.rollbacks == 1
            self.samples += 1
            return Result(DiagnosticRow(blocked=self.samples == 2))

        async def rollback(self) -> None:
            self.rollbacks += 1

    observer = SnapshotObserver()
    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: observer)

    await _wait_blocked({4321}, timeout=0.5)

    assert observer.samples == 2
    assert observer.rollbacks == 1
