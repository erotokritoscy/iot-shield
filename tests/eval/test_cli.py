"""End-to-end: the researcher's command, on the committed corpus.

Also pins the two boundary properties the harness is built around (ADR-0021 D1): a benchmark
never touches a store, and it measures the *real* pipeline rather than a parallel code path.
"""

import json
from pathlib import Path

from iotshield.eval import evaluate, load_corpus, render
from iotshield.eval.__main__ import main
from iotshield.eval.fit import FitProtocol
from iotshield.pipeline import PipelineSpec, load_spec

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
        assert case.detections_per_incident > 100
        assert case.alerts_per_incident < 10
        assert spec_report.compression_ratio is not None
        assert spec_report.compression_ratio > 10


class TestCleanCorpusRendering:
    """A corpus with no faults reports false alarms, not per-incident ratios."""

    def _clean_report(self):
        from datetime import UTC, datetime, timedelta

        from iotshield.eval import Case, Corpus, GroundTruth

        t0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        rows = [
            f"{(t0 + timedelta(seconds=i)).isoformat()},skab-testbed,Pressure,{value}"
            for i, value in enumerate([10.0] * 5 + [95.0] * 3)
        ]
        corpus = Corpus(
            name="clean_test",
            description="synthetic clean recording",
            cases=[
                Case(
                    name="clean",
                    inputs=["\n".join(rows) + "\n"],
                    truth=GroundTruth.from_timeline(
                        [(t0 + timedelta(seconds=i), False) for i in range(8)]
                    ),
                )
            ],
        )
        return evaluate(corpus, [("stub", load_spec(STUB_SPEC))], protocol=FitProtocol.NONE)

    def test_zero_incidents_renders_false_alarms_not_zero_scores(self) -> None:
        # The trap this avoids: with no incidents every per-incident ratio divides by zero
        # and renders 0.00, which reads as a perfect score rather than "not applicable".
        rendered = render(self._clean_report())
        assert "FALSE ALARM" in rendered
        assert "det/inc" not in rendered

    def test_the_false_alarms_are_counted(self) -> None:
        (spec_report,) = self._clean_report().specs
        assert spec_report.is_clean_corpus
        summary = spec_report.summary()
        assert summary["detections"] == 3  # the three over-threshold rows
        assert (summary["alerts"] or 0) >= 1
