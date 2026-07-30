"""The shipped corpora: they must be fixed on disk, and they must discriminate.

Two properties per corpus. **Fixed**: what is committed has to equal what ``build`` produces,
or the bundle a benchmark reads is not the one this code describes — a gap nothing checked
before (``test_corpus`` pinned write-then-rewrite idempotence, which is a different claim).
**Discriminating**: a corpus where every detector scores the same teaches nothing, so each one
has to separate detectors that a weaker corpus would tie.
"""

from functools import cache
from pathlib import Path

import pytest

from iotshield.contracts import Lane
from iotshield.eval import CaseReport, FitProtocol, evaluate, load_bundle, write_bundle
from iotshield.eval.corpus import DEFAULT_CORPUS_ROOT
from iotshield.eval.generate import SCENARIOS
from iotshield.eval.generate.scenarios import CAMERA, GATEWAY, SERVER
from iotshield.eval.generate.telemetry import _PROFILES as _TELEMETRY_PROFILES
from iotshield.eval.generate.telemetry import THRESHOLD
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV
from iotshield.pipeline import build_detectors, compute_result, load_spec, normalize_sources
from tests.eval.generate.conftest import built

STUB_SPEC = Path("config/bench/stub.yaml")
_NAMES = sorted(SCENARIOS)

SIZE_BUDGET_BYTES = 4 * 1024 * 1024
"""Total committed corpus bytes. Generous but finite: SKAB's labelled subsets are 599 KB
gzipped, so this leaves room for several lanes while still failing loudly if a scenario starts
committing megabytes that should have been a fetch step."""


class TestShape:
    def test_at_least_one_scenario_ships(self) -> None:
        assert _NAMES, "no scenarios: every test below would be vacuous"

    @pytest.mark.parametrize("name", _NAMES)
    def test_it_declares_its_own_licence_and_provenance(self, name: str) -> None:
        # A bundle that cannot say where it came from is one nobody can safely reuse — the
        # whole reason third-party data ships as a fetch step instead.
        corpus = built(name)
        assert "iotshield" in corpus.provenance
        assert corpus.licence

    @pytest.mark.parametrize("name", _NAMES)
    def test_every_case_supplies_one_input_per_declared_lane(self, name: str) -> None:
        # A case's inputs align 1:1 and positionally with a spec's sources
        # (runner._check_alignment), so this tuple is the shape any bench spec must match.
        scenario = SCENARIOS[name]
        for case in built(name).cases:
            assert [given.source_format for given in case.inputs] == list(scenario.formats)

    @pytest.mark.parametrize("name", _NAMES)
    def test_it_carries_clean_recordings_as_well_as_faulty_ones(self, name: str) -> None:
        # Both are needed: faults score recall, clean recordings score false alarms, and a
        # corpus of only faults cannot measure the second at all.
        corpus = built(name)
        assert any(case.truth.is_clean for case in corpus.cases)
        assert any(not case.truth.is_clean for case in corpus.cases)

    @pytest.mark.parametrize("name", _NAMES)
    def test_it_round_trips_through_a_bundle(self, name: str, tmp_path: Path) -> None:
        corpus = built(name)
        write_bundle(tmp_path / name, corpus)
        restored = load_bundle(tmp_path / name)
        assert len(restored.cases) == len(corpus.cases)
        assert restored.incidents == corpus.incidents
        for before, after in zip(corpus.cases, restored.cases, strict=True):
            assert after.truth.assets == before.truth.assets


def test_no_telemetry_profiles_normal_readings_reach_the_boundary() -> None:
    """``_Profile``'s claim that "every profile must straddle THRESHOLD" — actually pinned.

    The docstring said "a meta-test pins it" and none existed, which is the same class of false
    pin-claim ADR-0028 was written to remove. A profile whose normal readings sit *above* the
    detector's boundary would false-alarm on every instant of every clean recording; one whose
    channels a fault cannot lift above it would be invisible to the whole lane. Four sigma is
    the margin: gaussian noise clears it about once in sixteen thousand samples, and a corpus
    has hundreds of thousands.
    """
    assert _TELEMETRY_PROFILES, "no telemetry profiles: this test would be vacuous"
    for kind, profile in _TELEMETRY_PROFILES.items():
        for channel, (mean, spread) in profile.channels.items():
            assert mean + 4 * spread < THRESHOLD, f"{kind}/{channel} normally trips the detector"


