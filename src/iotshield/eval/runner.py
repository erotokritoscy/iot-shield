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

from iotshield.eval.corpus import Case, Corpus
from iotshield.eval.fit import FitProtocol, prepare_detector
from iotshield.eval.report import (
    TIMELINE_SPAN_CAP,
    CaseReport,
    CaseTimeline,
    EvaluationReport,
    SpecReport,
)
from iotshield.eval.score import score_alerts, score_detections
from iotshield.pipeline import (
    PipelineSpec,
    build_detectors,
    compute_result,
    normalize_sources,
)
from iotshield.sink import RunResult


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


def _timeline(case: Case, result: RunResult) -> CaseTimeline:
    """Capture where truth, detections and alerts fell — capped, and honest about it."""
    detections = [(d.window_start, d.window_end) for d in result.detections]
    alerts = [(a.window_start, a.window_end) for a in result.alerts]
    return CaseTimeline(
        truth=[(incident.start, incident.end) for incident in case.truth.incidents],
        detections=detections[:TIMELINE_SPAN_CAP],
        alerts=alerts[:TIMELINE_SPAN_CAP],
        truncated=max(len(detections), len(alerts)) > TIMELINE_SPAN_CAP,
    )


def evaluate_case(
    spec: PipelineSpec, case: Case, *, protocol: FitProtocol, include_timeline: bool = False
) -> CaseReport:
    """Run one spec over one labelled case and score both levels."""
    _check_alignment(spec, case)
    texts = [given.text for given in case.inputs]
    ingested = normalize_sources(spec, texts, origins=[case.name] * len(texts))

    holdout_events = (
        normalize_sources(
            spec, [given.text for given in case.holdout], origins=[f"{case.name}:holdout"]
        ).events
        if case.holdout
        else None
    )

    outcomes = [
        prepare_detector(
            detector,
            protocol=protocol,
            events=ingested.events,
            truth=case.truth,
            holdout_events=holdout_events,
        )
        for detector in build_detectors(spec)
    ]
    result = compute_result(spec, ingested, [o.detector for o in outcomes])

    return CaseReport.build(
        case=case.name,
        detector_metrics=score_detections(case.truth, result.detections),
        # The engine ranks its output, and precision@k reads positions in that ranking.
        alert_metrics=score_alerts(case.truth, result.alerts),
        # "Fitted" only if every detector that was asked to fit actually did — a partially
        # fitted chain must not read as a clean fitted run.
        fitted=all(o.fitted for o in outcomes) and protocol is not FitProtocol.NONE,
        notes=sorted({o.note for o in outcomes if o.note}),
        timeline=_timeline(case, result) if include_timeline else None,
    )


def evaluate_spec(
    spec: PipelineSpec,
    corpus: Corpus,
    *,
    label: str,
    protocol: FitProtocol,
    include_timeline: bool = False,
) -> SpecReport:
    return SpecReport(
        spec=label,
        detectors=[entry.name for entry in spec.detectors],
        alert_engine=spec.alert_engine.name,
        cases=[
            evaluate_case(spec, case, protocol=protocol, include_timeline=include_timeline)
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
    """Benchmark every spec over every case in the corpus, head to head."""
    return EvaluationReport(
        corpus=corpus.name,
        corpus_description=corpus.description,
        fit_protocol=protocol.value,
        cases=len(corpus.cases),
        incidents=corpus.incidents,
        specs=[
            evaluate_spec(
                spec, corpus, label=label, protocol=protocol, include_timeline=include_timeline
            )
            for label, spec in specs
        ],
    )
