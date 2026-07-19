"""The pipeline runner: compute a run's result in memory, then write it to sinks.

Two phases, deliberately separate so the result *store* is pluggable (ADR-0018):

- ``compute_run`` — pure and store-agnostic: for each configured input source,
  normalize -> pool events -> detect (per lane) -> resolve assets (in memory) ->
  prioritize, producing a ``RunResult``. No database is touched.
- ``execute_run`` — hand that result to each configured sink. The Postgres sink (the
  default) owns its own atomic transaction; a file sink writes JSONL; a future webhook
  sink delivers. The runner itself no longer knows anything about databases.

A run ingests a *list* of sources (ADR-0019): each source's content is normalized by its
own adapter, and the events pool into the detector lanes, so one run can synthesize
detections across mixed modalities (telemetry CSV + syslog). The alert engine sees this
run's detections only; cross-run correlation is future work (ADR-0006), as is enriching
assets from a persistent store across runs.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from iotshield.contracts import (
    Asset,
    DetectionResult,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
    discovered_asset,
)
from iotshield.detect import Detector
from iotshield.pipeline.compat import CompatLevel, ensure_compatible
from iotshield.pipeline.config import PipelineConfig, PipelineSpec, SinkConfig
from iotshield.pipeline.registry import (
    build_alert_engine,
    build_detector,
    build_normalizer,
    build_sink,
    sink_requires_database,
)
from iotshield.sink import RunResult, SinkResources, SourceProvenance
from iotshield.storage import create_schema, describe_location, make_engine, make_session_factory

TOP_ALERTS_IN_REPORT = 5

# Above this fraction of rejected input, a source is probably misconfigured (the wrong
# normalizer for the data) rather than merely carrying a few corrupt lines, so the
# report says so (ADR-0017). A default only — not yet operator-tunable.
_MALFORMED_WARN_RATE = 0.5


@dataclass(frozen=True)
class RunReport:
    """End-of-run summary: counts, where the result went, top alerts, warnings."""

    origin: str
    destinations: list[str]
    events: int
    malformed: int
    detections: int
    alerts: int
    top_alerts: list[PrioritizedAlert]
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_result(cls, result: RunResult, *, destinations: list[str]) -> "RunReport":
        return cls(
            # The header names the run's sources; a multi-source run lists them all.
            origin=" + ".join(source.origin for source in result.sources) or "run",
            destinations=destinations,
            events=len(result.events),
            malformed=len(result.malformed),
            detections=len(result.detections),
            alerts=len(result.alerts),
            top_alerts=result.alerts[:TOP_ALERTS_IN_REPORT],
            warnings=list(result.warnings),
        )

    def render(self) -> str:
        lines = [
            f"Pipeline run complete: {self.origin}",
            f"  destinations: {', '.join(self.destinations) or 'none'}",
            f"  events: {self.events}   malformed: {self.malformed}"
            f"   detections: {self.detections}   alerts: {self.alerts}",
        ]
        if self.warnings:
            lines.append("  Warnings:")
            lines.extend(f"   ! {warning}" for warning in self.warnings)
        if self.top_alerts:
            lines.append("  Top alerts:")
            for rank, alert in enumerate(self.top_alerts, start=1):
                lines.append(
                    # ASCII-only separator: Windows consoles mangle UTF-8 punctuation.
                    f"   {rank}. [{alert.severity.value.upper()}] {alert.asset_id}"
                    f" priority={alert.priority_score:.1f} - {alert.rationale}"
                )
        return "\n".join(lines)


def resolve_assets(seeded: Sequence[Asset], events: Sequence[NormalizedEvent]) -> list[Asset]:
    """The alert engine's asset inventory, resolved in memory (no database).

    Registration wins: start from the operator's seeded assets, then add an
    auto-discovered stub for every event ``asset_id`` not already present — the same rule
    the storage layer applies on insert (ADR-0005), sharing ``discovered_asset`` so the
    two can never diverge. Store-agnostic by construction, which is what lets the result
    store be pluggable (ADR-0018). Cross-run enrichment from a persistent store is a
    deferred follow-on.
    """
    resolved: dict[str, Asset] = {asset.asset_id: asset for asset in seeded}
    for asset_id in {event.asset_id for event in events}:
        resolved.setdefault(asset_id, discovered_asset(asset_id))
    return list(resolved.values())


def _high_malformed_warning(normalizer: str, events: int, malformed: int) -> str | None:
    """Flag a source where most input failed to parse — usually the wrong normalizer.

    A few rejects in a healthy feed are normal ("capture, never drop" — ADR-0011); a
    *high* reject rate instead means the input probably doesn't match the chosen
    normalizer at all (e.g. CSV fed to the syslog normalizer), which otherwise hides
    as a big malformed pile that reads like a successful run. The signal is the
    normalizer's own reject count, so there is no rival format-sniffer to disagree
    with it (ADR-0017). Returns None when there is nothing to say.
    """
    total = events + malformed
    if total == 0 or malformed / total < _MALFORMED_WARN_RATE:
        return None
    pct = round(100 * malformed / total)
    return (
        f"the {normalizer!r} normalizer rejected {pct}% of input "
        f"({malformed}/{total} records); the input may not match the chosen normalizer"
    )


def _resolve_origins(spec: PipelineSpec, origins: Sequence[str] | None) -> list[str]:
    """The provenance label for each source: caller-supplied, else the path, else an index."""
    if origins is not None:
        if len(origins) != len(spec.sources):
            raise ValueError("origins must align 1:1 with the configured sources")
        return list(origins)
    return [source.path or f"source[{index}]" for index, source in enumerate(spec.sources)]


@dataclass(frozen=True)
class NormalizedInputs:
    """What the ingest phase produced: the pooled events plus everything to carry forward.

    A phase boundary rather than a contract — it exists so ``normalize_sources`` and
    ``compute_result`` can be composed with a step in between (the evaluation harness fits
    detectors on the events before judging them, ADR-0021) without either phase, or the
    ``compute_run`` composition, changing.
    """

    events: list[NormalizedEvent]
    malformed: list[MalformedRecord]
    provenance: list[SourceProvenance]
    warnings: list[str]


def normalize_sources(
    spec: PipelineSpec,
    inputs: Sequence[str],
    *,
    origins: Sequence[str] | None = None,
) -> NormalizedInputs:
    """Ingest phase: verify the chain, route each source to its normalizer, pool the events.

    ``inputs`` are the raw texts, one per configured source and aligned to ``spec.sources``
    by position (``origins`` likewise). The compatibility guard runs first — ERRORs raise
    before any work, WARNINGs ride forward — joined by the per-source input-appropriateness
    check (ADR-0017).
    """
    if len(inputs) != len(spec.sources):
        raise ValueError(
            f"expected {len(spec.sources)} input(s) for the configured sources, got {len(inputs)}"
        )
    resolved_origins = _resolve_origins(spec, origins)

    warnings = [
        f"[{issue.stage}] {issue.message}"
        for issue in ensure_compatible(spec)
        if issue.level is CompatLevel.WARNING
    ]

    # Build normalizers before reading anything: a bad name/param fails fast, before any
    # work. They dedupe by name — a run with three syslog sources builds one.
    normalizers = {
        source.normalizer: build_normalizer(source.normalizer) for source in spec.sources
    }

    events: list[NormalizedEvent] = []
    malformed: list[MalformedRecord] = []
    provenance: list[SourceProvenance] = []
    for index, source in enumerate(spec.sources):
        origin = resolved_origins[index]
        result = normalizers[source.normalizer].normalize(
            inputs[index], source_format=source.source_format, origin=origin
        )
        quality = _high_malformed_warning(
            source.normalizer, len(result.events), len(result.malformed)
        )
        if quality is not None:
            warnings.append(f"[input:{origin}] {quality}")
        events.extend(result.events)
        malformed.extend(result.malformed)
        provenance.append(SourceProvenance(source.normalizer, source.source_format, origin))

    return NormalizedInputs(
        events=events, malformed=malformed, provenance=provenance, warnings=warnings
    )


def run_detectors(
    detectors: Sequence[Detector], events: Sequence[NormalizedEvent]
) -> list[DetectionResult]:
    """Judge phase: hand each detector its own lane's events, in the order the port promises.

    A detector sees its lane's events sorted by ``(asset_id, timestamp)``, so a
    windowed/lagged model can rely on it even when several sources pool into the same lane
    (ADR-0019). Stable sort: same-timestamp events keep their ingest order.
    """
    detections: list[DetectionResult] = []
    for detector in detectors:
        lane_events = sorted(
            (event for event in events if event.source_type == detector.lane),
            key=lambda event: (event.asset_id, event.timestamp),
        )
        detections.extend(detector.detect(lane_events))
    return detections


def compute_result(
    spec: PipelineSpec,
    ingested: NormalizedInputs,
    *,
    detectors: Sequence[Detector] | None = None,
) -> RunResult:
    """Judge and prioritize already-ingested events — no storage, no re-normalization.

    ``detectors`` defaults to the spec's configured ones. Passing them explicitly is the
    seam the evaluation harness uses to judge with *fitted* detectors while still running
    this exact code (ADR-0021); the product path never passes it. Asset criticalities are
    resolved in memory, so the alert engine needs no database.
    """
    if detectors is None:
        detectors = [build_detector(entry.name, entry.params) for entry in spec.detectors]
    alert_engine = build_alert_engine(spec.alert_engine.name, spec.alert_engine.params)

    detections = run_detectors(detectors, ingested.events)
    assets = resolve_assets(spec.assets, ingested.events)
    asset_map = {asset.asset_id: asset for asset in assets}
    alerts = alert_engine.build_alerts(detections, asset_map)

    return RunResult(
        run_at=datetime.now(UTC),
        sources=ingested.provenance,
        assets=assets,
        events=ingested.events,
        malformed=ingested.malformed,
        detections=detections,
        alerts=alerts,
        warnings=ingested.warnings,
    )


def compute_run(
    spec: PipelineSpec,
    inputs: Sequence[str],
    *,
    origins: Sequence[str] | None = None,
) -> RunResult:
    """Compute a run's full result in memory — no storage — the store-agnostic core.

    The composition of the two phases above, and the product's entry into them. Typed
    ``PipelineSpec``, not ``PipelineConfig``: this phase reads only the operator's chosen
    shape and never the deployment's ``database_url`` or ``sinks``, so taking the narrower
    type makes "compute cannot reach a store" a fact the type checker enforces rather than
    a convention (ADR-0018, ADR-0021). ``PipelineConfig`` still satisfies it by subclassing.
    """
    return compute_result(spec, normalize_sources(spec, inputs, origins=origins))


def _destination_label(sink_config: SinkConfig, config: PipelineConfig) -> str:
    # For the Postgres sink, show the resolved DB location so a cwd/URL mismatch is
    # visible in the report (what the old 'database:' line did); others show their name.
    if sink_config.name == "postgres" and config.database_url:
        return f"postgres ({describe_location(config.database_url)})"
    return sink_config.name


def execute_run(
    config: PipelineConfig,
    inputs: Sequence[str],
    *,
    origins: Sequence[str] | None = None,
    resources: SinkResources,
) -> RunReport:
    """Compute a run, then write it to every configured sink; return the summary.

    Compute is store-agnostic (``compute_run``); the sinks decide where the result goes.
    Sinks run in config order (put the system-of-record sink first); a sink that *raises*
    fails the run — there is no cross-store atomicity, so a best-effort delivery sink must
    swallow its own errors. Guard ERRORs raise inside ``compute_run``, before any sink runs.
    """
    result = compute_run(config, inputs, origins=origins)
    destinations: list[str] = []
    for sink_config in config.sinks:
        sink = build_sink(sink_config.name, sink_config.params, resources)
        sink.write(result)
        destinations.append(_destination_label(sink_config, config))
    return RunReport.from_result(result, destinations=destinations)


def _load_source_texts(
    config: PipelineConfig, input_path: Path | None
) -> tuple[list[str], list[str]]:
    """Read each configured source's content, returning (texts, origins) aligned to sources.

    Two shapes: the legacy single-file CLI (``--input <file>`` for a one-source config), and
    a self-contained multi-source config whose sources each name a ``path``. ``utf-8-sig``
    strips a leading BOM (Excel/Notepad on Windows) that would otherwise reject line 1.
    """
    if input_path is not None:
        if len(config.sources) != 1:
            raise ValueError(
                f"--input names a single file but the config has {len(config.sources)} "
                "sources; give each source its own 'path' instead of --input"
            )
        return [input_path.read_text(encoding="utf-8-sig")], [str(input_path)]

    texts: list[str] = []
    origins: list[str] = []
    for index, source in enumerate(config.sources):
        if source.path is None:
            raise ValueError(
                f"source[{index}] has no 'path'; give each source a 'path', "
                "or pass --input for a single-source config"
            )
        texts.append(Path(source.path).read_text(encoding="utf-8-sig"))
        origins.append(source.path)
    return texts, origins


def run_pipeline(config: PipelineConfig, input_path: Path | None = None) -> RunReport:
    """CLI / standalone entry point: read the configured input(s) and run the pipeline.

    ``input_path`` supplies the content for a single-source config (the legacy ``--input``
    form); otherwise each source's ``path`` is read. Builds a ``SinkResources`` for this
    run, creating a database engine (and schema) only if a configured sink needs one — so a
    file-only run needs no database at all. The Pipeline API instead injects its own
    long-lived engine (ADR-0015).
    """
    inputs, origins = _load_source_texts(config, input_path)
    needs_db = any(sink_requires_database(sink.name) for sink in config.sinks)
    engine = make_engine(config.database_url) if needs_db and config.database_url else None
    try:
        resources = SinkResources()
        if engine is not None:
            create_schema(engine)
            resources = SinkResources(session_factory=make_session_factory(engine))
        return execute_run(config, inputs, origins=origins, resources=resources)
    finally:
        if engine is not None:
            engine.dispose()