class TestDeclaredDifficulty:
    """A scenario's *intent* about how hard a fault is, pinned rather than merely written down.

    The gap ADR-0028 D6 closed. ``_thermal_rise`` said "kept modest on purpose — the telemetry
    signal alone should be arguable, so that the multimodal corpus measures corroboration rather
    than a spike anyone would catch", and ``_trimodal_recordings`` said "each is arguable alone —
    which is the point". Both were false: a dimensioned ``intensity`` of 30 drove the camera's
    temperature to 316.7 against a threshold of 80. Nothing failed, because no test asserted the
    one property the corpus was designed around.
    """

    MARGINAL = 2.0
    """How many multiples of its boundary a fault may reach and still count as *marginal*.

    Generous on purpose: the claim being defended is "arguable rather than obvious", not a
    specific magnitude, and pinning it tighter would make the test a restatement of the scenario
    instead of a check on it.
    """

    def test_the_trimodal_compromise_is_marginal_on_every_lane(self) -> None:
        compromise = [
            fault
            for recording in SCENARIOS["device_trimodal"].recordings
            for fault in recording.faults
            if fault.kind == "compromise"
        ]
        assert compromise, "the corroboration corpus has no compromise fault to check"
        for fault in compromise:
            assert 1.0 < fault.intensity <= self.MARGINAL, (
                f"{fault.fault_id} at {fault.intensity}x its boundary is not marginal; the "
                "corroboration A/B would compare corroboration against an already-solved problem"
            )

    def test_the_telemetry_half_of_a_compromise_stays_near_the_threshold(self) -> None:
        """The same claim measured in the bytes, not in the scenario's declaration.

        Both are needed: the declaration test catches a scenario edited to an obvious magnitude,
        this one catches a *behaviour* whose conversion drifts from the boundary it claims to use.
        """
        corpus = built("device_trimodal")
        case = next(c for c in corpus.cases if c.name == "compromise")
        telemetry = next(i for i in case.inputs if i.source_format == STUB_CSV)
        peak = max(
            float(line.split(",")[3])
            for line in telemetry.text.splitlines()
            if line and line.split(",")[1] == CAMERA
        )
        assert peak <= THRESHOLD * self.MARGINAL, (
            f"the compromised camera peaks at {peak} against a boundary of {THRESHOLD}: the "
            "telemetry arm catches it outright and the A/B measures nothing"
        )


class TestCommittedBundlesMatchTheCode:
    """``generated inputs must be fixed`` (ADR-0021 D5b), checked rather than asserted."""

    @pytest.mark.parametrize("name", _NAMES)
    def test_a_fresh_build_reproduces_the_committed_bytes(self, name: str, tmp_path: Path) -> None:
        committed = DEFAULT_CORPUS_ROOT / name
        if not committed.is_dir():
            pytest.skip(f"{name} is not committed in this checkout")
        write_bundle(tmp_path / name, built(name))
        for fresh in sorted((tmp_path / name).rglob("*")):
            if fresh.is_file():
                mirror = committed / fresh.relative_to(tmp_path / name)
                assert mirror.is_file(), f"{mirror} is missing; regenerate the bundle"
                assert fresh.read_bytes() == mirror.read_bytes(), (
                    f"{mirror.name} differs from a fresh build; run "
                    f"`python -m iotshield.eval generate {name}`"
                )

    def test_the_committed_corpora_stay_within_the_size_budget(self) -> None:
        total = sum(
            path.stat().st_size
            for name in _NAMES
            for path in (DEFAULT_CORPUS_ROOT / name).rglob("*")
            if path.is_file()
        )
        assert total <= SIZE_BUDGET_BYTES, f"{total} bytes committed under data/eval"


