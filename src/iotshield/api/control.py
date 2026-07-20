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
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select

import iotshield
from iotshield.api._eval_gate import eval_api_enabled
from iotshield.capabilities import formats_in
from iotshield.contracts import PrioritizedAlert
from iotshield.pipeline.compat import (
    CompatIssue,
    IncompatiblePipelineError,
    check_compatibility,
    has_errors,
)
from iotshield.pipeline.config import PipelineSpec, config_from_spec
from iotshield.pipeline.registry import (
    ALERT_ENGINES,
    DETECTORS,
    NORMALIZERS,
    SINKS,
    AdapterSpec,
    SinkSpec,
    config_parameters,
)
from iotshield.pipeline.runner import RunReport, execute_run
from iotshield.sink import SinkResources
from iotshield.storage import (
    database_url_from_env,
    describe_location,
    make_engine,
    make_session_factory,
)

# --- Wire models: the Pipeline API's request/response schema -----------------


class ParamInfo(BaseModel):
    """One tunable of an adapter, introspected from its factory signature."""

    name: str
    type: str | None = None
    default: Any = None
    required: bool
    choices: list[str] | None = None
    """The allowed values, when the factory annotates the param ``Literal[...]``.

    A closed vocabulary is a fact the factory already knows (a detector preset, a lane), so
    the catalogue reports it rather than making the operator recall it. Without this the UI
    renders any non-numeric param as free text — which is how a mistyped preset became a
    guard error instead of an impossible input (ADR-0023 D9).
    """


class ModuleInfo(BaseModel):
    name: str
    kind: str
    consumes: list[str]
    produces: list[str]
    formats: list[str]  # input formats decoded from `consumes` (normalizers); [] otherwise
    params: list[ParamInfo]


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


class IssueInfo(BaseModel):
    level: str
    stage: str
    message: str


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: PipelineSpec


class ValidateResponse(BaseModel):
    ok: bool  # True when there are no ERROR-level issues (WARNINGs still ok)
    issues: list[IssueInfo]


