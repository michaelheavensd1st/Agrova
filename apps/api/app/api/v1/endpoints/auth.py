"""Authentication endpoints — cookie-first with body-token fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.cookies import clear_auth_cookies, set_auth_cookies
from app.core.trusted_proxy import get_client_ip
from app.deps import (
    CurrentUser,
    DBSession,
    RequestCtx,
    get_auth_service,
    get_password_recovery_service,
)
from app.email.base import EmailSender
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    PasswordRecoveryRequest,
    PasswordRecoveryResetRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    VerifyEmailRequest,
)
from app.schemas.common import MessageResponse
from app.schemas.user import PermissionScopePublic, UserPublic
from app.security.authorize import resolve_permission_scopes
from app.services.auth_service import AuthService
from app.services.password_recovery import (
    PasswordRecoveryService,
    RecoveryDelivery,
    deliver_recovery_email,
)

router = APIRouter()
_settings = get_settings()
logger = logging.getLogger("app.auth")

_RECOVERY_REQUEST_MESSAGE = (
    "If an eligible account exists, password recovery instructions will be sent."
)
_recovery_delivery_tasks: set[asyncio.Task[None]] = set()
_recovery_delivery_tails: dict[str, asyncio.Task[None]] = {}


async def _deliver_recovery_operational(
    *,
    email_sender: EmailSender,
    delivery: RecoveryDelivery,
    predecessor: asyncio.Task[None] | None,
) -> None:
    if predecessor is not None:
        await predecessor
    try:
        await deliver_recovery_email(
            email_sender=email_sender,
            settings=_settings,
            delivery=delivery,
        )
    except Exception as exc:
        # Operational metadata only: never log recipient, token, content,
        # authorization material, or whether an account was eligible.
        logger.warning(
            "auth.recovery.delivery.failed",
            extra={
                "provider": _settings.email_provider.lower(),
                "error_type": type(exc).__name__,
            },
        )


def _schedule_recovery_delivery(*, email_sender: EmailSender, delivery: RecoveryDelivery) -> None:
    """Keep password-recovery delivery alive without retaining its DB session.

    Issuance is committed before this is called. This deliberately small
    in-process handoff has an at-most-once gap: a process crash after commit
    and before/during delivery can leave a valid token whose email was not sent.
    """
    account_key = delivery.to.strip().lower()
    predecessor = _recovery_delivery_tails.get(account_key)
    task = asyncio.create_task(
        _deliver_recovery_operational(
            email_sender=email_sender,
            delivery=delivery,
            predecessor=predecessor,
        ),
        name="password-recovery-email-delivery",
    )
    _recovery_delivery_tasks.add(task)
    _recovery_delivery_tails[account_key] = task

    def release_references(completed: asyncio.Task[None]) -> None:
        _recovery_delivery_tasks.discard(completed)
        if _recovery_delivery_tails.get(account_key) is completed:
            _recovery_delivery_tails.pop(account_key, None)

    task.add_done_callback(release_references)


def _to_public(
    user: User,
    permissions: list[str] | None = None,
    permission_scopes: list[PermissionScopePublic] | None = None,
) -> UserPublic:
    return UserPublic.model_validate(
        {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "is_verified": user.is_verified,
            "is_superuser": user.is_superuser,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "permissions": permissions or [],
            "permission_scopes": permission_scopes or [],
        }
    )


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserPublic:
    user = await service.register(
        email=payload.email, password=payload.password, full_name=payload.full_name
    )
    return _to_public(user)


@router.post("/verify", response_model=UserPublic)
async def verify(
    payload: VerifyEmailRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserPublic:
    user = await service.verify_email(token=payload.token)
    return _to_public(user)


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    payload: ResendVerificationRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> MessageResponse:
    ip = get_client_ip(request)
    await service.resend_verification(email=payload.email, ip_address=ip)
    # Always OK to avoid leaking account existence.
    return MessageResponse(message="If the account exists, a verification email was sent.")


@router.post(
    "/recovery/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_recovery(
    payload: PasswordRecoveryRequest,
    request: Request,
    request_ctx: RequestCtx,
    session: DBSession,
    service: Annotated[PasswordRecoveryService, Depends(get_password_recovery_service)],
) -> MessageResponse:
    delivery = await service.prepare_request(
        email=payload.email,
        ip_address=get_client_ip(request),
        request_ctx=request_ctx,
    )
    if delivery is not None:
        # Issuance and its audit record must be durable before the raw token
        # crosses the delivery boundary.
        await session.commit()
        _schedule_recovery_delivery(email_sender=service.email_sender, delivery=delivery)
    return MessageResponse(message=_RECOVERY_REQUEST_MESSAGE)


@router.post("/recovery/reset", response_model=MessageResponse)
async def reset_password(
    payload: PasswordRecoveryResetRequest,
    response: Response,
    request_ctx: RequestCtx,
    service: Annotated[PasswordRecoveryService, Depends(get_password_recovery_service)],
) -> MessageResponse:
    await service.reset_password(
        raw_token=payload.token,
        new_password=payload.new_password,
        request_ctx=request_ctx,
    )
    clear_auth_cookies(response)
    return MessageResponse(message="Password reset successful. Please sign in again.")


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> JSONResponse:
    ua = request.headers.get("user-agent")
    ip = get_client_ip(request)
    _, tokens = await service.login(
        email=payload.email, password=payload.password, user_agent=ua, ip_address=ip
    )

    access_life, refresh_life = service.token_lifetimes()
    response = JSONResponse({"token_type": "bearer", "expires_in": tokens.expires_in})
    set_auth_cookies(
        response,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_max_age_s=int(access_life.total_seconds()),
        refresh_max_age_s=int(refresh_life.total_seconds()),
    )
    return response


@router.post("/refresh")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    cookie_refresh: Annotated[str | None, Cookie(alias=_settings.cookie_refresh_name)] = None,
) -> JSONResponse:
    refresh_token = payload.refresh_token or cookie_refresh
    if not refresh_token:
        return JSONResponse({"detail": "Missing refresh token."}, status_code=401)
    ua = request.headers.get("user-agent")
    ip = get_client_ip(request)
    tokens = await service.refresh(refresh_token=refresh_token, user_agent=ua, ip_address=ip)

    access_life, refresh_life = service.token_lifetimes()
    response = JSONResponse({"token_type": "bearer", "expires_in": tokens.expires_in})
    set_auth_cookies(
        response,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_max_age_s=int(access_life.total_seconds()),
        refresh_max_age_s=int(refresh_life.total_seconds()),
    )
    return response


@router.post("/logout", response_model=MessageResponse)
async def logout(
    payload: LogoutRequest,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    cookie_refresh: Annotated[str | None, Cookie(alias=_settings.cookie_refresh_name)] = None,
) -> MessageResponse:
    refresh_token = payload.refresh_token or cookie_refresh
    if refresh_token:
        await service.logout(refresh_token=refresh_token)
    clear_auth_cookies(response)
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=UserPublic)
async def me(user: CurrentUser, session: DBSession) -> UserPublic:
    scopes = await resolve_permission_scopes(session, user)
    public_scopes = [
        PermissionScopePublic(
            organization_id=scope.organization_id,
            farm_id=scope.farm_id,
            permissions=list(scope.permissions),
        )
        for scope in scopes
        if scope.organization_id is not None
    ]
    platform_permissions = next(
        (list(scope.permissions) for scope in scopes if scope.organization_id is None), []
    )
    return _to_public(user, platform_permissions, public_scopes)
