"""FastAPI write-side service — the Pipeline API (control plane).

The operator-facing surface of the productized pipeline: it accepts a ``PipelineSpec``
(which adapters to run) plus one raw input per source, validates the chain with the
compatibility guard, runs it, and returns a run summary. Results are dispatched to the
spec's sinks (Postgres by default, ADR-0018); the read-side API serves the stored ones.

Where results are stored is the server's decision, not the caller's: the app is built
around an injected engine (like the read API) and supplies ``database_url`` itself, so a
caller can never point a run at another database — the tenancy boundary.

This is a *writer* — it evolves ADR-0007's "the runner is the only writer" (the run logic
is still the runner's, now invocable over HTTP). It is a separate app/role from the read
API so the read plane stays write-free, independently scalable, and unaffected by a slow
or crashing run (ADR-0015). Serve: python -m iotshield.api.control
"""

import inspect
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select

import iotshield
from iotshield.api._auth import AccountDep, auth_enabled, authenticate
from iotshield.api._eval_gate import eval_api_enabled
from iotshield.api.execution import (
    MAX_SOURCE_CHARS,
    IssueInfo,
    SourceInput,
    issue_info,
    perform_run,
)
from iotshield.capabilities import formats_in
from iotshield.contracts import MalformedRecord, PrioritizedAlert
from iotshield.pipeline.compat import (
    check_compatibility,
    has_errors,
)
from iotshield.pipeline.config import PipelineSpec, SourceSpec, config_from_spec
from iotshield.pipeline.registry import (
    ALERT_ENGINES,
    DETECTORS,
    NORMALIZERS,
    SINKS,
    AdapterSpec,
    SinkSpec,
    config_parameters,
)
from iotshield.pipeline.runner import (
    RejectionLevel,
    RunReport,
    preview_source,
)
from iotshield.storage import (
    database_url_from_env,
    describe_location,
    make_engine,
    make_session_factory,
)
from iotshield.storage.runs import MAX_NAME_LENGTH as MAX_RUN_NAME_LENGTH

# --- Wire models: the Pipeline API's request/response schema -----------------


class ParamInfo(BaseModel):
    """One tunable of an adapter, introspected from its factory signature."""

    name: str
    type: str | None = None
    default: Any = None
    required: bool
    choices: list[str] | None = None
    """The allowed values, when this param has a closed vocabulary.

    A closed vocabulary is a fact the adapter already knows (a detector preset, a lane, a
    shipped artifact id), so the catalogue reports it rather than making the operator recall
    it. Without this the UI renders any non-numeric param as free text — which is how a
    mistyped preset became a guard error instead of an impossible input (ADR-0023 D9), and
    how the artifact that decides whether a detector can judge at all was a box with nothing
    to type into it (ADR-0035 D1).

    Two sources, one field: a ``Literal[...]`` annotation, or ids the registry discovered
    (``describe_params``'s ``discovered_choices``). A client renders both the same way, which
    is the point — the *shape* of the fact is identical and only its provenance differs.
    """


class ModuleInfo(BaseModel):
    name: str
    kind: str
    consumes: list[str]
    produces: list[str]
    formats: list[str]  # input formats decoded from `consumes` (normalizers); [] otherwise
    params: list[ParamInfo]
    artifact_param: str | None = None
    """Which param names a cached fit, when the adapter needs one to judge (ADR-0022).

    Published here — not only on the Evaluate side's ``DetectorSummary`` — because it is
    what separates "ready to run" from "untrained until you name weights", and a *run*
    refuses the latter outright. Without it the compose page can only rank detectors by
    lane, so it will happily open on a chain that cannot run (which is what `meter_iforest`
    did on arrival, sorting ahead of `stub_threshold`). Declared, never name-sniffed.
    """


class SinkInfo(BaseModel):
    """A result sink for the catalogue: no capability (terminal consumer), just params."""

    name: str
    params: list[ParamInfo]


