"""The email's content (ADR-0052): what a success, a failure and an empty run each say."""

from datetime import UTC, datetime
from email.message import EmailMessage
from uuid import uuid4

import pytest

from iotshield.contracts import AlertSeverity, PrioritizedAlert
from iotshield.notify import Outcome, RunCounts, compose_email

FINISHED = datetime(2026, 11, 9, 2, 30, tzinfo=UTC)
RUN = RunCounts(
    run_id=uuid4(), name="Nightly · 2026-11-09", events=110, malformed=2, detections=8, alerts=2
)


def _alert(asset: str, score: float, severity: AlertSeverity, threat: str) -> PrioritizedAlert:
    return PrioritizedAlert(
        asset_id=asset,
        severity=severity,
        priority_score=score,
        threat_class=threat,
        rationale="because",
        detection_ids=[uuid4()],
        window_start=FINISHED,
        window_end=FINISHED,
    )


ALERTS = [
    _alert("10.0.0.24", 80.0, AlertSeverity.CRITICAL, "Remote Services (T1021)"),
    _alert("10.0.0.11", 39.6, AlertSeverity.MEDIUM, "unknown"),
]


def _parts(message: EmailMessage) -> tuple[str, str]:
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert plain is not None and html is not None
    return plain.get_content(), html.get_content()


def test_a_success_names_the_counts_the_queue_head_and_the_run() -> None:
    outcome = Outcome(
        kind="job",
        name="Nightly",
        trigger="schedule",
        requested_by=None,
        finished_at=FINISHED,
        run=RUN,
        top_alerts=ALERTS,
    )
    message = compose_email(
        outcome,
        sender="iotshield@example.org",
        recipients=["a@example.com", "b@example.com"],
        dashboard_url="http://localhost:8080/",
    )
    assert message["Subject"] == "[iotshield] Job 'Nightly' finished: 2 alerts"
    assert message["To"] == "a@example.com, b@example.com"
    text, html = _parts(message)
    assert "2026-11-09 02:30 UTC (on its schedule)" in text
    assert "events      110" in text and "alerts      2" in text
    assert " 80.0  CRITICAL  10.0.0.24 - Remote Services (T1021)" in text
    assert " 39.6  MEDIUM    10.0.0.11\n" in text  # "unknown" is not printed as a threat
    assert f"http://localhost:8080/runs/{RUN.run_id}" in text
    assert f'href="http://localhost:8080/runs/{RUN.run_id}"' in html


def test_a_failure_says_why_and_offers_no_run() -> None:
    outcome = Outcome(
        kind="job",
        name="Nightly",
        trigger="manual",
        requested_by="ops@example.com",
        finished_at=FINISHED,
        error="Plant API refused the credentials",
    )
    message = compose_email(
        outcome, sender="x@example.org", recipients=["a@example.com"], dashboard_url="http://h"
    )
    assert message["Subject"] == "[iotshield] Job 'Nightly' failed"
    text, _ = _parts(message)
    assert "run now by ops@example.com" in text
    assert "Why: Plant API refused the credentials" in text
    assert "/runs/" not in text


def test_a_quiet_run_says_no_alerts_and_one_alert_is_singular() -> None:
    quiet = RunCounts(run_id=uuid4(), name="q", events=5, malformed=0, detections=0, alerts=0)
    outcome = Outcome(
        kind="job", name="Q", trigger="schedule", requested_by=None, finished_at=FINISHED, run=quiet
    )
    message = compose_email(outcome, sender="x@example.org", recipients=["a@example.com"])
    assert message["Subject"] == "[iotshield] Job 'Q' finished: 0 alerts"
    assert "No alerts." in _parts(message)[0]
    one = Outcome(
        kind="job",
        name="Q",
        trigger="schedule",
        requested_by=None,
        finished_at=FINISHED,
        run=RunCounts(run_id=uuid4(), name="q", events=5, malformed=0, detections=1, alerts=1),
    )
    message = compose_email(one, sender="x@example.org", recipients=["a@example.com"])
    assert message["Subject"].endswith("1 alert")


def test_the_html_part_escapes_what_it_quotes() -> None:
    outcome = Outcome(
        kind="job",
        name="<script>x</script>",
        trigger="schedule",
        requested_by=None,
        finished_at=FINISHED,
        run=RUN,
        top_alerts=[_alert("<img src=x>", 50.0, AlertSeverity.HIGH, "a&b")],
    )
    message = compose_email(outcome, sender="x@example.org", recipients=["a@example.com"])
    _, html = _parts(message)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img" not in html and "a&amp;b" in html


def test_it_refuses_to_address_nobody() -> None:
    outcome = Outcome(
        kind="job", name="Q", trigger="schedule", requested_by=None, finished_at=FINISHED
    )
    with pytest.raises(ValueError, match="recipient"):
        compose_email(outcome, sender="x@example.org", recipients=[])


def test_a_run_is_named_as_a_run_and_says_who_ran_it() -> None:
    outcome = Outcome(
        kind="run",
        name="Camera check",
        trigger="manual",
        requested_by="ops@example.com",
        finished_at=FINISHED,
        run=RUN,
        top_alerts=ALERTS,
    )
    message = compose_email(outcome, sender="x@example.org", recipients=["ops@example.com"])
    assert message["Subject"] == "[iotshield] Run 'Camera check' finished: 2 alerts"
    text, html = _parts(message)
    assert text.startswith(
        "Run: Camera check\nFinished: 2026-11-09 02:30 UTC (run by ops@example.com)"
    )
    assert "Run: Nightly" not in text  # a run's name is the heading, not repeated below it
    assert "you ran it with Email me the result ticked" in text
    assert "Run: Camera check</h2>" in html


def test_a_failed_run_says_why() -> None:
    outcome = Outcome(
        kind="run",
        name="Unnamed run",
        trigger="manual",
        requested_by="ops@example.com",
        finished_at=FINISHED,
        error="connection 'biology' is unavailable",
    )
    message = compose_email(outcome, sender="x@example.org", recipients=["ops@example.com"])
    assert message["Subject"] == "[iotshield] Run 'Unnamed run' failed"
    text, _ = _parts(message)
    assert "The run FAILED; nothing it read was stored." in text
    assert "Why: connection 'biology' is unavailable" in text
