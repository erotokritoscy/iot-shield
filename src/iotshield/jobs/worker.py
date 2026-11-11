"""The job worker: queues due jobs and runs queued executions, one at a time (ADR-0050).

Serve: ``python -m iotshield.jobs.worker`` (compose's ``worker`` service). Each tick:

1. **recover** — a ``running`` execution whose heartbeat stopped belonged to a worker that died
   (a restart, an OOM); it is failed, since its run's transaction was never committed;
2. **schedule** — every enabled job whose ``next_run_at`` has come gets a queued execution,
   unless it already has one queued or running (a slow job is skipped, never stacked), and its
   ``next_run_at`` moves to the schedule's next time after *now* — so a worker that was down
   for a day runs a missed job once, not once per missed slot;
3. **run** — the oldest queued execution is claimed and run to completion, its heartbeat beaten
   from a side thread meanwhile.

Rows are claimed with ``FOR UPDATE SKIP LOCKED`` on Postgres, so more than one worker may run;
SQLite (tests) has no row locks and needs none with a single worker. Nothing here waits on a
browser, so a job has no nginx timeout — which is where a pull of years belongs (ADR-0052).
"""

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from iotshield.jobs.model import Schedule
from iotshield.jobs.run import run_execution
from iotshield.storage import (
    database_url_from_env,
    describe_location,
    make_engine,
    make_session_factory,
)
from iotshield.storage import jobs as store

log = logging.getLogger("iotshield.jobs.worker")

POLL_SECONDS = 15.0
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(minutes=5)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Worker:
    engine: Engine
    output_dir: Path
    clock: Clock = utc_now
    heartbeat_seconds: float = HEARTBEAT_SECONDS
    stale_after: timedelta = STALE_AFTER
    session_factory: sessionmaker[Session] = field(init=False)

    def __post_init__(self) -> None:
        self.session_factory = make_session_factory(self.engine)
        self.database_url = self.engine.url.render_as_string(hide_password=False)

    def recover(self) -> int:
        with self.session_factory() as session:
            failed = store.fail_stale(session, now=self.clock(), stale_after=self.stale_after)
            session.commit()
        if failed:
            log.warning("failed %d execution(s) whose worker stopped", failed)
        return failed

    def schedule(self) -> int:
        """Queue every due job; returns how many executions were queued."""
        now = self.clock()
        queued = 0
        with self.session_factory() as session:
            for row in store.due_jobs(session, now=now):
                busy = store.active_execution(session, row.job_id, org_id=row.org_id)
                if busy is None:
                    store.queue_execution(
                        session,
                        row.job_id,
                        org_id=row.org_id,
                        trigger="schedule",
                        requested_by=None,
                        now=now,
                    )
                    queued += 1
                else:
                    log.info("job %s is still %s; this slot is skipped", row.job_id, busy.status)
                row.next_run_at = (
                    Schedule.model_validate(row.schedule).next_after(now) if row.schedule else None
                )
            session.commit()
        return queued

    def run_one(self) -> bool:
        """Claim and run the oldest queued execution; False when there was none."""
        with self.session_factory() as session:
            execution = store.claim_next(session, now=self.clock())
            session.commit()
        if execution is None:
            return False
        log.info("running job %s (execution %s)", execution.job_id, execution.execution_id)
        stop = threading.Event()
        beater = threading.Thread(
            target=self._beat, args=(execution.execution_id, stop), daemon=True
        )
        beater.start()
        try:
            run_id, error = run_execution(
                execution,
                session_factory=self.session_factory,
                database_url=self.database_url,
                output_dir=self.output_dir,
                now=self.clock(),
            )
        except Exception as exc:  # the worker must outlive any one job
            log.exception("job %s crashed", execution.job_id)
            run_id, error = None, f"the run failed unexpectedly: {exc}"
        finally:
            stop.set()
            beater.join()
        with self.session_factory() as session:
            store.finish(
                session, execution.execution_id, now=self.clock(), run_id=run_id, error=error
            )
            session.commit()
        log.info("job %s %s", execution.job_id, "failed: " + error if error else "succeeded")
        return True

    def _beat(self, execution_id: UUID, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                with self.session_factory() as session:
                    store.beat(session, execution_id, now=self.clock())
                    session.commit()
            except Exception:  # pragma: no cover - a missed beat is retried next interval
                log.exception("heartbeat failed")

    def tick(self) -> None:
        """One pass: recover, schedule, then run queued executions until none is left."""
        self.recover()
        self.schedule()
        while self.run_one():
            self.schedule()  # a long run may have let another job come due


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = database_url_from_env()
    if url is None:
        return 2
    output_dir = Path(os.environ.get("IOTSHIELD_OUTPUT_DIR", "results"))
    poll = float(os.environ.get("IOTSHIELD_WORKER_POLL_SECONDS", POLL_SECONDS))
    print(f"iotshield job worker using database: {describe_location(url)}", file=sys.stderr)
    worker = Worker(engine=make_engine(url), output_dir=output_dir)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            worker.tick()
        except Exception:  # pragma: no cover - the database went away; try again next poll
            log.exception("worker tick failed")
        stopping.wait(poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
