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
from pathlib import Path

from iotshield.eval.corpus import Case, Corpus
from iotshield.eval.fit import FitProtocol, prepare_detector
from iotshield.eval.report import CaseReport, EvaluationReport, SpecReport
from iotshield.eval.score import score_alerts, score_detections
from iotshield.pipeline import PipelineSpec, compute_result, normalize_sources
from iotshield.pipeline.registry import build_detector


def evaluate_case(spec: PipelineSpec, case: Case, *, protocol: FitProtocol) -> CaseReport:
    """Run one spec over one labelled case and score both levels."""
    if len(case.inputs) != len(spec.sources):
        raise ValueError(
            f"case {case.name!r} supplies {len(case.inputs)} input(s) but the spec declares "
            f"{len(spec.sources)} source(s)"
        )
    ingested = normalize_sources(spec, case.inputs, origins=[case.name] * len(case.inputs))

    holdout_events = (
        normalize_sources(spec, case.holdout, origins=[f"{case.name}:holdout"]).events
        if case.holdout
        else None
    )

    outcomes = [
        prepare_detector(
            build_detector(entry.name, entry.params),
            protocol=protocol,
            events=ingested.events,
            truth=case.truth,
            holdout_events=holdout_events,
        )
        for entry in spec.detectors
    ]
    result = compute_result(spec, ingested, detectors=[o.detector for o in outcomes])

    return CaseReport.build(
        case=case.name,
        detector_metrics=score_detections(case.truth, result.detections),
        # The engine ranks its output, and precision@k reads positions in that ranking.
        alert_metrics=score_alerts(case.truth, result.alerts),
        # "Fitted" only if every detector that was asked to fit actually did — a partially
        # fitted chain must not read as a clean fitted run.
        fitted=all(o.fitted for o in outcomes) and protocol is not FitProtocol.NONE,
        notes=sorted({o.note for o in outcomes if o.note}),
    )


def evaluate_spec(
    spec: PipelineSpec, corpus: Corpus, *, label: str, protocol: FitProtocol
) -> SpecReport:
    return SpecReport(
        spec=label,
        detectors=[entry.name for entry in spec.detectors],
        alert_engine=spec.alert_engine.name,
        cases=[evaluate_case(spec, case, protocol=protocol) for case in corpus.cases],
    )


def evaluate(
    corpus: Corpus,
    specs: Sequence[tuple[str, PipelineSpec]],
    *,
    protocol: FitProtocol = FitProtocol.NONE,
) -> EvaluationReport:
    """Benchmark every spec over every case in the corpus, head to head."""
    return EvaluationReport(
        corpus=corpus.name,
        corpus_description=corpus.description,
        fit_protocol=protocol.value,
        cases=len(corpus.cases),
        incidents=corpus.incidents,
        specs=[
            evaluate_spec(spec, corpus, label=label, protocol=protocol) for label, spec in specs
        ],
    )


def spec_label(path: Path) -> str:
    """The short name a spec appears under in the report (its filename, no extension)."""
    return path.stem
