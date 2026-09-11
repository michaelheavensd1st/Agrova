"""Narrow delivery-adapter and sensitive log tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from app.core.logging import AgrovixLogHandler, JsonFormatter, configure_logging
from app.email import factory
from app.email.base import EmailDeliveryError, EmailMessage
from app.email.factory import EmailSenderUnavailableError
from app.email.log_sender import LogEmailSender
from app.email.resend_sender import ResendEmailSender

pytestmark = pytest.mark.asyncio


async def test_log_sender_redacts_recovery_content(caplog) -> None:
    raw = "raw-recovery-secret"
    with caplog.at_level(logging.INFO):
        await LogEmailSender().send(
            EmailMessage(
                to="person@example.test",
                subject="reset",
                text_body=f"https://example.test/reset?token={raw}",
                template="auth.password_recovery",
                context={"reset_url": f"https://example.test/reset?token={raw}"},
            )
        )
    assert raw not in caplog.text
    assert any(
        getattr(record, "sensitive_content", None) == "redacted" for record in caplog.records
    )


async def test_logging_configuration_preserves_capture_and_is_idempotent(caplog, capsys) -> None:
    raw = "raw-recovery-secret"
    root = logging.getLogger()
    external_handlers = tuple(
        handler for handler in root.handlers if not isinstance(handler, AgrovixLogHandler)
    )

    configure_logging()
    configure_logging()

    assert all(handler in root.handlers for handler in external_handlers)
    assert sum(isinstance(handler, AgrovixLogHandler) for handler in root.handlers) == 1

    with caplog.at_level(logging.INFO):
        await LogEmailSender().send(
            EmailMessage(
                to="person@example.test",
                subject="reset",
                text_body=f"https://example.test/reset?token={raw}",
                template="auth.password_recovery",
                context={"reset_url": f"https://example.test/reset?token={raw}"},
            )
        )

    assert raw not in caplog.text
    redaction_record = next(
        record
        for record in caplog.records
        if getattr(record, "sensitive_content", None) == "redacted"
    )
    formatted = JsonFormatter().format(redaction_record)
    assert '"message":"email.dispatch"' in formatted
    assert '"template":"auth.password_recovery"' in formatted
    assert '"sensitive_content":"redacted"' in formatted
    assert raw not in formatted
    assert raw not in capsys.readouterr().out


async def test_resend_adapter_uses_authorization_and_wraps_failures(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("rejected", request=None, response=None)

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    sender = ResendEmailSender(
        api_key="test-key", from_address="noreply@example.test", from_name="Agrovix"
    )
    with pytest.raises(EmailDeliveryError):
        await sender.send(
            EmailMessage(to="person@example.test", subject="subject", text_body="body")
        )
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["to"] == ["person@example.test"]


@pytest.mark.parametrize(
    ("provider", "production", "api_key"),
    [("log", True, None), ("unsupported", False, None), ("resend", False, None)],
)
async def test_sender_factory_rejects_unsafe_configuration(
    monkeypatch, provider: str, production: bool, api_key: str | None
) -> None:
    factory.get_email_sender.cache_clear()
    settings = SimpleNamespace(
        email_provider=provider,
        is_production=production,
        resend_api_key=api_key,
        email_from_address="noreply@example.com",
        email_from_name="Agrovix",
    )
    monkeypatch.setattr(factory, "get_settings", lambda: settings)
    with pytest.raises(EmailSenderUnavailableError):
        factory.get_email_sender()
    factory.get_email_sender.cache_clear()
