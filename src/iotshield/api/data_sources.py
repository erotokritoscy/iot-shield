"""The data-source routes: an organization's register of where its data comes from (ADR-0041).

Mounted on the **Pipeline API** always — with auth off too, where every request is the admin
of the default organization — because it writes, and ADR-0007 keeps the read API write-free.
Anyone in the organization may list its sources; only its admins add, change or remove them,
the same split as its users.

A source's ``normalizer`` and ``source_format`` are checked against the registry, exactly as
a run's are: a source that names a reader which does not exist, or a format that reader does
not read, would hand the Compose page a pairing the guard then refuses.

A source may also name a **connection** (ADR-0042): a database declared in the server's
``config/connections.yaml``, which anyone in the organization can then **pull** a date range
from, as text its normalizer reads. A request names a connection, never a host; and a
connection lists the organizations allowed to use it, so one organization cannot reach another's
database by naming it.
"""

from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import AccountDep, AdminDep
from iotshield.api.control import MAX_SOURCE_CHARS, ParamInfo, model_params
from iotshield.capabilities import formats_in
from iotshield.connect import (
    CONNECTORS,
    Connection,
    ConnectionUnavailable,
    ConnectorSpec,
    PullRefused,
    list_devices,
    load_connections,
    pull,
)
from iotshield.pipeline.registry import NORMALIZERS
from iotshield.storage import repository
from iotshield.storage.accounts import Account
from iotshield.storage.data_sources import (
    DataSource,
    DataSourceConflict,
    DataSourceFields,
    SourceType,
    create_data_source,
    delete_data_source,
    get_data_source,
    list_data_sources,
    update_data_source,
)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class DataSourceRequest(BaseModel):
    """A data source as an admin writes it. Blank optional text means "not set"."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    type: SourceType
    description: str | None = Field(default=None, max_length=500)
    normalizer: str | None = Field(default=None, max_length=64)
    source_format: str | None = Field(default=None, max_length=64)
    connection: str | None = Field(
        default=None,
        max_length=64,
        description="A connection declared on the server; its connector decides the reader",
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("description", "normalizer", "source_format", "connection")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _reader(self) -> "DataSourceRequest":
        if self.normalizer is None:
            if self.source_format is not None:
                raise ValueError("a format needs the normalizer that reads it")
            return self
        spec = NORMALIZERS.get(self.normalizer)
        if spec is None:
            known = ", ".join(sorted(NORMALIZERS))
            raise ValueError(f"unknown normalizer {self.normalizer!r}; known: {known}")
        formats = sorted(formats_in(spec.capability({}).consumes))
        if self.source_format is None:
            # One format leaves nothing to choose; several must be named, never guessed.
            if len(formats) != 1:
                raise ValueError(
                    f"normalizer {self.normalizer!r} reads {', '.join(formats)}; name one"
                )
            self.source_format = formats[0]
        elif self.source_format not in formats:
            raise ValueError(
                f"normalizer {self.normalizer!r} does not read {self.source_format!r} "
                f"(reads: {', '.join(formats)})"
            )
        return self

    def fields(self) -> DataSourceFields:
        return DataSourceFields(
            name=self.name,
            type=self.type,
            description=self.description,
            normalizer=self.normalizer,
            source_format=self.source_format,
            connection=self.connection,
        )


class DataSourceInfo(BaseModel):
    source_id: UUID
    name: str
    type: SourceType
    description: str | None
    normalizer: str | None
    source_format: str | None
    connection: str | None
    created_at: str


def _info(source: DataSource) -> DataSourceInfo:
    return DataSourceInfo(
        source_id=source.source_id,
        name=source.name,
        type=source.type,
        description=source.description,
        normalizer=source.normalizer,
        source_format=source.source_format,
        connection=source.connection,
        created_at=source.created_at.isoformat(),
    )


class ConnectionInfo(BaseModel):
    """A database this organization may pull from, and what a pull from it yields."""

    name: str
    connector: str
    description: str
    normalizer: str
    source_format: str
    params: list[ParamInfo]
    has_devices: bool


class DeviceInfo(BaseModel):
    id: str
    label: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date = Field(description="First day, inclusive, in the source's own clock")
    end: date = Field(description="Last day, inclusive")
    params: dict[str, Any] = Field(default_factory=dict, description="The connector's pull params")


class PullResponse(BaseModel):
    """A window of the source, as text its normalizer reads — ready to be a run's input."""

    content: str
    normalizer: str
    source_format: str
    rows: int
    origin: str


def _connections() -> dict[str, Connection]:
    try:
        return load_connections()
    except ValueError as exc:
        # The file's path and the parse error are server detail; say what is wrong, not where.
        raise HTTPException(
            status_code=503, detail="the server's connections file is invalid"
        ) from exc


def _usable(name: str, account: Account) -> tuple[Connection, ConnectorSpec]:
    """A connection this organization may use, with its connector — or a 422 saying not."""
    connection = _connections().get(name)
    # Not declared and not allowed read the same, so naming another organization's
    # connection reveals nothing about whether it exists.
    if connection is None or not connection.allows(account.org_id):
        raise HTTPException(status_code=422, detail=f"no connection named {name!r} is available")
    spec = CONNECTORS.get(connection.connector)
    if spec is None:
        raise HTTPException(
            status_code=503, detail=f"connection {name!r} names an unknown connector"
        )
    return connection, spec


