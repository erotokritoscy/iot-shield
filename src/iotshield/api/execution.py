"""Running a composed chain on an organization's behalf: what ``POST /runs`` and a job share.

A run from the Compose page and a run of a saved job (ADR-0050) must refuse the same things
with the same words and store the same rows, so the checking of pulls, the streaming of their
windows and the mapping of a failed run to a status live here once. The route calls it inside
a request; the job worker calls it with nobody logged in, which is why everything takes the
tenant (``org_id``) and the author (``run_by``) rather than an account.

Failures are raised as ``HTTPException`` with the Pipeline API's ``{message, issues}`` detail,
because the route is the first caller and its responses must not change. The worker reads the
same detail back with ``failure_message`` — one wording, two readers.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.capabilities import lanes_in
from iotshield.connect import ConnectionUnavailable, PullRefused, PullTarget
from iotshield.pipeline.compat import CompatIssue, IncompatiblePipelineError
from iotshield.pipeline.config import PipelineConfig, SourceSpec
from iotshield.pipeline.registry import NORMALIZERS
from iotshield.pipeline.runner import Feed, ProgressCallback, RunReport, execute_run, text_rows
from iotshield.sink import SinkResources
from iotshield.storage.data_sources import DataSource

MAX_SOURCE_CHARS = 4_000_000
"""Ceiling on one source's payload — a declared limit, not an OOM.

A run executes synchronously inside the request and holds the whole payload plus the events
it fans out into, each retaining its `raw` line: measured at ~41x amplification (4.3 MB of
CSV -> ~177 MB of heap, 3.5 s of one worker). Unbounded, a handful of concurrent requests
takes the container down, and the caller learns nothing. Declared here so the failure is a
422 naming the field instead.

Keep this, nginx's `client_max_body_size` and the README's stated limit in agreement — three
places that must not drift, which is why each names the others (ADR-0033).
"""


class PullInput(BaseModel):
    """A window of an organization's data source, read by the server when the run starts."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID = Field(description="The data source to pull from (it names a connection)")
    start: date = Field(description="First day, inclusive, in the source's own clock")
    end: date = Field(description="Last day, inclusive")
    params: dict[str, Any] = Field(default_factory=dict, description="The connector's pull params")


