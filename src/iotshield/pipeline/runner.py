"""The pipeline runner: composes the ports into one atomic batch run.

Runs inside ONE transaction — a crash leaves zero half-written rows, so a run
either happened or it didn't (repository functions flush; only the runner
commits). The alert engine sees this run's detections only; cross-run
correlation is future work (ADR-0006).
"""

from dataclasses import dataclass
from pathlib import Path

from iotshield.contracts import DetectionResult, PrioritizedAlert
from iotshield.pipeline.config import PipelineConfig
from iotshield.pipeline.registry import build_alert_engine, build_detector, build_normalizer
from iotshield.storage import create_schema, make_engine, make_session_factory, repository

TOP_ALERTS_IN_REPORT = 5


@dataclass(frozen=True)
class RunReport:
    """End-of-run summary: the demo surface until the API/dashboard exist."""

    input_path: str
    events: int
    malformed: int
    detections: int
    alerts: int
    top_alerts: list[PrioritizedAlert]

    def render(self) -> str:
        lines = [
            f"Pipeline run complete: {self.input_path}",
            f"  events: {self.events}   malformed: {self.malformed}"
            f"   detections: {self.detections}   alerts: {self.alerts}",
        ]
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
    # Build adapters and read input first: config/file errors fail fast,
    # before any database is touched.
    normalizer = build_normalizer(config.normalizer)
    detectors = [build_detector(d.name, d.params) for d in config.detectors]
    alert_engine = build_alert_engine(config.alert_engine.name, config.alert_engine.params)
    text = input_path.read_text(encoding="utf-8")

    engine = make_engine(config.database_url)
    try:
        # Schema ownership question deferred: once a second service (API) shares
        # this database in compose, creation moves to a dedicated init step.
        create_schema(engine)
        factory = make_session_factory(engine)
        with factory() as session, session.begin():
            if config.assets:
                repository.upsert_assets(session, config.assets)

            result = normalizer.normalize(
                text, source_format=config.source_format, origin=str(input_path)
            )
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
    finally:
        engine.dispose()

    return RunReport(
        input_path=str(input_path),
        events=len(result.events),
        malformed=len(result.malformed),
        detections=len(detections),
        alerts=len(alerts),
        top_alerts=alerts[:TOP_ALERTS_IN_REPORT],
    )
