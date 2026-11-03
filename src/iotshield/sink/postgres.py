"""Postgres result sink: the default store, and what the read API / dashboard serve.

Persists a run with the repository as the only SQL, behind the ``ResultSink`` port. The
transaction lives *here* (a Postgres concern), not in the runner, which is precisely what lets
the store be swapped. Assets: seeded registrations upsert (human knowledge wins); the rest
auto-discover on insert, only filling absence so a registration made in an earlier run is
never clobbered (ADR-0005). Built with an injected session factory, so the API reuses its
long-lived engine and a caller can never name the database.

Every row is written into the run's organization (ADR-0039), which the deployment supplies
through ``SinkResources.org_id``. The organization row is ensured first, so a CLI run into a
fresh database needs no registered account. Every row is also stamped with the run's
``run_id`` (ADR-0045), which is what lets the dashboard open exactly one run's results, and
the run itself is saved — named, with the chain that ran — so it can be listed and reopened
later (ADR-0046).

**One transaction per run, however many batches (ADR-0047).** A run arrives in parts, and the
writer holds one transaction open across all of them: each part is flushed and then *expunged*
from the session, so what stays in this process's memory is one batch, while the database holds
the uncommitted rest. ``finish`` saves the run's record with its totals and commits; ``abort``
rolls everything back. So a listed run always has its rows, and a run that failed in its last
batch left nothing in the first.
"""

from sqlalchemy.orm import Session, sessionmaker

from iotshield.sink import RunHeader, RunResult, RunTotals, SinkResources, write_result
from iotshield.storage import DEFAULT_ORG_ID, repository, runs


class PostgresSink:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        org_id: str,
        *,
        run_name: str | None = None,
        run_by: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._org_id = org_id
        self._run_name = run_name
        self._run_by = run_by

    def open(self, header: RunHeader) -> "_PostgresWriter":
        return _PostgresWriter(self, header)

    def write(self, result: RunResult) -> None:
        """A complete result as a one-batch run."""
        write_result(self, result)


class _PostgresWriter:
    def __init__(self, sink: PostgresSink, header: RunHeader) -> None:
        self._sink = sink
        self._header = header
        self._session = sink._session_factory()
        self._transaction = self._session.begin()
        repository.ensure_organization(self._session, sink._org_id)

    def add(self, part: RunResult) -> None:
        session, org_id, run_id = self._session, self._sink._org_id, self._header.run_id
        # Discovered stubs are re-derived by the repository on insert (only-fill-absence,
        # so cross-run registrations survive); we upsert only the true registrations. Every
        # batch carries the seeded ones again, and an upsert of the same registration is a
        # no-op, so that costs a statement rather than a correctness question.
        registrations = [a for a in part.assets if not a.metadata.get("auto_discovered")]
        if registrations:
            repository.upsert_assets(session, registrations, org_id=org_id)
        repository.insert_events(session, part.events, org_id=org_id, run_id=run_id)
        repository.insert_malformed(session, part.malformed, org_id=org_id, run_id=run_id)
        repository.insert_detections(session, part.detections, org_id=org_id, run_id=run_id)
        repository.insert_alerts(session, part.alerts, org_id=org_id, run_id=run_id)
        # Flushed by each insert; dropping the ORM rows is what keeps a run of millions of
        # events at one batch of memory while the transaction stays open.
        session.expunge_all()

    def finish(self, totals: RunTotals) -> None:
        try:
            runs.insert_run(self._session, self._saved_run(totals), org_id=self._sink._org_id)
            self._transaction.commit()
        finally:
            self._session.close()

    def abort(self) -> None:
        try:
            if self._transaction.is_active:
                self._transaction.rollback()
        finally:
            self._session.close()

    def _saved_run(self, totals: RunTotals) -> runs.SavedRun:
        header = self._header
        sources = [
            runs.RunSource(s.normalizer, s.source_format, s.origin, s.pull) for s in header.sources
        ]
        return runs.SavedRun(
            run_id=header.run_id,
            name=runs.clean_name(self._sink._run_name)
            or runs.default_run_name([s.origin for s in sources], header.run_at),
            created_by=self._sink._run_by,
            run_at=header.run_at,
            spec=header.spec,
            sources=sources,
            warnings=list(totals.warnings),
            events=totals.events,
            malformed=totals.malformed,
            detections=totals.detections,
            alerts=totals.alerts,
        )


def build(resources: SinkResources) -> PostgresSink:
    if resources.session_factory is None:
        raise ValueError(
            "the 'postgres' sink requires a database "
            "(set database_url in the config or IOTSHIELD_DATABASE_URL)"
        )
    return PostgresSink(
        resources.session_factory,
        resources.org_id or DEFAULT_ORG_ID,
        run_name=resources.run_name,
        run_by=resources.run_by,
    )