class ModulesResponse(BaseModel):
    """The adapter catalogue that drives the orchestration UI's menus."""

    normalizers: list[ModuleInfo]
    detectors: list[ModuleInfo]
    alert_engines: list[ModuleInfo]
    sinks: list[SinkInfo]


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: PipelineSpec


class ValidateResponse(BaseModel):
    ok: bool  # True when there are no ERROR-level issues (WARNINGs still ok)
    issues: list[IssueInfo]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: PipelineSpec
    inputs: list[SourceInput] = Field(
        min_length=1, description="One raw input per source in spec.sources (same order)"
    )
    name: str | None = Field(
        default=None,
        max_length=MAX_RUN_NAME_LENGTH,
        description="What the saved run is called (ADR-0046); blank names it after its inputs",
    )


PREVIEW_REJECTS_SHOWN = 3


class PreviewRequest(BaseModel):
    """One source and its content, read by its normalizer alone — nothing is judged or stored."""

    model_config = ConfigDict(extra="forbid")

    source: SourceSpec
    content: str = Field(
        max_length=MAX_SOURCE_CHARS,
        description=f"The source's raw content, at most {MAX_SOURCE_CHARS:,} characters",
    )


class RejectedLine(BaseModel):
    line: int | None = Field(description="1-based line number in the content, when it can be found")
    raw: str = Field(description="The rejected record, truncated to 200 characters")
    error: str


class PreviewResponse(BaseModel):
    """How much of one source its normalizer could read (ADR-0017, before the run)."""

    events: int
    malformed: int
    level: RejectionLevel
    rejected: list[RejectedLine] = Field(
        description=f"The first {PREVIEW_REJECTS_SHOWN} rejected records, in input order"
    )


def _preview_response(
    content: str, events: int, malformed: Sequence[MalformedRecord], level: RejectionLevel
) -> PreviewResponse:
    # A MalformedRecord carries its line verbatim but not where it was; finding it again is
    # what lets the page say "line 7" rather than only quoting it.
    line_of: dict[str, int] = {}
    for number, line in enumerate(content.splitlines(), start=1):
        line_of.setdefault(line.strip(), number)
    return PreviewResponse(
        events=events,
        malformed=len(malformed),
        level=level,
        rejected=[
            RejectedLine(
                line=line_of.get(record.raw.strip()), raw=record.raw[:200], error=record.error
            )
            for record in malformed[:PREVIEW_REJECTS_SHOWN]
        ],
    )


class RunResponse(BaseModel):
    """A run summary. Full results are read back from the read-side API, by ``run_id``."""

    run_id: UUID
    events: int
    malformed: int
    detections: int
    alerts: int
    warnings: list[str]
    destinations: list[str]  # where the result was written (the configured sinks)
    top_alerts: list[PrioritizedAlert]


# --- Registry introspection (drives /modules) --------------------------------


def _optional_inner(annotation: Any) -> Any:
    """``X | None`` -> ``X``; anything else unchanged.

    Learning adapters declare every hyperparameter as ``T | None`` — ``None`` is the
    "not given" sentinel ADR-0022 needs — so this unwrapping decides what a UI sees for
    almost every tunable in the registry. Shared by both describers below because they
    must agree about what the annotation *is*: when only the type name unwrapped,
    ``Literal["water", "electricity"] | None`` reported its type through the union and its
    choices through the raw annotation, so a closed vocabulary published ``choices: null``
    and rendered as a free-text box (the failure ADR-0023 D9 exists to prevent).

    A ``Literal`` is returned untouched. Its arguments are *values*, not types, so the
    one-member ``Literal["water"]`` would otherwise unwrap to the string ``"water"``.
    """
    if get_origin(annotation) is Literal:
        return annotation
    inner = [arg for arg in get_args(annotation) if arg is not type(None)]
    return inner[0] if len(inner) == 1 else annotation


def _type_name(annotation: Any) -> str | None:
    """The name a form should type a widget by, seeing through ``X | None``."""
    if annotation is inspect.Parameter.empty:
        return None
    annotation = _optional_inner(annotation)
    # A closed vocabulary (ADR-0023 D9) reports the type of its *members*, which are
    # strings by construction. Without this the name would be "Literal", which no client
    # can map to a widget.
    if get_origin(annotation) is Literal:
        return "str"
    return getattr(annotation, "__name__", None)


