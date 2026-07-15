"""Result sink adapters: the pluggable output seam (ADR-0018)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from iotshield.contracts import discovered_asset
from iotshield.sink import RunResult, SinkResources
from iotshield.sink.file import FileSink
from iotshield.sink.file import build as build_file
from iotshield.sink.postgres import build as build_postgres


def _result() -> RunResult:
    return RunResult(
        origin="test",
        source_format="stub-csv",
        normalizer="stub",
        run_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        assets=[discovered_asset("pump-02")],
        events=[],
        malformed=[],
        detections=[],
        alerts=[],
        warnings=["heads up"],
    )


def test_file_sink_appends_one_json_line_per_run(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    sink = FileSink(out)
    sink.write(_result())
    sink.write(_result())
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append, not overwrite — one self-contained record per run
    record = json.loads(lines[0])
    assert record["normalizer"] == "stub"
    assert record["warnings"] == ["heads up"]
    assert record["assets"][0]["asset_id"] == "pump-02"
    assert record["run_at"].startswith("2026-07-07T12:00:00")  # ISO-8601 via the contracts


def test_file_sink_creates_missing_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "out.jsonl"
    FileSink(out).write(_result())
    assert out.exists()


def test_file_sink_confines_path_to_output_dir(tmp_path: Path) -> None:
    # With a server-configured output_dir, only the basename is honored, so a traversal
    # attempt cannot escape the directory (the API/UI safety net).
    base = tmp_path / "results"
    build_file(SinkResources(output_dir=base), path="../../etc/evil.jsonl").write(_result())
    assert (base / "evil.jsonl").exists()
    assert not (tmp_path / "etc" / "evil.jsonl").exists()


def test_file_sink_honors_raw_path_without_output_dir(tmp_path: Path) -> None:
    target = tmp_path / "here.jsonl"
    build_file(SinkResources(), path=str(target)).write(_result())
    assert target.exists()


def test_postgres_sink_requires_a_session_factory() -> None:
    with pytest.raises(ValueError, match="requires a database"):
        build_postgres(SinkResources())
