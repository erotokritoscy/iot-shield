"""How far each running ``POST /runs`` has got, for the Compose page's progress bar (ADR-0051).

A run is one synchronous request, so the browser learns nothing until it ends. It therefore
names a ``progress_id`` of its own choosing in the request, and polls
``GET /runs/progress/{progress_id}`` while it waits; the run reports into this board from its
own thread (``execute_run``'s ``progress`` callback).

The board lives in the Pipeline API's memory. That is enough because the API is one process
(``python -m iotshield.api.control``); a deployment that ran several workers would need it in
a store they share, and a poll that reached the wrong one would only read "not started yet".
Nothing here is a record: an entry is dropped ``KEEP_FINISHED_S`` after its run ends, the saved
run (ADR-0046) being the durable account.

An entry belongs to the organization whose run it is, and is answered only to that
organization, exactly like every other row (ADR-0039): another org's id finds nothing.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from iotshield.pipeline.runner import RunProgress

KEEP_FINISHED_S = 120.0
"""How long a finished run's last state stays readable — long enough for a last poll."""

MAX_ENTRIES = 1_000
"""A ceiling, so ids that are reported and never polled cannot grow the board without bound."""

Phase = Literal["starting", "reading", "judging", "saving", "finished", "failed"]


class ProgressInfo(BaseModel):
    """One run's progress as the dashboard reads it."""

    phase: Phase = Field(
        description="starting (checking and counting), reading, judging, saving, finished, failed"
    )
    done: int = Field(description="Rows read, or events judged, so far in this phase")
    total: int | None = Field(description="What `done` is counting towards; null when unknown")
    elapsed_s: float = Field(description="Seconds since the run started")


@dataclass
class _Entry:
    org_id: str
    started: float
    phase: Phase = "starting"
    done: int = 0
    total: int | None = None
    ended: float | None = None


class ProgressBoard:
    """Thread-safe: the run writes from a worker thread while polls read from others."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[UUID, _Entry] = {}
        self._lock = threading.Lock()
        self._now = now

    def start(self, progress_id: UUID, *, org_id: str) -> bool:
        """Register a run; False if the id is already another live run's, which keeps it."""
        with self._lock:
            self._prune()
            current = self._entries.get(progress_id)
            if current is not None and (current.ended is None or current.org_id != org_id):
                return False
            self._entries[progress_id] = _Entry(org_id=org_id, started=self._now())
            return True

    def update(self, progress_id: UUID, progress: RunProgress) -> None:
        with self._lock:
            entry = self._entries.get(progress_id)
            if entry is None or entry.ended is not None:
                return
            entry.phase, entry.done, entry.total = progress.phase, progress.done, progress.total

    def end(self, progress_id: UUID, *, failed: bool) -> None:
        with self._lock:
            entry = self._entries.get(progress_id)
            if entry is None:
                return
            entry.phase = "failed" if failed else "finished"
            entry.ended = self._now()

    def read(self, progress_id: UUID, *, org_id: str) -> ProgressInfo | None:
        with self._lock:
            entry = self._entries.get(progress_id)
            if entry is None or entry.org_id != org_id:
                return None
            until = entry.ended if entry.ended is not None else self._now()
            return ProgressInfo(
                phase=entry.phase,
                done=entry.done,
                total=entry.total,
                elapsed_s=round(until - entry.started, 1),
            )

    def _prune(self) -> None:
        now = self._now()
        for key in [
            key
            for key, entry in self._entries.items()
            if entry.ended is not None and now - entry.ended > KEEP_FINISHED_S
        ]:
            del self._entries[key]
        if len(self._entries) >= MAX_ENTRIES:
            # Oldest first; a run still going is dropped only when nothing else is left.
            ordered = sorted(
                self._entries,
                key=lambda key: (self._entries[key].ended is None, self._entries[key].started),
            )
            for key in ordered[: len(self._entries) - MAX_ENTRIES + 1]:
                del self._entries[key]
