"""Release 6.0.5 Sprint 5.1 recovery-kernel persistence tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
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


def _kernel(session) -> PasswordRecoveryKernel:
    return PasswordRecoveryKernel(
        user_repo=UserRepository(session),
        token_repo=PasswordRecoveryTokenRepository(session),
    )


async def _user(db_session) -> User:
    user = User(
        email=f"recovery-{uuid4().hex}@example.test",
        hashed_password="not-used-in-sprint-5.1",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_token_generation_and_hash_contract():
    raw_token = generate_recovery_token()
    digest = hash_recovery_token(raw_token)

    assert len(raw_token) >= 43
    assert len(digest) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest != raw_token


@pytest.mark.parametrize("minutes", [14, 121])
async def test_recovery_expiry_configuration_rejects_out_of_range(minutes):
    with pytest.raises(ValidationError):
        Settings(password_recovery_token_expire_minutes=minutes)


async def test_issue_uses_configured_expiry_and_persists_only_hash(db_session):
    user = await _user(db_session)
    issued_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

    result = await _kernel(db_session).issue(user_id=user.id, now=issued_at)

    assert result is not None
    raw_token, row = result
    assert row.token_hash == hash_recovery_token(raw_token)
    assert row.expires_at == issued_at + timedelta(
        minutes=get_settings().password_recovery_token_expire_minutes
    )
    assert "token" not in {column.name for column in PasswordRecoveryToken.__table__.columns}


async def test_new_issue_invalidates_prior_outstanding_even_when_expired(db_session):
    user = await _user(db_session)
    first_at = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    first = await _kernel(db_session).issue(user_id=user.id, now=first_at)
    assert first is not None
    _, first_row = first

    second_at = first_row.expires_at + timedelta(minutes=1)
    second = await _kernel(db_session).issue(user_id=user.id, now=second_at)
    assert second is not None
    _, second_row = second

    await db_session.refresh(first_row)
    assert first_row.invalidated_at == second_at
    assert first_row.consumed_at is None
    assert second_row.invalidated_at is None
    assert second_row.consumed_at is None

    outstanding = list(
        (
            await db_session.execute(
                select(PasswordRecoveryToken).where(
                    PasswordRecoveryToken.user_id == user.id,
                    PasswordRecoveryToken.consumed_at.is_(None),
                    PasswordRecoveryToken.invalidated_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in outstanding] == [second_row.id]


async def test_consumption_is_single_use(db_session):
    user = await _user(db_session)
    issued_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    issued = await _kernel(db_session).issue(user_id=user.id, now=issued_at)
    assert issued is not None
    raw_token, row = issued

    consumed = await _kernel(db_session).consume(
        raw_token=raw_token,
        now=issued_at + timedelta(minutes=1),
    )
    replay = await _kernel(db_session).consume(
        raw_token=raw_token,
        now=issued_at + timedelta(minutes=2),
    )

    assert consumed is not None
    assert consumed.id == row.id
    assert consumed.consumed_at == issued_at + timedelta(minutes=1)
    assert replay is None


async def test_expired_token_is_not_consumed_or_implicitly_closed(db_session):
    user = await _user(db_session)
    issued_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    issued = await _kernel(db_session).issue(user_id=user.id, now=issued_at)
    assert issued is not None
    raw_token, row = issued

    consumed = await _kernel(db_session).consume(
        raw_token=raw_token,
        now=row.expires_at,
    )

    assert consumed is None
    await db_session.refresh(row)
    assert row.consumed_at is None
    assert row.invalidated_at is None


async def test_partial_unique_index_rejects_two_outstanding_rows(db_session):
    user = await _user(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            PasswordRecoveryToken(
                user_id=user.id,
                token_hash=hash_recovery_token(generate_recovery_token()),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            PasswordRecoveryToken(
                user_id=user.id,
                token_hash=hash_recovery_token(generate_recovery_token()),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_terminal_state_constraint_rejects_consumed_and_invalidated(db_session):
    user = await _user(db_session)
    now = datetime.now(UTC)
    db_session.add(
        PasswordRecoveryToken(
            user_id=user.id,
            token_hash=hash_recovery_token(generate_recovery_token()),
            created_at=now,
            expires_at=now + timedelta(hours=1),
            consumed_at=now,
            invalidated_at=now,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_expiry_constraint_rejects_non_positive_lifetime(db_session):
    user = await _user(db_session)
    now = datetime.now(UTC)
    db_session.add(
        PasswordRecoveryToken(
            user_id=user.id,
            token_hash=hash_recovery_token(generate_recovery_token()),
            created_at=now,
            expires_at=now,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_hash_constraint_rejects_non_sha256_text(db_session):
    user = await _user(db_session)
    now = datetime.now(UTC)
    db_session.add(
        PasswordRecoveryToken(
            user_id=user.id,
            token_hash="G" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_repository_transaction_mismatch_is_rejected(db_session, _engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(_engine) as other_session:
        with pytest.raises(ValueError, match="share one transaction"):
            PasswordRecoveryKernel(
                user_repo=UserRepository(db_session),
                token_repo=PasswordRecoveryTokenRepository(other_session),
            )
