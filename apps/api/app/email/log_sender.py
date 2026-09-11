"""Development EmailSender — logs the outbound message instead of sending.

The verification/invitation URLs are printed to the JSON access log so a
developer can copy them directly from the terminal.
"""

from __future__ import annotations

import logging

from app.email.base import EmailMessage, EmailSender

_logger = logging.getLogger("app.email")


class LogEmailSender(EmailSender):
    async def send(self, message: EmailMessage) -> None:
        if message.template == "auth.password_recovery":
            _logger.info(
                "email.dispatch",
                extra={
                    "template": message.template,
                    "sensitive_content": "redacted",
                },
            )
            return
        _logger.info(
            "email.dispatch",
            extra={
                "to": message.to,
                "subject": message.subject,
                "template": message.template,
                "context": message.context,
                "body": message.text_body,
            },
        )
