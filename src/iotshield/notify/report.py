"""The email a finished run or job execution sends (ADR-0052).

One message per outcome, success or failure alike: a job's execution goes to the job's
recipients — a nightly job that silently stopped producing runs is exactly what a mailbox
should hear about — and a run started from Compose to whoever started it, when they asked for
it (``RunRequest.email_result``, ADR-0052 amendment). It carries
what the Run page's tiles say (the four counts), the head of the triage queue, and a link to
the run; never the data itself, which stays behind the login.

Plain text first, with an HTML alternative of the same content: a mail client that renders
neither colour nor tables still reads the whole report.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from iotshield.contracts import PrioritizedAlert
from iotshield.storage import repository, runs

TOP_ALERTS_IN_EMAIL = 5

Kind = Literal["job", "run"]


@dataclass(frozen=True)
class RunCounts:
    """What the run stored: the same four numbers as the Run page's tiles."""

    run_id: UUID
    name: str
    events: int
    malformed: int
    detections: int
    alerts: int


@dataclass(frozen=True)
class Outcome:
    """How one run or job execution ended, as much as the email needs to say.

    ``kind`` decides the wording: a ``job`` is named after the job and says how it was
    triggered; a ``run`` (started from Compose) is named after the run and says who ran it.
    """

    kind: Kind
    name: str
    trigger: str  # "schedule" | "manual"; a run is always "manual"
    requested_by: str | None
    finished_at: datetime
    error: str | None = None  # None = it succeeded
    run: RunCounts | None = None
    top_alerts: Sequence[PrioritizedAlert] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return self.error is None


def stored_run(
    session: Session, *, org_id: str, run_id: UUID
) -> tuple[RunCounts | None, list[PrioritizedAlert]]:
    """What the email reports about a stored run: its counts and the head of its queue.

    Read back from storage rather than taken from what the run returned, so the email says
    what anyone opening the link will see. ``None`` counts when no ``runs`` row was written
    (a chain with no Postgres sink).
    """
    saved = runs.get_run(session, run_id, org_id=org_id)
    counts = (
        None
        if saved is None
        else RunCounts(
            run_id=saved.run_id,
            name=saved.name,
            events=saved.events,
            malformed=saved.malformed,
            detections=saved.detections,
            alerts=saved.alerts,
        )
    )
    top = repository.get_alerts(session, org_id=org_id, run_id=run_id, limit=TOP_ALERTS_IN_EMAIL)
    return counts, list(top)


def _label(outcome: Outcome) -> str:
    return "Job" if outcome.kind == "job" else "Run"


def _subject(outcome: Outcome) -> str:
    if not outcome.succeeded:
        return f"[iotshield] {_label(outcome)} '{outcome.name}' failed"
    alerts = outcome.run.alerts if outcome.run is not None else 0
    plural = "" if alerts == 1 else "s"
    return f"[iotshield] {_label(outcome)} '{outcome.name}' finished: {alerts} alert{plural}"


def _how(outcome: Outcome) -> str:
    if outcome.kind == "run":
        return f"run by {outcome.requested_by}" if outcome.requested_by else "run from Compose"
    if outcome.trigger == "manual" and outcome.requested_by:
        return f"run now by {outcome.requested_by}"
    return "on its schedule" if outcome.trigger == "schedule" else "on request"


def _run_url(dashboard_url: str | None, run_id: UUID) -> str | None:
    if not dashboard_url:
        return None
    return f"{dashboard_url.rstrip('/')}/runs/{run_id}"


def _alert_line(alert: PrioritizedAlert) -> str:
    threat = f" - {alert.threat_class}" if alert.threat_class != "unknown" else ""
    return (
        f"{alert.priority_score:5.1f}  {alert.severity.value.upper():<8}  {alert.asset_id}{threat}"
    )


def _why_you(outcome: Outcome) -> str:
    if outcome.kind == "job":
        return "you are listed on this job"
    return "you ran it with Email me the result ticked"