def describe_params(
    factory: Any,
    params_type: type[BaseModel] | None = None,
    discovered_choices: Mapping[str, Sequence[str]] | None = None,
) -> list[ParamInfo]:
    """Introspect a factory's configuration surface: the adapter's tunables.

    ``params_type`` supplies the *real* defaults when given. A learning adapter's factory
    params all default to ``None`` (the sentinel above), so the signature alone would tell
    a UI nothing to prefill; its hyperparameter model holds the true values (ADR-0022).
    Public because the evaluation API reports the same schema on ``/eval/detectors``, so
    the Evaluate page can build a hyperparameter form from one call.

    ``discovered_choices`` carries a closed vocabulary the *signature cannot express*: the
    artifact ids an adapter ships and the rule sets on a deployment's shelf are both files
    discovered at runtime, so the widest annotation writable for them is a bare ``str`` (or
    ``str | None`` where "not given" means something, as it does for an artifact — ADR-0035 D1,
    ADR-0037). Supplying them here rather than as a field of their own means every form that
    already renders a closed
    vocabulary as a ``<select>`` renders these too, and there is one representation of "allowed
    values" on the wire instead of two that can disagree.
    """
    fields = params_type.model_fields if params_type is not None else {}
    discovered = discovered_choices or {}
    params: list[ParamInfo] = []
    for parameter in config_parameters(factory):
        has_default = parameter.default is not inspect.Parameter.empty
        default = parameter.default if has_default else None
        if default is None and parameter.name in fields:
            default = fields[parameter.name].get_default(call_default_factory=True)
        # `if found` rather than `is not None`: a vocabulary that came back empty publishes no
        # choices at all, never an empty `<select>`. That rule lives here, once, so a caller of
        # this public function cannot publish one by supplying `{param: []}` — an adapter
        # packaged without its artifacts falls back to free text, the ADR-0035 D1 escape hatch.
        found = discovered.get(parameter.name)
        choices = list(found) if found else _literal_choices(parameter.annotation)
        params.append(
            ParamInfo(
                name=parameter.name,
                type=_type_name(parameter.annotation),
                default=default,
                required=not has_default,
                choices=choices,
            )
        )
    return params


def model_params(model: type[BaseModel]) -> list[ParamInfo]:
    """A pydantic params model's fields as the catalogue's ``ParamInfo``.

    For a surface with no factory to introspect — a connector's pull parameters (ADR-0042) are
    a model and nothing else. Reading the fields directly also avoids the synthesized
    signature, whose default for a ``default_factory`` field is a pydantic sentinel.
    """
    return [
        ParamInfo(
            name=name,
            type=_type_name(field.annotation),
            default=None if field.is_required() else field.get_default(call_default_factory=True),
            required=field.is_required(),
            choices=_literal_choices(field.annotation),
        )
        for name, field in model.model_fields.items()
    ]


def params_of(spec: AdapterSpec[Any]) -> list[ParamInfo]:
    """The configuration surface of a registered adapter, as both catalogues publish it.

    Every argument ``describe_params`` needs comes from the one ``AdapterSpec``, so asking
    for the spec is the honest signature and there is nothing for a caller to assemble.
    That matters more than it looks: a caller who forgets the discovered choices silently
    publishes a free-text box where a closed vocabulary exists, which is the defect ADR-0035
    D1 fixes — and the test helper in ``tests/pipeline/test_capabilities.py`` was exactly
    that mistake until this call became one thing to copy.

    ``describe_params`` stays public underneath for ``_sink_info``, which has a factory and
    no spec.

    The spec's vocabularies are resolved *here*, per call, because one of them is a directory
    the operator may add to while the server runs (ADR-0037). What an empty result publishes is
    ``describe_params``'s rule, not restated here.
    """
    return describe_params(
        spec.factory,
        spec.params_type,
        {param: find() for param, find in spec.discovered.items()},
    )


