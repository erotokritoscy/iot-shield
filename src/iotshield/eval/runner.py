"""Running a benchmark: the same pipeline the product runs, scored against ground truth.

The load-bearing property is that this measures the **real pipeline**. It does not
re-implement normalize→detect→correlate; it composes ``pipeline.normalize_sources`` and
``pipeline.compute_result`` — the exact functions ``compute_run`` composes — with a fitting
step in between. A benchmark that scored a parallel code path would eventually measure
something the product does not do.

The other load-bearing property is what it *cannot* do: it never touches a sink. That is
enforced by types rather than discipline — these functions take a ``PipelineSpec``, which
has no ``database_url`` at all, so twenty benchmark runs cannot leak synthetic alerts into
an operator's triage queue (ADR-0021 D1).
"""

from collections.abc import Sequence
from typing import Any

from iotshield.contracts import DetectionResult, PrioritizedAlert
from iotshield.detect._fitted import is_fitted
from iotshield.eval.corpus import Case, Corpus
from iotshield.eval.fit import FitProtocol, Reservation, prepare_detector, reserve
from iotshield.eval.report import (
    TIMELINE_SPAN_CAP,
    CaseReport,
    CaseTimeline,
    EvaluationReport,
    SpecReport,
)
from iotshield.eval.score import score_alerts, score_detections
from iotshield.eval.truth import GroundTruth
from iotshield.pipeline import (
    NormalizedInputs,
    PipelineSpec,
    build_detectors,
    compute_result,
    normalize_sources,
)
from iotshield.pipeline.compat import CompatLevel, check_compatibility
from iotshield.pipeline.config import config_fingerprint


def _check_alignment(spec: PipelineSpec, case: Case) -> None:
    """Refuse a corpus whose inputs do not match what the spec says it ingests.

    Count *and* declared format, because a silent mismatch is the most misleading outcome
    the harness can produce: feed stub-csv to a syslog spec and every line is quarantined
    as malformed, the detector sees nothing, and the report reads like a detector that
    simply failed. Refusing is the whole point of tagging inputs (ADR-0019's explicit
    routing, applied to corpora).
    """
    if len(case.inputs) != len(spec.sources):
        raise ValueError(
            f"case {case.name!r} supplies {len(case.inputs)} input(s) but the spec declares "
            f"{len(spec.sources)} source(s)"
        )
    for index, (given, source) in enumerate(zip(case.inputs, spec.sources, strict=True)):
        if given.source_format != source.source_format:
            raise ValueError(
                f"case {case.name!r} input[{index}] is {given.source_format!r} but the spec's "
                f"sources[{index}] declares {source.source_format!r}"
            )


def _compatibility_notes(spec: PipelineSpec) -> list[str]:
    """Guard warnings about this spec, so a row of zeros arrives with its own explanation.

    ``_check_alignment`` compares a case's inputs against the spec's *sources*, which leaves
    one layer unchecked: a spec can be perfectly aligned and still incoherent downstream.
    Ingest the FLOW lane and hand it to a TELEMETRY detector and every input matches, the
    detector is handed nothing, and the report reads ``evF1 0.00`` — indistinguishable from a
    detector that ran and was simply bad. That is the single most misleading thing this
    harness can print, and it is the same failure ``_check_alignment`` exists to prevent, one
    joint further along.

    The compatibility guard already diagnoses it, in words, and was never asked here. Asking
    it costs nothing and covers every client at once: the CLI, the API and the dashboard all
    arrive through ``evaluate``.

    Reported, not refused. A detector that stays deliberately silent on a corpus is a
    legitimate measurement — it is how ``severity_spike`` demonstrates it sees nothing in a
    burst of INFO lines — so the honest outcome is the zero, scored, *with* its reason
    attached. ERROR-level issues are left alone: they describe a spec that cannot construct,
    and ``build_detectors`` already fails on those with its own message.
    """
    return [
        f"{issue.stage}: {issue.message}"
        for issue in check_compatibility(spec)
        if issue.level is CompatLevel.WARNING
    ]


