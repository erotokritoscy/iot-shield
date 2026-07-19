"""End-to-end: the researcher's command, on the committed corpus.

Also pins the two boundary properties the harness is built around (ADR-0021 D1): a benchmark
never touches a store, and it measures the *real* pipeline rather than a parallel code path.
"""

import json
from pathlib import Path

import pytest

from iotshield.eval import (
    Case,
    CaseInput,
    Corpus,
    EvaluationReport,
    evaluate,
    load_corpus,
    render,
)
from iotshield.eval.__main__ import main
from iotshield.eval.fit import FitProtocol
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV
from iotshield.pipeline import PipelineSpec, load_spec
from tests.eval.conftest import at, truth_of

TEAM3_SPEC = Path("config/bench/team3.yaml")
STUB_SPEC = Path("config/bench/stub.yaml")


class TestCli:
    def test_runs_on_the_committed_corpus(self, capsys) -> None:
        assert main(["--corpus", "demo_micro", "--spec", str(TEAM3_SPEC)]) == 0
        out = capsys.readouterr().out
        assert "demo_micro" in out and "team3" in out

    def test_compares_two_specs_head_to_head(self, capsys) -> None:
        exit_code = main(
            ["--corpus", "demo_micro", "--spec", str(TEAM3_SPEC), "--spec", str(STUB_SPEC)]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "team3" in out and "stub" in out

    def test_writes_a_json_report(self, tmp_path: Path, capsys) -> None:
        out_path = tmp_path / "nested" / "report.json"
        argv = ["--corpus", "demo_micro", "--spec", str(TEAM3_SPEC), "--out", str(out_path)]
        assert main(argv) == 0
        capsys.readouterr()
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["corpus"] == "demo_micro"
        assert payload["specs"][0]["cases"][0]["incidents"] == 1

    def test_unknown_corpus_exits_two(self, capsys) -> None:
        assert main(["--corpus", "nope"]) == 2
        assert "unknown corpus" in capsys.readouterr().err

    def test_missing_external_data_skips_rather_than_failing(self, tmp_path: Path, capsys) -> None:
        # Exit 0 so CI running the harness over optional corpora stays green.
        assert main(["--corpus", "skab_valve1", "--corpus-path", str(tmp_path)]) == 0
        assert "skipped" in capsys.readouterr().err

    def test_list_corpora(self, capsys) -> None:
        assert main(["--list-corpora"]) == 0
        out = capsys.readouterr().out
        assert "demo_micro" in out and "skab_valve1" in out


class TestBoundaries:
    def test_a_bench_spec_carries_no_database(self) -> None:
        # load_spec produces a PipelineSpec, which has no database_url field at all — so a
        # benchmark cannot write synthetic alerts into an operator's triage queue.
        spec = load_spec(TEAM3_SPEC)
        assert isinstance(spec, PipelineSpec)
        assert not hasattr(spec, "database_url")

    def test_bench_specs_load_without_a_database_url(self) -> None:
        # load_config would refuse these: sinks defaults to [postgres], which requires one.
        for path in (TEAM3_SPEC, STUB_SPEC):
            assert load_spec(path).detectors


class TestReport:
    def test_report_round_trips_and_renders(self) -> None:
        corpus = load_corpus("demo_micro")
        report = evaluate(corpus, [("team3", load_spec(TEAM3_SPEC))], protocol=FitProtocol.NONE)
        assert report.cases == 1 and report.incidents == 1
        restored = type(report).model_validate_json(report.model_dump_json())
        assert render(restored) == render(report)

    def test_fitting_is_recorded_per_spec(self) -> None:
        corpus = load_corpus("demo_micro")
        specs = [("team3", load_spec(TEAM3_SPEC)), ("stub", load_spec(STUB_SPEC))]
        report = evaluate(corpus, specs, protocol=FitProtocol.PREFIX)
        team3, stub = report.specs
        # team3 can learn a baseline; the threshold rule cannot, and says so rather than
        # failing the run.
        assert team3.cases_fitted == 1
        assert stub.cases_fitted == 0
        assert any("not fittable" in note for case in stub.cases for note in case.notes)

    def test_a_row_scoring_detector_shows_its_fragmentation(self) -> None:
        # The stub flags rows and never merges them, so one fault becomes hundreds of
        # detections that the alert layer then compresses. This is the compression ratio
        # the two-level split exists to expose (ADR-0021 D2).
        corpus = load_corpus("demo_micro")
        report = evaluate(corpus, [("stub", load_spec(STUB_SPEC))], protocol=FitProtocol.NONE)
        (spec_report,) = report.specs
        (case,) = spec_report.cases
        assert case.detections_per_incident is not None and case.detections_per_incident > 100
        assert case.alerts_per_incident is not None and case.alerts_per_incident < 10
        assert spec_report.compression_ratio is not None
        assert spec_report.compression_ratio > 10


def synthetic_corpus(*, flags: list[bool], values: list[float]) -> Corpus:
    """A one-case corpus from inline readings and labels — no external dataset."""
    rows = [
        f"{at(index).isoformat()},skab-testbed,Pressure,{value}"
        for index, value in enumerate(values)
    ]
    return Corpus(
        name="synthetic",
        description="synthetic recording",
        cases=[
            Case(
                name="synthetic",
                inputs=[CaseInput(STUB_CSV, "\n".join(rows) + "\n")],
                truth=truth_of(flags),
            )
        ],
    )


class TestUndefinedIsNotZero:
    """A rate with no denominator is undefined, and stays undefined all the way out.

    The trap: on a clean recording every per-incident ratio divides by zero. Reporting
    0.0 makes a detector that correctly stayed silent indistinguishable from one that
    missed everything — in the JSON a CI gate reads, not merely in the console table.
    """

    def _clean_report(self) -> EvaluationReport:
        corpus = synthetic_corpus(flags=[False] * 8, values=[10.0] * 5 + [95.0] * 3)
        return evaluate(corpus, [("stub", load_spec(STUB_SPEC))], protocol=FitProtocol.NONE)

    def test_rates_are_none_and_counts_are_real(self) -> None:
        (spec_report,) = self._clean_report().specs
        summary = spec_report.summary()
        assert summary["event_recall"] is None  # no faults, so nothing to recall
        assert summary["incident_coverage"] is None
        assert summary["detections_per_incident"] is None
        assert summary["detections"] == 3  # the three over-threshold rows
        assert (summary["alerts"] or 0) >= 1

    def test_the_none_survives_the_json_round_trip(self) -> None:
        # The JSON is the artifact a CI gate keys into; if None collapsed to 0.0 here, the
        # gate could not tell a perfect run from the worst possible one.
        report = self._clean_report()
        restored = EvaluationReport.model_validate_json(report.model_dump_json())
        assert restored.specs[0].cases[0].event_recall is None
        assert restored.specs[0].cases[0].detections == 3

    def test_rendering_shows_a_dash_and_the_false_alarm_framing(self) -> None:
        rendered = render(self._clean_report())
        assert "FALSE ALARM" in rendered
        # One table serves both cases: the rate columns are present and print '-'.
        assert "det/inc" in rendered

    def test_a_defined_rate_is_not_dragged_down_by_an_undefined_one(self) -> None:
        # A corpus mixing clean and faulty recordings: averaging the clean case in as 0.0
        # would halve the reported recall for no reason.
        faulty = synthetic_corpus(flags=[False] * 4 + [True] * 4, values=[10.0] * 4 + [95.0] * 4)
        clean = synthetic_corpus(flags=[False] * 8, values=[10.0] * 8)
        mixed = Corpus(
            name="mixed",
            description="one faulty case, one clean",
            cases=[*faulty.cases, *clean.cases],
        )
        (spec_report,) = evaluate(mixed, [("stub", load_spec(STUB_SPEC))]).specs
        assert spec_report.summary()["event_recall"] == 1.0


class TestCorpusSpecAlignment:
    """A corpus declares its format, and a mismatched spec is refused, not scored."""

    def test_a_format_mismatch_is_refused(self) -> None:
        corpus = synthetic_corpus(flags=[False, True], values=[10.0, 95.0])
        syslog_spec = PipelineSpec.model_validate(
            {
                "sources": [{"normalizer": "syslog", "source_format": "syslog"}],
                "detectors": [{"name": "stub_threshold"}],
            }
        )
        # Without the check this scores event_f1 None with every line quarantined as
        # malformed — indistinguishable from a detector that simply failed.
        with pytest.raises(ValueError, match="declares 'syslog'"):
            evaluate(corpus, [("syslog", syslog_spec)])

    def test_an_input_count_mismatch_is_refused(self) -> None:
        corpus = synthetic_corpus(flags=[False, True], values=[10.0, 95.0])
        two_source = PipelineSpec.model_validate(
            {
                "sources": [
                    {"normalizer": "stub", "source_format": STUB_CSV},
                    {"normalizer": "stub", "source_format": STUB_CSV},
                ],
                "detectors": [{"name": "stub_threshold"}],
            }
        )
        with pytest.raises(ValueError, match="supplies 1 input"):
            evaluate(corpus, [("two", two_source)])
