"""Shared pytest fixtures — SQLite-backed for hermetic unit tests.

The canonical Postgres path is exercised in CI via the `api-quality` job
(see `.github/workflows/ci.yml`). These fixtures keep the local test
loop hermetic and fast — no Postgres or Redis process required.

They use SQLite through SQLAlchemy's Async engine with the
``check_same_thread=False`` pragma; the JSONB column is transparently
mapped to plain JSON, and the Postgres enums degrade to Python enums.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

# Deterministic + safe defaults BEFORE any app imports. External
# environment variables (e.g. from the CI Postgres integration job)
# take precedence — `setdefault` never overwrites an existing value.
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("COOKIE_SAMESITE", "lax")
os.environ.setdefault("ALLOW_UNVERIFIED_LOGIN", "true")  # verify flow tested explicitly
os.environ.setdefault("EMAIL_PROVIDER", "log")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DATABASE_URL_SYNC", "sqlite:///:memory:")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from alembic import command
from alembic.config import Config

# Reset cached settings so env overrides take effect.
from app.core.config import get_settings

get_settings.cache_clear()

# Force the process-wide rate limiter to be in-memory during tests —
# Redis is not available in the hermetic suite.
from app.core import rate_limit_factory  # noqa: E402
from app.core.rate_limit import InMemoryRateLimiter  # noqa: E402

rate_limit_factory.get_rate_limiter.cache_clear()
_test_rate_limiter = InMemoryRateLimiter()
rate_limit_factory.get_rate_limiter = lambda: _test_rate_limiter  # type: ignore[assignment]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_shared_rate_limiter():
    """Reset the shared in-memory limiter between tests.

    Individual tests that need cumulative rate-limit behavior install
    their own fresh limiter (see ``test_verification.py`` and
    ``test_login_security.py``). This fixture keeps other tests hermetic
    so that ``login``/``refresh``/``logout`` don't burn cross-test quota
    and flake randomly.
    """
    with contextlib.suppress(AttributeError):
        _test_rate_limiter._counters.clear()  # type: ignore[attr-defined]
    yield


# Patch the JSONB column used in AuditEvent → JSON when running against
# SQLite (unit-test default). When DATABASE_URL points at Postgres this
# patch is a no-op so real JSONB behaviour is exercised.
if os.environ["DATABASE_URL"].startswith("sqlite"):
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB

    JSONB.impl = JSON  # type: ignore[attr-defined]

from app.db import session as db_session_module  # noqa: E402
from app.deps import get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.seed import seed_permissions_and_roles  # noqa: E402


def _validate_disposable_postgres_urls(database_url: str, database_url_sync: str) -> None:
    """Refuse destructive test bootstrap unless both URLs identify one local test DB."""
    async_url = make_url(database_url)
    sync_url = make_url(database_url_sync)
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    test_markers = ("test", "testing", "pytest", "validation")
    default_postgres_port = 5432

    if os.environ.get("APP_ENV", "").lower() != "test":
        raise RuntimeError("PostgreSQL test reset requires APP_ENV=test")
    if not async_url.drivername.startswith("postgresql"):
        raise RuntimeError("PostgreSQL test reset requires a PostgreSQL DATABASE_URL")
    if not sync_url.drivername.startswith("postgresql"):
        raise RuntimeError("PostgreSQL test reset requires a PostgreSQL DATABASE_URL_SYNC")
    if async_url.host not in allowed_hosts or sync_url.host not in allowed_hosts:
        raise RuntimeError("PostgreSQL test reset is restricted to loopback hosts")
    if (
        async_url.database != sync_url.database
        or async_url.host != sync_url.host
        or (async_url.port or default_postgres_port) != (sync_url.port or default_postgres_port)
    ):
        raise RuntimeError("PostgreSQL test URLs must target the same local database")
    database_name = (async_url.database or "").lower()
    if not any(marker in database_name for marker in test_markers):
        raise RuntimeError("PostgreSQL test database name must contain an explicit test marker")


def _reset_and_migrate_postgres(database_url: str, database_url_sync: str) -> None:
    """Recreate the disposable public schema, then bootstrap it through Alembic."""
    _validate_disposable_postgres_urls(database_url, database_url_sync)
    sync_engine = create_engine(database_url_sync, future=True)
    try:
        with sync_engine.begin() as connection:
            current_database = connection.scalar(text("SELECT current_database()"))
            if current_database != make_url(database_url_sync).database:
                raise RuntimeError("Connected PostgreSQL database does not match the reset target")
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        sync_engine.dispose()

    _upgrade_postgres_to_head()


def _programmatic_alembic_config() -> Config:
    """Build Alembic config without applying ``alembic.ini`` logging state."""
    api_root = Path(__file__).resolve().parents[1]
    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(api_root / "alembic"))
    return alembic_config


def _upgrade_postgres_to_head() -> None:
    """Run test-database migrations without reconfiguring host logging."""
    command.upgrade(_programmatic_alembic_config(), "head")


# NOTE — event-loop management is handled entirely by pytest-asyncio
# (>=0.23) via the ``asyncio_default_fixture_loop_scope = "session"``
# and ``asyncio_default_test_loop_scope = "session"`` settings in
# pyproject.toml. Defining a custom ``event_loop`` fixture here is
# deprecated in pytest-asyncio 1.x and reintroduces "Future attached
# to a different loop" errors when session-scoped async DB fixtures
# (``_engine``) are shared with function-scoped tests running on
# their own per-test loop. Keep this comment as a marker so the
# fixture is not accidentally re-added.


@pytest_asyncio.fixture(scope="session")
async def _engine():
    database_url = os.environ["DATABASE_URL"]
    database_url_sync = os.environ["DATABASE_URL_SYNC"]
    is_sqlite = database_url.startswith("sqlite")

    if not is_sqlite:
        _reset_and_migrate_postgres(database_url, database_url_sync)

    engine_kwargs: dict = {"future": True}
    if is_sqlite:
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        engine_kwargs["poolclass"] = StaticPool

    engine = create_async_engine(database_url, **engine_kwargs)

    if is_sqlite:
        # Enable FK enforcement for SQLite (off by default).
        @event.listens_for(engine.sync_engine, "connect")
        def _fk_pragma_on_connect(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async with engine.begin() as conn:
        if is_sqlite:
            await conn.run_sync(Base.metadata.create_all)

    # Swap in our engine + session factory
    db_session_module.engine = engine
    db_session_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )

    await seed_permissions_and_roles()

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine) -> AsyncSession:
    async with db_session_module.AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(_engine) -> AsyncClient:
    # Route the app's DB dep through our shared engine.
    async def _override_get_db_session():
        async with db_session_module.AsyncSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_session] = _override_get_db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
