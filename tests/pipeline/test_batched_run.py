"""A run judged a batch of assets at a time is the run judged whole (ADR-0047).

The claim batching rests on is that detectors and alert buckets are per asset, so splitting a
run *between* assets changes nothing. These tests hold the claim where it could break: the
same inputs through ``execute_run`` with batches forced to their smallest (and spilled to
disk), against one batch, against ``compute_run`` — and across three lanes, so a detector that
quietly kept cross-asset state would show up as a difference.
"""

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from iotshield.connect import CONNECTORS, pull, pull_chunks
from iotshield.contracts import Lane, NormalizedEvent
from iotshield.pipeline import runner as runner_module
from iotshield.pipeline.batches import BUCKETS, AssetPartition
from iotshield.pipeline.config import PipelineConfig, SinkConfig, load_config
from iotshield.pipeline.runner import Feed, compute_run, execute_run
from iotshield.sink import SinkResources
from iotshield.storage import create_schema, make_engine, make_session_factory
from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    DetectionRow,
    EventRow,
    MalformedRow,
    RunRow,
)
from tests.connect_world import World

REPO = Path(__file__).resolve().parents[2]

# Minted per row, so two computations of one run can never share them.
_MINTED = {"event_id", "record_id", "detection_id", "alert_id", "created_at", "detection_ids"}


def _unminted(value: Any) -> Any:
    """``value`` without minted ids at any depth (a detection's evidence cites an event_id)."""
    if isinstance(value, dict):
        return {k: _unminted(v) for k, v in value.items() if k not in _MINTED}
    if isinstance(value, list):
        return [_unminted(item) for item in value]
    return value


def _canon(rows: list[dict[str, Any]]) -> Counter[str]:
    """Rows as a multiset, minus what is minted: batching may reorder rows across assets."""
    return Counter(json.dumps(_unminted(row), sort_keys=True) for row in rows)


def _demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PipelineConfig:
    monkeypatch.chdir(REPO)  # the demo config names its inputs relative to the repo
    config = load_config(REPO / "config" / "demo.yaml")
    sinks = [SinkConfig(name="file", params={"path": str(tmp_path / "out.jsonl")})]
    return config.model_copy(update={"sinks": sinks})


def _texts(config: PipelineConfig) -> list[str]:
    return [(REPO / str(source.path)).read_text(encoding="utf-8") for source in config.sources]


def _record(tmp_path: Path) -> dict[str, Any]:
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "a run is one line however many batches it took"
    return json.loads(lines[0])


class TestBatchingChangesNothing:
    @pytest.mark.parametrize("batch_events", [1, 7, 1_000_000])
    def test_the_demo_is_the_same_run_however_it_is_batched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch_events: int
    ) -> None:
        config = _demo(tmp_path, monkeypatch)
        texts = _texts(config)
        whole = compute_run(config, texts)

        report = execute_run(config, texts, resources=SinkResources(), batch_events=batch_events)
        record = _record(tmp_path)

        # The walkthrough's own numbers (CLAUDE.md) — a batched run is still the demo.
        assert (report.events, report.malformed, report.detections, report.alerts) == (
            110,
            2,
            8,
            2,
        )
        for name, rows in (
            ("events", whole.events),
            ("malformed", whole.malformed),
            ("detections", whole.detections),
            ("alerts", whole.alerts),
        ):
            expected = _canon([row.model_dump(mode="json") for row in rows])
            assert _canon(record[name]) == expected, name
        assert record["assets"] == [asset.model_dump(mode="json") for asset in whole.assets]
        assert record["warnings"] == whole.warnings
        assert [a.priority_score for a in report.top_alerts] == [
            a.priority_score for a in whole.alerts[:5]
        ]

    def test_one_batch_writes_the_same_line_as_writing_the_whole_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Byte for byte, bar the minted ids: the file sink's format did not change.
        config = _demo(tmp_path, monkeypatch)
        texts = _texts(config)
        execute_run(config, texts, resources=SinkResources())
        batched = _record(tmp_path)
        (tmp_path / "out.jsonl").unlink()
        from iotshield.sink.file import FileSink

        FileSink(tmp_path / "out.jsonl").write(compute_run(config, texts))
        whole = _record(tmp_path)
        assert list(batched) == list(whole)  # same keys, same order
        for name in ("events", "malformed", "detections", "alerts"):
            assert _unminted(batched[name]) == _unminted(whole[name]), name

    def test_a_streamed_pull_is_the_same_run_as_the_whole_text(
        self, tmp_path: Path, world: World
    ) -> None:
        # One chunk per meter and one event per batch: the meter normalizer differences each
        # meter's counter, so a cut inside a meter would lose an hour and show up here.
        spec = CONNECTORS["sensipro"]
        start, end = date(2023, 7, 1), date(2023, 7, 5)
        text = pull(spec, world.sensipro, start, end, {}).content
        config = PipelineConfig.model_validate(
            {
                "sources": [{"normalizer": "meter", "source_format": "sensipro-hourly-csv"}],
                "detectors": [{"name": "meter_quality"}],
                "sinks": [{"name": "file", "params": {"path": str(tmp_path / "out.jsonl")}}],
            }
        )
        whole = compute_run(config, [text], origins=["pull"])
        feed = Feed(
            chunks=pull_chunks(spec, world.sensipro, start, end, {}, chunk_lines=1),
            origin="pull",
        )
        report = execute_run(config, [feed], resources=SinkResources(), batch_events=1)
        record = _record(tmp_path)
        assert report.events == len(whole.events) > 0
        assert report.malformed == len(whole.malformed) > 0
        assert report.detections == len(whole.detections) > 0
        for name, rows in (
            ("events", whole.events),
            ("malformed", whole.malformed),
            ("detections", whole.detections),
            ("alerts", whole.alerts),
        ):
            assert _canon(record[name]) == _canon([r.model_dump(mode="json") for r in rows]), name