def _literal_choices(annotation: Any) -> list[str] | None:
    """The allowed values of a ``Literal[...]`` annotation, else None.

    Read off the factory signature so a closed vocabulary is declared exactly once — in the
    factory that already validates it — instead of being restated in a UI list that can
    drift. Meta-tests pin each Literal against the vocabulary it mirrors.

    Sees through ``X | None`` for the same reason ``_type_name`` does: an optional closed
    vocabulary is still a closed vocabulary, and a client that is handed no choices has
    nothing to render but a text box.
    """
    annotation = _optional_inner(annotation)
    if get_origin(annotation) is not Literal:
        return None
    return [str(value) for value in get_args(annotation)]


def _module_info(kind: str, name: str, spec: AdapterSpec[Any]) -> ModuleInfo:
    # Default-params shape; the UI edits params to change a param-driven capability.
    capability = spec.capability({})
    return ModuleInfo(
        name=name,
        kind=kind,
        consumes=sorted(token.render() for token in capability.consumes),
        produces=sorted(token.render() for token in capability.produces),
        formats=sorted(formats_in(capability.consumes)),
        params=params_of(spec),
        artifact_param=spec.artifact_param,
    )


def _sink_info(name: str, spec: SinkSpec) -> SinkInfo:
    return SinkInfo(name=name, params=describe_params(spec.factory))


def _run_response(report: RunReport) -> RunResponse:
    return RunResponse(
        run_id=report.run_id,
        events=report.events,
        malformed=report.malformed,
        detections=report.detections,
        alerts=report.alerts,
        warnings=report.warnings,
        destinations=report.destinations,
        top_alerts=report.top_alerts,
    )


def _modules_response() -> ModulesResponse:
    """The adapter catalogue, built per request.

    This was a module-level constant, on the reasoning that the registries are module-level
    constants and therefore so is the catalogue. That held until a param's vocabulary became a
    *directory the operator may add to while the server runs* (ADR-0037): the registry entry is
    still invariant, but one of the answers it publishes is not, and a catalogue snapshotted at
    import would have served the shelf as it looked when the process started. Every layer
    underneath already resolved it freshly (that is why ``AdapterSpec.discovered`` holds
    callables), so caching here was the one place that made the whole chain stale.

    Measured at 0.84 ms for a full rebuild — a signature introspection per parameter per
    adapter, on an endpoint a client hits when a page loads. ``/eval/specs`` had already made
    this trade for the same reason, and against a cost it stated in the same terms.
    """
    return ModulesResponse(
        normalizers=[_module_info("normalizer", n, s) for n, s in NORMALIZERS.items()],
        detectors=[_module_info("detector", n, s) for n, s in DETECTORS.items()],
        alert_engines=[_module_info("alert_engine", n, s) for n, s in ALERT_ENGINES.items()],
        sinks=[_sink_info(n, s) for n, s in SINKS.items()],
    )


# --- App ---------------------------------------------------------------------