class TestSyntheticDiscriminates:
    """The reason for three telemetry shapes: they must not rank every detector identically."""

    @staticmethod
    @cache
    def _recall_by_case() -> dict[str, float | None]:
        report = evaluate(built("synthetic"), [("stub", load_spec(STUB_SPEC))])
        return {case.case: case.event_recall for case in report.specs[0].cases}

    def test_a_fixed_threshold_catches_spikes_and_drift(self) -> None:
        for name, recall in self._recall_by_case().items():
            if name.startswith(("spike", "drift")):
                assert recall == 1.0, name

    def test_a_fixed_threshold_is_blind_to_a_stuck_sensor(self) -> None:
        # The discriminating shape: the reading never leaves its normal range, so no absolute
        # threshold can see it. A detector that models *normal* can.
        stuck = [
            recall for name, recall in self._recall_by_case().items() if name.startswith("stuck")
        ]
        assert stuck and all(recall == 0.0 for recall in stuck)

    def test_clean_recordings_produce_no_false_alarms(self) -> None:
        report = evaluate(built("synthetic"), [("stub", load_spec(STUB_SPEC))])
        clean = [case for case in report.specs[0].cases if case.case.startswith("clean")]
        assert clean and all(case.detections == 0 for case in clean)

    def test_more_than_one_recording_per_shape(self) -> None:
        # A single example per shape would let a detector look good or bad by luck, and
        # per-case variance is one of the things the harness exists to show.
        names = [case.name for case in built("synthetic").cases]
        for kind in ("spike", "drift", "stuck"):
            assert sum(1 for name in names if name.startswith(kind)) > 1, kind

    def test_each_faulty_case_has_exactly_one_incident(self) -> None:
        # One fault per recording makes "alerts per incident" directly readable, the same
        # property that made SKAB a good benchmark shape.
        for case in built("synthetic").cases:
            expected = 0 if case.name.startswith("clean") else 1
            assert len(case.truth.incidents) == expected, case.name

    def test_the_bundle_description_states_the_size_it_actually_has(self) -> None:
        """Counts are computed, so they cannot drift — this pins that they are still there.

        The hand-typed version drifted immediately: one scenario claimed a device count a
        recording in it contradicted, and only this scenario's numbers were checked at all.
        """
        corpus = built("synthetic")
        assert f"{len(corpus.cases)} recordings" in corpus.description
        assert f"{corpus.incidents} injected fault(s)" in corpus.description


FLOW_SPEC = Path("config/bench/flow.yaml")


@cache
def _flow_cases() -> dict[str, CaseReport]:
    report = evaluate(built("fleet_scan"), [("flow", load_spec(FLOW_SPEC))])
    return {case.case: case for case in report.specs[0].cases}


@cache
def _alerting_assets() -> frozenset[str]:
    """Which devices the flow spec actually alerts on, across the whole corpus.

    A report is per *case*, so the identity of an alerting device is not in it — and these two
    controls are claims about identity specifically. Composing the pipeline's own phases is how
    the harness runs a benchmark anyway (``runner``), so this measures the same path.
    """
    spec = load_spec(FLOW_SPEC)
    alerting: set[str] = set()
    for case in built("fleet_scan").cases:
        ingested = normalize_sources(
            spec, [given.text for given in case.inputs], origins=[case.name]
        )
        result = compute_result(spec, ingested, build_detectors(spec))
        alerting.update(alert.asset_id for alert in result.alerts)
    return frozenset(alerting)


