"""Postgres result sink: the default store, and what the read API / dashboard serve.

Persists a ``RunResult`` exactly as the runner did before the output seam existed — one
atomic transaction, the repository as the only SQL — now behind the ``ResultSink`` port.
The transaction lives *here* (a Postgres concern), not in the runner, which is precisely
what lets the store be swapped. Assets: seeded registrations upsert (human knowledge
wins); the rest auto-discover on insert, only filling absence so a registration made in
an earlier run is never clobbered (ADR-0005). Built with an injected session factory, so
the API reuses its long-lived engine and a caller can never name the database.

Every row is written into the run's organization (ADR-0039), which the deployment supplies
through ``SinkResources.org_id``. The organization row is ensured first, so a CLI run into a
fresh database needs no registered account.
"""

from sqlalchemy.orm import Session, sessionmaker

from iotshield.sink import RunResult, SinkResources
from iotshield.storage import DEFAULT_ORG_ID, repository


class PostgresSink:
    def __init__(self, session_factory: sessionmaker[Session], org_id: str) -> None:
        self._session_factory = session_factory
        self._org_id = org_id

    def write(self, result: RunResult) -> None:
        # Discovered stubs are re-derived by the repository on insert (only-fill-absence,
        # so cross-run registrations survive); we upsert only the true registrations.
        registrations = [a for a in result.assets if not a.metadata.get("auto_discovered")]
        org_id = self._org_id
        with self._session_factory() as session, session.begin():
            repository.ensure_organization(session, org_id)
            if registrations:
                repository.upsert_assets(session, registrations, org_id=org_id)
            repository.insert_events(session, result.events, org_id=org_id)
            repository.insert_malformed(session, result.malformed, org_id=org_id)
            repository.insert_detections(session, result.detections, org_id=org_id)
            repository.insert_alerts(session, result.alerts, org_id=org_id)


def build(resources: SinkResources) -> PostgresSink:
    if resources.session_factory is None:
        raise ValueError(
            "the 'postgres' sink requires a database "
            "(set database_url in the config or IOTSHIELD_DATABASE_URL)"
        )
    return PostgresSink(resources.session_factory, resources.org_id or DEFAULT_ORG_ID)
