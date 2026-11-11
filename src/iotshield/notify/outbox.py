"""Send mail off the request path, and never let a send fail anything (ADR-0052 amendment).

The Pipeline API emails a run's outcome to whoever ran it, but a run is one synchronous request
and a slow relay (up to the mailer's 10 s timeout) must not hold up the response that already
has the answer. So the API hands the work here: one background thread, in order, each item
building its message (it may read the stored run) and sending it. Whatever goes wrong — the
database, the relay — is logged; the run is already stored and its response already sent.

``background=False`` runs each item inline, which is what the tests use to see what was sent.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

from iotshield.notify.mailer import Mailer

log = logging.getLogger("iotshield.notify")

Compose = Callable[[str], EmailMessage | None]
"""Given the mailer's sender address, the message to send — or None for nothing to send."""


class Outbox:
    def __init__(self, mailer: Mailer, *, background: bool = True) -> None:
        self.mailer = mailer
        self._pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail") if background else None
        )

    def post(self, compose: Compose) -> None:
        """Build and send one message, in the background unless built with ``background=False``."""
        if self._pool is None:
            self._deliver(compose)
        else:
            self._pool.submit(self._deliver, compose)

    def _deliver(self, compose: Compose) -> None:
        try:
            message = compose(self.mailer.sender)
            if message is None:
                return
            self.mailer.send(message)
            log.info("emailed %r to %s", message["Subject"], message["To"])
        except Exception:
            log.exception("could not send an email; the run it reports on is unaffected")

    def close(self) -> None:
        """Wait for what was posted, then stop the thread (tests, shutdown)."""
        if self._pool is not None:
            self._pool.shutdown(wait=True)
