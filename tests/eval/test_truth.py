"""Ground truth: collapsing a labelled instant timeline into incidents, per asset and lane.

The answer key everything else is measured against, so its edges are pinned hard — a wrong
incident boundary would silently corrupt every event-level metric downstream, and a wrong
*asset* or *lane* would silently credit the wrong device or modality (ADR-0026, ADR-0028 D2).
"""

from iotshield.contracts import Lane
from iotshield.eval import AssetTruth, GroundTruth, Incident
from tests.eval.conftest import ASSET, at, fleet_truth, rows, truth_of


def incident(
    start: int,
    end: int,
    asset_id: str = ASSET,
    lane: Lane = Lane.TELEMETRY,
    fault_id: str = "",
) -> Incident:
    """One projection. ``fault_id`` defaults to what ``from_timeline`` synthesizes for run 0.

    Spelled out rather than left empty because it is no longer decorative: the alert level
    divides by *distinct faults*, so an id is part of an incident's identity (ADR-0028 D2).
    """
    return Incident.over(
        at(start),
        at(end),
        asset_id=asset_id,
        lane=lane,
        fault_id=fault_id or f"{asset_id}:run:0",
    )


class TestCollapse:
    def test_consecutive_anomalous_rows_are_one_incident(self) -> None:
        truth = truth_of([False, True, True, True, False])
        assert truth.incidents == (incident(1, 3),)

    def test_a_quiet_gap_separates_incidents(self) -> None:
        truth = truth_of([True, True, False, False, True])
        # Distinct synthesized fault ids, because two runs are two causes as far as a converted
        # corpus can tell — and the alert level divides by causes (ADR-0028 D2).
        assert truth.incidents == (
            incident(0, 1, fault_id=f"{ASSET}:run:0"),
            incident(4, 4, fault_id=f"{ASSET}:run:1"),
        )

    def test_a_single_row_is_a_zero_length_incident(self) -> None:
        # Mirrors the contract: a point anomaly is a zero-length window (ADR-0003).
        (found,) = truth_of([False, True, False]).incidents
        assert found.start == found.end == at(1)

    def test_a_fault_running_to_the_end_is_closed(self) -> None:
        (found,) = truth_of([False, True, True]).incidents
        assert found.end == at(2)

    def test_clean_recording_has_no_incidents(self) -> None:
        truth = truth_of([False, False, False])
        assert truth.incidents == ()
        assert truth.is_clean
        assert truth.anomalous_instants == 0

    def test_instants_are_sorted_before_collapsing(self) -> None:
        # A shuffled corpus file would otherwise produce silently wrong boundaries, and a
        # wrong answer key is worse than a loud failure.
        shuffled = [(at(2), True), (at(0), False), (at(1), True)]
        truth = GroundTruth.from_timeline(shuffled, asset_id=ASSET)
        assert truth.incidents == (incident(1, 2),)

    def test_every_collapsed_incident_carries_its_asset(self) -> None:
        # The collapse is where an incident first exists, so it is where identity must be
        # attached; an incident that forgot whose it was could never be matched correctly.
        truth = truth_of([False, True, True], asset_id="gw-01")
        assert {found.asset_id for found in truth.incidents} == {"gw-01"}


class TestOverlap:
    def test_touching_at_a_boundary_counts(self) -> None:
        # Closed on both ends: a zero-length detection exactly at the incident's edge is a
        # hit. A half-open test would silently never match a point anomaly.
        found = incident(10, 20)
        assert found.overlaps(at(20), at(20))
        assert found.overlaps(at(10), at(10))

    def test_disjoint_windows_do_not_overlap(self) -> None:
        found = incident(10, 20)
        assert not found.overlaps(at(21), at(30))
        assert not found.overlaps(at(0), at(9))

    def test_a_window_enclosing_the_incident_overlaps(self) -> None:
        assert incident(10, 20).overlaps(at(0), at(99))

    def test_overlap_is_deliberately_blind_to_the_asset(self) -> None:
        """The predicate answers "in time?" only; the caller compares identities.

        Load-bearing rather than an oversight: scoring needs both "did the right device fire
        here?" and "did *anything* fire here?" — the second is what lets a misattribution be
        reported as such instead of merely counted wrong.
        """
        assert incident(10, 20, asset_id="cam-07").overlaps(at(15), at(15))


class TestRoster:
    """Who the answer key claims to know about — and the three states that follow."""

    def test_a_listed_asset_with_incidents_is_faulty(self) -> None:
        truth = fleet_truth(scanner=[False, True, True], gateway=[False, False, False])
        assert "scanner" in truth.labelled_assets
        scanner = truth.of("scanner")
        assert scanner is not None and scanner.incidents

    def test_a_listed_asset_with_no_incidents_is_asserted_clean(self) -> None:
        # Not "no data": the corpus is stating this device was fine throughout, which is what
        # makes a detection on it a false alarm rather than an unknown.
        truth = fleet_truth(scanner=[False, True, True], gateway=[False, False, False])
        assert truth.clean_assets == frozenset({"gateway"})

    def test_an_unlisted_asset_is_not_in_the_roster(self) -> None:
        truth = fleet_truth(scanner=[False, True, True])
        assert "stranger" not in truth.labelled_assets
        assert truth.of("stranger") is None

    def test_the_corpus_is_clean_only_when_no_asset_has_a_fault(self) -> None:
        assert fleet_truth(a=[False, False], b=[False, False]).is_clean
        assert not fleet_truth(a=[False, False], b=[False, True]).is_clean

    def test_assets_are_sorted_so_labels_are_deterministic(self) -> None:
        # A frozenset or dict iteration order would make a regenerated bundle churn.
        truth = fleet_truth(zulu=[False], alpha=[False], mike=[False])
        assert [asset.asset_id for asset in truth.assets] == ["alpha", "mike", "zulu"]

    def test_incidents_flatten_in_roster_order(self) -> None:
        truth = fleet_truth(bravo=[True, False], alpha=[False, True])
        assert [found.asset_id for found in truth.incidents] == ["alpha", "bravo"]

    def test_anomalous_instants_sum_across_the_fleet(self) -> None:
        truth = fleet_truth(a=[True, True, False], b=[False, True, False])
        assert truth.anomalous_instants == 3


