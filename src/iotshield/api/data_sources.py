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

Or a source may be an **HTTP API** (ADR-0049): its admin enters the URL, the authentication
(none, basic, or an API key) and a mapping from the API's JSON to events. That gives up
ADR-0042's "never a host in a request" on purpose, so ``connect.api.guard`` takes over: https,
public addresses only unless the operator allows a host. The credentials are **write-only** —
sealed under ``IOTSHIELD_SECRET_KEY`` on the way in, never in a response (``has_credentials``
says whether some are stored), and a blank field on an edit keeps what is stored.
"""

from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import AccountDep, AdminDep
from iotshield.api.control import ParamInfo, model_params
from iotshield.capabilities import formats_in
from iotshield.connect import (
    API_NORMALIZER,
    CONNECTORS,
    ApiTarget,
    Connection,
    ConnectionUnavailable,
    ConnectorSpec,
    DatabaseTarget,
    PullRefused,
    PullTarget,
    load_connections,
)
from iotshield.connect.api import ApiCredentials, ApiEndpoint, UrlRefused, check_url
from iotshield.connect.api.client import current_resolver
from iotshield.connect.api.mapping import SOURCE_FORMAT as API_FORMAT
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
from iotshield.storage.endpoints import (
    StoredEndpoint,
    delete_endpoint,
    endpoints_of,
    get_endpoint,
    set_endpoint,
)
from iotshield.storage.secrets import SecretsUnavailable, seal, unseal


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
    api: ApiEndpoint | None = Field(
        default=None, description="An HTTP API this source is pulled from (ADR-0049)"
    )
    credentials: "CredentialsRequest | None" = Field(
        default=None,
        description="Write-only: the API's password or key. Omitted or blank keeps what is stored",
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
        if self.api is not None:
            if self.connection is not None:
                raise ValueError("a source is pulled from a connection or an API, not both")
            # An API's records are mapped to normalized events, so they are only validated.
            if self.normalizer not in (None, API_NORMALIZER) or self.source_format not in (
                None,
                API_FORMAT,
            ):
                raise ValueError(
                    f"an API source is read by {API_NORMALIZER} ({API_FORMAT}); leave the "
                    "reader blank"
                )
            self.normalizer, self.source_format = API_NORMALIZER, API_FORMAT
            return self
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


class CredentialsRequest(BaseModel):
    """The secret half of an API source. Each field is optional: blank keeps what is stored."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, max_length=4000)

    @field_validator("password", "api_key")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        # Not stripped inside: a password may legitimately start or end with a space.
        return value if value is not None and value.strip() else None


DataSourceRequest.model_rebuild()


class DataSourceInfo(BaseModel):
    source_id: UUID
    name: str
    type: SourceType
    description: str | None
    normalizer: str | None
    source_format: str | None
    connection: str | None
    api: ApiEndpoint | None = Field(
        default=None, description="The HTTP API it is pulled from; never its credentials"
    )
    has_credentials: bool = Field(
        default=False, description="Whether credentials are stored for its API"
    )
    reader_params: dict[str, str] = Field(
        default_factory=dict,
        description="Normalizer params a pull of this source must be read with (an API's lane)",
    )
    created_at: str


def _endpoint_of(stored: StoredEndpoint) -> ApiEndpoint | None:
    try:
        return ApiEndpoint.model_validate(stored.config)
    except ValueError:  # pragma: no cover - only this module writes it, validated
        return None


