"""The mail transport (ADR-0052): plain SMTP from the stdlib, configured from the environment.

Compose points it at **MailHog**, which accepts everything and delivers nothing, so a job's
email can be read at http://127.0.0.1:8025 without any address on the internet receiving it.
A real relay is the same four variables; STARTTLS and a login are used only when configured.

Unset ``IOTSHIELD_SMTP_HOST`` means *no mail at all*: ``mailer_from_env`` returns None and the
worker skips notifying, so host runs and tests need no SMTP server.
"""

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

DEFAULT_PORT = 25
DEFAULT_SENDER = "iotshield@localhost"
DEFAULT_TIMEOUT = 10.0


class Mailer(Protocol):
    """Sends one message to the recipients its headers name, or raises."""

    @property
    def sender(self) -> str: ...

    def send(self, message: EmailMessage) -> None: ...


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int = DEFAULT_PORT
    sender: str = DEFAULT_SENDER
    username: str | None = None
    password: str | None = None
    starttls: bool = False
    timeout: float = DEFAULT_TIMEOUT


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def settings_from_env(env: dict[str, str] | None = None) -> MailSettings | None:
    """The SMTP settings, or None when ``IOTSHIELD_SMTP_HOST`` is unset (mail is off)."""
    env = dict(os.environ) if env is None else env
    host = env.get("IOTSHIELD_SMTP_HOST", "").strip()
    if not host:
        return None
    port_text = env.get("IOTSHIELD_SMTP_PORT", "").strip()
    try:
        port = int(port_text) if port_text else DEFAULT_PORT
    except ValueError:
        raise ValueError(f"IOTSHIELD_SMTP_PORT must be a number, not {port_text!r}") from None
    return MailSettings(
        host=host,
        port=port,
        sender=env.get("IOTSHIELD_MAIL_FROM", "").strip() or DEFAULT_SENDER,
        username=env.get("IOTSHIELD_SMTP_USERNAME", "").strip() or None,
        password=env.get("IOTSHIELD_SMTP_PASSWORD") or None,
        starttls=_flag(env.get("IOTSHIELD_SMTP_STARTTLS")),
    )


@dataclass(frozen=True)
class SmtpMailer:
    settings: MailSettings

    @property
    def sender(self) -> str:
        return self.settings.sender

    def send(self, message: EmailMessage) -> None:
        s = self.settings
        with smtplib.SMTP(s.host, s.port, timeout=s.timeout) as smtp:
            if s.starttls:
                smtp.starttls()
            if s.username is not None and s.password is not None:
                smtp.login(s.username, s.password)
            smtp.send_message(message)


def mailer_from_env(env: dict[str, str] | None = None) -> SmtpMailer | None:
    settings = settings_from_env(env)
    return SmtpMailer(settings) if settings is not None else None
