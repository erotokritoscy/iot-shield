"""The pipeline runner: compute a run's result in memory, then write it to sinks.

Two phases, deliberately separate so the result *store* is pluggable (ADR-0018):

- ``compute_run`` — pure and store-agnostic: normalize -> detect -> resolve assets (in
  memory) -> prioritize, producing a ``RunResult``. No database is touched.
- ``execute_run`` — hand that result to each configured sink. The Postgres sink (the
  default) owns its own atomic transaction; a file sink writes JSONL; a future webhook
  sink delivers. The runner itself no longer knows anything about databases.

The alert engine sees this run's detections only; cross-run correlation is future work
(ADR-0006), as is enriching assets from a persistent store across runs.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from iotshield.contracts import (
    Asset,
    DetectionResult,
    NormalizedEvent,
    PrioritizedAlert,
    discovered_asset,
)
from iotshield.pipeline.compat import CompatLevel, ensure_compatible
from iotshield.pipeline.config import PipelineConfig, SinkConfig
from iotshield.pipeline.registry import (
    build_alert_engine,
    build_detector,
    build_normalizer,
    build_sink,
    sink_requires_database,
)
from iotshield.sink import RunResult, SinkResources
from iotshield.storage import create_schema, describe_location, make_engine, make_session_factory

TOP_ALERTS_IN_REPORT = 5

# Above this fraction of rejected input, a run is probably misconfigured (the wrong
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
            origin=result.origin,
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
    """Flag a run where most input failed to parse — usually the wrong normalizer.

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


def compute_run(config: PipelineConfig, text: str, *, origin: str) -> RunResult:
    """Compute a run's full result in memory — no storage — the store-agnostic core.

    The compatibility guard runs here (ERRORs raise before any work; WARNINGs ride into
    the result), as does the input-appropriateness check (ADR-0017). Asset criticalities
    are resolved in memory, so the alert engine needs no database.
    """
    warnings = [
        f"[{issue.stage}] {issue.message}"
        for issue in ensure_compatible(config)
        if issue.level is CompatLevel.WARNING
    ]

    # Build adapters first: bad names/params fail fast, before any sink is touched.
    normalizer = build_normalizer(config.normalizer)
    detectors = [build_detector(d.name, d.params) for d in config.detectors]
    alert_engine = build_alert_engine(config.alert_engine.name, config.alert_engine.params)

    normalized = normalizer.normalize(text, source_format=config.source_format, origin=origin)

    detections: list[DetectionResult] = []
    for detector in detectors:
        lane_events = [e for e in normalized.events if e.source_type == detector.lane]
        detections.extend(detector.detect(lane_events))

    assets = resolve_assets(config.assets, normalized.events)
    asset_map = {asset.asset_id: asset for asset in assets}
    alerts = alert_engine.build_alerts(detections, asset_map)

    quality = _high_malformed_warning(
        config.normalizer, len(normalized.events), len(normalized.malformed)
    )
    if quality is not None:
        warnings.append(f"[input] {quality}")

    return RunResult(
        origin=origin,
        source_format=config.source_format,
        normalizer=config.normalizer,
        run_at=datetime.now(UTC),
        assets=assets,
        events=normalized.events,
        malformed=normalized.malformed,
        detections=detections,
        alerts=alerts,
        warnings=warnings,
    )


def _destination_label(sink_config: SinkConfig, config: PipelineConfig) -> str:
    # For the Postgres sink, show the resolved DB location so a cwd/URL mismatch is
    # visible in the report (what the old 'database:' line did); others show their name.
    if sink_config.name == "postgres" and config.database_url:
        return f"postgres ({describe_location(config.database_url)})"
    return sink_config.name


def execute_run(
    config: PipelineConfig,
    text: str,
    *,
    origin: str,
    resources: SinkResources,
) -> RunReport:
    """Compute a run, then write it to every configured sink; return the summary.

    Compute is store-agnostic (``compute_run``); the sinks decide where the result goes.
    Sinks run in config order (put the system-of-record sink first); a sink that *raises*
    fails the run — there is no cross-store atomicity, so a best-effort delivery sink must
    swallow its own errors. Guard ERRORs raise inside ``compute_run``, before any sink runs.
    """
    result = compute_run(config, text, origin=origin)
    destinations: list[str] = []
    for sink_config in config.sinks:
        sink = build_sink(sink_config.name, sink_config.params, resources)
        sink.write(result)
        destinations.append(_destination_label(sink_config, config))
    return RunReport.from_result(result, destinations=destinations)


def run_pipeline(config: PipelineConfig, input_path: Path) -> RunReport:
    """CLI / standalone entry point: read an input file and run the pipeline over its text.

    Builds a ``SinkResources`` for this run, creating a database engine (and schema) only
    if a configured sink needs one — so a file-only run needs no database at all. The
    Pipeline API instead injects its own long-lived engine (ADR-0015). ``utf-8-sig`` strips
    a leading BOM (Excel/Notepad default on Windows) that would otherwise reject line 1.
    """
    text = input_path.read_text(encoding="utf-8-sig")
    needs_db = any(sink_requires_database(sink.name) for sink in config.sinks)
    engine = make_engine(config.database_url) if needs_db and config.database_url else None
    try:
        resources = SinkResources()
        if engine is not None:
            create_schema(engine)
            resources = SinkResources(session_factory=make_session_factory(engine))
        return execute_run(config, text, origin=str(input_path), resources=resources)
    finally:
        if engine is not None:
            engine.dispose()
