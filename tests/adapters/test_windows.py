"""The shared sliding-window skeleton: aggregates, and how windows become episodes.

Tested directly rather than only through its adapters, because the merge rule is the part
ADR-0020 D4 showed is load-bearing for alert priority — an off-by-one here would inflate
every alert the volumetric detectors produce, quietly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from iotshield.detect._windows import (
    Aggregate,
    CountAggregate,
    DistinctAggregate,
    Episode,
    SumAggregate,
    find_episodes,
)

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Ping:
    """A minimal stand-in: the skeleton is generic over what it walks."""

    at: int  # seconds after T0
    peer: str = "a"
    port: int | None = 80
    size: int = 10

    @property
    def when(self) -> datetime:
        return T0 + timedelta(seconds=self.at)


def by_peer() -> DistinctAggregate[Ping]:
    return DistinctAggregate(lambda ping: ping.peer)


def by_port() -> DistinctAggregate[Ping]:
    return DistinctAggregate(lambda ping: ping.port)


def by_size() -> SumAggregate[Ping]:
    return SumAggregate(lambda ping: ping.size)


def episodes(
    items: list[Ping],
    *,
    window_s: int = 60,
    threshold: float = 2,
    aggregate: Callable[[], Aggregate[Ping]] = CountAggregate,
) -> list[Episode]:
    return find_episodes(
        items,
        timestamp=lambda p: p.when,
        window=timedelta(seconds=window_s),
        threshold=threshold,
        aggregate=aggregate,
    )


class TestThresholdSemantics:
    def test_the_comparison_is_strictly_greater_than(self) -> None:
        # threshold is the exceedance boundary: exactly at it is still normal, matching the
        # Detector port's law that an emitted detection has score > threshold.
        at_boundary = [Ping(0), Ping(1)]
        assert episodes(at_boundary, threshold=2) == []
        assert episodes([*at_boundary, Ping(2)], threshold=2) != []

    def test_empty_input_yields_no_episodes(self) -> None:
        assert episodes([]) == []


class TestEpisodeFormation:
    def test_one_incident_becomes_one_episode_not_one_per_position(self) -> None:
        """The merge that keeps a 90-event burst a single judgment (ADR-0020 D4)."""
        burst = [Ping(second) for second in range(20)]
        found = episodes(burst, threshold=2)
        assert len(found) == 1
        assert (found[0].start, found[0].end) == (0, 19)

    def test_a_real_quiet_gap_opens_a_new_episode(self) -> None:
        # Two clusters an hour apart are two incidents, not one long one.
        first = [Ping(second) for second in range(5)]
        second = [Ping(3600 + offset) for offset in range(5)]
        found = episodes(first + second, threshold=2)
        assert len(found) == 2
        assert found[0].end < found[1].start

    def test_peak_is_the_highest_value_reached_anywhere_in_the_episode(self) -> None:
        # 5 events inside the window, then a straggler: the peak must remember the 5.
        found = episodes([Ping(0), Ping(1), Ping(2), Ping(3), Ping(4), Ping(50)], threshold=2)
        assert found[0].peak == 6.0

    def test_events_outside_the_window_leave_the_tally(self) -> None:
        # Spread wider than the window: never more than 2 at once, so nothing fires.
        spread = [Ping(second * 40) for second in range(10)]
        assert episodes(spread, window_s=60, threshold=2) == []


class TestAggregates:
    def test_count_measures_volume(self) -> None:
        assert episodes([Ping(0), Ping(1), Ping(2)], threshold=2)[0].peak == 3.0

    def test_distinct_separates_a_scan_from_a_busy_client(self) -> None:
        """The whole signal for scanning: 100 calls to one host is a chatty client; one
        call each to 100 hosts is reconnaissance. Volume cannot tell them apart."""
        chatty = [Ping(second, peer="a") for second in range(10)]
        scanning = [Ping(second, peer=f"h{second}") for second in range(10)]
        assert episodes(chatty, threshold=3, aggregate=by_peer) == []
        assert episodes(scanning, threshold=3, aggregate=by_peer)[0].peak == 10.0

    def test_distinct_only_drops_a_key_when_its_last_occurrence_slides_out(self) -> None:
        # A repeated key must not be removed from the tally while another copy is still in
        # the window — the reason the aggregate is a multiset rather than a set.
        items = [Ping(0, peer="a"), Ping(1, peer="a"), Ping(2, peer="b"), Ping(3, peer="c")]
        found = episodes(items, threshold=2, aggregate=by_peer)
        assert found[0].peak == 3.0  # a, b, c — not 4, and not 2

    def test_distinct_ignores_none_keys(self) -> None:
        # A portless protocol has no port to be distinct.
        items = [Ping(s, port=None) for s in range(10)]
        assert episodes(items, threshold=1, aggregate=by_port) == []

    def test_sum_totals_a_quantity(self) -> None:
        items = [Ping(0, size=100), Ping(1, size=250)]
        found = episodes(items, threshold=300, aggregate=by_size)
        assert found[0].peak == 350.0

    def test_sum_subtracts_what_leaves_the_window(self) -> None:
        # Two big transfers an hour apart never coexist, so neither clears the threshold.
        items = [Ping(0, size=1000), Ping(3600, size=1000)]
        assert episodes(items, threshold=1500, aggregate=by_size) == []


class TestDeterminism:
    def test_a_fresh_aggregate_is_used_per_call(self) -> None:
        # The factory exists so one call cannot inherit residue from another; if it did,
        # the second call here would start with the first call's totals still in it.
        items = [Ping(0), Ping(1), Ping(2)]
        first = episodes(items, threshold=2)
        second = episodes(items, threshold=2)
        assert [(e.start, e.end, e.peak) for e in first] == [
            (e.start, e.end, e.peak) for e in second
        ]


class TestOrderIsRequired:
    """Unordered input does not degrade the walk, it inverts it — so it must not be silent.

    A backwards timestamp stops the trailing edge advancing, the window grows without bound,
    and every position qualifies: a detector that fires on everything while passing the port
    suite. `group_by_asset` guarantees the order; this is the guard for a caller that
    bypasses it.
    """

    def test_backwards_timestamps_raise_rather_than_over_fire(self) -> None:
        import pytest

        backwards = [Ping(100), Ping(0), Ping(1), Ping(2)]
        with pytest.raises(ValueError, match="time-ordered"):
            episodes(backwards, threshold=2)

    def test_equal_timestamps_are_not_treated_as_backwards(self) -> None:
        assert episodes([Ping(0), Ping(0), Ping(0)], threshold=2)
