"""Email a Compose run's outcome to whoever ran it (ADR-0052 amendment).

A job emails its own recipient list from the worker; a run posted to ``POST /runs`` emails the
one person who pressed *Run*, when they left ``email_result`` on and their username is an
address (an account made before ADR-0052 may not be). Two outcomes are mailed:

- **finished** — the stored run's counts, the head of its queue and a link, read back from
  storage in the outbox's thread, after the run's transaction has committed;
- **failed while running** — a 5xx: a pulled source's database went away (502), or a sink
  failed writing (500). The message is the one the response carries.

A 4xx is not mailed: the chain was refused before anything ran, and the page that asked is
showing why. Nothing here can fail the request — see ``notify/outbox.py``.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import cast

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker

from iotshield.notify import (
    TOP_ALERTS_IN_EMAIL,
    Outbox,
    Outcome,
    RunCounts,
    compose_email,
    stored_run,
)
from iotshield.pipeline.runner import RunReport
from iotshield.storage.accounts import validate_email

UNNAMED = "Unnamed run"


def recipient_for(username: str, *, wanted: bool) -> str | None:
    """Who a run's email goes to: the runner, if they asked and their username is an address."""
    if not wanted:
        return None
    try:
        return validate_email(username)
    except ValueError:
        return None


def _failure_message(exc: HTTPException) -> str:
    detail: object = exc.detail
    if isinstance(detail, dict):
        message = cast(dict[str, object], detail).get("message")
        if isinstance(message, str):
            return message
    return str(detail)


def post_finished(
    outbox: Outbox,
    *,
    session_factory: sessionmaker[Session],
    dashboard_url: str | None,
    org_id: str,
    to: str,
    report: RunReport,
    requested_name: str | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Queue the email for a run that finished: what was stored, read once committed."""
    finished_at = clock()

    def compose(sender: str) -> EmailMessage:
        with session_factory() as session:
            counts, top = stored_run(session, org_id=org_id, run_id=report.run_id)
        if counts is None:  # no Postgres sink: say what the run itself returned
            counts = RunCounts(
                run_id=report.run_id,
                name=requested_name or report.origin or UNNAMED,
                events=report.events,
                malformed=report.malformed,
                detections=report.detections,
                alerts=report.alerts,
            )
            top = report.top_alerts[:TOP_ALERTS_IN_EMAIL]
        outcome = Outcome(
            kind="run",
            name=counts.name,
            trigger="manual",
            requested_by=to,
            finished_at=finished_at,
            run=counts,
            top_alerts=top,
        )
        return compose_email(outcome, sender=sender, recipients=[to], dashboard_url=dashboard_url)

    outbox.post(compose)


def post_failed(
    outbox: Outbox,
    *,
    to: str,
    exc: HTTPException,
    requested_name: str | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Queue the email for a run that failed while running (a 5xx); a 4xx is not mailed."""
    if exc.status_code < 500:
        return
    outcome = Outcome(
        kind="run",
        name=requested_name or UNNAMED,
        trigger="manual",
        requested_by=to,
        finished_at=clock(),
        error=_failure_message(exc),
    )
    outbox.post(lambda sender: compose_email(outcome, sender=sender, recipients=[to]))
