"""Email sender factory."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.email.base import EmailSender
from app.email.log_sender import LogEmailSender
from app.email.resend_sender import ResendEmailSender


class EmailSenderUnavailableError(RuntimeError):
    """Configured transactional email delivery is unsafe or incomplete."""


@lru_cache(maxsize=1)
def get_email_sender() -> EmailSender:
    settings = get_settings()
    provider = settings.email_provider.lower()
    if provider == "log":
        if settings.is_production:
            raise EmailSenderUnavailableError("EMAIL_PROVIDER=log is not allowed in production.")
        return LogEmailSender()
    if provider == "resend":
        if not settings.resend_api_key:
            raise EmailSenderUnavailableError(
                "RESEND_API_KEY is required when EMAIL_PROVIDER=resend."
            )
        return ResendEmailSender(
            api_key=settings.resend_api_key,
            from_address=settings.email_from_address,
            from_name=settings.email_from_name,
        )
    raise EmailSenderUnavailableError(f"Unsupported EMAIL_PROVIDER: {provider}.")


__all__ = ["EmailSenderUnavailableError", "get_email_sender"]