class TestStorageForm:
    """Intervals plus a clock, which is what a bundle stores (ADR-0021 D5)."""

    def test_intervals_and_a_clock_rebuild_the_instant_flags(self) -> None:
        clock = [at(second) for second in range(5)]
        asset = AssetTruth.from_incidents(ASSET, {Lane.TELEMETRY: clock}, [incident(1, 2)])
        flags = [flagged for _, flagged in asset.timelines[Lane.TELEMETRY]]
        assert flags == [False, True, True, False, False]

    def test_two_touching_incidents_stay_two(self) -> None:
        """Strictly more expressive than per-row flags, which is why intervals are stored.

        Rows 1..4 anomalous with no quiet row between them is ONE run of flags but TWO
        incidents — a distinction a synthetic corpus can legitimately produce and per-row
        labels cannot represent.
        """
        clock = [at(second) for second in range(6)]
        asset = AssetTruth.from_incidents(
            ASSET, {Lane.TELEMETRY: clock}, [incident(1, 2), incident(3, 4)]
        )
        assert len(asset.incidents) == 2
        flags = [flagged for _, flagged in asset.timelines[Lane.TELEMETRY]]
        assert flags == [False, True, True, True, True, False]

    def test_a_clock_with_no_incidents_is_a_clean_asset(self) -> None:
        asset = AssetTruth.from_incidents(ASSET, {Lane.TELEMETRY: [at(0), at(1)]}, [])
        assert asset.is_clean
        assert asset.timelines[Lane.TELEMETRY] == ((at(0), False), (at(1), False))

    def test_descriptive_fields_survive_a_round_trip_in_memory(self) -> None:
        # kind/fault_id are what a generated corpus knows and a converted one does not; they
        # must ride along untouched rather than being recomputed from the flags.
        rich = Incident.over(
            at(1),
            at(2),
            asset_id=ASSET,
            lane=Lane.FLOW,
            kind="compromise",
            fault_id=f"{ASSET}:compromise:1",
        )
        asset = AssetTruth.from_incidents(ASSET, {Lane.FLOW: [at(s) for s in range(4)]}, [rich])
        assert asset.incidents == (rich,)

    def test_an_incident_only_flags_its_own_lanes_clock(self) -> None:
        """The whole point of a lane axis (ADR-0028 D2).

        A flow fault must leave the telemetry clock alone. Under the set-valued predecessor an
        incident with an empty lane set flagged *every* lane, and that fallback is the reason one
        field had to be read two contradictory ways.
        """
        clock = [at(second) for second in range(4)]
        asset = AssetTruth.from_incidents(
            ASSET,
            {Lane.FLOW: clock, Lane.TELEMETRY: clock},
            [incident(1, 2, lane=Lane.FLOW)],
        )
        assert [f for _, f in asset.timelines[Lane.FLOW]] == [False, True, True, False]
        assert [f for _, f in asset.timelines[Lane.TELEMETRY]] == [False] * 4

    def test_incidents_on_selects_one_lanes_projections(self) -> None:
        clock = [at(second) for second in range(4)]
        asset = AssetTruth.from_incidents(
            ASSET,
            {Lane.FLOW: clock, Lane.LOG: clock},
            [incident(1, 2, lane=Lane.FLOW), incident(0, 3, lane=Lane.LOG)],
        )
        assert [i.lane for i in asset.incidents_on(Lane.FLOW)] == [Lane.FLOW]
        assert [i.lane for i in asset.incidents_on(Lane.LOG)] == [Lane.LOG]
        assert asset.incidents_on(Lane.TELEMETRY) == ()

    def test_projections_of_one_cause_are_one_fault(self) -> None:
        """The alert level's denominator, and why ``fault_id`` is load-bearing now.

        Three lanes seeing one compromise is three incidents and ONE fault; dividing alerts by
        projections would report a correctly corroborated alert as one-third coverage.
        """
        clock = [at(second) for second in range(4)]
        shared = "cam:compromise:1"
        asset = AssetTruth.from_incidents(
            ASSET,
            {lane: clock for lane in Lane},
            [incident(1, 2, lane=lane, fault_id=shared) for lane in Lane],
        )
        truth = GroundTruth.from_assets([asset])
        assert len(truth.incidents) == len(list(Lane))
        assert truth.faults == {(ASSET, shared)}


def test_a_converted_corpus_declares_the_one_lane_it_covers() -> None:
    """SKAB's `anomaly` column says a fault happened, on a telemetry dataset.

    So its incidents name TELEMETRY, which is true, and per-lane recall reports that one lane
    rather than the empty dict the set-valued shape forced. "The flow lane found nothing" and
    "nobody claimed it should find anything" still do not read alike — flow simply never
    appears, because no incident names it (ADR-0028 D2).
    """
    truth = GroundTruth.from_timeline(rows([False, True, True]), asset_id="skab-testbed")
    assert [found.lane for found in truth.incidents] == [Lane.TELEMETRY]


def test_a_converted_corpus_synthesizes_a_fault_id_per_run() -> None:
    # Otherwise every run in a recording collapses into one "fault" and the alert level divides
    # by one however many separate faults the recording contained.
    truth = GroundTruth.from_timeline(
        rows([True, False, True, False, True]), asset_id="skab-testbed"
    )
    assert len(truth.faults) == 3
