"""The shared span helper: turning a per-row verdict into inclusive index ranges.

Small, but load-bearing in two places — every row-scoring detector forms its judgments with
it, and the evaluation harness collapses a labelled timeline into incidents with it. Ground
truth and detections being built by the *same* rule is what makes event-level scoring
apples-to-apples (ADR-0021), so the edge cases are pinned here once.
"""

import numpy as np

from iotshield.detect import merge_runs


def runs(mask: list[bool]) -> list[tuple[int, int]]:
    return list(merge_runs(mask))


class TestSpans:
    def test_maximal_true_ranges(self) -> None:
        assert runs([False, True, True, False, True, False]) == [(1, 2), (4, 4)]

    def test_single_flag_is_a_zero_length_span(self) -> None:
        # The contract models a point anomaly as a zero-length window (ADR-0003).
        assert runs([False, True, False]) == [(1, 1)]

    def test_run_reaching_the_end_is_closed(self) -> None:
        # The classic off-by-one: a mask that never returns to False still terminates.
        assert runs([False, True, True]) == [(1, 2)]

    def test_all_true_is_one_span(self) -> None:
        assert runs([True, True, True]) == [(0, 2)]

    def test_all_false_and_empty_yield_nothing(self) -> None:
        assert runs([False, False]) == []
        assert runs([]) == []

    def test_adjacent_runs_separated_by_one_gap(self) -> None:
        assert runs([True, False, True]) == [(0, 0), (2, 2)]


class TestInputTypes:
    def test_accepts_a_numpy_boolean_array(self) -> None:
        # team3 passes a numpy mask; the helper stays dependency-free so a pure-Python
        # adapter is never forced to take numpy just to reach it.
        assert list(merge_runs(np.array([False, True, True, False]))) == [(1, 2)]

    def test_is_lazy(self) -> None:
        # A generator, so a long mask does not materialize every span before the first.
        assert next(iter(merge_runs([False, True, True]))) == (1, 2)
