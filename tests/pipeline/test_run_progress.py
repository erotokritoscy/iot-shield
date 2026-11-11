"""A run says how far it has got (ADR-0051): reading, then judging, then saving.

The callback only observes, so the properties that matter are the ones a progress bar relies
on: the phases come in order, the counts never go backwards, each phase ends at its total, and
the totals are the real ones (rows in the input, events judged).
"""

from datetime import date
from pathlib import Path

import pytest

from iotshield.connect import CONNECTORS, pull_chunks, pull_summary
from iotshield.connect.target import DatabaseTarget
from iotshield.pipeline.config import PipelineConfig, SinkConfig, load_config
from iotshield.pipeline.runner import Feed, RunProgress, execute_run, text_rows
from iotshield.sink import SinkResources
from tests.connect_world import World

REPO = Path(__file__).resolve().parents[2]
PHASES = ("reading", "judging", "saving")


def _demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PipelineConfig, list[str]]:
    monkeypatch.chdir(REPO)
    config = load_config(REPO / "config" / "demo.yaml")
    sinks = [SinkConfig(name="file", params={"path": str(tmp_path / "out.jsonl")})]
    texts = [(REPO / str(source.path)).read_text(encoding="utf-8") for source in config.sources]
    return config.model_copy(update={"sinks": sinks}), texts


def _phase(steps: list[RunProgress], phase: str) -> list[RunProgress]:
    return [step for step in steps if step.phase == phase]


class TestProgress:
    def test_phases_come_in_order_and_counts_end_at_their_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, texts = _demo(tmp_path, monkeypatch)
        feeds = [
            Feed(chunks=[text], origin=f"source-{i}", rows=text_rows(text))
            for i, text in enumerate(texts)
        ]
        steps: list[RunProgress] = []
        report = execute_run(
            config, feeds, resources=SinkResources(), batch_events=7, progress=steps.append
        )

        order = [PHASES.index(step.phase) for step in steps]
        assert order == sorted(order), "a phase never comes back once the next has begun"
        assert {step.phase for step in steps} == set(PHASES)

        reading = _phase(steps, "reading")
        rows = sum(text_rows(text) for text in texts)
        assert [step.done for step in reading] == sorted(step.done for step in reading)
        assert reading[0].done == 0
        assert reading[-1].done == reading[-1].total == rows

        judging = _phase(steps, "judging")
        assert [step.done for step in judging] == sorted(step.done for step in judging)
        assert all(step.total == report.events for step in judging)
        assert judging[-1].done == report.events == 110
        # Seven events a batch: the bar moves more than once while judging.
        assert len(judging) > 2

        assert _phase(steps, "saving") == [RunProgress("saving", 0, None)]

    def test_a_source_that_cannot_count_itself_leaves_reading_without_a_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, texts = _demo(tmp_path, monkeypatch)
        feeds = [Feed(chunks=[text], origin=f"source-{i}") for i, text in enumerate(texts)]
        steps: list[RunProgress] = []
        execute_run(config, feeds, resources=SinkResources(), progress=steps.append)
        reading = _phase(steps, "reading")
        assert all(step.total is None for step in reading)
        assert reading[-1].done == sum(text_rows(text) for text in texts)

    def test_no_callback_is_the_run_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, texts = _demo(tmp_path, monkeypatch)
        report = execute_run(config, texts, resources=SinkResources())
        assert (report.events, report.detections, report.alerts) == (110, 8, 2)

    def test_a_callback_that_raises_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, texts = _demo(tmp_path, monkeypatch)

        def broken(step: RunProgress) -> None:
            if step.phase == "judging":
                raise RuntimeError("observer failed")

        with pytest.raises(RuntimeError, match="observer failed"):
            execute_run(config, texts, resources=SinkResources(), progress=broken)
        assert not (tmp_path / "out.jsonl").exists(), "a failed run leaves nothing behind"


class TestPullTotals:
    def test_a_pull_counts_its_rows_and_reading_reaches_them(
        self, tmp_path: Path, world: World
    ) -> None:
        spec = CONNECTORS["sensipro"]
        start, end = date(2023, 7, 1), date(2023, 7, 5)
        target = DatabaseTarget(spec=spec, connection=world.sensipro)
        rows = target.count(start, end, {})
        assert rows == pull_summary(spec, world.sensipro, start, end, {}).rows > 0
        assert target.header_lines == 1

        config = PipelineConfig.model_validate(
            {
                "sources": [{"normalizer": "meter", "source_format": "sensipro-hourly-csv"}],
                "detectors": [{"name": "meter_quality"}],
                "sinks": [{"name": "file", "params": {"path": str(tmp_path / "out.jsonl")}}],
            }
        )
        feed = Feed(
            chunks=pull_chunks(spec, world.sensipro, start, end, {}, chunk_lines=1),
            origin="pull",
            rows=rows,
            header_lines=target.header_lines,
        )
        steps: list[RunProgress] = []
        execute_run(config, [feed], resources=SinkResources(), progress=steps.append)
        reading = _phase(steps, "reading")
        assert len(reading) > 2, "one update per chunk, not one per run"
        # The count includes rows the connector skips (filler hours), so reading may end
        # short of it — but never past it, and never with a total under what was read.
        assert all(step.total is not None and step.done <= step.total for step in reading)
        assert reading[-1].total == rows
