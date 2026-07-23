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
from iotshield.contracts import Lane
from iotshield.pipeline.config import PipelineSpec, config_fingerprint
from iotshield.pipeline.registry import (
    alert_engine_capability,
    detector_capability,
    effective_detector_params,
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


def check_compatibility(config: PipelineSpec) -> list[CompatIssue]:
    """Return every coherence issue in ``config`` (empty means a clean chain)."""
    issues: list[CompatIssue] = []

    if not config.sources:
        return [CompatIssue(CompatLevel.ERROR, "sources", "no input sources configured")]

    # Ingest joints: every source's asserted source_format must be one its normalizer
    # accepts, else that normalizer raises at runtime (a real crash) -> ERROR. The lanes the
    # sources produce pool into one set the detectors draw from. When ingest is itself
    # broken, the downstream lane warnings would be noise, so they are suppressed below.
    ingest_ok = True
    produced_lanes: set[Lane] = set()
    for index, source in enumerate(config.sources):
        stage = f"sources[{index}]"
        try:
            normalizer_cap = normalizer_capability(source.normalizer, source.params)
        except ValueError as exc:
            issues.append(CompatIssue(CompatLevel.ERROR, stage, str(exc)))
            ingest_ok = False
            continue
        accepted = formats_in(normalizer_cap.consumes)
        if source.source_format not in accepted:
            issues.append(
                CompatIssue(
                    CompatLevel.ERROR,
                    stage,
                    f"normalizer {source.normalizer!r} does not accept source_format "
                    f"{source.source_format!r} (accepts: {_join(accepted)})",
                )
            )
            ingest_ok = False
            continue
        produced_lanes |= lanes_in(normalizer_cap.produces)

    # Duplicate joint: the same adapter configured twice with the SAME params is always a
    # mistake, and an expensive one. Both instances judge the same events identically, so
    # every finding lands in its correlation bucket twice — and the alert engine's `volume`
    # factor counts detections as *independent corroboration*, which measurably inflates
    # priority for evidence that was only ever gathered once (ADR-0020 D4). Configuring one
    # adapter twice with *different* params is the legitimate case and stays silent.
    issues.extend(_duplicate_detector_issues(config))

    # Lane joint: each detector's lane must be one some source produces, else the detector
    # idles (legal, sometimes intentional) -> WARNING.
    detector_products: set[Token] = set()
    consumed_lanes: set[Lane] = set()
    for index, detector in enumerate(config.detectors):
        try:
            detector_cap = detector_capability(detector.name, detector.params)
        except ValueError as exc:
            issues.append(CompatIssue(CompatLevel.ERROR, f"detectors[{index}]", str(exc)))
            continue
        detector_products |= detector_cap.produces
        wanted = lanes_in(detector_cap.consumes)
        consumed_lanes |= wanted
        idle_lanes = wanted - produced_lanes
        if ingest_ok and idle_lanes:
            issues.append(
                CompatIssue(
                    CompatLevel.WARNING,
                    f"detectors[{index}]",
                    f"detector {detector.name!r} consumes lane(s) "
                    f"{_join(lane.value for lane in idle_lanes)} but no configured source "
                    f"produces them ({_join(lane.value for lane in produced_lanes)}); "
                    f"it will not fire",
                )
            )

    # Coverage joint: a lane the sources produce that no detector consumes is ingested but
    # never judged -> WARNING (those events yield no detections). The multi-source mirror of
    # the idle-detector check, and the per-lane feedback the operator UI surfaces (ADR-0019).
    if ingest_ok:
        for lane in sorted(produced_lanes - consumed_lanes, key=lambda lane: lane.value):
            issues.append(
                CompatIssue(
                    CompatLevel.WARNING,
                    "ingest",
                    f"source lane {lane.value!r} has no detector that consumes it; "
                    f"those events will produce no detections",
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


def ensure_compatible(config: PipelineSpec) -> list[CompatIssue]:
    """Run the guard; raise on any ERROR, else return the remaining (warning) issues.

    The single place the run-or-refuse decision is made — shared by the runner (and
    thus the CLI and the API, which both reach the guard through ``execute_run``).
    """
    issues = check_compatibility(config)
    errors = [issue for issue in issues if issue.level is CompatLevel.ERROR]
    if errors:
        raise IncompatiblePipelineError(errors)
    return issues


def _duplicate_detector_issues(config: PipelineSpec) -> list[CompatIssue]:
    """Warn once per detector entry that exactly repeats an earlier one.

    Became reachable when the orchestration UI started rendering ``detectors`` as the list
    it always was (ADR-0023): a multiselect over names could not express a duplicate, so
    this footgun did not previously exist on that path. YAML and the API always could.
    """
    issues: list[CompatIssue] = []
    seen: dict[str, int] = {}
    for index, detector in enumerate(config.detectors):
        # Compare *effective* params (factory defaults filled in), so `{}` and
        # `{preset: fan_out}` are recognised as the same detector rather than only textually
        # identical entries. Resolution can raise for an unknown name; that is a separate
        # ERROR the detector loop below already reports, so fall back to the written params.
        try:
            resolved = effective_detector_params(detector.name, detector.params)
        except ValueError:
            resolved = dict(detector.params)
        fingerprint = config_fingerprint(detector.name, resolved)
        first = seen.get(fingerprint)
        if first is None:
            seen[fingerprint] = index
            continue
        issues.append(
            CompatIssue(
                CompatLevel.WARNING,
                f"detectors[{index}]",
                f"detector {detector.name!r} is configured identically to detectors[{first}]; "
                f"it will judge the same events twice and its findings will be double-counted "
                f"as corroborating evidence. Vary its params, or remove one.",
            )
        )
    return issues


def _join(names: Iterable[str]) -> str:
    return ", ".join(sorted(names)) or "nothing"
