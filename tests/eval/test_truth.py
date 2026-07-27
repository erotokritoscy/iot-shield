"""Ground truth: collapsing a labelled row timeline into incidents, per asset.

The answer key everything else is measured against, so its edges are pinned hard — a wrong
incident boundary would silently corrupt every event-level metric downstream, and a wrong
*asset* would silently credit the wrong device (ADR-0026).
"""

from iotshield.contracts import Lane
from iotshield.eval import AssetTruth, GroundTruth, Incident
from tests.eval.conftest import ASSET, at, fleet_truth, rows, truth_of


def incident(start: int, end: int, asset_id: str = ASSET) -> Incident:
    return Incident(asset_id=asset_id, start=at(start), end=at(end))


class TestCollapse:
    def test_consecutive_anomalous_rows_are_one_incident(self) -> None:
        truth = truth_of([False, True, True, True, False])
        assert truth.incidents == (incident(1, 3),)

    def test_a_quiet_gap_separates_incidents(self) -> None:
        truth = truth_of([True, True, False, False, True])
        assert truth.incidents == (incident(0, 1), incident(4, 4))

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
        assert truth.anomalous_rows == 0

    def test_rows_are_sorted_before_collapsing(self) -> None:
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

    def test_anomalous_rows_sums_across_the_fleet(self) -> None:
        truth = fleet_truth(a=[True, True, False], b=[False, True, False])
        assert truth.anomalous_rows == 3


class TestStorageForm:
    """Intervals plus a clock, which is what a bundle stores (ADR-0021 D5)."""

    def test_intervals_and_a_clock_rebuild_the_row_flags(self) -> None:
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
        # kind/fault_id/lanes are what a generated corpus knows and a converted one does not;
        # they must ride along untouched rather than being recomputed from the flags.
        rich = Incident(
            asset_id=ASSET,
            start=at(1),
            end=at(2),
            kind="compromise",
            fault_id=f"{ASSET}:compromise:1",
            lanes=frozenset({Lane.FLOW, Lane.LOG}),
        )
        asset = AssetTruth.from_incidents(ASSET, {Lane.FLOW: [at(s) for s in range(4)]}, [rich])
        assert asset.incidents == (rich,)


def test_a_converted_corpus_declares_no_lanes_rather_than_guessing() -> None:
    """SKAB's `anomaly` column says a fault happened, not which modality showed it.

    So `lanes` is empty for a converted corpus, and per-lane recall is then *undefined*
    rather than zero — "the flow lane found nothing" and "nobody claimed it should find
    anything" must not read alike.
    """
    truth = GroundTruth.from_timeline(rows([False, True, True]), asset_id="skab-testbed")
    assert all(found.lanes == frozenset() for found in truth.incidents)
