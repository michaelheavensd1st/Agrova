"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit_factory import (
    RateLimiterUnavailableError,
    check_rate_limiter_health,
    get_rate_limiter,
)
from app.db.session import dispose_engine
from app.email.factory import EmailSenderUnavailableError, get_email_sender

configure_logging()
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "app.start",
        extra={"app": settings.app_name, "version": settings.app_version, "env": settings.app_env},
    )
    # Fail fast in production if the rate-limiter backend is misconfigured.
    # ``get_rate_limiter`` itself raises when Redis is required but missing;
    # here we additionally verify reachability (PING) so that a stale/dead
    # Redis is caught before we start serving traffic.
    try:
        get_rate_limiter()
        healthy, backend = await check_rate_limiter_health()
        if not healthy and settings.is_production and not settings.rate_limit_allow_inmemory:
            raise RuntimeError(
                f"Rate limiter backend unhealthy in production: {backend}",
            )
        logger.info("rate_limit.ready", extra={"backend": backend, "healthy": healthy})
    except RateLimiterUnavailableError as exc:
        logger.error("rate_limit.unavailable", extra={"error": str(exc)})
        raise
    try:
        sender = get_email_sender()
        logger.info("email.ready", extra={"provider": settings.email_provider.lower()})
        _ = sender
    except EmailSenderUnavailableError as exc:
        logger.error("email.unavailable", extra={"error": str(exc)})
        raise
    try:
        yield
    finally:
        logger.info("app.shutdown")
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.api_debug,
        docs_url="/docs" if settings.api_debug else None,
        redoc_url="/redoc" if settings.api_debug else None,
        openapi_url="/openapi.json" if settings.api_debug else None,
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", tags=["meta"], summary="Service banner")
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "environment": settings.app_env,
            "docs": "/docs",
        }

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    async def health() -> JSONResponse:
        # ``/health`` also checks the rate-limiter backend so that
        # orchestrators can gate traffic when Redis is down (in production).
        rl_healthy, rl_backend = await check_rate_limiter_health()
        overall_ok = rl_healthy or (
            not settings.is_production or settings.rate_limit_allow_inmemory
        )
        status_code = 200 if overall_ok else 503
        return JSONResponse(
            {
                "status": "ok" if overall_ok else "degraded",
                "rate_limiter": {"healthy": rl_healthy, "backend": rl_backend},
            },
            status_code=status_code,
        )

    @app.get("/version", tags=["meta"], summary="Version information")
    async def version() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.app_env,
            "api_prefix": settings.api_v1_prefix,
        }

    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