def _with_connector_reader(body: DataSourceRequest, account: Account) -> DataSourceRequest:
    """A connected source is read the way its connector writes: fill that in, or refuse a
    different reader, so the source cannot promise a format its pulls never produce."""
    if body.connection is None:
        return body
    _, spec = _usable(body.connection, account)
    if body.normalizer is None:
        return body.model_copy(
            update={"normalizer": spec.normalizer, "source_format": spec.source_format}
        )
    if (body.normalizer, body.source_format) != (spec.normalizer, spec.source_format):
        raise HTTPException(
            status_code=422,
            detail=(
                f"connection {body.connection!r} yields {spec.source_format} for the "
                f"{spec.normalizer} normalizer; leave the reader blank or choose that"
            ),
        )
    return body


def _connected_source(
    source_id: UUID, account: Account, session: Session
) -> tuple[DataSource, Connection, ConnectorSpec]:
    source = get_data_source(session, source_id, org_id=account.org_id)
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    if source.connection is None:
        raise HTTPException(
            status_code=422, detail="this data source has no connection to pull from"
        )
    connection, spec = _usable(source.connection, account)
    return source, connection, spec


def _session(request: Request) -> Iterator[Session]:
    # Each writing route commits as its last step, so a route that raises commits nothing.
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_session)]


def create_data_sources_router() -> APIRouter:
    router = APIRouter(prefix="/data-sources", tags=["data sources"])

    @router.get("")
    def list_sources(account: AccountDep, session: SessionDep) -> list[DataSourceInfo]:
        """This organization's data sources, IT before OT, then by name."""
        return [_info(source) for source in list_data_sources(session, org_id=account.org_id)]

    @router.get("/connections")
    def list_connections(account: AccountDep) -> list[ConnectionInfo]:
        """The declared databases this organization may pull from (ADR-0042)."""
        infos: list[ConnectionInfo] = []
        for name, connection in sorted(_connections().items()):
            spec = CONNECTORS.get(connection.connector)
            if spec is None or not connection.allows(account.org_id):
                continue
            infos.append(
                ConnectionInfo(
                    name=name,
                    connector=spec.name,
                    description=spec.description,
                    normalizer=spec.normalizer,
                    source_format=spec.source_format,
                    params=model_params(spec.params_type),
                    has_devices=spec.devices is not None,
                )
            )
        return infos

    @router.get("/{source_id}/devices")
    def source_devices(
        source_id: UUID, request: Request, account: AccountDep, session: SessionDep
    ) -> list[DeviceInfo]:
        """What a pull from this source can be narrowed to (meters, hosts); query string =
        the connector's params, e.g. ``?utility=water``."""
        _, connection, spec = _connected_source(source_id, account, session)
        params = {k: v for k, v in request.query_params.items() if k != "devices"}
        try:
            devices = list_devices(spec, connection, params)
        except PullRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [DeviceInfo(id=device.id, label=device.label) for device in devices]

    @router.post("/{source_id}/pull")
    def pull_source(
        source_id: UUID, body: PullRequest, account: AccountDep, session: SessionDep
    ) -> PullResponse:
        """A date range of this source's database, as text for its normalizer. Read-only."""
        source, connection, spec = _connected_source(source_id, account, session)
        try:
            pulled = pull(
                spec, connection, body.start, body.end, body.params, max_chars=MAX_SOURCE_CHARS
            )
        except PullRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return PullResponse(
            content=pulled.content,
            normalizer=pulled.normalizer,
            source_format=pulled.source_format,
            rows=pulled.rows,
            origin=f"{source.name} {body.start.isoformat()}..{body.end.isoformat()}",
        )

    @router.post("", status_code=201)
    def add_source(
        body: DataSourceRequest, account: AdminDep, session: SessionDep
    ) -> DataSourceInfo:
        body = _with_connector_reader(body, account)
        # With auth off the default organization may not have a row yet (no one registered it).
        repository.ensure_organization(session, account.org_id)
        try:
            source = create_data_source(session, body.fields(), org_id=account.org_id)
        except DataSourceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return _info(source)

    @router.put("/{source_id}")
    def change_source(
        source_id: UUID, body: DataSourceRequest, account: AdminDep, session: SessionDep
    ) -> DataSourceInfo:
        body = _with_connector_reader(body, account)
        try:
            source = update_data_source(session, source_id, body.fields(), org_id=account.org_id)
        except DataSourceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Another organization's source is a 404, exactly like an id that never existed.
        if source is None:
            raise HTTPException(status_code=404, detail="data source not found")
        session.commit()
        return _info(source)

    @router.delete("/{source_id}", status_code=204)
    def remove_source(source_id: UUID, account: AdminDep, session: SessionDep) -> None:
        if not delete_data_source(session, source_id, org_id=account.org_id):
            raise HTTPException(status_code=404, detail="data source not found")
        session.commit()

    return router
