"""Safety regressions for the destructive PostgreSQL test bootstrap."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import (
    _programmatic_alembic_config,
    _upgrade_postgres_to_head,
    _validate_disposable_postgres_urls,
)

LOCAL_ASYNC = "postgresql+asyncpg://tester:secret@127.0.0.1:5432/agrovix_validation"
LOCAL_SYNC = "postgresql+psycopg2://tester:secret@127.0.0.1:5432/agrovix_validation"


def test_postgres_reset_guard_accepts_matching_local_validation_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    _validate_disposable_postgres_urls(LOCAL_ASYNC, LOCAL_SYNC)


def test_postgres_reset_guard_normalizes_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    _validate_disposable_postgres_urls(
        LOCAL_ASYNC,
        "postgresql+psycopg2://tester:secret@127.0.0.1/agrovix_validation",
    )


@pytest.mark.parametrize(
    ("async_url", "sync_url", "app_env"),
    [
        (LOCAL_ASYNC, LOCAL_SYNC, "production"),
        (
            "postgresql+asyncpg://tester:secret@db.example.com:5432/agrovix_test",
            "postgresql+psycopg2://tester:secret@db.example.com:5432/agrovix_test",
            "test",
        ),
        (
            "postgresql+asyncpg://tester:secret@127.0.0.1:5432/agrovix",
            "postgresql+psycopg2://tester:secret@127.0.0.1:5432/agrovix",
            "test",
        ),
        (
            LOCAL_ASYNC,
            "postgresql+psycopg2://tester:secret@127.0.0.1:5432/other_test",
            "test",
        ),
        (
            LOCAL_ASYNC,
            "postgresql+psycopg2://tester:secret@127.0.0.1:5433/agrovix_validation",
            "test",
        ),
    ],
)
def test_postgres_reset_guard_rejects_unsafe_targets(
    monkeypatch: pytest.MonkeyPatch,
    async_url: str,
    sync_url: str,
    app_env: str,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    with pytest.raises(RuntimeError):
        _validate_disposable_postgres_urls(async_url, sync_url)


def test_programmatic_alembic_config_avoids_ini_logging_configuration() -> None:
    config = _programmatic_alembic_config()
    script_location = config.get_main_option("script_location")

    assert config.config_file_name is None
    assert script_location is not None
    assert Path(script_location).resolve() == Path(__file__).resolve().parents[1] / "alembic"


def test_repeated_programmatic_upgrade_preserves_host_logging_state(_engine: object) -> None:
    if not os.environ["DATABASE_URL"].startswith("postgresql"):
        pytest.skip("Requires the PostgreSQL test bootstrap")

    root = logging.getLogger()
    app_email = logging.getLogger("app.email")
    handlers_before = tuple(root.handlers)
    root_level_before = root.level
    app_email_state_before = (
        app_email.disabled,
        app_email.level,
        app_email.propagate,
    )

    _upgrade_postgres_to_head()
    _upgrade_postgres_to_head()

    assert tuple(root.handlers) == handlers_before
    assert root.level == root_level_before
    assert (app_email.disabled, app_email.level, app_email.propagate) == app_email_state_before

    engine = create_engine(os.environ["DATABASE_URL_SYNC"], future=True)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()
    assert revision == "0016_session_version"
