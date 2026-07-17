"""Result sink port: where a completed run's results go (ADR-0018).

The pipeline's output seam, symmetric with the normalize (input) seam. The runner
computes a ``RunResult`` entirely in memory (no storage), then hands it to one or more
sinks; each sink owns *how* it stores or delivers that result. Postgres is the default
sink (and what the read API / dashboard consume); a deployment can add or replace sinks
— a file export today, a webhook/SIEM tomorrow — without touching the runner. The store
is pluggable (framing B of the week-2 design).

Unlike the other ports, a sink declares no ``Capability``: it is a *terminal* consumer
of the whole result, not a typed joint in the transformation chain, so the compatibility
guard does not apply to it. Its extensibility comes from registration + discovery +
swap-by-name, exactly like the other adapters.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from iotshield.contracts import (
    Asset,
    DetectionResult,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
)


@dataclass(frozen=True)
class SourceProvenance:
    """Where one of a run's inputs came from and how it was parsed (ADR-0019).

    A run can ingest several sources (mixed modalities), so provenance is a list of these
    rather than a single normalizer/format/origin triple. Per-record provenance still lives
    on the events (``source_format``) and malformed records (``origin``); this is the
    run-level index of the sources that fed it.
    """

    normalizer: str
    source_format: str
    origin: str


@dataclass(frozen=True)
class RunResult:
    """The complete, computed result of one pipeline run — the unit every sink consumes.

    Produced entirely in memory by the compute phase (no storage), so it is the clean
    hand-off between "what the pipeline found" and "where it goes". Carries the full
    payload (so a file-only sink loses nothing) plus per-source provenance metadata.
    """

    run_at: datetime
    sources: list[SourceProvenance]
    assets: list[Asset]
    events: list[NormalizedEvent]
    malformed: list[MalformedRecord]
    detections: list[DetectionResult]
    alerts: list[PrioritizedAlert]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SinkResources:
    """Deployment-provided handles a sink may need to do its job.

    The injection bag that keeps the port store-agnostic in the abstract while letting a
    concrete sink reach what its deployment owns: the DB session factory for the Postgres
    sink, a confined base directory for file sinks (so a caller-supplied filename cannot
    escape onto arbitrary server paths). A sink takes only what it needs and ignores the
    rest; a new sink that needs a new kind of handle adds a field here.
    """

    session_factory: sessionmaker[Session] | None = None
    output_dir: Path | None = None


class ResultSink(Protocol):
    """Where a run's results are stored or delivered. The pipeline's fourth port."""

    def write(self, result: RunResult) -> None: ...