def _timeline(
    truth: GroundTruth,
    detections: Sequence[DetectionResult],
    alerts: Sequence[PrioritizedAlert],
) -> CaseTimeline:
    """Capture where truth, detections and alerts fell — capped, and honest about it.

    Drawn from the **scored** truth and findings, not the raw run. A chart of spans the table
    beside it deliberately does not count is the same misreading the notes exist to prevent,
    and under ``--fit-protocol prefix --with-timeline`` that is exactly what an unfiltered
    timeline would show.
    """
    detection_spans = [(d.window_start, d.window_end) for d in detections]
    alert_spans = [(a.window_start, a.window_end) for a in alerts]
    return CaseTimeline(
        truth=[(incident.start, incident.end) for incident in truth.incidents],
        detections=detection_spans[:TIMELINE_SPAN_CAP],
        alerts=alert_spans[:TIMELINE_SPAN_CAP],
        truncated=max(len(detection_spans), len(alert_spans)) > TIMELINE_SPAN_CAP,
    )


def _ingest_key(spec: PipelineSpec, case: Case) -> tuple[Any, ...]:
    """What makes two (spec, case) pairs ingest identically: the case and the source chain.

    Detectors are excluded deliberately — they consume the ingest phase's output and never
    influence it. That is exactly what makes the cache pay off for a sweep, whose specs
    differ *only* in detector params (``eval/sweep.py:_applied`` rewrites nothing else).

    A source's identity is its name **and its params**, via the one rule the runner and the
    guard already share. Keying on the name alone was correct only while normalizers took
    no params; once they did (ADR-0023 D8), two specs differing only in ``home_networks``
    collided here and the second was scored against the first's events — a benchmark
    publishing a number it did not measure, which is the one failure this harness exists to
    prevent.
    """
    return (
        case.name,
        tuple(
            (source.source_format, config_fingerprint(source.normalizer, source.params))
            for source in spec.sources
        ),
    )


def evaluate_case(
    spec: PipelineSpec,
    case: Case,
    *,
    protocol: FitProtocol,
    include_timeline: bool = False,
    ingest_cache: dict[tuple[Any, ...], NormalizedInputs] | None = None,
    reservation_cache: dict[str, Reservation] | None = None,
    spec_notes: Sequence[str] = (),
) -> CaseReport:
    """Run one spec over one labelled case and score both levels.

    ``spec_notes`` are remarks that hold for the whole spec regardless of case — today the
    compatibility warnings. They are passed in rather than derived here because they depend
    only on the spec: computing them per case would repeat identical work once per recording
    (sixteen times over SKAB), and the renderer dedupes notes across cases anyway.

    ``ingest_cache`` is an optional memo shared across the specs of one ``evaluate`` call.
    Ingest is pure and depends only on the source chain, so a sweep of N values would
    otherwise re-parse and re-validate the whole corpus N times — measured at ~3 s per
    extra value on SKAB's 163k rows, for byte-identical output. Splitting normalize from
    compute (ADR-0021) is what makes lifting it out possible; this is that payoff.
    """
    _check_alignment(spec, case)
    key = _ingest_key(spec, case)
    cached = ingest_cache.get(key) if ingest_cache is not None else None
    if cached is None:
        texts = [given.text for given in case.inputs]
        ingested = normalize_sources(spec, texts, origins=[case.name] * len(texts))
        if ingest_cache is not None:
            ingest_cache[key] = ingested
    else:
        ingested = cached

    # What the protocol takes for training is what scoring must not look at — one object, so
    # the two ends cannot drift (``fit.Reservation``). Memoized beside ingest for the same
    # reason: it depends only on the case and the protocol, both invariant across the specs of
    # one call, so a sweep of N values would otherwise recompute it N times (measured 0.1 s per
    # 16-case spec-run on SKAB).
    if reservation_cache is None:
        reservation = reserve(case.truth, protocol)
    else:
        reservation = reservation_cache.setdefault(case.name, reserve(case.truth, protocol))
    # Selected once, not once per detector: the choice depends on no detector, so calling it
    # inside the comprehension was a full scan of the recording per detector — and under
    # `none`, where `prepare_detector` returns on its first line, a scan whose result was
    # always discarded (measured at 18% of a trimodal run).
    training = reservation.training(ingested.events) if protocol is not FitProtocol.NONE else []
    outcomes = [
        prepare_detector(detector, protocol=protocol, training=training)
        for detector in build_detectors(spec)
    ]
    # An untrained detector the protocol could not fit is dropped rather than called: its
    # detect() would raise NotFitted and take the whole benchmark down, when what the
    # reader needs is this case scored honestly at zero with the reason attached
    # (ADR-0022). Every other outcome judges, fitted or as-configured.
    judging = [o.detector for o in outcomes if is_fitted(o.detector)]
    # Detection runs over the WHOLE recording — a windowed detector needs its warm-up — and
    # only the findings that reach the scored region are judged.
    result = compute_result(spec, ingested, judging)
    detections = reservation.scoreable(result.detections)
    alerts = reservation.scoreable(result.alerts)

    attempted = [outcome for outcome in outcomes if outcome.attempted]
    return CaseReport.build(
        case=case.name,
        detector_metrics=score_detections(reservation.truth, detections),
        # The engine ranks its output, and precision@k reads positions in that ranking. The
        # detections ride along so the alert level can report how many lanes backed each
        # true alert — the direct evidence that cross-lane corroboration fired.
        alert_metrics=score_alerts(reservation.truth, alerts, detections),
        # "Fitted" only if every detector the protocol *asked* to fit actually did. Asking it
        # of every detector instead made one rule-based control zero the count for the whole
        # run, so a report reading `0/11` sat beside a score only a successful fit could
        # produce (``fit.FitOutcome.attempted``).
        fitted=bool(attempted) and all(outcome.fitted for outcome in attempted),
        # Ingest warnings ride with the compatibility ones. ``normalize_sources`` already
        # computes "this normalizer rejected 100% of input", and dropping it was how a spec
        # whose params quarantined every record reported zeros with an empty note list:
        # ``_check_alignment`` compares declared *formats*, so a params-level mismatch passes
        # it and then silently ingests nothing.
        notes=sorted(
            {outcome.note for outcome in outcomes if outcome.note}
            | set(spec_notes)
            | set(ingested.warnings)
        ),
        timeline=(_timeline(reservation.truth, detections, alerts) if include_timeline else None),
    )


