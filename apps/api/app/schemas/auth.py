"""Authentication request / response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.config import get_settings

_settings = get_settings()


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr
    password: str = Field(..., min_length=_settings.password_min_length, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=10)


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=10)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class PasswordRecoveryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr


class PasswordRecoveryResetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    token: str = Field(..., min_length=32, max_length=256)
    new_password: str = Field(..., min_length=_settings.password_min_length, max_length=128)


class CookieAuthResponse(BaseModel):
    """Browser response; credentials are carried only by httpOnly cookies."""

    token_type: str = Field(default="bearer")
    expires_in: int


class TokenPair(CookieAuthResponse):
    """Explicit bearer transport response for native clients."""

    access_token: str
    refresh_token: str
