"""End-to-end runner tests: the walking skeleton's first full walk.

The inline fixture has known-exact counts so every causal link in the chain
(input line -> event/reject -> detection -> alert lineage) is asserted, through
a real (file-backed SQLite) database inspected by a second engine.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from iotshield.contracts import Asset, Criticality, Lane, NormalizedEvent
from iotshield.pipeline import PipelineConfig, compute_run, run_pipeline
from iotshield.pipeline import runner as runner_module
from iotshield.storage import make_engine, make_session_factory, repository

REPO_ROOT = Path(__file__).parents[2]

# 4 good lines, 2 bad (single field; naive timestamp). Values > 80 trigger the
# stub detector: 96, 99 (same 5-min bucket) and 90 (a distant bucket) -> 2 alerts.
INLINE_CSV = """\
2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0
2026-07-07T12:01:00+00:00,pump-02,Pressure,96.0
2026-07-07T12:01:30+00:00,pump-02,Pressure,99.0
2026-07-07T12:30:00+00:00,gw-01,Temperature,90.0
garbage line
2026-07-07T12:02:00,pump-02,Pressure,50.0
"""

# One parseable telemetry line and three that aren't: a 75% reject rate, the
# signature of the wrong normalizer for the data (ADR-0017), not a few corrupt lines.
MOSTLY_MALFORMED_CSV = """\
2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0
not a telemetry row
also not a row
neither is this
"""


def make_config(tmp_path: Path, **overrides: Any) -> PipelineConfig:
    base: dict[str, Any] = {
        "database_url": f"sqlite:///{(tmp_path / 'run.db').as_posix()}",
        "source_format": "stub-csv",
        "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
    }
    return PipelineConfig.model_validate(base | overrides)


def write_input(tmp_path: Path, text: str = INLINE_CSV) -> Path:
    path = tmp_path / "input.csv"
    path.write_text(text, encoding="utf-8")
    return path


def open_db(config: PipelineConfig) -> Session:
    """Open a second engine onto the database a completed run already created."""
    assert config.database_url is not None  # these fixtures always configure a postgres sink
    return make_session_factory(make_engine(config.database_url))()


class TestEndToEnd:
    def test_report_counts_match_fixture(self, tmp_path: Path) -> None:
        report = run_pipeline(make_config(tmp_path), write_input(tmp_path))
        assert (report.events, report.malformed, report.detections, report.alerts) == (4, 2, 3, 2)

    def test_database_state_matches_report(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        report = run_pipeline(config, write_input(tmp_path))
        with open_db(config) as session:
            assert len(repository.get_events(session)) == report.events
            assert len(repository.list_malformed(session)) == report.malformed
            assert len(repository.get_detections(session)) == report.detections
            assert len(repository.get_alerts(session)) == report.alerts

    def test_alert_lineage_resolves_and_covers_all_detections(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        run_pipeline(config, write_input(tmp_path))
        with open_db(config) as session:
            detection_ids = {d.detection_id for d in repository.get_detections(session)}
            linked = [
                d_id for alert in repository.get_alerts(session) for d_id in alert.detection_ids
            ]
            assert set(linked) == detection_ids  # every detection covered ...
            assert len(linked) == len(detection_ids)  # ... by exactly one alert

    def test_assets_discovered_and_registrations_win(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            assets=[
                {
                    "asset_id": "pump-02",
                    "name": "Feed pump 2",
                    "asset_type": "pump",
                    "criticality": "high",
                }
            ],
        )
        run_pipeline(config, write_input(tmp_path))
        with open_db(config) as session:
            assets = {a.asset_id: a for a in repository.list_assets(session)}
        assert assets["pump-02"].name == "Feed pump 2"  # seeded registration kept
        assert assets["pump-02"].criticality == Criticality.HIGH
        assert assets["gw-01"].metadata == {"auto_discovered": True}  # from data

    def test_crash_mid_run_leaves_no_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def exploding_engine(name: str, params: dict[str, Any]) -> Any:
            def build_alerts(detections: Any, assets: Any) -> Any:
                raise RuntimeError("boom")

            return SimpleNamespace(build_alerts=build_alerts)

        monkeypatch.setattr(runner_module, "build_alert_engine", exploding_engine)
        config = make_config(tmp_path)
        with pytest.raises(RuntimeError, match="boom"):
            run_pipeline(config, write_input(tmp_path))
        with open_db(config) as session:
            # Atomicity: events were flushed before the crash, but never committed.
            assert repository.get_events(session) == []
            assert repository.list_malformed(session) == []

    def test_report_render_is_human_readable(self, tmp_path: Path) -> None:
        report = run_pipeline(make_config(tmp_path), write_input(tmp_path))
        text = report.render()
        assert "events: 4" in text and "alerts: 2" in text
        assert "pump-02" in text  # top alert names its asset
        assert "detection(s)" in text  # ... and carries a rationale
        assert "destinations:" in text and "postgres" in text  # where results went

    def test_utf8_bom_input_is_not_rejected(self, tmp_path: Path) -> None:
        # A UTF-8 BOM (Excel/Notepad default on Windows) must not glue onto the
        # first field and reject line 1.
        path = tmp_path / "bom.csv"
        path.write_bytes(b"\xef\xbb\xbf" + INLINE_CSV.encode("utf-8"))
        report = run_pipeline(make_config(tmp_path), path)
        assert report.events == 4  # first line survived the BOM

    def test_incompatible_config_is_refused_up_front(self, tmp_path: Path) -> None:
        # An unsupported source_format would crash the normalizer; the guard blocks
        # it before any adapter is built or any database touched.
        config = make_config(tmp_path, source_format="sislog")
        with pytest.raises(ValueError, match="incompatible pipeline configuration"):
            run_pipeline(config, write_input(tmp_path))

    def test_high_malformed_rate_warns_of_likely_wrong_normalizer(self, tmp_path: Path) -> None:
        # Most input rejected -> the run is probably pointed at the wrong normalizer;
        # the report must say so, not hide it as a big malformed pile (ADR-0017). The
        # run still completes and the malformed records are still captured.
        report = run_pipeline(make_config(tmp_path), write_input(tmp_path, MOSTLY_MALFORMED_CSV))
        assert report.malformed > report.events
        assert any("rejected" in w and "may not match" in w for w in report.warnings)
        assert "Warnings:" in report.render()


class TestDemoArtifacts:
    """The committed demo files are covered by exact counts: the demo cannot rot."""

    def test_demo_csv_and_config_produce_expected_run(self, tmp_path: Path) -> None:
        from iotshield.pipeline import load_config

        config = load_config(REPO_ROOT / "config" / "demo.yaml").model_copy(
            update={"database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"}
        )
        report = run_pipeline(config, REPO_ROOT / "data" / "demo" / "demo.csv")
        assert (report.events, report.malformed, report.detections, report.alerts) == (19, 3, 4, 2)
        # Registered high-criticality pump outranks the discovered gateway.
        assert report.top_alerts[0].asset_id == "pump-02"
        assert report.top_alerts[1].asset_id == "gw-01"

    def test_file_demo_config_writes_jsonl_with_no_database(self, tmp_path: Path) -> None:
        # The file-only demo: the same normalize+detect as demo.yaml, but a file sink and
        # no database at all (ADR-0018). Redirect the output into tmp so the run is hermetic.
        from iotshield.pipeline import SinkConfig, load_config

        out = tmp_path / "results.jsonl"
        config = load_config(REPO_ROOT / "config" / "demo_file.yaml").model_copy(
            update={"sinks": [SinkConfig(name="file", params={"path": str(out)})]}
        )
        assert config.database_url is None
        report = run_pipeline(config, REPO_ROOT / "data" / "demo" / "demo.csv")
        assert (report.events, report.malformed, report.detections) == (19, 3, 4)
        assert report.destinations == ["file"]
        record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        assert len(record["events"]) == 19

    def test_team3_demo_produces_a_real_detection_and_alert(self, tmp_path: Path) -> None:
        # The team3 detector on a real SKAB valve fault: one sustained anomaly
        # episode -> one prioritized alert on the registered testbed asset.
        from iotshield.contracts import AlertSeverity
        from iotshield.pipeline import load_config

        config = load_config(REPO_ROOT / "config" / "demo_team3.yaml").model_copy(
            update={"database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"}
        )
        report = run_pipeline(config, REPO_ROOT / "data" / "demo" / "demo_team3.csv")
        assert (report.events, report.malformed, report.detections, report.alerts) == (
            2400,
            0,
            1,
            1,
        )
        top = report.top_alerts[0]
        assert top.asset_id == "skab-testbed"
        assert top.severity == AlertSeverity.CRITICAL

    def test_triage_demo_ranks_by_severity_criticality_and_volume(self, tmp_path: Path) -> None:
        # The flagship: two detectors (team3_zscore + stub_threshold) feed the
        # weighted engine over four assets, so the ranked queue exercises every
        # single-lane priority factor at once (ADR-0012 + ADR-0013). The stub
        # threshold (300) sits above SKAB's largest channel, so the two detectors
        # never overlap: team3 owns skab-testbed, stub owns the pumps.
        from iotshield.contracts import AlertSeverity
        from iotshield.pipeline import load_config

        config = load_config(REPO_ROOT / "config" / "demo_triage.yaml").model_copy(
            update={"database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"}
        )
        report = run_pipeline(config, REPO_ROOT / "data" / "demo" / "demo_triage.csv")
        assert (report.events, report.malformed, report.detections, report.alerts) == (
            2414,
            0,
            8,
            4,
        )
        ranked = [(alert.asset_id, alert.severity) for alert in report.top_alerts]
        assert ranked == [
            ("skab-testbed", AlertSeverity.HIGH),  # real team3 anomaly, severity saturated
            ("reactor-pump", AlertSeverity.HIGH),  # 750 breach x CRITICAL asset
            ("utility-pump", AlertSeverity.MEDIUM),  # IDENTICAL breach, LOW asset -> demoted
            ("boiler-pump", AlertSeverity.MEDIUM),  # mild readings but 5x -> volume escalates
        ]

    def test_syslog_demo_normalizes_multi_host_logs(self, tmp_path: Path) -> None:
        # The normalizer showcase: two syslog dialects across three hosts, no
        # numeric readings, so the telemetry detector fires on nothing. The visible
        # results are LOG events and the quarantined malformed lines (ADR-0011).
        from iotshield.pipeline import load_config

        config = load_config(REPO_ROOT / "config" / "demo_syslog.yaml").model_copy(
            update={"database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"}
        )
        report = run_pipeline(config, REPO_ROOT / "data" / "demo" / "demo_syslog.log")
        assert (report.events, report.malformed, report.detections, report.alerts) == (
            12,
            3,
            0,
            0,
        )
        # The telemetry detector on log-lane data idles by design (ADR-0011): the
        # guard surfaces that as a warning rather than refusing the showcase run.
        assert any("will not fire" in warning for warning in report.warnings)
        assert "Warnings:" in report.render()
        # ... and the 20% reject rate stays below the malformed-rate threshold, so the
        # showcase is not falsely flagged as a wrong-normalizer misconfig (ADR-0017).
        assert not any("may not match" in warning for warning in report.warnings)


class TestMalformedRateWarning:
    """The threshold logic behind the input-appropriateness warning (ADR-0017)."""

    @pytest.mark.parametrize(
        "events,malformed,warns",
        [
            (0, 0, False),  # empty input: no data, no signal
            (10, 0, False),  # a clean feed
            (10, 3, False),  # 23% rejects: normal partial corruption, below threshold
            (1, 1, True),  # 50%: at the threshold
            (1, 3, True),  # 75%: mostly rejected
            (0, 5, True),  # nothing parsed at all
        ],
    )
    def test_threshold(self, events: int, malformed: int, warns: bool) -> None:
        warning = runner_module._high_malformed_warning("stub", events, malformed)
        assert (warning is not None) == warns
        if warning is not None:
            assert "stub" in warning and "may not match" in warning


def _event(asset_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        asset_id=asset_id,
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        raw="x",
    )


class TestStoreAgnosticCompute:
    """The compute phase produces a full result without touching any store (ADR-0018)."""

    def test_compute_run_touches_no_database(self, tmp_path: Path) -> None:
        # make_config points database_url at run.db; compute_run must never open it.
        config = make_config(tmp_path)
        result = compute_run(config, INLINE_CSV, origin="inline")
        counts = (
            len(result.events),
            len(result.malformed),
            len(result.detections),
            len(result.alerts),
        )
        assert counts == (4, 2, 3, 2)
        assert {a.asset_id for a in result.assets} == {"pump-02", "gw-01"}
        assert not (tmp_path / "run.db").exists()  # no database was created or opened

    def test_resolve_assets_registration_beats_discovery(self) -> None:
        seeded = [
            Asset(
                asset_id="pump-02",
                name="Feed pump 2",
                asset_type="pump",
                criticality=Criticality.HIGH,
            )
        ]
        resolved = {
            a.asset_id: a
            for a in runner_module.resolve_assets(seeded, [_event("pump-02"), _event("gw-01")])
        }
        assert resolved["pump-02"].criticality is Criticality.HIGH  # registration kept
        assert resolved["pump-02"].metadata == {}  # not a discovered stub
        assert resolved["gw-01"].criticality is Criticality.MEDIUM  # discovered default
        assert resolved["gw-01"].metadata == {"auto_discovered": True}

    def test_file_only_run_writes_jsonl_without_a_database(self, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        config = PipelineConfig.model_validate(
            {
                "source_format": "stub-csv",
                "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
                "sinks": [{"name": "file", "params": {"path": str(out)}}],
            }
        )
        assert config.database_url is None  # a file-only run needs no database
        report = run_pipeline(config, write_input(tmp_path))
        assert (report.events, report.malformed, report.detections, report.alerts) == (4, 2, 3, 2)
        assert report.destinations == ["file"]
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # one self-contained record for this run
        record = json.loads(lines[0])
        assert record["normalizer"] == "stub"
        assert len(record["events"]) == 4 and len(record["alerts"]) == 2
