"""End-to-end runner tests: the walking skeleton's first full walk.

The inline fixture has known-exact counts so every causal link in the chain
(input line -> event/reject -> detection -> alert lineage) is asserted, through
a real (file-backed SQLite) database inspected by a second engine.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy.orm import Session

from iotshield.contracts import Asset, Criticality, Lane, NormalizedEvent
from iotshield.pipeline import (
    PipelineConfig,
    PipelineSpec,
    build_detectors,
    compute_result,
    compute_run,
    normalize_sources,
    run_pipeline,
)
from iotshield.pipeline import runner as runner_module
from iotshield.pipeline.compat import IncompatiblePipelineError
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
            assert list(repository.get_events(session)) == []
            assert list(repository.list_malformed(session)) == []

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


class TestWalkthroughDemo:
    """The dashboard walkthrough, pinned by exact counts: the demo cannot rot.

    These build the spec inline rather than loading a YAML file, because what must not rot is
    the two runs a presenter actually performs from the Compose page. `config/demo.yaml` is
    arm B of the same walkthrough for the CLI and the compose `runner`, and it is checked
    where its own failure mode lives (`test_compat.py`: a chain can be perfectly compatible
    while naming an input that was deleted). Arm A, the flow lane alone, exists only as a
    click, so pinning it here rather than in a file is not a shortcut.

    ``scripts/make_demo_data.py`` writes the three inputs. Every number below is arithmetic
    a presenter says out loud in front of an audience — the detection counts add up by lane,
    and the priority moves for one device and not the other — so a change to either side
    fails here before it fails on a projector.

    **No ``assets:``, deliberately.** ``ComposePage`` has no asset-registration section, so a
    run driven by clicking auto-discovers every device at MEDIUM. Registering the camera at
    HIGH is legal from YAML and raises its priority (52.7 -> 55.7 on one lane), which is a
    real feature and is pinned separately below — but pinning it *here* would document
    numbers nobody can reproduce from the dashboard, and the dashboard is the demo.
    """

    CAMERA = "10.0.0.24"
    GATEWAY = "10.0.0.11"
    SENSOR = "10.0.0.31"

    REGISTERED_CAMERA: ClassVar[list[dict[str, str]]] = [
        {
            "asset_id": "10.0.0.24",
            "name": "Line-1 IP camera (cam-07)",
            "asset_type": "camera",
            "location": "line-1",
            "criticality": "high",
        }
    ]

    def _flow_only(
        self, tmp_path: Path, assets: list[dict[str, str]] | None = None
    ) -> PipelineConfig:
        """Arm A: the flow lane alone — one modality, judged on its own."""
        return PipelineConfig.model_validate(
            {
                "database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}",
                "sources": [
                    {
                        "normalizer": "flow",
                        "source_format": "zeek-conn",
                        "path": str(REPO_ROOT / "data" / "demo" / "camera_flow.log"),
                    }
                ],
                "detectors": [{"name": "rules", "params": {"lane": "flow"}}],
                "alert_engine": {"name": "weighted", "params": {"bucket_minutes": 5}},
                "assets": assets or [],
            }
        )

    def _all_lanes(self, tmp_path: Path) -> PipelineConfig:
        """Arm B: the same bytes plus two more lanes on the same device."""
        demo = REPO_ROOT / "data" / "demo"
        return PipelineConfig.model_validate(
            {
                "database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}",
                "sources": [
                    {
                        "normalizer": "flow",
                        "source_format": "zeek-conn",
                        "path": str(demo / "camera_flow.log"),
                    },
                    {
                        "normalizer": "syslog",
                        "source_format": "syslog",
                        "path": str(demo / "camera_syslog.log"),
                    },
                    {
                        "normalizer": "stub",
                        "source_format": "stub-csv",
                        "path": str(demo / "camera_telemetry.csv"),
                    },
                ],
                "detectors": [
                    {"name": "rules", "params": {"lane": "flow"}},
                    {"name": "rules", "params": {"lane": "log"}},
                    {"name": "stub_threshold"},
                ],
                "alert_engine": {"name": "weighted", "params": {"bucket_minutes": 5}},
            }
        )

    def test_arm_a_judges_one_lane_and_names_the_threat(self, tmp_path: Path) -> None:
        # Signatures, not rate detection: the camera probes SIX hosts on telnet (fan-out's
        # boundary is 20) and uploads 4.2 MB (exfiltration's is 50 MB), so every statistical
        # detector would correctly stay silent and a rule catches it anyway.
        from iotshield.contracts import AlertSeverity

        report = run_pipeline(self._flow_only(tmp_path))
        assert (report.events, report.malformed, report.detections, report.alerts) == (12, 0, 3, 2)

        camera, gateway = report.top_alerts
        assert camera.asset_id == self.CAMERA
        assert camera.severity is AlertSeverity.HIGH
        assert camera.priority_score == pytest.approx(52.7, abs=0.05)
        assert len(camera.detection_ids) == 2  # telnet propagation + the bulk upload
        assert camera.score_components["corroboration"] == 1.0  # one lane, on its own
        # The alert says WHAT it is, which is the whole point of the rule detector.
        assert camera.threat_class == "Remote Services (T1021)"

        # A real but lesser finding, which is what gives the queue something to rank.
        assert gateway.asset_id == self.GATEWAY
        assert gateway.severity is AlertSeverity.MEDIUM
        assert gateway.threat_class == "External Remote Services (T1133)"

    def test_arm_b_corroborates_three_lanes_on_the_same_device(self, tmp_path: Path) -> None:
        # The payoff. corroboration is 1 + 0.5*(lanes-1), so three lanes make it 2.0 — its
        # highest value with today's three lanes.
        from iotshield.contracts import AlertSeverity

        report = run_pipeline(self._all_lanes(tmp_path))
        assert (report.events, report.malformed, report.detections, report.alerts) == (110, 2, 8, 2)

        camera = report.top_alerts[0]
        assert camera.asset_id == self.CAMERA
        assert camera.severity is AlertSeverity.CRITICAL
        assert camera.priority_score == pytest.approx(80.0, abs=0.05)
        assert camera.score_components["corroboration"] == 2.0
        # 2 flow + 2 log + 3 telemetry. The sum is checkable by eye on a projector, which is
        # why the thermal excursion is three samples rather than a long climb.
        assert len(camera.detection_ids) == 7

    def test_registering_the_camera_raises_it_above_the_discovered_default(
        self, tmp_path: Path
    ) -> None:
        """Asset criticality is a real ranking factor, and the dashboard cannot reach it.

        `ComposePage` has no asset-registration section, so every device a clicked run sees is
        auto-discovered at MEDIUM (`asset_weight` 1.10). Declaring the camera HIGH from YAML
        weights it 1.20 and moves it 52.7 -> 55.7 on the flow lane alone. The *band* does not
        change, which is why the walkthrough loses nothing by not registering anything — but
        the factor exists and is worth pinning, since the gap between what config can express
        and what the UI can is exactly the kind of thing that rots unnoticed.
        """
        discovered = run_pipeline(self._flow_only(tmp_path)).top_alerts[0]
        registered = run_pipeline(
            self._flow_only(tmp_path, assets=self.REGISTERED_CAMERA)
        ).top_alerts[0]

        assert discovered.score_components["asset_weight"] == pytest.approx(1.10)
        assert registered.score_components["asset_weight"] == pytest.approx(1.20)
        assert registered.priority_score > discovered.priority_score
        assert registered.severity is discovered.severity

    def test_the_extra_lanes_move_the_camera_and_leave_the_gateway_alone(
        self, tmp_path: Path
    ) -> None:
        """The walkthrough's actual claim, asserted as the difference between the two arms.

        A priority that rose everywhere would prove nothing — it could be a scoring change.
        What corroboration means is that the device with independent agreement across lanes
        moves and the device without it does not.
        """
        solo = {a.asset_id: a for a in run_pipeline(self._flow_only(tmp_path)).top_alerts}
        joint = {a.asset_id: a for a in run_pipeline(self._all_lanes(tmp_path)).top_alerts}

        assert joint[self.CAMERA].priority_score > solo[self.CAMERA].priority_score
        assert joint[self.GATEWAY].priority_score == solo[self.GATEWAY].priority_score
        # Same detection, same score, same rule: only the corroboration factor differs.
        assert (
            joint[self.CAMERA].score_components["severity"]
            == solo[self.CAMERA].score_components["severity"]
        )

    def test_the_quiet_device_stays_quiet(self, tmp_path: Path) -> None:
        # A ranked queue is only credible if something in the input earns nothing. The sensor
        # appears on the flow and log lanes and raises no alert in either arm.
        for config in (self._flow_only(tmp_path), self._all_lanes(tmp_path)):
            report = run_pipeline(config)
            assert self.SENSOR not in {alert.asset_id for alert in report.top_alerts}

    def test_unparseable_lines_are_quarantined_rather_than_dropped(self, tmp_path: Path) -> None:
        # "Nothing is silently dropped" is a claim the Data Health page makes visible, so the
        # demo input carries two genuinely broken lines: a truncated write and a line with no
        # PRI. Both are captured with their original text.
        report = run_pipeline(self._all_lanes(tmp_path))
        assert report.malformed == 2
        # ...and two bad lines out of 112 stays far below the wrong-normalizer warning rate,
        # so the walkthrough is not flagged as a misconfiguration (ADR-0017).
        assert not any("may not match" in warning for warning in report.warnings)

    def test_the_demo_chain_is_clean(self, tmp_path: Path) -> None:
        # Every produced lane has a detector that consumes it, so the compatibility guard has
        # nothing to say. A warning here would appear on screen during the walkthrough.
        assert run_pipeline(self._all_lanes(tmp_path)).warnings == []


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


class TestComputePhases:
    """compute_run is the composition of its two phases, and each is usable alone.

    The seam the evaluation harness needs: it fits detectors on the ingested events before
    judging them, so it must be able to stop between the phases and inject detectors —
    while still running this exact code rather than a parallel implementation (ADR-0021).
    """

    def test_compute_run_equals_its_phases_composed(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        composed = compute_run(config, [INLINE_CSV])
        by_hand = compute_result(
            config, normalize_sources(config, [INLINE_CSV]), build_detectors(config)
        )
        assert len(by_hand.events) == len(composed.events)
        assert len(by_hand.detections) == len(composed.detections)
        assert len(by_hand.alerts) == len(composed.alerts)
        assert by_hand.warnings == composed.warnings

    def test_injected_detectors_replace_the_configured_ones(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        ingested = normalize_sources(config, [INLINE_CSV])
        assert compute_result(config, ingested, []).detections == []
        # ...and the ingest half is untouched by the substitution.
        assert compute_result(config, ingested, []).events == ingested.events

    def test_a_bare_spec_computes(self, tmp_path: Path) -> None:
        # compute_run takes PipelineSpec, so a caller with no deployment wiring — no
        # database_url, no sink to satisfy — can compute. This is what makes "a benchmark
        # cannot reach a store" a typed fact rather than a convention.
        spec = PipelineSpec.model_validate(
            {
                "source_format": "stub-csv",
                "normalizer": "stub",
                "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
            }
        )
        assert not hasattr(spec, "database_url")
        assert compute_run(spec, [INLINE_CSV]).detections
        assert not (tmp_path / "run.db").exists()

    def test_an_untrained_detector_is_refused_before_judging(self) -> None:
        """A run never fits (ADR-0022), so a spec with no cached fit is a config error.

        Naming hyperparameters and no artifact is how you ask the *harness* to fit from
        scratch; asking the product to run it means judging with an untrained model. It is
        refused as a named config error rather than surfacing as a NotFitted from deep
        inside the judge phase — and before any sink runs.
        """
        spec = PipelineSpec.model_validate(
            {
                "source_format": "stub-csv",
                "normalizer": "stub",
                "detectors": [{"name": "team3_zscore", "params": {"window_size": 20}}],
            }
        )
        with pytest.raises(IncompatiblePipelineError, match="no trained state"):
            compute_run(spec, [INLINE_CSV])

    def test_a_configured_artifact_runs_normally(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "source_format": "stub-csv",
                "normalizer": "stub",
                "detectors": [{"name": "team3_zscore", "params": {"artifact": "skab_zscore"}}],
            }
        )
        assert compute_run(spec, [INLINE_CSV]).events  # no raise; it simply finds nothing

    def test_guard_errors_still_raise_before_any_work(self, tmp_path: Path) -> None:
        # The guard runs inside the ingest phase, so both compute_run and a harness that
        # calls the phases directly inherit it.
        spec = PipelineSpec.model_validate(
            {
                "sources": [{"normalizer": "stub", "source_format": "syslog-rfc5424"}],
                "detectors": [{"name": "stub_threshold"}],
            }
        )
        with pytest.raises(ValueError):
            normalize_sources(spec, ["irrelevant"])


class TestStoreAgnosticCompute:
    """The compute phase produces a full result without touching any store (ADR-0018)."""

    def test_compute_run_touches_no_database(self, tmp_path: Path) -> None:
        # make_config points database_url at run.db; compute_run must never open it.
        config = make_config(tmp_path)
        result = compute_run(config, [INLINE_CSV])
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
        assert record["sources"][0]["normalizer"] == "stub"
        assert len(record["events"]) == 4 and len(record["alerts"]) == 2


class TestMultiSource:
    """Multi-source ingestion: per-source routing, pooling into lanes, ordering (ADR-0019)."""

    TELE_A = "2026-07-07T12:00:00+00:00,pump-02,Pressure,95.0\n"
    TELE_B = "2026-07-07T12:03:00+00:00,pump-02,Pressure,99.0\n"
    SYSLOG = "<35>1 2026-07-07T12:03:05Z pump-02 sensord - - - reading out of range\n"

    def multi_config(
        self, tmp_path: Path, sources: list[dict[str, Any]], **overrides: Any
    ) -> PipelineConfig:
        base: dict[str, Any] = {
            "database_url": f"sqlite:///{(tmp_path / 'run.db').as_posix()}",
            "sources": sources,
            "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
        }
        return PipelineConfig.model_validate(base | overrides)

    def test_two_formats_route_and_pool_into_their_lanes(self, tmp_path: Path) -> None:
        config = self.multi_config(
            tmp_path,
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
        )
        result = compute_run(config, [self.TELE_A + self.TELE_B, self.SYSLOG])
        assert {event.source_type for event in result.events} == {Lane.TELEMETRY, Lane.LOG}
        assert len(result.events) == 3  # 2 telemetry + 1 log, pooled into one run
        assert len(result.detections) == 2  # only the telemetry detector fires
        assert any("no detector that consumes it" in w for w in result.warnings)

    def test_two_sources_of_one_normalizer_with_different_params_stay_distinct(
        self, tmp_path: Path
    ) -> None:
        """Normalizers dedupe by name AND params (ADR-0023 D8).

        Two flow sources watching different home networks are genuinely two normalizers.
        Deduping on the name alone would build one and reuse it for both, silently
        attributing the second source's flows using the first's networks — here that would
        reject every row of source 2 as "neither endpoint is local".
        """
        header = "start,src_ip,src_port,dst_ip,dst_port,proto\n"
        site_a = header + "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp\n"
        site_b = header + "2026-07-07T12:00:01+00:00,198.51.100.4,51515,203.0.113.9,23,tcp\n"
        config = self.multi_config(
            tmp_path,
            [
                {"normalizer": "flow", "source_format": "flow-csv"},
                {
                    "normalizer": "flow",
                    "source_format": "flow-csv",
                    "params": {"home_networks": ["198.51.100.0/24"]},
                },
            ],
        )
        result = compute_run(config, [site_a, site_b])
        assert result.malformed == []
        assert sorted(event.asset_id for event in result.events) == ["10.0.0.7", "198.51.100.4"]

    def test_pooled_lane_events_reach_the_detector_in_time_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two telemetry sources, out of order across and within the sources; the detector
        # must still see (asset, time)-ordered events (the port guarantee, ADR-0019).
        captured: list[NormalizedEvent] = []

        class Spy:
            lane = Lane.TELEMETRY
            method = "spy"

            def detect(self, events: Any) -> list[Any]:
                captured.extend(events)
                return []

        monkeypatch.setattr(runner_module, "build_detector", lambda name, params: Spy())
        late = "2026-07-07T12:09:00+00:00,pump-02,P,1\n2026-07-07T12:01:00+00:00,pump-02,P,2\n"
        mid = "2026-07-07T12:05:00+00:00,pump-02,P,3\n"
        config = self.multi_config(
            tmp_path,
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "stub", "source_format": "stub-csv"},
            ],
        )
        compute_run(config, [late, mid])
        times = [event.timestamp for event in captured]
        assert len(times) == 3 and times == sorted(times)

    def test_inputs_must_align_one_to_one_with_sources(self, tmp_path: Path) -> None:
        config = self.multi_config(
            tmp_path,
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
        )
        with pytest.raises(ValueError, match="expected 2 input"):
            compute_run(config, [self.TELE_A])
