"""Outbound notifications (ADR-0052): email the outcome of a run or a saved job's execution.

``mailer`` is the transport (SMTP via the stdlib — MailHog in compose), ``report`` the
message, ``outbox`` the background sender the Pipeline API posts to. The worker emails a job's
recipients; the API emails whoever ran a chain from Compose. The pipeline never sees either,
exactly as it never sees who ran it.
"""

from iotshield.notify.mailer import Mailer, MailSettings, SmtpMailer, mailer_from_env
from iotshield.notify.outbox import Outbox
from iotshield.notify.report import (
    TOP_ALERTS_IN_EMAIL,
    Outcome,
    RunCounts,
    compose_email,
    stored_run,
)

__all__ = [
    "TOP_ALERTS_IN_EMAIL",
    "MailSettings",
    "Mailer",
    "Outbox",
    "Outcome",
    "RunCounts",
    "SmtpMailer",
    "compose_email",
    "mailer_from_env",
    "stored_run",
]
