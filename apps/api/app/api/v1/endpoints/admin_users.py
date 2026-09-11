"""Platform user-administration endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import (
    CurrentUser,
    DBSession,
    RequestCtx,
    get_admin_user_service,
    require_permission,
)
from app.models.user import User
from app.schemas.user import (
    AdminUserMutationRequest,
    AdminUserPage,
    AdminUserPublic,
    AdminUserSessionsRevokeResponse,
    AdminUserStatus,
)
from app.services.admin_user_service import AdminUserService

router = APIRouter()

_require_platform_admin = require_permission("platform.admin")
PlatformAdmin = Annotated[User, Depends(_require_platform_admin)]
ServiceDep = Annotated[AdminUserService, Depends(get_admin_user_service)]


async def require_directory_platform_admin(user: CurrentUser, session: DBSession) -> User:
    """Apply the canonical platform check without tenant query parameters."""
    return await _require_platform_admin(user=user, session=session)


DirectoryPlatformAdmin = Annotated[User, Depends(require_directory_platform_admin)]


@router.get("", response_model=AdminUserPage)
async def list_users(
    _admin: DirectoryPlatformAdmin,
    service: ServiceDep,
    search: str | None = Query(default=None, max_length=255),
    status_filter: AdminUserStatus | None = Query(default=None, alias="status"),
    verified: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminUserPage:
    is_active = None
    if status_filter is AdminUserStatus.ACTIVE:
        is_active = True
    elif status_filter is AdminUserStatus.DISABLED:
        is_active = False

    users, total = await service.list_directory(
        search=search,
        is_active=is_active,
        is_verified=verified,
        limit=limit,
        offset=offset,
    )
    return AdminUserPage(
        items=[AdminUserPublic.model_validate(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=AdminUserPublic)
async def inspect_user(
    user_id: uuid.UUID,
    _admin: PlatformAdmin,
    service: ServiceDep,
) -> AdminUserPublic:
    return AdminUserPublic.model_validate(await service.inspect(target_id=user_id))


@router.post("/{user_id}/disable", response_model=AdminUserPublic)
async def disable_user(
    user_id: uuid.UUID,
    payload: AdminUserMutationRequest,
    admin: PlatformAdmin,
    request_ctx: RequestCtx,
    service: ServiceDep,
) -> AdminUserPublic:
    target = await service.disable(
        actor=admin,
        target_id=user_id,
        reason=payload.reason,
        request_ctx=request_ctx,
    )
    return AdminUserPublic.model_validate(target)


@router.post("/{user_id}/enable", response_model=AdminUserPublic)
async def enable_user(
    user_id: uuid.UUID,
    payload: AdminUserMutationRequest,
    admin: PlatformAdmin,
    request_ctx: RequestCtx,
    service: ServiceDep,
) -> AdminUserPublic:
    target = await service.enable(
        actor=admin,
        target_id=user_id,
        reason=payload.reason,
        request_ctx=request_ctx,
    )
    return AdminUserPublic.model_validate(target)


@router.post("/{user_id}/sessions/revoke", response_model=AdminUserSessionsRevokeResponse)
async def revoke_user_sessions(
    user_id: uuid.UUID,
    payload: AdminUserMutationRequest,
    admin: PlatformAdmin,
    request_ctx: RequestCtx,
    service: ServiceDep,
) -> AdminUserSessionsRevokeResponse:
    target, revoked_sessions = await service.revoke_sessions(
        actor=admin,
        target_id=user_id,
        reason=payload.reason,
        request_ctx=request_ctx,
    )
    return AdminUserSessionsRevokeResponse(
        user=AdminUserPublic.model_validate(target),
        revoked_sessions=revoked_sessions,
    )
