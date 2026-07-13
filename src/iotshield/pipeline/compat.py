"""Compatibility guard: validate a pipeline config as a chain of typed connectors.

A pipeline is stages plugged together: ``source -> normalizer -> detectors -> alert
engine``. The config is coherent only when adjacent plugs fit. ``check_compatibility``
resolves each named adapter's declared capability from the registry (by name + params,
WITHOUT constructing it), walks the known topology, and compares each downstream
stage's ``consumes`` against the upstream ``produces``.

Two failure modes, two severities (ADR-0014):

- **ERROR** — the run is structurally broken and would crash. The chosen
  ``source_format`` is not one the chosen normalizer accepts (the normalizer raises
  ``ValueError`` at runtime), or an adapter name/param cannot even be resolved.
- **WARNING** — a stage is *idle*: a detector whose lane no normalizer produces will
  never fire. This is legitimate (the syslog demo showcases normalization with no
  detections) and guaranteed-safe by the Detector port's foreign-lane law, so it is
  surfaced, not blocked.

The single result feeds three consumers: the runner/CLI raise on any ERROR; the
Pipeline API returns 422 on ERRORs and passes WARNINGs back; the UI shows both inline.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from iotshield.capabilities import Token, formats_in, lanes_in
from iotshield.pipeline.config import PipelineConfig
from iotshield.pipeline.registry import (
    alert_engine_capability,
    detector_capability,
    normalizer_capability,
)


class CompatLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class CompatIssue:
    """One finding about a config's coherence, tied to the stage that owns it."""

    level: CompatLevel
    stage: str
    message: str


def check_compatibility(config: PipelineConfig) -> list[CompatIssue]:
    """Return every coherence issue in ``config`` (empty means a clean chain)."""
    issues: list[CompatIssue] = []

    # The normalizer anchors the chain; if it can't be resolved, nothing downstream
    # can be judged, so report just that.
    try:
        normalizer_cap = normalizer_capability(config.normalizer)
    except ValueError as exc:
        return [CompatIssue(CompatLevel.ERROR, "normalizer", str(exc))]

    # Ingest joint: the asserted source_format must be one the normalizer accepts,
    # else the normalizer raises at runtime (a real crash) -> ERROR.
    accepted = formats_in(normalizer_cap.consumes)
    if config.source_format not in accepted:
        issues.append(
            CompatIssue(
                CompatLevel.ERROR,
                "ingest",
                f"normalizer {config.normalizer!r} does not accept source_format "
                f"{config.source_format!r} (accepts: {_join(accepted)})",
            )
        )

    produced_lanes = lanes_in(normalizer_cap.produces)

    # Lane joint: each detector's lane must be one the normalizer produces, else the
    # detector idles (legal, sometimes intentional) -> WARNING.
    detector_products: set[Token] = set()
    for index, detector in enumerate(config.detectors):
        try:
            detector_cap = detector_capability(detector.name, detector.params)
        except ValueError as exc:
            issues.append(CompatIssue(CompatLevel.ERROR, f"detectors[{index}]", str(exc)))
            continue
        detector_products |= detector_cap.produces
        idle_lanes = lanes_in(detector_cap.consumes) - produced_lanes
        if idle_lanes:
            wanted = _join(lane.value for lane in idle_lanes)
            available = _join(lane.value for lane in produced_lanes)
            issues.append(
                CompatIssue(
                    CompatLevel.WARNING,
                    f"detectors[{index}]",
                    f"detector {detector.name!r} consumes lane(s) {wanted} but the "
                    f"{config.normalizer!r} normalizer only produces {available}; it will not fire",
                )
            )

    # Detect -> alert joint: the alert engine's inputs must be produced by some
    # detector. Universal today (all detectors produce detections); modelled so a
    # future lane-specific engine is just another token to check.
    try:
        alert_cap = alert_engine_capability(config.alert_engine.name, config.alert_engine.params)
    except ValueError as exc:
        issues.append(CompatIssue(CompatLevel.ERROR, "alert_engine", str(exc)))
        return issues

    unmet = alert_cap.consumes - detector_products
    if detector_products and unmet:
        missing = _join(token.render() for token in unmet)
        issues.append(
            CompatIssue(
                CompatLevel.WARNING,
                "alert_engine",
                f"alert engine {config.alert_engine.name!r} consumes {missing} which no "
                f"configured detector produces; it will not fire",
            )
        )

    return issues


class IncompatiblePipelineError(ValueError):
    """A config with ERROR-level issues — the run is refused.

    Carries the structured issues so a caller (the Pipeline API) can surface them
    without re-running the guard. Subclasses ValueError so the CLI's existing
    error handling still catches it and prints the message.
    """

    def __init__(self, issues: list[CompatIssue]) -> None:
        self.issues = issues
        detail = "\n".join(f"  - [{issue.stage}] {issue.message}" for issue in issues)
        super().__init__(f"incompatible pipeline configuration:\n{detail}")


def has_errors(issues: list[CompatIssue]) -> bool:
    """Whether any issue blocks a run. The 'what blocks' predicate lives here, once."""
    return any(issue.level is CompatLevel.ERROR for issue in issues)


def ensure_compatible(config: PipelineConfig) -> list[CompatIssue]:
    """Run the guard; raise on any ERROR, else return the remaining (warning) issues.

    The single place the run-or-refuse decision is made — shared by the runner (and
    thus the CLI and the API, which both reach the guard through ``execute_run``).
    """
    issues = check_compatibility(config)
    errors = [issue for issue in issues if issue.level is CompatLevel.ERROR]
    if errors:
        raise IncompatiblePipelineError(errors)
    return issues


def _join(names: Iterable[str]) -> str:
    return ", ".join(sorted(names)) or "nothing"
