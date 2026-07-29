"""Boundary behaviour a run depends on: bad values in, bad config out, stable output.

Each of these is a case where the product used to fail in the one way ADR-0004 forbids —
crashing, half-writing, or silently varying — rather than quarantining and carrying on
(ADR-0033).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.pipeline.config import PipelineSpec
from iotshield.pipeline.registry import SinkResources, build_sink
from iotshield.pipeline.runner import compute_run, resolve_assets

STUB_ROW = "2026-07-07T12:00:00+00:00,pump-02,Pressure,120\n"


def _spec(**overrides: object) -> PipelineSpec:
    base: dict[str, object] = {
        "source_format": "stub-csv",
        "normalizer": "stub",
        "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
        "alert_engine": {"name": "weighted"},
    }
    return PipelineSpec.model_validate(base | overrides)


class TestUnusableValuesAreQuarantinedNotFatal:
    def test_a_timestamp_that_overflows_on_utc_conversion_is_a_validation_error(self) -> None:
        """astimezone raises OverflowError near datetime.min/max, and pydantic only converts
        ValueError — so this escaped the normalizer's handler and killed the whole run."""
        with pytest.raises(ValidationError):
            NormalizedEvent.model_validate(
                {
                    "timestamp": "9999-12-31T23:59:59-05:00",
                    "asset_id": "gw-01",
                    "source_type": Lane.LOG,
                    "source_format": "syslog",
                    "raw": "x",
                }
            )

    def test_a_syslog_line_with_such_a_timestamp_becomes_a_malformed_record(self) -> None:
        from iotshield.pipeline.registry import build_normalizer

        result = build_normalizer("syslog", {}).normalize(
            "<34>1 9999-12-31T23:59:59.000-05:00 host su - ID47 - test\n",
            source_format="syslog",
            origin="net",
        )

        assert not result.events
        assert len(result.malformed) == 1

    @pytest.mark.parametrize(
        "metadata",
        [
            {"quality": float("nan")},
            {"quality": float("inf")},
            {"nested": {"deep": [1.0, float("-inf")]}},
        ],
    )
    def test_a_non_finite_number_in_metadata_is_refused(self, metadata: dict) -> None:
        """json.dumps emits the non-JSON tokens NaN/Infinity: Postgres refuses them and takes
        the whole run's transaction with it, while SQLite stored them and the file sink
        rewrote them to null. The contract is where the stores stop disagreeing."""
        with pytest.raises(ValidationError):
            NormalizedEvent(
                timestamp=datetime(2026, 7, 7, tzinfo=UTC),
                asset_id="pump-02",
                source_type=Lane.TELEMETRY,
                source_format="stub-csv",
                value=1.0,
                raw="x",
                metadata=metadata,
            )

    def test_ordinary_metadata_still_passes(self) -> None:
        event = NormalizedEvent(
            timestamp=datetime(2026, 7, 7, tzinfo=UTC),
            asset_id="pump-02",
            source_type=Lane.TELEMETRY,
            source_format="stub-csv",
            value=1.0,
            raw="x",
            metadata={"counter": 12.5, "flags": [1, 2], "ok": True, "note": None},
        )

        assert event.metadata["counter"] == 12.5


class TestSinkConfigFailsBeforeAnySideEffect:
    @pytest.mark.parametrize("bad", ["..", ".", "", "   "])
    def test_a_path_that_does_not_name_a_file_is_refused(self, bad: str, tmp_path: Path) -> None:
        """Path('..').name is '..', not '', so the default-filename fallback did not fire and
        the sink was handed the parent directory — an opaque OSError, not a 422."""
        with pytest.raises(ValueError):
            build_sink("file", {"path": bad}, SinkResources(output_dir=tmp_path))

    @pytest.mark.parametrize("path", ["../../etc/evil.jsonl", "/abs/evil.jsonl", "sub/dir/x.jsonl"])
    def test_a_traversing_path_is_confined_not_rejected(self, path: str, tmp_path: Path) -> None:
        """Observed through a real write rather than the sink's private attribute, so the
        assertion is about where output LANDS."""
        sink = build_sink("file", {"path": path}, SinkResources(output_dir=tmp_path))
        sink.write(compute_run(_spec(), [STUB_ROW], origins=["x"]))

        written = list(tmp_path.iterdir())
        assert [item.name for item in written] == [Path(path).name]

    def test_a_typo_in_the_second_sink_does_not_let_the_first_one_write(
        self, tmp_path: Path
    ) -> None:
        """The default deployment shape is [postgres, file]: building inside the write loop
        committed the run and *then* exited, so re-running duplicated the data."""
        from iotshield.pipeline import PipelineConfig
        from iotshield.pipeline.runner import execute_run

        first = tmp_path / "first.jsonl"
        config = PipelineConfig.model_validate(
            {
                "source_format": "stub-csv",
                "normalizer": "stub",
                "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
                "alert_engine": {"name": "weighted"},
                "sinks": [
                    {"name": "file", "params": {"path": "first.jsonl"}},
                    {"name": "file", "params": {"pth": "second.jsonl"}},
                ],
            }
        )

        with pytest.raises((ValueError, TypeError)):
            execute_run(
                config, [STUB_ROW], origins=["x"], resources=SinkResources(output_dir=tmp_path)
            )

        assert not first.exists()


class TestRunOutputIsStable:
    def test_discovered_assets_come_out_in_a_defined_order(self) -> None:
        """Set iteration follows per-process string hashing, so identical input produced
        different JSONL between runs of the same file."""
        events = [
            NormalizedEvent(
                timestamp=datetime(2026, 7, 7, tzinfo=UTC),
                asset_id=asset_id,
                source_type=Lane.TELEMETRY,
                source_format="stub-csv",
                value=1.0,
                raw="x",
            )
            for asset_id in ("pump-02", "gw-01", "pump-03")
        ]

        resolved = resolve_assets([], events)

        assert [asset.asset_id for asset in resolved] == ["gw-01", "pump-02", "pump-03"]

    def test_a_run_over_the_same_input_is_reproducible(self) -> None:
        first = compute_run(_spec(), [STUB_ROW], origins=["x"])
        second = compute_run(_spec(), [STUB_ROW], origins=["x"])

        assert [a.asset_id for a in first.assets] == [a.asset_id for a in second.assets]
        assert [a.priority_score for a in first.alerts] == [a.priority_score for a in second.alerts]
