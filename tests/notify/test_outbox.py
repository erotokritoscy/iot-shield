"""The outbox (ADR-0052 amendment): sends in order; a failure to build or send is only logged."""

import logging
from email.message import EmailMessage

import pytest

from iotshield.notify import Outbox


class Recorder:
    sender = "iotshield@example.org"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        if self.fail:
            raise ConnectionRefusedError("no relay")
        self.sent.append(message)


def _message(subject: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = "a@example.com"
    message.set_content("x")
    return message


def test_the_background_thread_sends_everything_in_order() -> None:
    mailer = Recorder()
    outbox = Outbox(mailer)
    for n in range(5):
        outbox.post(lambda sender, n=n: _message(f"{sender} {n}"))
    outbox.close()
    assert [m["Subject"] for m in mailer.sent] == [f"iotshield@example.org {n}" for n in range(5)]


def test_nothing_to_send_sends_nothing() -> None:
    mailer = Recorder()
    Outbox(mailer, background=False).post(lambda _sender: None)
    assert mailer.sent == []


@pytest.mark.parametrize("where", ["compose", "send"])
def test_a_failure_is_logged_and_goes_no_further(
    where: str, caplog: pytest.LogCaptureFixture
) -> None:
    def compose(_sender: str) -> EmailMessage:
        if where == "compose":
            raise RuntimeError("the database went away")
        return _message("s")

    outbox = Outbox(Recorder(fail=where == "send"), background=False)
    with caplog.at_level(logging.ERROR, logger="iotshield.notify"):
        outbox.post(compose)  # does not raise
    assert "could not send an email" in caplog.text
