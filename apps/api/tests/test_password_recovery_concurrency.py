"""PostgreSQL concurrency proofs for the Sprint 5.1 recovery kernel.

Every contender uses an independent session and transaction. Controller
transactions hold the user lock until both contenders have reached an
explicit start barrier, proving real database contention rather than
cooperative coroutine ordering.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session as db
from app.models.password_recovery import PasswordRecoveryToken
from app.models.user import User
from app.repositories.password_recovery import PasswordRecoveryTokenRepository
from app.repositories.user_repo import UserRepository
from app.services.password_recovery import (
    PasswordRecoveryKernel,
    generate_recovery_token,
    hash_recovery_token,
)

pytestmark = pytest.mark.asyncio

_postgres_only = pytest.mark.skipif(
    "postgresql" not in os.environ.get("DATABASE_URL", ""),
    reason="Requires independent PostgreSQL sessions and row locks.",
)


@pytest_asyncio.fixture(autouse=True)
async def _ensure_engine(_engine):
    yield


def _kernel(session) -> PasswordRecoveryKernel:
    return PasswordRecoveryKernel(
        user_repo=UserRepository(session),
        token_repo=PasswordRecoveryTokenRepository(session),
    )


async def _seed_user() -> UUID:
    async with db.AsyncSessionLocal() as session:
        user = User(
            email=f"recovery-race-{uuid4().hex}@agrovix.dev",
            hashed_password="not-used-in-sprint-5.1",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _issue_contender(
    session: AsyncSession,
    user_id: UUID,
    backend_pid: asyncio.Future[int],
    start: asyncio.Event,
):
    backend_pid.set_result(await session.scalar(select(func.pg_backend_pid())))
    await start.wait()
    result = await _kernel(session).issue(user_id=user_id)
    assert result is not None
    raw_token, row = result
    await session.commit()
    return raw_token, row.id


async def _wait_until_postgres_reports_lock_contention(
    contender_pids: set[int],
) -> None:
    """Return only after PostgreSQL reports every contender blocked on a lock."""
    statement = text("""
        SELECT pid, wait_event_type, pg_blocking_pids(pid) AS blocking_pids, query
        FROM pg_stat_activity
        WHERE pid = ANY(CAST(:pids AS INTEGER[]))
        """)
    async with db.AsyncSessionLocal() as observer:
        while True:
            rows = (await observer.execute(statement, {"pids": list(contender_pids)})).all()
            if len(rows) == len(contender_pids) and all(
                row.wait_event_type == "Lock"
                and row.blocking_pids
                and "from users" in row.query.lower()
                and "for update" in row.query.lower()
                for row in rows
            ):
                return
            await asyncio.sleep(0.01)


async def _collect_in_completion_order(tasks):
    return [await future for future in asyncio.as_completed(tasks)]


async def _run_issuance_contention(
    user_id: UUID,
    *,
    contention_waiter: Callable[[set[int]], Awaitable[None]] = (
        _wait_until_postgres_reports_lock_contention
    ),
    contention_timeout: float = 10,
    contender_tasks: list[asyncio.Task] | None = None,
    contender_pids: set[int] | None = None,
    controller_sessions: list[AsyncSession] | None = None,
    contender_sessions: list[AsyncSession] | None = None,
    cleanup_state: dict[str, bool] | None = None,
    rollback_controller: Callable[[AsyncSession], Awaitable[None]] | None = None,
):
    """Run the issuance race and leave no task, transaction, or lock behind."""
    tasks = contender_tasks if contender_tasks is not None else []
    pids = contender_pids if contender_pids is not None else set()
    state = cleanup_state if cleanup_state is not None else {}
    controller = db.AsyncSessionLocal()
    sessions = [db.AsyncSessionLocal(), db.AsyncSessionLocal()]
    if controller_sessions is not None:
        controller_sessions.append(controller)
    if contender_sessions is not None:
        contender_sessions.extend(sessions)
    try:
        await UserRepository(controller).get_by_id_for_update(user_id)
        loop = asyncio.get_running_loop()
        pid_a = loop.create_future()
        pid_b = loop.create_future()
        start = asyncio.Event()
        tasks.extend(
            [
                asyncio.create_task(_issue_contender(sessions[0], user_id, pid_a, start)),
                asyncio.create_task(_issue_contender(sessions[1], user_id, pid_b, start)),
            ]
        )
        pids.update(await asyncio.wait_for(asyncio.gather(pid_a, pid_b), timeout=10))
        assert len(pids) == 2
        start.set()
        await asyncio.wait_for(contention_waiter(pids), timeout=contention_timeout)
        assert all(not task.done() for task in tasks)
        await controller.commit()
        return await asyncio.wait_for(
            _collect_in_completion_order(tasks),
            timeout=10,
        )
    finally:
        rollback_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            if controller.in_transaction():
                state["rollback_attempted"] = True
                rollback = rollback_controller or (lambda session: session.rollback())
                try:
                    await asyncio.wait_for(rollback(controller), timeout=5)
                except BaseException as exc:
                    rollback_error = exc
        finally:
            state["close_attempted"] = True
            try:
                await asyncio.wait_for(controller.close(), timeout=5)
                state["controller_closed"] = True
            except BaseException as exc:
                close_error = exc

        for task in tasks:
            if not task.done():
                task.cancel()

        drain_error: BaseException | None = None
        for _attempt in range(2):
            unfinished = [task for task in tasks if not task.done()]
            if not unfinished:
                break
            for task in unfinished:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5,
                )
            except BaseException as exc:
                drain_error = exc

        state["transaction_inactive"] = not controller.in_transaction()
        state["tasks_drained"] = all(task.done() for task in tasks)
        contender_cleanup_errors: list[BaseException] = []
        for session in sessions:
            try:
                if session.in_transaction():
                    await asyncio.wait_for(session.rollback(), timeout=5)
            except BaseException as exc:
                contender_cleanup_errors.append(exc)
            finally:
                try:
                    await asyncio.wait_for(session.close(), timeout=5)
                except BaseException as exc:
                    contender_cleanup_errors.append(exc)
        state["contender_transactions_inactive"] = all(
            not session.in_transaction() for session in sessions
        )
        state["contender_sessions_closed"] = not contender_cleanup_errors
        if close_error is not None or not state["transaction_inactive"]:
            raise RuntimeError("Controller session could not be safely closed") from close_error
        if not state["tasks_drained"]:
            raise RuntimeError("Contender tasks remained pending after cleanup") from drain_error
        if contender_cleanup_errors or not state["contender_transactions_inactive"]:
            raise RuntimeError("Contender sessions could not be safely closed") from (
                contender_cleanup_errors[0] if contender_cleanup_errors else None
            )
        if rollback_error is not None:
            state["rollback_failed_but_close_succeeded"] = True


@_postgres_only
async def test_simultaneous_issuance_serializes_and_newest_commit_wins():
    user_id = await _seed_user()
    completion_order = await _run_issuance_contention(user_id)

    first_result, second_result = completion_order
    async with db.AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(PasswordRecoveryToken)
                    .where(PasswordRecoveryToken.user_id == user_id)
                    .order_by(PasswordRecoveryToken.created_at, PasswordRecoveryToken.id)
                )
            )
            .scalars()
            .all()
        )
        outstanding = [
            row for row in rows if row.consumed_at is None and row.invalidated_at is None
        ]

    assert len(rows) == 2
    assert len(outstanding) == 1
    assert outstanding[0].id == second_result[1]
    assert outstanding[0].token_hash == hash_recovery_token(second_result[0])
    assert next(row for row in rows if row.id == first_result[1]).invalidated_at is not None


@_postgres_only
async def test_issuance_timeout_cleans_tasks_transactions_and_controller_lock():
    user_id = await _seed_user()
    tasks: list[asyncio.Task] = []
    pids: set[int] = set()
    controllers: list[AsyncSession] = []
    contender_sessions: list[AsyncSession] = []
    cleanup_state: dict[str, bool] = {}
    contention_observed = asyncio.Event()

    async def observe_contention_then_stall(observed_pids: set[int]) -> None:
        await _wait_until_postgres_reports_lock_contention(observed_pids)
        contention_observed.set()
        await asyncio.Event().wait()

    with pytest.raises(asyncio.TimeoutError):
        await _run_issuance_contention(
            user_id,
            contention_waiter=observe_contention_then_stall,
            contention_timeout=2,
            contender_tasks=tasks,
            contender_pids=pids,
            controller_sessions=controllers,
            contender_sessions=contender_sessions,
            cleanup_state=cleanup_state,
        )

    assert len(tasks) == 2
    assert all(task.done() for task in tasks)
    assert len(pids) == 2
    assert contention_observed.is_set()
    assert len(controllers) == 1
    assert cleanup_state["close_attempted"]
    assert cleanup_state["controller_closed"]
    assert cleanup_state["transaction_inactive"]
    assert cleanup_state["tasks_drained"]
    assert cleanup_state["contender_transactions_inactive"]
    assert cleanup_state["contender_sessions_closed"]
    assert not controllers[0].in_transaction()
    assert all(not session.in_transaction() for session in contender_sessions)

    async with db.AsyncSessionLocal() as observer:
        lingering = await observer.scalar(
            text("""
                SELECT count(*)
                FROM pg_stat_activity
                WHERE pid = ANY(CAST(:pids AS INTEGER[]))
                  AND state = 'idle in transaction'
                """),
            {"pids": list(pids)},
        )
        assert lingering == 0

    async with db.AsyncSessionLocal() as fresh_session:
        locked_user = await asyncio.wait_for(
            UserRepository(fresh_session).get_by_id_for_update(user_id),
            timeout=2,
        )
        assert locked_user is not None
        await fresh_session.rollback()


@_postgres_only
async def test_issuance_cleanup_closes_controller_when_rollback_fails():
    user_id = await _seed_user()
    tasks: list[asyncio.Task] = []
    controllers: list[AsyncSession] = []
    contender_sessions: list[AsyncSession] = []
    cleanup_state: dict[str, bool] = {}
    contention_observed = asyncio.Event()

    async def observe_contention_then_stall(observed_pids: set[int]) -> None:
        await _wait_until_postgres_reports_lock_contention(observed_pids)
        contention_observed.set()
        await asyncio.Event().wait()

    async def fail_rollback(_controller: AsyncSession) -> None:
        raise RuntimeError("injected rollback failure")

    with pytest.raises(asyncio.TimeoutError):
        await _run_issuance_contention(
            user_id,
            contention_waiter=observe_contention_then_stall,
            contention_timeout=2,
            contender_tasks=tasks,
            controller_sessions=controllers,
            contender_sessions=contender_sessions,
            cleanup_state=cleanup_state,
            rollback_controller=fail_rollback,
        )

    assert contention_observed.is_set()
    assert len(tasks) == 2
    assert all(task.done() for task in tasks)
    assert len(controllers) == 1
    assert cleanup_state["rollback_attempted"]
    assert cleanup_state["close_attempted"]
    assert cleanup_state["controller_closed"]
    assert cleanup_state["rollback_failed_but_close_succeeded"]
    assert cleanup_state["transaction_inactive"]
    assert cleanup_state["tasks_drained"]
    assert cleanup_state["contender_transactions_inactive"]
    assert cleanup_state["contender_sessions_closed"]
    assert not controllers[0].in_transaction()
    assert all(not session.in_transaction() for session in contender_sessions)

    async with db.AsyncSessionLocal() as fresh_session:
        locked_user = await asyncio.wait_for(
            UserRepository(fresh_session).get_by_id_for_update(user_id),
            timeout=2,
        )
        assert locked_user is not None
        await fresh_session.rollback()


async def _consume_contender(raw_token: str, ready: asyncio.Event, start: asyncio.Event):
    async with db.AsyncSessionLocal() as session:
        ready.set()
        await start.wait()
        row = await _kernel(session).consume(raw_token=raw_token)
        await session.commit()
        return None if row is None else row.id


@_postgres_only
async def test_simultaneous_consumers_have_exactly_one_winner_without_deadlock():
    user_id = await _seed_user()
    async with db.AsyncSessionLocal() as session:
        issued = await _kernel(session).issue(user_id=user_id)
        assert issued is not None
        raw_token, issued_row = issued
        await session.commit()
        token_id = issued_row.id

    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    start = asyncio.Event()
    async with db.AsyncSessionLocal() as controller:
        await UserRepository(controller).get_by_id_for_update(user_id)
        task_a = asyncio.create_task(_consume_contender(raw_token, ready_a, start))
        task_b = asyncio.create_task(_consume_contender(raw_token, ready_b, start))
        await asyncio.gather(ready_a.wait(), ready_b.wait())
        start.set()
        await asyncio.sleep(0.05)
        assert not task_a.done()
        assert not task_b.done()
        await controller.commit()

    results = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=10)
    assert sorted(result is None for result in results) == [False, True]
    assert token_id in results

    async with db.AsyncSessionLocal() as session:
        row = await session.get(PasswordRecoveryToken, token_id)
        assert row is not None
        assert row.consumed_at is not None
        assert row.invalidated_at is None


@_postgres_only
async def test_partial_unique_index_blocks_bypass_with_independent_sessions():
    user_id = await _seed_user()
    barrier = asyncio.Barrier(2)

    async def insert_directly():
        async with db.AsyncSessionLocal() as session:
            now = datetime.now(UTC)
            row = PasswordRecoveryToken(
                user_id=user_id,
                token_hash=hash_recovery_token(generate_recovery_token()),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
            session.add(row)
            await barrier.wait()
            try:
                await session.commit()
                return "committed"
            except IntegrityError:
                await session.rollback()
                return "conflict"

    outcomes = await asyncio.wait_for(
        asyncio.gather(insert_directly(), insert_directly()),
        timeout=10,
    )
    assert sorted(outcomes) == ["committed", "conflict"]

    async with db.AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PasswordRecoveryToken)
            .where(
                PasswordRecoveryToken.user_id == user_id,
                PasswordRecoveryToken.consumed_at.is_(None),
                PasswordRecoveryToken.invalidated_at.is_(None),
            )
        )
        assert count == 1


@_postgres_only
async def test_rollback_leaves_no_partial_consumption_or_invalidation():
    user_id = await _seed_user()
    async with db.AsyncSessionLocal() as session:
        issued = await _kernel(session).issue(user_id=user_id)
        assert issued is not None
        raw_token, issued_row = issued
        await session.commit()
        token_id = issued_row.id

    async with db.AsyncSessionLocal() as session:
        consumed = await _kernel(session).consume(raw_token=raw_token)
        assert consumed is not None
        assert consumed.consumed_at is not None
        await session.rollback()

    async with db.AsyncSessionLocal() as session:
        persisted = await session.get(PasswordRecoveryToken, token_id)
        assert persisted is not None
        assert persisted.consumed_at is None
        assert persisted.invalidated_at is None
        consumed = await _kernel(session).consume(raw_token=raw_token)
        assert consumed is not None
        await session.commit()


@_postgres_only
async def test_issuance_rollback_restores_prior_outstanding_token():
    user_id = await _seed_user()
    async with db.AsyncSessionLocal() as session:
        first = await _kernel(session).issue(user_id=user_id)
        assert first is not None
        first_raw, first_row = first
        await session.commit()
        first_id = first_row.id

    async with db.AsyncSessionLocal() as session:
        second = await _kernel(session).issue(user_id=user_id)
        assert second is not None
        _, second_row = second
        second_id = second_row.id
        assert second_id != first_id
        await session.rollback()

    async with db.AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(PasswordRecoveryToken).where(PasswordRecoveryToken.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.id for row in rows] == [first_id]
        assert rows[0].invalidated_at is None
        assert rows[0].consumed_at is None
        consumed = await _kernel(session).consume(raw_token=first_raw)
        assert consumed is not None
        await session.commit()
