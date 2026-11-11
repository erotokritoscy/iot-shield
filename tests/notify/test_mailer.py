"""The SMTP transport (ADR-0052): configured from env, and really speaking SMTP.

The exchange test runs a minimal SMTP server on a loopback socket — the subset of RFC 5321 that
``smtplib.send_message`` uses — so the mailer is proven to deliver a message a server accepts,
not merely to call a method on a mock. MailHog does the same job in compose.
"""

import socket
import threading
from collections.abc import Iterator
from email import message_from_bytes
from email.message import EmailMessage

import pytest

from iotshield.notify.mailer import (
    DEFAULT_PORT,
    DEFAULT_SENDER,
    MailSettings,
    SmtpMailer,
    mailer_from_env,
    settings_from_env,
)


class TinySmtp:
    """Accepts one connection, records MAIL FROM / RCPT TO / DATA, answers 250 to the rest."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port: int = self.sock.getsockname()[1]
        self.sender = ""
        self.recipients: list[str] = []
        self.data = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        with conn, conn.makefile("rb") as reader:
            conn.sendall(b"220 tiny ESMTP\r\n")
            while line := reader.readline():
                command = line.decode().strip()
                verb = command.split(" ", 1)[0].upper()
                if verb in {"EHLO", "HELO"}:
                    conn.sendall(b"250 tiny\r\n")
                elif verb == "MAIL":
                    self.sender = command.split(":", 1)[1].strip(" <>")
                    conn.sendall(b"250 ok\r\n")
                elif verb == "RCPT":
                    self.recipients.append(command.split(":", 1)[1].strip(" <>"))
                    conn.sendall(b"250 ok\r\n")
                elif verb == "DATA":
                    conn.sendall(b"354 go\r\n")
                    body = b""
                    while (chunk := reader.readline()) != b".\r\n":
                        body += chunk
                    self.data = body
                    conn.sendall(b"250 queued\r\n")
                elif verb == "QUIT":
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 ok\r\n")
        self.sock.close()


@pytest.fixture()
def server() -> Iterator[TinySmtp]:
    tiny = TinySmtp()
    yield tiny
    tiny.thread.join(timeout=5)


def test_unset_host_means_mail_is_off() -> None:
    assert settings_from_env({}) is None
    assert mailer_from_env({"IOTSHIELD_SMTP_HOST": "  "}) is None


def test_settings_are_read_with_defaults() -> None:
    assert settings_from_env({"IOTSHIELD_SMTP_HOST": "mailhog"}) == MailSettings(
        host="mailhog", port=DEFAULT_PORT, sender=DEFAULT_SENDER
    )
    full = settings_from_env(
        {
            "IOTSHIELD_SMTP_HOST": "relay.example.org",
            "IOTSHIELD_SMTP_PORT": "587",
            "IOTSHIELD_MAIL_FROM": "alerts@example.org",
            "IOTSHIELD_SMTP_USERNAME": "u",
            "IOTSHIELD_SMTP_PASSWORD": "p",
            "IOTSHIELD_SMTP_STARTTLS": "yes",
        }
    )
    assert full == MailSettings(
        host="relay.example.org",
        port=587,
        sender="alerts@example.org",
        username="u",
        password="p",
        starttls=True,
    )


def test_a_bad_port_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="IOTSHIELD_SMTP_PORT"):
        settings_from_env({"IOTSHIELD_SMTP_HOST": "h", "IOTSHIELD_SMTP_PORT": "smtp"})


def test_the_mailer_delivers_to_an_smtp_server(server: TinySmtp) -> None:
    mailer = SmtpMailer(MailSettings(host="127.0.0.1", port=server.port, sender="me@example.org"))
    message = EmailMessage()
    message["Subject"] = "hello"
    message["From"] = mailer.sender
    message["To"] = "a@example.com, b@example.com"
    message.set_content("body text\n")
    mailer.send(message)
    server.thread.join(timeout=5)
    assert server.sender == "me@example.org"
    assert server.recipients == ["a@example.com", "b@example.com"]
    received = message_from_bytes(server.data)
    assert received["Subject"] == "hello"
    assert "body text" in received.get_payload()
