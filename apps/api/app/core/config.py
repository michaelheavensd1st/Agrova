"""Application settings (env-driven, cached)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Meta ---
    app_name: str = Field(default="Agrovix AgOS API")
    app_version: str = Field(default="0.1.0")
    app_env: str = Field(default="development")
    api_debug: bool = Field(default=True)

    # --- HTTP ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    api_prefix: str = Field(default="/api")
    api_v1_prefix: str = Field(default="/api/v1")
    api_cors_origins: str = Field(default="http://localhost:3000")

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://agrovix:agrovix_dev@localhost:5432/agrovix_agos",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://agrovix:agrovix_dev@localhost:5432/agrovix_agos",
    )
    database_pool_size: int = Field(default=10)
    database_max_overflow: int = Field(default=20)
    database_echo: bool = Field(default=False)

    # --- Redis ---
    # Default is empty so that "missing REDIS_URL in production" reliably
    # triggers the fail-fast path in ``rate_limit_factory.get_rate_limiter``.
    # Dev + tests set this explicitly via ``.env`` / ``docker-compose.yml``.
    redis_url: str = Field(default="")

    # --- JWT / Security ---
    jwt_secret_key: str = Field(default="change-me-in-production")
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_token_expire_minutes: int = Field(default=15)
    jwt_refresh_token_expire_days: int = Field(default=30)
    jwt_issuer: str = Field(default="agrovix-agos")
    jwt_audience: str = Field(default="agrovix-agos-clients")

    password_min_length: int = Field(default=8)
    bcrypt_rounds: int = Field(default=12)

    # --- Cookies (web canonical auth transport) ---
    cookie_domain: str | None = Field(default=None)
    cookie_secure: bool = Field(default=True)
    cookie_samesite: str = Field(default="lax")  # 'lax' | 'strict' | 'none'
    cookie_access_name: str = Field(default="agrovix_access")
    cookie_refresh_name: str = Field(default="agrovix_refresh")

    # --- Email (development sender by default) ---
    email_from_address: str = Field(default="no-reply@agrovix.dev")
    email_from_name: str = Field(default="Agrovix AgOS")
    email_provider: str = Field(default="log")  # 'log' | 'resend'
    resend_api_key: str | None = Field(default=None)
    web_app_url: str = Field(default="http://localhost:3000")
    verification_token_expire_hours: int = Field(default=24)
    invitation_token_expire_days: int = Field(default=14)
    password_recovery_token_expire_minutes: int = Field(default=60, ge=15, le=120)

    # --- Rate limiting: password recovery ---
    password_recovery_request_max_per_email_hour: int = Field(default=3, ge=1)
    password_recovery_request_max_per_ip_hour: int = Field(default=10, ge=1)
    password_recovery_request_window_seconds: int = Field(default=3600, ge=1)

    # --- Auth policy ---
    allow_unverified_login: bool = Field(default=False)  # dev override

    # --- Trusted proxy / X-Forwarded-For policy ---
    # Comma-separated list of IPs or CIDRs that we trust to set the
    # forwarding header. Empty (default) => header is IGNORED completely.
    # See ``app/core/trusted_proxy.py`` for the full policy.
    trusted_proxies: str = Field(default="")
    trusted_proxy_header: str = Field(default="x-forwarded-for")

    # --- Rate limiting: transport policy ---
    # In production, the process-wide limiter MUST be Redis-backed so that
    # limits are shared across API workers. Silent fallback to the in-memory
    # limiter is a security downgrade and is disabled by default. Set
    # ``RATE_LIMIT_ALLOW_INMEMORY=true`` only when you understand the risk
    # (e.g. single-process staging).
    rate_limit_allow_inmemory: bool = Field(default=False)

    # --- Rate limiting: resend-verification abuse guard ---
    resend_verification_max_per_email_hour: int = Field(default=3)
    resend_verification_max_per_ip_hour: int = Field(default=10)
    resend_verification_window_seconds: int = Field(default=3600)

    # --- Rate limiting: login (brute-force + enumeration guard) ---
    login_max_per_email_hour: int = Field(default=10)
    login_max_per_ip_hour: int = Field(default=30)
    login_window_seconds: int = Field(default=3600)

    # --- Rate limiting: invitation acceptance ---
    invitation_accept_max_per_user_hour: int = Field(default=20)
    invitation_accept_max_per_ip_hour: int = Field(default=60)
    invitation_accept_window_seconds: int = Field(default=3600)

    @field_validator("api_cors_origins")
    @classmethod
    def _strip_cors(cls, value: str) -> str:
        return value.strip()

    @field_validator("cookie_samesite")
    @classmethod
    def _validate_samesite(cls, value: str) -> str:
        v = value.lower()
        if v not in {"lax", "strict", "none"}:
            raise ValueError("cookie_samesite must be one of: lax, strict, none")
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
