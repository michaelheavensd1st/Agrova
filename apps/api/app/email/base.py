"""EmailSender interface + a small message record.

Business code depends only on :class:`EmailSender`. Swapping in Resend,
SendGrid, or SES later is a one-line factory change — the domain layer
never has to be touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text_body: str
    html_body: str | None = None
    template: str | None = None
    context: dict[str, str] = field(default_factory=dict)


class EmailSender(Protocol):
    """Send transactional email. Implementations must be idempotent."""

    async def send(self, message: EmailMessage) -> None: ...


class EmailDeliveryError(RuntimeError):
    """A bounded provider failure safe for application-level handling."""


__all__ = ["EmailDeliveryError", "EmailMessage", "EmailSender"]
