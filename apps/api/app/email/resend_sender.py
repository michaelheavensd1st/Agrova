"""Narrow Resend adapter for transactional authentication email."""

from __future__ import annotations

import httpx

from app.email.base import EmailDeliveryError, EmailMessage, EmailSender


class ResendEmailSender(EmailSender):
    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        from_name: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._from = f"{from_name} <{from_address}>"
        self._timeout = timeout_seconds

    async def send(self, message: EmailMessage) -> None:
        payload: dict[str, object] = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text_body,
        }
        if message.html_body is not None:
            payload["html"] = message.html_body
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise EmailDeliveryError("Transactional email delivery failed.") from exc


__all__ = ["ResendEmailSender"]
