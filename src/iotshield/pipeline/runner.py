"""The pipeline runner: composes the ports into one atomic batch run.

Runs inside ONE transaction — a crash leaves zero half-written rows, so a run
either happened or it didn't (repository functions flush; only the runner
commits). The alert engine sees this run's detections only; cross-run
correlation is future work (ADR-0006).
"""

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from iotshield.contracts import DetectionResult, PrioritizedAlert
from iotshield.pipeline.compat import CompatLevel, ensure_compatible
from iotshield.pipeline.config import PipelineConfig
from iotshield.pipeline.registry import build_alert_engine, build_detector, build_normalizer
from iotshield.storage import (
    create_schema,
    describe_location,
    make_engine,
    make_session_factory,
    repository,
)

TOP_ALERTS_IN_REPORT = 5

# Above this fraction of rejected input, a run is probably misconfigured (the wrong
# normalizer for the data) rather than merely carrying a few corrupt lines, so the
# report says so (ADR-0017). A default only — not yet operator-tunable.
_MALFORMED_WARN_RATE = 0.5


@dataclass(frozen=True)
class RunReport:
    """End-of-run summary: the demo surface until the API/dashboard exist."""

    input_path: str
    database: str
    events: int
    malformed: int
    detections: int
    alerts: int
    top_alerts: list[PrioritizedAlert]
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Pipeline run complete: {self.input_path}",
            f"  database: {self.database}",
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


def run_pipeline(config: PipelineConfig, input_path: Path) -> RunReport:
    """CLI / standalone entry point: read an input file and run the pipeline over its text.

    Owns a per-run engine and creates the schema, so a fresh database works in one
    command (in compose the init step already created it, making create_schema a
    no-op; the read-side API never creates it). utf-8-sig strips a leading BOM
    (Excel/Notepad default on Windows) that would otherwise glue U+FEFF onto the first
    field and reject line 1. Reading the file here rather than in the core is the
    input-acquisition seam: the Pipeline API feeds text straight into execute_run with
    its own long-lived engine (ADR-0015).
    """
    text = input_path.read_text(encoding="utf-8-sig")
    engine = make_engine(config.database_url)
    try:
        create_schema(engine)
        return execute_run(
            config, text, origin=str(input_path), session_factory=make_session_factory(engine)
        )
    finally:
        engine.dispose()


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


def execute_run(
    config: PipelineConfig,
    text: str,
    *,
    origin: str,
    session_factory: sessionmaker[Session],
) -> RunReport:
    """Guard, then run normalize -> detect -> alert atomically against a session factory.

    The shared core of every run, whatever supplied the engine: the CLI (via
    run_on_text, which owns a per-run engine) and the Pipeline API (which reuses its
    long-lived engine) both land here. Runs inside ONE transaction — a crash leaves
    zero half-written rows. The schema is assumed to exist (the caller owns it).
    Guard ERRORs block before any row is written; WARNINGs ride along in the report,
    alongside a data-quality WARNING when most of the input failed to parse (ADR-0017).
    """
    warnings = [
        f"[{issue.stage}] {issue.message}"
        for issue in ensure_compatible(config)
        if issue.level is CompatLevel.WARNING
    ]

    # Build adapters first: bad names/params fail fast, before any database write.
    normalizer = build_normalizer(config.normalizer)
    detectors = [build_detector(d.name, d.params) for d in config.detectors]
    alert_engine = build_alert_engine(config.alert_engine.name, config.alert_engine.params)

    with session_factory() as session, session.begin():
        if config.assets:
            repository.upsert_assets(session, config.assets)

        result = normalizer.normalize(text, source_format=config.source_format, origin=origin)
        repository.insert_events(session, result.events)
        repository.insert_malformed(session, result.malformed)

        detections: list[DetectionResult] = []
        for detector in detectors:
            lane_events = [e for e in result.events if e.source_type == detector.lane]
            detections.extend(detector.detect(lane_events))
        repository.insert_detections(session, detections)

        assets = {asset.asset_id: asset for asset in repository.list_assets(session)}
        alerts = alert_engine.build_alerts(detections, assets)
        repository.insert_alerts(session, alerts)

    # Content-appropriateness, distinct from the guard's structural checks: a run that
    # rejected most of its input is probably pointed at the wrong normalizer (ADR-0017).
    quality = _high_malformed_warning(config.normalizer, len(result.events), len(result.malformed))
    if quality is not None:
        warnings.append(f"[input] {quality}")

    return RunReport(
        input_path=origin,
        database=describe_location(config.database_url),
        events=len(result.events),
        malformed=len(result.malformed),
        detections=len(detections),
        alerts=len(alerts),
        top_alerts=alerts[:TOP_ALERTS_IN_REPORT],
        warnings=warnings,
    )