class SourceInput(BaseModel):
    """One source's raw payload, aligned to ``spec.sources`` by position."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="Raw input payload for this source")
    origin: str = Field(default="api-run", description="Provenance label for this source")


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: PipelineSpec
    inputs: list[SourceInput] = Field(
        min_length=1, description="One raw input per source in spec.sources (same order)"
    )


class RunResponse(BaseModel):
    """A run summary. Full results are read back from the read-side API."""

    events: int
    malformed: int
    detections: int
    alerts: int
    warnings: list[str]
    destinations: list[str]  # where the result was written (the configured sinks)
    top_alerts: list[PrioritizedAlert]


# --- Registry introspection (drives /modules) --------------------------------


def _type_name(annotation: Any) -> str | None:
    """The name a form should type a widget by, seeing through ``X | None``.

    Learning adapters declare every hyperparameter as ``T | None`` — ``None`` is the
    "not given" sentinel ADR-0022 needs — and a union has no ``__name__``, so without this
    every one of them would reach a UI as an untyped text box.
    """
    if annotation is inspect.Parameter.empty:
        return None
    # A closed vocabulary (ADR-0023 D9) reports the type of its *members*, which are
    # strings by construction. Without this the name would be "Literal", which no client
    # can map to a widget — and the union-unwrapping below would mangle a one-member
    # Literal into the value itself.
    if get_origin(annotation) is Literal:
        return "str"
    inner = [arg for arg in get_args(annotation) if arg is not type(None)]
    if len(inner) == 1:
        annotation = inner[0]
    return getattr(annotation, "__name__", None)


def describe_params(factory: Any, params_type: type[BaseModel] | None = None) -> list[ParamInfo]:
    """Introspect a factory's configuration surface: the adapter's tunables.

    ``params_type`` supplies the *real* defaults when given. A learning adapter's factory
    params all default to ``None`` (the sentinel above), so the signature alone would tell
    a UI nothing to prefill; its hyperparameter model holds the true values (ADR-0022).
    Public because the evaluation API reports the same schema on ``/eval/detectors``, so
    the Evaluate page can build a hyperparameter form from one call.
    """
    fields = params_type.model_fields if params_type is not None else {}
    params: list[ParamInfo] = []
    for parameter in config_parameters(factory):
        has_default = parameter.default is not inspect.Parameter.empty
        default = parameter.default if has_default else None
        if default is None and parameter.name in fields:
            default = fields[parameter.name].get_default(call_default_factory=True)
        choices = _literal_choices(parameter.annotation)
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


def _literal_choices(annotation: Any) -> list[str] | None:
    """The allowed values of a ``Literal[...]`` annotation, else None.

    Read off the factory signature so a closed vocabulary is declared exactly once — in the
    factory that already validates it — instead of being restated in a UI list that can
    drift. Meta-tests pin each Literal against the vocabulary it mirrors.
    """
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
        params=describe_params(spec.factory, spec.params_type),
    )


def _sink_info(name: str, spec: SinkSpec) -> SinkInfo:
    return SinkInfo(name=name, params=describe_params(spec.factory))


def _issue_info(issue: CompatIssue) -> IssueInfo:
    return IssueInfo(level=issue.level.value, stage=issue.stage, message=issue.message)


def _run_response(report: RunReport) -> RunResponse:
    return RunResponse(
        events=report.events,
        malformed=report.malformed,
        detections=report.detections,
        alerts=report.alerts,
        warnings=report.warnings,
        destinations=report.destinations,
        top_alerts=report.top_alerts,
    )


# The adapter catalogue is invariant for the process (the registries are module-level
# constants), so build it once rather than re-introspecting every factory per request.
_MODULES_RESPONSE = ModulesResponse(
    normalizers=[_module_info("normalizer", n, s) for n, s in NORMALIZERS.items()],
    detectors=[_module_info("detector", n, s) for n, s in DETECTORS.items()],
    alert_engines=[_module_info("alert_engine", n, s) for n, s in ALERT_ENGINES.items()],
    sinks=[_sink_info(n, s) for n, s in SINKS.items()],
)


# --- App ---------------------------------------------------------------------


def create_control_app(engine: Engine) -> FastAPI:
    """Build the Pipeline API around an injected engine (tests pass SQLite; prod Postgres)."""
    app = FastAPI(title="iotshield Pipeline API", version=iotshield.__version__)
    app.state.session_factory = make_session_factory(engine)
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
        """The adapter catalogue: names, capabilities, and param schemas (built once)."""
        return _MODULES_RESPONSE

    @app.post("/runs/validate")
    def validate_run(request: ValidateRequest) -> ValidateResponse:
        """Dry-run the compatibility guard: no execution, just the issues (for live UI feedback)."""
        config = config_from_spec(request.spec, app.state.database_url)
        issues = check_compatibility(config)
        return ValidateResponse(ok=not has_errors(issues), issues=[_issue_info(i) for i in issues])

    @app.post("/runs")
    def create_run(request: RunRequest) -> RunResponse:
        """Run the pipeline and return a summary; 422 with issues if the chain is incompatible.

        The guard runs once, inside execute_run, which raises IncompatiblePipelineError on
        an ERROR-level issue; the runner owns the 'what blocks a run' policy and we just map
        its typed error to a structured 422. It raises before any row is written.
        """
        config = config_from_spec(request.spec, app.state.database_url)
        if len(request.inputs) != len(config.sources):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        f"expected {len(config.sources)} input(s) to match spec.sources, "
                        f"got {len(request.inputs)}"
                    ),
                    "issues": [],
                },
            )
        try:
            report = execute_run(
                config,
                [item.content for item in request.inputs],
                origins=[item.origin for item in request.inputs],
                resources=SinkResources(
                    session_factory=app.state.session_factory,
                    output_dir=app.state.output_dir,
                ),
            )
        except IncompatiblePipelineError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "incompatible pipeline configuration",
                    "issues": [_issue_info(i).model_dump() for i in exc.issues],
                },
            ) from exc
        return _run_response(report)

    # The evaluation plane is opt-in and *absent* otherwise: no route to reach or probe in a
    # product deployment, rather than a guard inside a live handler (ADR-0021 D1). Imported
    # lazily so the eval package can be deleted from a product image without breaking this
    # module's import.
    if eval_api_enabled():
        from iotshield.api.evaluation import create_eval_router

        app.include_router(create_eval_router())

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