class SourceInput(BaseModel):
    """One source's input, aligned to ``spec.sources`` by position: its text, or a pull.

    A pull is a *reference* (ADR-0047): the server streams the window from the source's
    database during the run, a batch of assets at a time, so it has no size limit and no window
    limit (ADR-0053). ``content`` is text the caller already holds — an upload, a paste —
    and keeps its ceiling, because it travels in this request.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None,
        max_length=MAX_SOURCE_CHARS,
        description=(f"Raw input payload for this source, at most {MAX_SOURCE_CHARS:,} characters"),
    )
    pull: PullInput | None = Field(
        default=None, description="Read this source from a data source instead (ADR-0047)"
    )
    origin: str = Field(default="api-run", description="Provenance label for this source")

    @model_validator(mode="after")
    def _one_input(self) -> "SourceInput":
        if (self.content is None) == (self.pull is None):
            raise ValueError("give a source exactly one of 'content' and 'pull'")
        return self


class IssueInfo(BaseModel):
    level: str
    stage: str
    message: str


def issue_info(issue: CompatIssue) -> IssueInfo:
    return IssueInfo(level=issue.level.value, stage=issue.stage, message=issue.message)


def refused(message: str, issues: Sequence[CompatIssue] = ()) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"message": message, "issues": [issue_info(i).model_dump() for i in issues]},
    )


def failure_message(exc: HTTPException) -> str:
    """The words a refused or failed run would have shown on the Compose page, as one line."""
    detail = exc.detail
    if isinstance(detail, Mapping):
        message = str(detail.get("message", ""))
        issues = [
            f"{issue.get('stage')}: {issue.get('message')}"
            for issue in detail.get("issues", [])
            if isinstance(issue, Mapping) and issue.get("level") == "error"
        ]
        return "; ".join([message, *issues]) if issues else message
    return str(detail)


def reader_params_mismatch(source: SourceSpec, required: Mapping[str, str]) -> str | None:
    """Why ``source`` cannot read its pull with the params it was given, or None.

    Compared through the normalizer's declared capability rather than as raw params, so a
    param left at its default reads the same as one spelled out: an API source mapped to the
    log lane must be read with ``lane: log``, and ``passthrough``'s default (telemetry) would
    reject every line it is handed (ADR-0049 D5). Only the lane is compared: a payload the
    operator declares beside it is a claim about the data the target cannot contradict.
    """
    if not required:
        return None
    spec = NORMALIZERS.get(source.normalizer)
    if spec is None:  # pragma: no cover - the spec was validated against the registry
        return None
    try:
        given = spec.capability(source.params).produces
    except ValueError as exc:
        return str(exc)
    if lanes_in(given) != lanes_in(spec.capability(dict(required)).produces):
        wanted = ", ".join(f"{name}: {value}" for name, value in sorted(required.items()))
        return f"this data source must be read with {source.normalizer} params {{{wanted}}}"
    return None


def checked_pull(
    index: int, source: SourceSpec, pull: PullInput, session: Session, *, org_id: str
) -> tuple[DataSource, PullTarget]:
    """The pull's data source and target, once everything that can refuse it has been asked.

    The source is this organization's, its connection is allowed, the spec reads it with its
    connector's own normalizer, format and lane, and the window and params are valid. Nothing
    is read: a job is checked this way when it is saved, and every run before it streams.
    """
    from iotshield.api.data_sources import pull_target  # it imports the control module

    data_source, target = pull_target(pull.source_id, session, org_id=org_id)
    if (source.normalizer, source.source_format) != (target.normalizer, target.source_format):
        raise refused(
            f"source {index + 1} is read with {source.normalizer!r} "
            f"({source.source_format}), but its pull is "
            f"{target.source_format} for {target.normalizer!r}"
        )
    mismatch = reader_params_mismatch(source, target.normalizer_params)
    if mismatch is not None:
        raise refused(f"source {index + 1}: {mismatch}")
    try:
        target.check(pull.start, pull.end, pull.params)
    except PullRefused as exc:
        raise refused(f"source {index + 1}: {exc}") from exc
    return data_source, target


def resolve_inputs(
    config: PipelineConfig,
    inputs: Sequence[SourceInput],
    session_factory: sessionmaker[Session],
    *,
    org_id: str,
) -> list[str | Feed]:
    """Each input as the runner takes it: text as it came, a pull as a lazy stream.

    Every pull is checked before any is read, so a bad third source is refused before the
    first is streamed, and before anything is stored.
    """
    if len(inputs) != len(config.sources):
        raise refused(
            f"expected {len(config.sources)} input(s) to match spec.sources, got {len(inputs)}"
        )
    resolved: list[str | Feed] = []
    with session_factory() as session:
        for index, (item, source) in enumerate(zip(inputs, config.sources, strict=True)):
            if item.pull is None:
                content = item.content or ""
                resolved.append(Feed(chunks=[content], origin=item.origin, rows=text_rows(content)))
                continue
            pulled = item.pull
            data_source, target = checked_pull(index, source, pulled, session, org_id=org_id)
            origin = (
                item.origin
                if item.origin != "api-run"
                else f"{data_source.name} {pulled.start.isoformat()}..{pulled.end.isoformat()}"
            )
            resolved.append(
                Feed(
                    chunks=target.chunks(pulled.start, pulled.end, pulled.params),
                    origin=origin,
                    pull=pulled.model_dump(mode="json"),
                    rows=_count(target, pulled),
                    header_lines=target.header_lines,
                )
            )
    return resolved


def _count(target: PullTarget, pulled: PullInput) -> int | None:
    """The window's row count for the progress bar (ADR-0051), or None if it cannot be had.

    Only a progress total: a database that fails to count fails the read too, a moment later,
    with the run's own 502 — so failing here would only change which words the caller sees.
    """
    try:
        return target.count(pulled.start, pulled.end, pulled.params)
    except (ConnectionUnavailable, PullRefused):
        return None


def perform_run(
    config: PipelineConfig,
    inputs: Sequence[SourceInput],
    *,
    session_factory: sessionmaker[Session],
    output_dir: Path,
    org_id: str,
    run_name: str | None,
    run_by: str | None,
    progress: ProgressCallback | None = None,
) -> RunReport:
    """Run and store the chain for ``org_id``, or raise the ``HTTPException`` that says why not.

    ``progress`` is handed to ``execute_run`` (ADR-0051).

    The guard runs once, inside ``execute_run``, which raises ``IncompatiblePipelineError`` on
    an ERROR-level issue before any row is written; this maps each typed failure to a status.
    """
    feeds = resolve_inputs(config, inputs, session_factory, org_id=org_id)
    try:
        return execute_run(
            config,
            feeds,
            resources=SinkResources(
                session_factory=session_factory,
                output_dir=output_dir,
                # Who asked decides whose run it is — never anything in the request body.
                org_id=org_id,
                run_name=run_name,
                run_by=run_by,
            ),
            progress=progress,
        )
    except IncompatiblePipelineError as exc:
        raise refused("incompatible pipeline configuration", exc.issues) from exc
    except ConnectionUnavailable as exc:
        # A pulled source's database failed mid-run. Every writer was aborted, so nothing
        # was stored; the message names the connection and nothing about where it is.
        raise HTTPException(status_code=502, detail={"message": str(exc), "issues": []}) from exc
    except ValueError as exc:
        # A bad param *value* (min_count=1, a negative threshold, an artifact combined
        # with hyperparameters) is refused at construction, inside execute_run and
        # before any sink runs. The guard cannot catch it — it reads declared
        # capabilities without building adapters — so validate() legitimately passes and
        # this is the first point the run is known to be impossible. That is a caller
        # error, not a server fault: without this it left as a bare 500 and the adapter's
        # own carefully worded message ("min_count must be >= 2") reached nobody.
        raise refused(str(exc)) from exc
    except Exception as exc:
        # Everything else — an OSError from a sink, the database going away mid-write —
        # used to escape as a bare 500 with an empty body. Sinks run in config order and
        # there is no cross-store atomicity, so the caller could get that opaque 500 for a
        # run whose Postgres rows were already committed, with nothing saying so. Naming
        # the failure — quoting the sink's own error — is what makes the outcome legible
        # instead of ambiguous (ADR-0033).
        raise HTTPException(
            status_code=500,
            detail={"message": f"the run failed while writing its result: {exc}", "issues": []},
        ) from exc
