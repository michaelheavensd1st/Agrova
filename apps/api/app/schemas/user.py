"""User schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class PermissionScopePublic(BaseModel):
    organization_id: UUID | None = None
    farm_id: UUID | None = None
    permissions: list[str] = Field(default_factory=list)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None = None
    is_active: bool
    is_verified: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime
    permissions: list[str] = Field(default_factory=list)
    permission_scopes: list[PermissionScopePublic] = Field(default_factory=list)


class AdminUserMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("reason must not be blank.")
        return reason


class AdminUserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None = None
    is_active: bool
    is_verified: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime


class AdminUserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AdminUserPage(BaseModel):
    items: list[AdminUserPublic]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AdminUserSessionsRevokeResponse(BaseModel):
    user: AdminUserPublic
    revoked_sessions: int = Field(ge=0)