def create_control_app(engine: Engine, *, auth: bool | None = None) -> FastAPI:
    """Build the Pipeline API around an injected engine (tests pass SQLite; prod Postgres).

    ``auth`` overrides ``IOTSHIELD_AUTH``. On, every route but ``/health`` and the login
    routes needs a session, the ``/auth`` router is registered, and a run's results are
    stored under the caller's organization (ADR-0039).
    """
    app = FastAPI(
        title="iotshield Pipeline API",
        version=iotshield.__version__,
        dependencies=[Depends(authenticate)],
    )
    app.state.session_factory = make_session_factory(engine)
    app.state.auth_enabled = auth_enabled() if auth is None else auth
    # Only used to fill the run report's (unexposed) location field; the run persists
    # through the injected engine, never a caller-named database.
    app.state.database_url = engine.url.render_as_string(hide_password=False)
    # If an operator selects a file sink, it writes under this server-configured dir; a
    # caller-supplied filename is confined to it (sink/file.py). Overridable per deploy.
    app.state.output_dir = Path(os.environ.get("IOTSHIELD_OUTPUT_DIR", "results"))

    @app.get("/health")
    def health() -> dict[str, str]:
        with app.state.session_factory() as session:
            session.execute(select(1))  # prove DB connectivity, not just liveness
        return {"status": "ok", "version": iotshield.__version__}

    @app.get("/modules")
    def modules() -> ModulesResponse:
        """The adapter catalogue: names, capabilities, and param schemas (built per request)."""
        return _modules_response()

    @app.post("/runs/validate")
    def validate_run(request: ValidateRequest) -> ValidateResponse:
        """Dry-run the compatibility guard: no execution, just the issues (for live UI feedback)."""
        config = config_from_spec(request.spec, app.state.database_url)
        issues = check_compatibility(config)
        return ValidateResponse(ok=not has_errors(issues), issues=[issue_info(i) for i in issues])

    @app.post("/runs/preview")
    def preview_run_source(request: PreviewRequest) -> PreviewResponse:
        """Read one source with its normalizer only, so a wrong format shows before the run.

        Nothing is detected or stored. 422 when the normalizer cannot even be asked: an
        unknown name, bad params, or a format it does not read.
        """
        try:
            preview = preview_source(request.source, request.content)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail={"message": str(exc), "issues": []}
            ) from exc
        return _preview_response(request.content, preview.events, preview.malformed, preview.level)

    @app.post("/runs")
    def create_run(request: RunRequest, account: AccountDep) -> RunResponse:
        """Run the pipeline and return a summary; 422 with issues if the chain is incompatible.

        The checks, the streaming of pulls and the mapping of failures to statuses are
        ``api/execution.py``'s, shared with the job worker (ADR-0050).
        """
        config = config_from_spec(request.spec, app.state.database_url)
        report = perform_run(
            config,
            request.inputs,
            session_factory=app.state.session_factory,
            output_dir=app.state.output_dir,
            org_id=account.org_id,
            run_name=request.name,
            run_by=account.user.username,
        )
        return _run_response(report)

    # An organization's register of its IT and OT data sources (ADR-0041). Always mounted:
    # with auth off it is the default organization's, like every other route here. Imported
    # here because it reuses this module's ParamInfo and source-size limit.
    from iotshield.api.data_sources import create_data_sources_router

    app.include_router(create_data_sources_router())

    # Saved runs (ADR-0046): beside POST /runs, which creates them. Always mounted, like the
    # data sources, because renaming and deleting write.
    from iotshield.api.runs import create_runs_router

    app.include_router(create_runs_router())

    # Saved jobs (ADR-0050): a chain of pulls run again on demand or on a schedule, by the
    # `worker` service. Always mounted; with no worker running, a queued job simply waits.
    from iotshield.api.jobs import create_jobs_router

    app.include_router(create_jobs_router())

    # The evaluation plane is opt-in and *absent* otherwise: no route to reach or probe in a
    # product deployment, rather than a guard inside a live handler (ADR-0021 D1). Imported
    # lazily so the eval package can be deleted from a product image without breaking this
    # module's import.
    if eval_api_enabled():
        from iotshield.api.evaluation import create_eval_router

        app.include_router(create_eval_router())

    # Absent rather than guarded when auth is off, like the eval router: the dashboard reads
    # a 404 from /auth/me as "this deployment has no login" (ADR-0039).
    if app.state.auth_enabled:
        from iotshield.api.admin import create_admin_router
        from iotshield.api.auth import create_auth_router

        app.include_router(create_auth_router())
        # The super admin's organization management (ADR-0040), absent with auth off too.
        app.include_router(create_admin_router())

    return app


def main() -> int:
    url = database_url_from_env()
    if url is None:
        return 2
    host = os.environ.get("IOTSHIELD_PIPELINE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("IOTSHIELD_PIPELINE_API_PORT", "8100"))
    print(
        f"iotshield Pipeline API on {host}:{port} using database: {describe_location(url)}",
        file=sys.stderr,
    )
    uvicorn.run(create_control_app(make_engine(url)), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