class TestPostgres:
    def _factory(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        engine = make_engine(f"sqlite:///{tmp_path / 'runs.db'}")
        create_schema(engine)
        return make_session_factory(engine)

    def _count(self, factory, row) -> int:  # type: ignore[no-untyped-def]
        with factory() as session:
            return int(session.scalar(select(func.count()).select_from(row)) or 0)

    def test_a_batched_run_is_one_saved_run_with_every_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = self._factory(tmp_path)
        config = _demo(tmp_path, monkeypatch).model_copy(
            update={"sinks": [SinkConfig(name="postgres")]}
        )
        report = execute_run(
            config,
            _texts(config),
            resources=SinkResources(session_factory=factory, org_id="acme"),
            batch_events=1,
        )
        assert [self._count(factory, row) for row in (EventRow, MalformedRow)] == [110, 2]
        assert [self._count(factory, row) for row in (DetectionRow, AlertRow)] == [8, 2]
        with factory() as session:
            saved = session.scalars(select(RunRow)).one()
        assert saved.run_id == report.run_id
        assert (saved.events, saved.malformed, saved.detections, saved.alerts) == (110, 2, 8, 2)

    def test_a_run_that_fails_in_a_later_batch_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = self._factory(tmp_path)
        config = _demo(tmp_path, monkeypatch).model_copy(
            update={"sinks": [SinkConfig(name="postgres")]}
        )
        judged = runner_module.compute_result
        calls = {"n": 0}

        def fail_on_the_second_batch(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("the second batch failed")
            return judged(*args, **kwargs)

        monkeypatch.setattr(runner_module, "compute_result", fail_on_the_second_batch)
        with pytest.raises(RuntimeError, match="second batch"):
            execute_run(
                config,
                _texts(config),
                resources=SinkResources(session_factory=factory),
                batch_events=1,
            )
        assert calls["n"] == 2  # the first batch WAS written — inside the transaction
        for row in (EventRow, MalformedRow, DetectionRow, AlertRow, AlertDetectionRow, RunRow):
            assert self._count(factory, row) == 0, row.__name__


class TestFileSinkParts:
    def test_an_aborted_run_appends_nothing_and_leaves_no_parts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _demo(tmp_path, monkeypatch)

        def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(runner_module, "compute_result", boom)
        with pytest.raises(RuntimeError):
            execute_run(config, _texts(config), resources=SinkResources())
        assert list(tmp_path.iterdir()) == []


class TestAssetPartition:
    def _events(self, assets: list[str], per_asset: int) -> list[NormalizedEvent]:
        start = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
        return [
            NormalizedEvent(
                timestamp=start + timedelta(seconds=i),
                asset_id=asset,
                source_type=Lane.TELEMETRY,
                source_format="stub-csv",
                metric="temperature",
                value=float(i),
                raw=f"{asset},{i}",
            )
            for i in range(per_asset)
            for asset in assets
        ]

    def test_a_batch_never_splits_an_asset(self) -> None:
        partition = AssetPartition(spill_after=3)
        events = self._events([f"a{i}" for i in range(40)], per_asset=5)
        partition.add(events)
        assert partition.spilled
        seen: dict[str, int] = {}
        for index, batch in enumerate(partition.batches(target=1)):
            for event in batch:
                assert seen.setdefault(event.asset_id, index) == index
        partition.close()
        assert len(seen) == 40

    def test_spilling_is_lossless_and_keeps_each_assets_order(self) -> None:
        events = self._events(["pump", "cam", "gw"], per_asset=20)
        partition = AssetPartition(spill_after=4)
        for event in events:
            partition.add([event])
        out = [event for batch in partition.batches(target=10**6) for event in batch]
        partition.close()
        assert sorted(e.model_dump_json() for e in out) == sorted(
            e.model_dump_json() for e in events
        )
        for asset in ("pump", "cam", "gw"):
            assert [e for e in out if e.asset_id == asset] == [
                e for e in events if e.asset_id == asset
            ]

    def test_a_small_run_stays_in_memory_as_one_batch(self) -> None:
        partition = AssetPartition(spill_after=1_000)
        partition.add(self._events(["a", "b"], per_asset=3))
        assert not partition.spilled
        assert [len(batch) for batch in partition.batches()] == [6]

    def test_nothing_read_is_still_one_empty_batch(self) -> None:
        assert list(AssetPartition().batches()) == [[]]

    def test_buckets_are_stable_across_processes(self) -> None:
        # crc32, not hash(): Python randomizes str hashing per process, which would make a
        # run's batches — and so its file output's row order — differ between two runs.
        from iotshield.pipeline.batches import _bucket

        assert _bucket("10.0.0.24") == 2981852356 % BUCKETS  # zlib.crc32(b"10.0.0.24")