def _info(source: DataSource, stored: StoredEndpoint | None = None) -> DataSourceInfo:
    endpoint = _endpoint_of(stored) if stored is not None else None
    return DataSourceInfo(
        source_id=source.source_id,
        name=source.name,
        type=source.type,
        description=source.description,
        normalizer=source.normalizer,
        source_format=source.source_format,
        connection=source.connection,
        api=endpoint,
        has_credentials=stored is not None and stored.credentials is not None,
        reader_params={"lane": endpoint.mapping.lane} if endpoint is not None else {},
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
    """What a window of the source holds, without the window itself (ADR-0047).

    A run reads the window from the database when it starts (``POST /runs`` with a ``pull``
    input), so this answers only what the compose page needs first: how many rows, and a
    sample of real connector output to check the format against — never every row.
    """

    sample: str = Field(description="The connector's header and the window's first lines")
    normalizer: str
    source_format: str
    rows: int = Field(description="Rows in the window, before the connector drops any")
    origin: str


def _connections() -> dict[str, Connection]:
    try:
        return load_connections()
    except ValueError as exc:
        # The file's path and the parse error are server detail; say what is wrong, not where.
        raise HTTPException(
            status_code=503, detail="the server's connections file is invalid"
        ) from exc


def _usable(name: str, org_id: str) -> tuple[Connection, ConnectorSpec]:
    """A connection this organization may use, with its connector — or a 422 saying not."""
    connection = _connections().get(name)
    # Not declared and not allowed read the same, so naming another organization's
    # connection reveals nothing about whether it exists.
    if connection is None or not connection.allows(org_id):
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
    _, spec = _usable(body.connection, account.org_id)
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


def _secrets_error(exc: SecretsUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def pull_target(source_id: UUID, session: Session, *, org_id: str) -> tuple[DataSource, PullTarget]:
    """The source and what a pull of it reads — its connection's database, or its API.

    Takes the tenant rather than an account because the job worker (ADR-0050) pulls on an
    organization's behalf with nobody logged in.
    """
    source = get_data_source(session, source_id, org_id=org_id)
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    stored = get_endpoint(session, source_id, org_id=org_id)
    if stored is not None:
        endpoint = _endpoint_of(stored)
        if endpoint is None:  # pragma: no cover - see _endpoint_of
            raise HTTPException(status_code=503, detail="this source's API settings are invalid")
        try:
            credentials = (
                ApiCredentials.from_sealed(unseal(stored.credentials))
                if stored.credentials is not None
                else ApiCredentials()
            )
        except SecretsUnavailable as exc:
            raise _secrets_error(exc) from exc
        return source, ApiTarget(name=source.name, endpoint=endpoint, credentials=credentials)
    if source.connection is None:
        raise HTTPException(
            status_code=422, detail="this data source has no connection or API to pull from"
        )
    connection, spec = _usable(source.connection, org_id)
    return source, DatabaseTarget(spec=spec, connection=connection)


def _save_endpoint(
    session: Session, source: DataSource, body: DataSourceRequest, *, org_id: str
) -> StoredEndpoint | None:
    """Store (or drop) the source's API, sealing credentials; refuses what could not be pulled.

    A blank credential keeps the stored one, so an admin can rename a source or change its
    mapping without retyping the key — but switching the authentication kind needs the new
    secret, since the old one is for a different scheme.
    """
    endpoint = body.api
    if endpoint is None:
        delete_endpoint(session, source.source_id, org_id=org_id)
        return None
    try:
        check_url(endpoint.url, resolve=current_resolver())
    except UrlRefused as exc:
        raise HTTPException(status_code=422, detail=f"api.url: {exc}") from exc
    previous = get_endpoint(session, source.source_id, org_id=org_id)
    try:
        kept = ApiCredentials()
        if previous is not None and previous.credentials is not None:
            old = _endpoint_of(previous)
            if old is not None and old.auth == endpoint.auth:
                kept = ApiCredentials.from_sealed(unseal(previous.credentials))
        given = body.credentials or CredentialsRequest()
        credentials = ApiCredentials(
            password=(given.password or kept.password) if endpoint.auth == "basic" else None,
            api_key=(given.api_key or kept.api_key) if endpoint.auth == "api_key" else None,
        )
        if not credentials.complete_for(endpoint):
            needed = "password" if endpoint.auth == "basic" else "API key"
            raise HTTPException(status_code=422, detail=f"enter the {needed} for this API")
        sealed = seal(credentials.as_sealable()) if endpoint.auth != "none" else None
    except SecretsUnavailable as exc:
        raise _secrets_error(exc) from exc
    return set_endpoint(
        session, source.source_id, endpoint.model_dump(mode="json"), sealed, org_id=org_id
    )


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
        stored = endpoints_of(session, org_id=account.org_id)
        return [
            _info(source, stored.get(source.source_id))
            for source in list_data_sources(session, org_id=account.org_id)
        ]

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
        _, target = pull_target(source_id, session, org_id=account.org_id)
        params = {k: v for k, v in request.query_params.items() if k != "devices"}
        try:
            devices = target.devices(params)
        except PullRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [DeviceInfo(id=device.id, label=device.label) for device in devices]

    @router.post("/{source_id}/pull")
    def pull_source(
        source_id: UUID, body: PullRequest, account: AccountDep, session: SessionDep
    ) -> PullResponse:
        """How many rows a date range of this source holds, and its first lines. Read-only.

        The rows themselves are read by the run (``POST /runs``, a ``pull`` input), streamed in
        batches, so there is no row limit here, and no limit on the window (ADR-0052).
        """
        source, target = pull_target(source_id, session, org_id=account.org_id)
        try:
            pulled = target.summary(body.start, body.end, body.params)
        except PullRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return PullResponse(
            sample=pulled.sample,
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
        stored = _save_endpoint(session, source, body, org_id=account.org_id)
        session.commit()
        return _info(source, stored)

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
        stored = _save_endpoint(session, source, body, org_id=account.org_id)
        session.commit()
        return _info(source, stored)

    @router.delete("/{source_id}", status_code=204)
    def remove_source(source_id: UUID, account: AdminDep, session: SessionDep) -> None:
        if not delete_data_source(session, source_id, org_id=account.org_id):
            raise HTTPException(status_code=404, detail="data source not found")
        session.commit()

    return router
