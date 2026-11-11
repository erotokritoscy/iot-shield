"""A Compose run emails its outcome to whoever ran it (ADR-0052 amendment).

Driven through the logged-in stack because the recipient is the session's user, never anything
in the request body. The outbox is swapped for an inline one around a fake mailer, so what
would have gone to MailHog is a list the test can read.
"""

from email.message import EmailMessage
from typing import Any

import pytest
from fastapi import HTTPException

from iotshield.api import control
from iotshield.auth.passwords import hash_password
from iotshield.notify import Outbox
from iotshield.storage import accounts, make_session_factory
from tests.api.conftest import PASSWORD, RUN_SPEC, STUB_CSV, Browser, Stack


class FakeMailer:
    sender = "iotshield@example.org"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


@pytest.fixture()
def mailer(stack: Stack) -> FakeMailer:
    fake = FakeMailer()
    stack.pipeline_app.state.outbox = Outbox(fake, background=False)
    stack.pipeline_app.state.dashboard_url = "http://localhost:8080"
    return fake


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return part.get_content()


def _run(browser: Browser, **extra: Any) -> Any:
    body = {"spec": RUN_SPEC, "inputs": [{"content": STUB_CSV, "origin": "pumps.csv"}]} | extra
    return browser.pipeline.post("/runs", json=body)


def _signed_in(stack: Stack, username: str = "ana@example.com") -> Browser:
    browser = stack.browser()
    assert browser.register("Acme", username).status_code == 201
    return browser


def test_a_finished_run_is_emailed_to_whoever_ran_it(stack: Stack, mailer: FakeMailer) -> None:
    response = _run(_signed_in(stack), name="Pumps")
    assert response.status_code == 200, response.text
    run = response.json()

    [message] = mailer.sent
    assert message["To"] == "ana@example.com"
    assert message["From"] == "iotshield@example.org"
    plural = "" if run["alerts"] == 1 else "s"
    assert message["Subject"] == f"[iotshield] Run 'Pumps' finished: {run['alerts']} alert{plural}"
    text = _body(message)
    assert "run by ana@example.com" in text
    assert f"events      {run['events']}" in text
    assert f"http://localhost:8080/runs/{run['run_id']}" in text


def test_an_unnamed_run_is_called_what_it_was_saved_as(stack: Stack, mailer: FakeMailer) -> None:
    browser = _signed_in(stack)
    run = _run(browser).json()
    saved = browser.pipeline.get(f"/runs/{run['run_id']}").json()
    [message] = mailer.sent
    assert message["Subject"].startswith(f"[iotshield] Run '{saved['name']}' finished")


def test_unticking_email_result_sends_nothing(stack: Stack, mailer: FakeMailer) -> None:
    assert _run(_signed_in(stack), email_result=False).status_code == 200
    assert mailer.sent == []


def test_an_account_whose_username_is_not_an_address_gets_nothing(
    stack: Stack, mailer: FakeMailer
) -> None:
    with make_session_factory(stack.engine)() as session:
        accounts.register_organization(
            session, name="Legacy", username="legacy", password_hash=hash_password(PASSWORD)
        )
        session.commit()
    browser = stack.browser()
    assert browser.login("legacy").status_code == 200
    assert _run(browser).status_code == 200
    assert mailer.sent == []


def test_a_refused_chain_is_not_emailed(stack: Stack, mailer: FakeMailer) -> None:
    spec = RUN_SPEC | {"detectors": [{"name": "no_such_detector"}]}
    response = _signed_in(stack).pipeline.post(
        "/runs", json={"spec": spec, "inputs": [{"content": STUB_CSV, "origin": "x"}]}
    )
    assert 400 <= response.status_code < 500
    assert mailer.sent == []


def test_a_run_that_fails_while_running_is_emailed_with_why(
    stack: Stack, mailer: FakeMailer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPException(
            status_code=502,
            detail={"message": "connection 'biology' is unavailable", "issues": []},
        )

    monkeypatch.setattr(control, "perform_run", unavailable)
    response = _run(_signed_in(stack), name="Nightly biology")
    assert response.status_code == 502

    [message] = mailer.sent
    assert message["Subject"] == "[iotshield] Run 'Nightly biology' failed"
    assert "Why: connection 'biology' is unavailable" in _body(message)


def test_without_a_mail_server_a_run_is_unaffected(stack: Stack) -> None:
    assert stack.pipeline_app.state.outbox is None  # IOTSHIELD_SMTP_HOST is unset in tests
    assert _run(_signed_in(stack)).status_code == 200
