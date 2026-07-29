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
  never fire, or whose lane matches but whose typed payload no source declares (ADR-0035
  D2), so it is handed events and discards every one. Both are legitimate (the syslog demo
  showcases normalization with no detections; a pass-through source may carry a vocabulary
  it cannot declare) and guaranteed-safe by the Detector port's foreign-lane law, so they
  are surfaced, not blocked.

The single result feeds three consumers: the runner/CLI raise on any ERROR; the
Pipeline API returns 422 on ERRORs and passes WARNINGs back; the UI shows both inline.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from iotshield.capabilities import Token, formats_in, lanes_in, payloads_in
from iotshield.contracts import Lane
from iotshield.pipeline.config import PipelineSpec, config_fingerprint
from iotshield.pipeline.registry import (
    alert_engine_capability,
    build_detector,
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
    produced_payloads: set[str] = set()
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
        produced_payloads |= payloads_in(normalizer_cap.produces)

    # Duplicate joint: the same adapter configured twice with the SAME params is always a
    # mistake, and an expensive one. Both instances judge the same events identically, so
    # every finding lands in its correlation bucket twice — and the alert engine's `volume`
    # factor counts detections as *independent corroboration*, which measurably inflates
    # priority for evidence that was only ever gathered once (ADR-0020 D4). Configuring one
    # adapter twice with *different* params is the legitimate case and stays silent.
    issues.extend(_duplicate_source_issues(config))
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
        # The detector's joints, reported FIRST-UNMET-WINS. A detector on a foreign lane is
        # also missing that lane's payload, and saying both would make the operator fix two
        # things to satisfy one break — the same rule `ingest_ok` applies one joint up.
        # An if/elif rather than a growing conjunction, so a fourth axis cannot silently
        # forget to exclude the third.
        #
        # Both are WARNING, deliberately: nothing crashes, the detector simply judges
        # nothing. The payload case also keeps `passthrough` usable without a declaration —
        # the operator who knows their JSONL carries meter readings can still run, and the
        # one who does not learns why it was quiet.
        idle_lanes = wanted - produced_lanes
        # The lane can be satisfied while the vocabulary inside it is not. `meter_iforest`
        # consumes lane:telemetry AND payload:meter, so a bare telemetry source (stub-csv, a
        # SKAB bundle) aligns perfectly and is then discarded event by event — every surface
        # that asks "can these connect?" said yes and the run published zeros (ADR-0035 D2).
        missing_payloads = payloads_in(detector_cap.consumes) - produced_payloads
        stage = f"detectors[{index}]"
        if not ingest_ok:
            pass
        elif idle_lanes:
            issues.append(
                CompatIssue(
                    CompatLevel.WARNING,
                    stage,
                    f"detector {detector.name!r} consumes lane(s) "
                    f"{_join(lane.value for lane in idle_lanes)} but no configured source "
                    f"produces them ({_join(lane.value for lane in produced_lanes)}); "
                    f"it will not fire",
                )
            )
        elif missing_payloads:
            issues.append(
                CompatIssue(
                    CompatLevel.WARNING,
                    stage,
                    f"detector {detector.name!r} judges only events carrying "
                    f"{_join(missing_payloads)} payload(s), which no configured source "
                    f"declares it produces ({_join(produced_payloads)}); its lane matches, so "
                    f"it will be handed events and discard all of them",
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


def _built_fingerprint(name: str, written: Mapping[str, Any], resolved: Mapping[str, Any]) -> str:
    """A detector's identity taken from what it BUILDS, not from what was written.

    ``effective_detector_params`` fills defaults from ``inspect.signature``, which is the
    real default only for adapters that put it there. Every *learning* adapter's factory
    params default to the ``None`` sentinel by design (``AdapterSpec.params_type`` says so),
    and ``flow_burst``'s window and threshold come from its **preset**, not its signature —
    so ``{preset: fan_out}`` and ``{preset: fan_out, threshold: 20}`` fingerprinted
    differently while constructing byte-identical detectors, and the duplicate warning never
    fired. ``config_fingerprint`` hashes with ``json.dumps`` besides, so YAML's ``80`` and a
    factory's ``80.0`` also disagreed.

    Constructing is the one thing that cannot drift from the truth, and it is affordable
    *here* because this runs only for a name that appears more than once — the guard's
    capability reads stay construction-free, which is what ADR-0014 actually requires.
    Falls back to the param fingerprint if construction fails: a bad param is a separate
    ERROR the detector loop already reports, and a duplicate warning must not mask it.
    """
    try:
        built = build_detector(name, dict(written))
    except (ValueError, TypeError):
        return config_fingerprint(name, resolved)
    state = {key: repr(value) for key, value in sorted(vars(built).items())}
    return config_fingerprint(name, state)


def _duplicate_detector_issues(config: PipelineSpec) -> list[CompatIssue]:
    """Warn once per detector entry that exactly repeats an earlier one.

    Became reachable when the orchestration UI started rendering ``detectors`` as the list
    it always was (ADR-0023): a multiselect over names could not express a duplicate, so
    this footgun did not previously exist on that path. YAML and the API always could.
    """
    issues: list[CompatIssue] = []
    seen: dict[str, int] = {}
    repeated_names = {
        name for name, count in Counter(item.name for item in config.detectors).items() if count > 1
    }
    for index, detector in enumerate(config.detectors):
        # Compare *effective* params (factory defaults filled in), so `{}` and
        # `{preset: fan_out}` are recognised as the same detector rather than only textually
        # identical entries. Resolution can raise for an unknown name; that is a separate
        # ERROR the detector loop below already reports, so fall back to the written params.
        try:
            resolved = effective_detector_params(detector.name, detector.params)
        except ValueError:
            resolved = dict(detector.params)
        fingerprint = (
            _built_fingerprint(detector.name, detector.params, resolved)
            if (detector.name in repeated_names)
            else config_fingerprint(detector.name, resolved)
        )
        first = seen.get(fingerprint)
        if first is None:
            seen[fingerprint] = index
            continue
        issues.append(
            CompatIssue(
                CompatLevel.WARNING,
                f"detectors[{index}]",
                f"detector {detector.name!r} builds the same detector as detectors[{first}]; "
                f"it will judge the same events twice and report every finding twice. "
                f"Remove one, or give it a genuinely different configuration.",
            )
        )
    return issues


def _duplicate_source_issues(config: PipelineSpec) -> list[CompatIssue]:
    """Warn once per source entry that ingests the same bytes as an earlier one.

    The detector guard above exists because judging one event twice double-reports it.
    Duplicating a *source* reaches the same place by a shorter road — every record is
    ingested twice, so every detector emits every finding twice — and nothing checked it.
    The project already knows the rule elsewhere: the corpus generator refuses a scenario
    that records two dialects of one lane, on the stated grounds that "a run pools its
    sources, so two dialects of one lane double every event" (ADR-0033).

    Keyed on ``path`` as well as normalizer/format/params. Path is deliberately absent from
    ``config_fingerprint`` because it is not part of the chain's *shape* — but identity of
    the bytes is exactly what this check is about. A spec with no paths (the API transport,
    where content arrives in the body) falls back to shape alone, which is the most that can
    be known there.
    """
    issues: list[CompatIssue] = []
    seen: dict[tuple[str, str, str | None], int] = {}
    for index, source in enumerate(config.sources):
        key = (
            source.source_format,
            config_fingerprint(source.normalizer, dict(source.params)),
            source.path,
        )
        first = seen.get(key)
        if first is None:
            seen[key] = index
            continue
        where = f" ({source.path})" if source.path else ""
        issues.append(
            CompatIssue(
                CompatLevel.WARNING,
                f"sources[{index}]",
                f"source{where} is ingested identically by sources[{first}]; a run pools its "
                f"sources, so every record will be counted twice and every detector will "
                f"report every finding twice. Remove one.",
            )
        )
    return issues


def _join(names: Iterable[str]) -> str:
    return ", ".join(sorted(names)) or "nothing"