def evaluate_spec(
    spec: PipelineSpec,
    corpus: Corpus,
    *,
    label: str,
    protocol: FitProtocol,
    include_timeline: bool = False,
    ingest_cache: dict[tuple[Any, ...], NormalizedInputs] | None = None,
    reservation_cache: dict[str, Reservation] | None = None,
) -> SpecReport:
    # Once per spec, not once per case: the guard reads the spec's shape and nothing about
    # the data, so its verdict is identical for every recording in the corpus.
    spec_notes = _compatibility_notes(spec)
    return SpecReport(
        spec=label,
        detectors=[entry.name for entry in spec.detectors],
        alert_engine=spec.alert_engine.name,
        cases=[
            evaluate_case(
                spec,
                case,
                protocol=protocol,
                include_timeline=include_timeline,
                ingest_cache=ingest_cache,
                reservation_cache=reservation_cache,
                spec_notes=spec_notes,
            )
            for case in corpus.cases
        ],
    )


def evaluate(
    corpus: Corpus,
    specs: Sequence[tuple[str, PipelineSpec]],
    *,
    protocol: FitProtocol = FitProtocol.NONE,
    include_timeline: bool = False,
) -> EvaluationReport:
    """Benchmark every spec over every case in the corpus, head to head.

    Ingest is memoized across the specs of one call: they typically read the same corpus
    through the same normalizer chain, and a sweep guarantees it. Scoped to the call rather
    than module-level so nothing outlives the benchmark that asked for it — a corpus's
    events are large, and a process-wide cache would pin every corpus ever benchmarked.
    """
    ingest_cache: dict[tuple[Any, ...], NormalizedInputs] = {}
    reservation_cache: dict[str, Reservation] = {}
    return EvaluationReport(
        corpus=corpus.name,
        corpus_description=corpus.description,
        fit_protocol=protocol.value,
        cases=len(corpus.cases),
        incidents=corpus.incidents,
        faults=corpus.faults,
        specs=[
            evaluate_spec(
                spec,
                corpus,
                label=label,
                protocol=protocol,
                include_timeline=include_timeline,
                ingest_cache=ingest_cache,
                reservation_cache=reservation_cache,
            )
            for label, spec in specs
        ],
    )