class TestFleetScanDiscriminates:
    """What a fleet corpus can measure that a single-device one cannot."""

    def test_the_scanner_is_caught(self) -> None:
        assert _flow_cases()["scan-one"].event_recall == 1.0

    def test_two_scanners_are_attributed_separately(self) -> None:
        # Only possible with asset-scoped truth: two faults on two devices, each credited to
        # the detection on its own device (ADR-0026).
        case = _flow_cases()["scan-two"]
        assert case.incidents == 2
        assert case.event_recall == 1.0

    def test_fan_out_is_blind_to_a_port_sweep(self) -> None:
        # The discriminating pair, and the flow analogue of a stuck sensor: peer cardinality
        # stays low while port cardinality explodes, so a fan-out rule cannot see it.
        assert _flow_cases()["port-sweep"].event_recall == 0.0

    def test_fan_out_is_blind_to_exfiltration(self) -> None:
        # One peer, enormous volume. Only the byte-summing preset can see this.
        assert _flow_cases()["exfiltration"].event_recall == 0.0

    def test_a_high_volume_single_destination_device_is_not_a_scanner(self) -> None:
        """`demo_flow.yaml`'s first claimed control, measured for the first time.

        The gateway sends thirty flows inside the window — all to one destination. Volume is
        not fan-out; cardinality is the signal.
        """
        assert GATEWAY not in _alerting_assets()

    def test_an_inbound_heavy_server_is_not_a_scanner(self) -> None:
        """The second claimed control: someone dialling IN is a different phenomenon, and
        counting it would make every popular server a scanner."""
        assert SERVER not in _alerting_assets()

    def test_the_scanner_itself_does_alert(self) -> None:
        # Guard the two controls above: "not in the alerting set" would pass vacuously if
        # nothing alerted at all.
        assert CAMERA in _alerting_assets()

    def test_a_chatty_device_over_the_threshold_is_a_measured_false_alarm(self) -> None:
        """The finding, not a defect.

        ADR-0024's Consequences predicted that a fleet-wide threshold "will produce false
        positives on genuinely chatty devices (a NAT gateway, an update mirror)". The corpus
        contains exactly such a device, so the prediction becomes a number: asserted-clean
        assets do raise alerts, and the harness says how many.
        """
        assert _flow_cases()["clean"].clean_assets_alerted > 0

    def test_misattribution_is_reported_rather_than_scored_as_a_catch(self) -> None:
        # The whole point of asset-scoped truth, on real generated data: the chatty device
        # fires *during* a scan window, and under time-only matching that counted as a catch.
        assert _flow_cases()["scan-one"].misattributed_detections > 0

    def test_the_bench_spec_registers_no_assets(self) -> None:
        # ADR-0021 D7: `asset_weight` is circular, so a criticality we typed in would be
        # indistinguishable from a ranking improvement. Nothing registered means uniform.
        assert not load_spec(FLOW_SPEC).assets


TRIMODAL_ARMS = (
    Path("config/bench/trimodal_telemetry.yaml"),
    Path("config/bench/trimodal_all.yaml"),
)


class TestCorroborationIsMeasurable:
    """The A/B that turns the product's headline claim into a benchmark (ADR-0021 D7).

    The corpus is built so this cannot pass vacuously: corroboration multiplies an alert's
    priority rather than creating one, so a single-device single-incident corpus would score
    identically in both arms. Decoys and a three-lane fault are what give it something to move.
    """

    @staticmethod
    @cache
    def _arms() -> tuple[CaseReport, CaseReport]:
        corpus = built("device_trimodal")
        report = evaluate(corpus, [(path.stem, load_spec(path)) for path in TRIMODAL_ARMS])
        one_lane, all_lanes = report.specs
        return (
            next(case for case in one_lane.cases if case.case == "compromise"),
            next(case for case in all_lanes.cases if case.case == "compromise"),
        )

    def test_judging_three_lanes_raises_the_true_alerts_priority(self) -> None:
        one_lane, all_lanes = self._arms()
        # Byte-identical evidence in both arms: the same bundle, differing only in which
        # detectors ran. So the lift is attributable to judging more modalities.
        assert one_lane.mean_true_priority is not None
        assert all_lanes.mean_true_priority is not None
        assert all_lanes.mean_true_priority > one_lane.mean_true_priority

    def test_the_lift_is_backed_by_more_lanes_not_just_a_bigger_number(self) -> None:
        one_lane, all_lanes = self._arms()
        assert one_lane.mean_lanes_per_true_alert == 1.0
        assert all_lanes.mean_lanes_per_true_alert == 3.0

    def test_true_alerts_outrank_false_ones(self) -> None:
        # Discrimination, not calibration: we claim the ordering, never that any score is right.
        _, all_lanes = self._arms()
        assert all_lanes.mean_true_priority is not None
        assert all_lanes.mean_false_priority is not None
        assert all_lanes.mean_true_priority > all_lanes.mean_false_priority
        assert all_lanes.precision_at_k[1] == 1.0

    def test_the_single_lane_arm_structurally_misses_other_lanes_incidents(self) -> None:
        # Per-lane recall is what makes this legible rather than a mystery: the telemetry-only
        # arm cannot see a flow or log fault at all, which is a coverage fact, not a bad score.
        corpus = built("device_trimodal")
        report = evaluate(corpus, [(TRIMODAL_ARMS[0].stem, load_spec(TRIMODAL_ARMS[0]))])
        case = next(c for c in report.specs[0].cases if c.case == "compromise")
        assert case.lane_recall[Lane.TELEMETRY] == 1.0
        assert case.lane_recall[Lane.FLOW] == 0.0
        assert case.lane_recall[Lane.LOG] == 0.0

    @pytest.mark.parametrize("path", TRIMODAL_ARMS)
    def test_both_arms_declare_no_assets(self, path: Path) -> None:
        assert not load_spec(path).assets


