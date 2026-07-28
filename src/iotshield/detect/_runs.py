"""Shared detector skeleton: turning a per-row verdict into spans, once.

A large family of detectors — anything that scores rows and thresholds them, which is
what a classical ML model's ``predict``/``decision_function`` gives you — faces the same
last step: the Detector port's output is a *window*, not a row label, so consecutive
flagged rows have to become spans before they can be reported.

``merge_runs`` is that step. It is deliberately tiny and dependency-free (it iterates any
sequence, including a numpy boolean array) so a pure-Python adapter is never forced to take
numpy just to reach it. It sits beside ``_group.py`` as the other half of the detect-side
skeleton: ``group_by_asset`` handles the port's routing laws, this handles the port's
judgment-formation guidance (see ``port.py``).

``truth`` in the evaluation harness collapses a labelled row timeline into incidents with
this same function, so ground truth and detections are formed by an identical rule — which
is what makes the event-level comparison apples-to-apples (ADR-0021).
"""

from collections.abc import Iterable, Iterator
from typing import Any

from iotshield.contracts import EventKind

MIN_COLLECTIVE = 3
"""Run length at/above which an episode is collective rather than contextual.

Vendored from team3's ``evaluation/postprocess.py:classify_event_type`` (ADR-0012) and
promoted here when the meter detector became the second row-scoring adapter to need it.
It lives beside ``merge_runs`` because it is the other half of the same step: that turns a
verdict mask into spans, this says what a span's *length* means.
"""


def classify_run(run_length: int) -> EventKind:
    """The shape of an episode, from how many consecutive rows it covers.

    One definition, so two detectors cannot label the same episode differently — which is
    exactly what a copy in each adapter would eventually do.
    """
    if run_length <= 1:
        return EventKind.POINT
    if run_length >= MIN_COLLECTIVE:
        return EventKind.COLLECTIVE
    return EventKind.CONTEXTUAL


def merge_runs(mask: Iterable[Any]) -> Iterator[tuple[int, int]]:
    """Yield the inclusive ``(start, end)`` index range of each maximal run of truthy values.

    ``[F, T, T, F, T]`` yields ``(1, 2)`` then ``(4, 4)`` — a single flagged row is a
    zero-length span, which the contract models as a point anomaly (ADR-0003).
    """
    start: int | None = None
    index = -1
    for index, flagged in enumerate(mask):
        if flagged and start is None:
            start = index
        elif not flagged and start is not None:
            yield start, index - 1
            start = None
    if start is not None:
        yield start, index
