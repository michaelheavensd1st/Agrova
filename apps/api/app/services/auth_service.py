"""Authentication + verification service.

Sprint 1 policy:
* Only one active email-verification token exists per user at any time
  (older ones are invalidated when a new one is issued). A Postgres
  partial unique index (see migration
  ``0003_verification_active_unique_index``) enforces the invariant at
  the database level so concurrent resends cannot bypass it.
* Verification tokens expire — default 24 h, tunable via
  ``VERIFICATION_TOKEN_EXPIRE_HOURS``.
* Successful verification invalidates the used token AND any residual
  outstanding tokens for that user (belt-and-suspenders).
* Sensitive endpoints (``resend_verification`` and ``login``) are
  rate-limited per-email and per-IP through the :class:`RateLimiter`
  abstraction so tests / dev do not need Redis.

Note on tokens: this service issues **signed, structured JWTs** (not
"opaque" tokens). Each access/refresh/verify token is a signed JWT that
also has a server-side counterpart — refresh and verification tokens
are additionally persisted as SHA-256 **hashed records** so they can be
revoked, single-use-enforced, and audited independently of client-side
JWT validity.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.core.rate_limit import RateLimiter
from app.core.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.email.base import EmailMessage, EmailSender
from app.models.user import User
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository
from app.repositories.verification_repo import VerificationTokenRepository
from app.schemas.auth import TokenPair


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        refresh_repo: RefreshTokenRepository,
        verification_repo: VerificationTokenRepository,
        email_sender: EmailSender,
        rate_limiter: RateLimiter,
    ) -> None:
        self.user_repo = user_repo
        self.refresh_repo = refresh_repo
        self.verification_repo = verification_repo
        self.email_sender = email_sender
        self.rate_limiter = rate_limiter
        self.settings = get_settings()

    # ------------------------------------------------------------------ #
    # Register + verification
    # ------------------------------------------------------------------ #
    async def register(self, *, email: str, password: str, full_name: str | None = None) -> User:
        existing = await self.user_repo.get_by_email(email)
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "An account with that email already exists."
            )
        user = await self.user_repo.create(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
        )
        await self._issue_verification_email(user)
        return user

    async def resend_verification(self, *, email: str, ip_address: str | None = None) -> None:
        # Rate-limit BEFORE any account lookup so brute-force enumeration
        # cannot bypass the throttle.
        await self._enforce_resend_rate_limit(email=email, ip_address=ip_address)

        user = await self.user_repo.get_by_email(email)
        # Do not leak account existence — silently no-op if user not found
        # or already verified.
        if user is None or user.is_verified:
            return

        await self._issue_verification_email(user)

    async def verify_email(self, *, token: str) -> User:
        try:
            payload = decode_token(token, expected_type="verify")
        except (TokenExpiredError, TokenInvalidError) as exc:
            raise self._bad_token() from exc

        token_hash = _hash_token(token)
        row = await self.verification_repo.get_by_hash(token_hash)
        if row is None or row.is_used:
            raise self._bad_token()

        # Column may be naive on SQLite tests; normalise to UTC-aware.
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp < datetime.now(UTC):
            raise self._bad_token()

        try:
            user_id = UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise self._bad_token() from exc
        if row.user_id != user_id:
            raise self._bad_token()

        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise self._bad_token()

        # Consume the used token AND invalidate every other outstanding
        # token for this user in one shot — enforces the "at most one
        # active verification token" invariant even against races.
        await self.verification_repo.mark_used(row)
        await self.verification_repo.invalidate_all_for_user(user.id)

        user = await self.user_repo.mark_verified(user)
        return user

    async def _issue_verification_email(self, user: User) -> None:
        # Enforce single-active-token: invalidate any existing tokens
        # BEFORE issuing a new one.
        await self.verification_repo.invalidate_all_for_user(user.id)

        token, expires_at = create_token(
            subject=user.id,
            token_type="verify",
            extra_claims={"jti": secrets.token_urlsafe(16)},
        )
        await self.verification_repo.create(
            user_id=user.id, token_hash=_hash_token(token), expires_at=expires_at
        )
        verify_url = f"{self.settings.web_app_url.rstrip('/')}/verify?token={token}"
        await self.email_sender.send(
            EmailMessage(
                to=user.email,
                subject="Verify your Agrovix AgOS account",
                text_body=(
                    "Welcome to Agrovix AgOS!\n\n"
                    f"Please verify your email by opening:\n{verify_url}\n\n"
                    f"This link expires in {self.settings.verification_token_expire_hours} hours.\n"
                ),
                template="auth.verify_email",
                context={"verify_url": verify_url, "user_email": user.email},
            )
        )

    async def _enforce_resend_rate_limit(self, *, email: str, ip_address: str | None) -> None:
        window = self.settings.resend_verification_window_seconds
        # Per-email throttle (prevents mailbox bombing).
        allowed, retry = await self.rate_limiter.hit(
            key=f"resend-verify:email:{email.lower()}",
            limit=self.settings.resend_verification_max_per_email_hour,
            window_seconds=window,
        )
        if not allowed:
            raise self._too_many_requests(retry)
        # Per-IP throttle (prevents distributed enumeration).
        if ip_address:
            allowed, retry = await self.rate_limiter.hit(
                key=f"resend-verify:ip:{ip_address}",
                limit=self.settings.resend_verification_max_per_ip_hour,
                window_seconds=window,
            )
            if not allowed:
                raise self._too_many_requests(retry)

    async def _enforce_login_rate_limit(self, *, email: str, ip_address: str | None) -> None:
        """Rate-limit login attempts to defeat brute-force + enumeration.

        Keyed by BOTH the normalized email address (blunts credential
        stuffing against a specific account) and the client IP (blunts
        broad scanning). Error messages remain deliberately generic so
        that attackers cannot distinguish "wrong password" from "rate
        limited" from a valid response.
        """
        window = self.settings.login_window_seconds
        # Per-email throttle.
        allowed, retry = await self.rate_limiter.hit(
            key=f"login:email:{email.strip().lower()}",
            limit=self.settings.login_max_per_email_hour,
            window_seconds=window,
        )
        if not allowed:
            raise self._too_many_login_attempts(retry)
        # Per-IP throttle.
        if ip_address:
            allowed, retry = await self.rate_limiter.hit(
                key=f"login:ip:{ip_address}",
                limit=self.settings.login_max_per_ip_hour,
                window_seconds=window,
            )
            if not allowed:
                raise self._too_many_login_attempts(retry)

    # ------------------------------------------------------------------ #
    # Login / refresh / logout
    # ------------------------------------------------------------------ #
    async def login(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[User, TokenPair]:
        # Enforce rate limits BEFORE the account lookup so that both
        # brute-force password guessing and account enumeration are throttled
        # identically for existing and non-existent emails.
        await self._enforce_login_rate_limit(email=email, ip_address=ip_address)

        user = await self.user_repo.get_by_email_for_update(email)
        if user is None or user.hashed_password is None:
            raise self._invalid_credentials()
        if not verify_password(password, user.hashed_password):
            raise self._invalid_credentials()
        if not user.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled.")
        if not user.is_verified and not self.settings.allow_unverified_login:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Please verify your email before signing in."
            )

        tokens = await self._issue_token_pair(
            user=user, user_agent=user_agent, ip_address=ip_address
        )
        return user, tokens

    async def refresh(
        self,
        *,
        refresh_token: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> TokenPair:
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
        except (TokenExpiredError, TokenInvalidError) as exc:
            raise self._invalid_refresh() from exc

        token_hash = _hash_token(refresh_token)
        identity = await self.refresh_repo.resolve_identity_by_hash(token_hash)
        if identity is None:
            raise self._invalid_refresh()

        try:
            user_id = UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise self._invalid_refresh() from exc

        if identity.user_id != user_id:
            raise self._invalid_refresh()

        user = await self.user_repo.get_by_id_for_update(user_id)
        if user is None or not user.is_active:
            raise self._invalid_refresh()

        stored = await self.refresh_repo.get_by_id_for_update(
            token_id=identity.id,
            user_id=user.id,
        )
        if stored is None or stored.token_hash != token_hash or stored.is_revoked:
            raise self._invalid_refresh()
        # SQLite drops tz info — normalise before comparing.
        exp = stored.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp < datetime.now(UTC):
            raise self._invalid_refresh()

        await self.refresh_repo.revoke_rows([stored])
        return await self._issue_token_pair(user=user, user_agent=user_agent, ip_address=ip_address)

    async def logout(self, *, refresh_token: str) -> None:
        await self.refresh_repo.revoke_by_hash(_hash_token(refresh_token))

    async def _issue_token_pair(
        self,
        *,
        user: User,
        user_agent: str | None,
        ip_address: str | None,
    ) -> TokenPair:
        access_token, access_exp = create_token(
            subject=user.id,
            token_type="access",
            extra_claims={"email": user.email, "sv": user.session_version},
        )
        jti = secrets.token_urlsafe(16)
        refresh_token, refresh_exp = create_token(
            subject=user.id,
            token_type="refresh",
            extra_claims={"jti": jti},
        )
        await self.refresh_repo.create(
            user_id=user.id,
            token_hash=_hash_token(refresh_token),
            expires_at=refresh_exp,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        expires_in = int((access_exp - datetime.now(UTC)).total_seconds())
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=max(expires_in, 0),
        )

    def token_lifetimes(self) -> tuple[timedelta, timedelta]:
        return (
            timedelta(minutes=self.settings.jwt_access_token_expire_minutes),
            timedelta(days=self.settings.jwt_refresh_token_expire_days),
        )

    @staticmethod
    def _invalid_credentials() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _invalid_refresh() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _bad_token() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token.",
        )

    @staticmethod
    def _too_many_requests(retry_after: int) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification-resend requests. Please try again later.",
            headers={"Retry-After": str(max(retry_after, 1))},
        )

    @staticmethod
    def _too_many_login_attempts(retry_after: int) -> HTTPException:
        # Deliberately generic — do not confirm whether the account exists
        # or hint at whether credentials would have been valid.
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Please try again later.",
            headers={"Retry-After": str(max(retry_after, 1))},
        )
