"""A row of zeros must arrive with its reason, not look like a bad detector.

`_check_alignment` compares a case's inputs against the spec's *sources*, which leaves one
joint unchecked. Ingest the FLOW lane and hand it to a TELEMETRY detector: every input
matches, the detector is handed nothing, and the report reads `evF1 0.00` — the single most
misleading thing this harness can print. The compatibility guard already diagnoses exactly
that, in words, and the eval plane simply never asked it (ADR-0026).
"""

from iotshield.eval import evaluate, load_corpus
from iotshield.eval.fit import FitProtocol
from iotshield.eval.runner import _compatibility_notes
from iotshield.pipeline import PipelineSpec


def spec_with(detector: str) -> PipelineSpec:
    """A flow-ingesting spec judged by whichever detector is named."""
    return PipelineSpec.model_validate(
        {
            "sources": [{"normalizer": "flow", "source_format": "flow-csv"}],
            "detectors": [{"name": detector}],
            "alert_engine": {"name": "weighted"},
        }
    )


def test_a_lane_mismatch_is_reported_even_though_the_sources_align() -> None:
    notes = _compatibility_notes(spec_with("stub_threshold"))
    assert any("will not fire" in note for note in notes), notes
    assert any("no detector that consumes it" in note for note in notes), notes


def test_a_coherent_spec_earns_no_notes() -> None:
    assert _compatibility_notes(spec_with("flow_burst")) == []


def test_the_note_reaches_every_case_of_the_report() -> None:
    """Attached per case because that is where the renderer reads notes from, and it dedupes
    them across cases — so one spec-level warning prints once, not once per recording."""
    report = evaluate(load_corpus("fleet_scan"), [("mismatched", spec_with("stub_threshold"))])
    cases = report.specs[0].cases
    assert cases, "fleet_scan should have cases"
    for case in cases:
        assert any("will not fire" in note for note in case.notes), case

    # The scored zero is still reported; the note explains it rather than replacing it.
    assert all(case.detections == 0 for case in cases)


def test_a_deliberately_silent_detector_is_measured_not_refused() -> None:
    """Reported, not refused: a detector that stays quiet on a corpus is a real measurement.

    It is how `severity_spike` demonstrates it sees nothing in a burst of INFO lines, so the
    harness must still score the run rather than reject the spec.
    """
    report = evaluate(load_corpus("fleet_scan"), [("mismatched", spec_with("stub_threshold"))])
    assert report.specs[0].cases  # it ran; a refusal would have raised


def test_a_normalizer_that_quarantined_everything_says_so() -> None:
    """The other silent zero, one layer up from the compatibility guard.

    `normalize_sources` already computes "this normalizer rejected 100% of input" and the
    runner threw it away, so a flow spec whose `home_networks` does not cover the corpus
    quarantined every record and reported zeros with an EMPTY note list. `_check_alignment`
    cannot catch it either: it compares declared *format* strings, and this is a mismatch in
    the normalizer's params.
    """
    misconfigured = PipelineSpec.model_validate(
        {
            "sources": [
                {
                    "normalizer": "flow",
                    "source_format": "flow-csv",
                    # The corpus is 10.0.0.0/24; nothing here is local, so orientation
                    # attributes every record to a peer and the whole file is quarantined.
                    "params": {"home_networks": ["192.0.2.0/24"]},
                }
            ],
            "detectors": [{"name": "flow_burst"}],
            "alert_engine": {"name": "weighted"},
        }
    )
    report = evaluate(load_corpus("fleet_scan"), [("misconfigured", misconfigured)])
    case = report.specs[0].cases[0]
    assert case.detections == 0
    assert any("rejected 100% of input" in note for note in case.notes), case.notes


def test_one_unfittable_detector_does_not_zero_the_runs_fit_count() -> None:
    """`fitted` counts the detectors the protocol ASKED to fit, not every detector.

    Adding a rule-based control beside a learning detector is the shape `sweep._applied`'s own
    docstring recommends, and it used to report `0/N fitted` beside a score only a successful
    fit could produce — so the reader concluded the protocol had done nothing.
    """
    spec = PipelineSpec.model_validate(
        {
            "sources": [{"normalizer": "stub", "source_format": "stub-csv"}],
            "detectors": [
                {"name": "team3_zscore", "params": {"window_size": 20}},
                {"name": "stub_threshold", "params": {"threshold": 80.0}},
            ],
            "alert_engine": {"name": "weighted"},
        }
    )
    report = evaluate(load_corpus("synthetic"), [("mixed", spec)], protocol=FitProtocol.PREFIX)
    fitted = report.specs[0].cases_fitted
    assert fitted == len(report.specs[0].cases), f"only {fitted} case(s) counted as fitted"
    # The unfittable one still explains itself; it simply does not vote on the count.
    assert any("not fittable" in note for case in report.specs[0].cases for note in case.notes)