def _text(outcome: Outcome, url: str | None) -> str:
    when = f"{outcome.finished_at:%Y-%m-%d %H:%M} UTC"
    lines = [f"{_label(outcome)}: {outcome.name}", f"Finished: {when} ({_how(outcome)})", ""]
    if not outcome.succeeded:
        lines += [
            f"The {outcome.kind} FAILED; nothing it read was stored.",
            "",
            f"Why: {outcome.error}",
        ]
    elif outcome.run is not None:
        run = outcome.run
        if outcome.kind == "job":
            lines += [f"Run: {run.name}", ""]
        lines += [
            f"  events      {run.events}",
            f"  malformed   {run.malformed}",
            f"  detections  {run.detections}",
            f"  alerts      {run.alerts}",
            "",
        ]
        if outcome.top_alerts:
            lines.append(f"Top {len(outcome.top_alerts)} alerts (priority, severity, asset):")
            lines += [f"  {_alert_line(alert)}" for alert in outcome.top_alerts]
        else:
            lines.append("No alerts.")
        if url is not None:
            lines += ["", f"Open the run: {url}"]
    lines += ["", "-- ", f"iotshield: you receive this because {_why_you(outcome)}."]
    return "\n".join(lines) + "\n"


def _html(outcome: Outcome, url: str | None) -> str:
    e = escape
    when = f"{outcome.finished_at:%Y-%m-%d %H:%M} UTC"
    parts = [
        '<html><body style="font-family:sans-serif;color:#1f2933">',
        f'<h2 style="margin:0 0 4px">{_label(outcome)}: {e(outcome.name)}</h2>',
        f'<p style="margin:0 0 16px;color:#52606d">Finished {e(when)} ({e(_how(outcome))})</p>',
    ]
    if not outcome.succeeded:
        parts += [
            f'<p><strong style="color:#b42318">The {outcome.kind} failed</strong>; '
            "nothing it read was stored.</p>",
            f"<p>Why: {e(outcome.error or '')}</p>",
        ]
    elif outcome.run is not None:
        run = outcome.run
        cells = "".join(
            f'<td style="padding:4px 16px 4px 0"><div style="color:#52606d">{label}</div>'
            f'<div style="font-size:20px"><strong>{value}</strong></div></td>'
            for label, value in (
                ("events", run.events),
                ("malformed", run.malformed),
                ("detections", run.detections),
                ("alerts", run.alerts),
            )
        )
        if outcome.kind == "job":
            parts.append(f"<p>Run: {e(run.name)}</p>")
        parts.append(f"<table><tr>{cells}</tr></table>")
        if outcome.top_alerts:
            rows = "".join(
                "<tr>"
                f'<td style="padding:2px 12px 2px 0">{alert.priority_score:.1f}</td>'
                f'<td style="padding:2px 12px 2px 0">{e(alert.severity.value.upper())}</td>'
                f'<td style="padding:2px 12px 2px 0">{e(alert.asset_id)}</td>'
                f'<td style="padding:2px 0">{e(alert.threat_class)}</td>'
                "</tr>"
                for alert in outcome.top_alerts
            )
            parts += [
                f'<h3 style="margin:16px 0 4px">Top {len(outcome.top_alerts)} alerts</h3>',
                '<table style="border-collapse:collapse"><tr style="color:#52606d">'
                "<th align=left>priority</th><th align=left>severity</th>"
                "<th align=left>asset</th><th align=left>threat</th></tr>"
                f"{rows}</table>",
            ]
        else:
            parts.append("<p>No alerts.</p>")
        if url is not None:
            parts.append(f'<p><a href="{e(url, quote=True)}">Open the run</a></p>')
    parts.append(
        '<p style="color:#7b8794;font-size:12px;margin-top:24px">'
        f"iotshield: you receive this because {e(_why_you(outcome))}.</p></body></html>"
    )
    return "".join(parts)


def compose_email(
    outcome: Outcome,
    *,
    sender: str,
    recipients: Sequence[str],
    dashboard_url: str | None = None,
) -> EmailMessage:
    """The message for ``outcome``, addressed to ``recipients``; refuses an empty list."""
    if not recipients:
        raise ValueError("an email needs at least one recipient")
    url = _run_url(dashboard_url, outcome.run.run_id) if outcome.run is not None else None
    message = EmailMessage()
    message["Subject"] = _subject(outcome)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = format_datetime(outcome.finished_at)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content(_text(outcome, url))
    message.add_alternative(_html(outcome, url), subtype="html")
    return message