METER_SPEC = Path("config/bench/meter.yaml")


class TestMeterFleetDiscriminates:
    """The behavioural half of ``generate.meter.CALIBRATION``, and why it may name no param.

    Every other lane quotes its fault magnitudes against a detector's *registered default*, and
    a meta-test pins the two together. ``meter_iforest`` has none: it fits its threshold to a
    false-alarm budget on each meter's own history, so its boundary is quoted in units the
    behaviour owns — multiples of what this hour of the week normally draws — and a number in
    those units can only be validated by measuring it. This class is that measurement, and it is
    what earns the ``param=None`` escape hatch rather than the escape hatch being free.
    """

    @staticmethod
    @cache
    def _cases() -> dict[str, CaseReport]:
        """One `evaluate` for the whole class — this corpus is the suite's most expensive.

        Cached as the reports rather than as a projection of them, the shape `_flow_cases` and
        `_arms` already use: two separately-cached accessors over one run meant fitting eight
        isolation forests twice for numbers the first report already held.
        """
        report = evaluate(
            built("meter_fleet"),
            [("meter", load_spec(METER_SPEC))],
            protocol=FitProtocol.PREFIX,
        )
        return {case.case: case for case in report.specs[0].cases}

    @classmethod
    def _recall_by_case(cls) -> dict[str, float | None]:
        return {name: case.event_recall for name, case in cls._cases().items()}

    def test_every_meter_fits_from_its_own_reserved_prefix(self) -> None:
        """Asserted before any number below is read, because the failure mode is silent.

        ``meter_iforest`` refuses a meter with less than ``min_train_hours`` of clean history,
        and ``prefix`` only reserves a fifth of the clock — so a corpus one row short produces a
        whole table of zeros that reads exactly like a detector that cannot detect. That is what
        ``generate.meter.fitting_hours`` computes the recording length to prevent, and this is
        the assertion that keeps it honest if either side's arithmetic moves.
        """
        refused = [
            note for case in self._cases().values() for note in case.notes if "fit refused" in note
        ]
        assert not refused, refused

    def test_a_consumption_surge_is_caught(self) -> None:
        # The floor shape. At 1.3x the declared boundary a detector that models normal must see
        # it, so a miss here means the detector is broken or the boundary is quoted wrong — the
        # two things the registry check would have caught on any other lane.
        assert self._recall_by_case()["spike-04"] == 1.0

    def test_a_frozen_register_is_caught_though_it_stays_in_range(self) -> None:
        # The discriminating shape, and the reason this corpus exists rather than a threshold
        # one: consumption reads exactly zero, which is a perfectly ordinary level for 3am, so
        # no magnitude rule can separate it from a quiet night. Only a model that knows this
        # meter draws 1.7x its daily mean at 7pm can.
        assert self._recall_by_case()["stuck-04"] == 1.0

    def test_both_utilities_are_scored(self) -> None:
        # One detector serving water and electricity is what ADR-0030 built the adapter around,
        # and one model per meter (D7) is what makes it possible. A corpus that faulted only the
        # water meter would leave the second half of that claim unmeasured.
        assert set(self._recall_by_case()) == {"spike-04", "drift-11", "stuck-04", "clean"}
        assert self._recall_by_case()["clean"] is None  # a rate is undefined with no incident
