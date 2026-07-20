"""Evaluation harness: measuring the pipeline against labelled data (ADR-0021).

The third plane. ``Monitor`` and ``Operate`` *use* the pipeline; this **judges** it — it
sits above the pipeline the way a test harness sits above the code under test, and it is
the only surface that needs ground truth, which production data by definition never has.

The dependency arrow points one way: this package imports the pipeline, and nothing in the
pipeline imports this. That is what makes it a separable plane rather than a feature, and
it is why a benchmark can never write to an operator's store.

    uv run python -m iotshield.eval list
    uv run python -m iotshield.eval --corpus synthetic --spec config/bench/team3.yaml
"""

from iotshield.eval.corpus import (
    FETCHABLE,
    Case,
    CaseInput,
    Corpus,
    CorpusInfo,
    CorpusUnavailable,
    corpus_root,
    list_corpora,
    load_bundle,
    load_corpus,
    write_bundle,
)
from iotshield.eval.fit import FitProtocol, prepare_detector
from iotshield.eval.report import (
    CaseReport,
    CaseTimeline,
    EvaluationReport,
    SpecReport,
    render,
)
from iotshield.eval.runner import evaluate, evaluate_case, evaluate_spec
from iotshield.eval.score import AlertMetrics, DetectorMetrics, score_alerts, score_detections
from iotshield.eval.truth import GroundTruth, Incident

__all__ = [
    "FETCHABLE",
    "AlertMetrics",
    "Case",
    "CaseInput",
    "CaseReport",
    "CaseTimeline",
    "Corpus",
    "CorpusInfo",
    "CorpusUnavailable",
    "DetectorMetrics",
    "EvaluationReport",
    "FitProtocol",
    "GroundTruth",
    "Incident",
    "SpecReport",
    "corpus_root",
    "evaluate",
    "evaluate_case",
    "evaluate_spec",
    "list_corpora",
    "load_bundle",
    "load_corpus",
    "prepare_detector",
    "render",
    "score_alerts",
    "score_detections",
    "write_bundle",
]
