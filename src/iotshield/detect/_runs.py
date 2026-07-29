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

from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from iotshield.contracts import EventKind

T = TypeVar("T")

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


SAMPLE_MESSAGES = 3
"""How many of an episode's own log lines ride along as evidence.

A cap rather than the whole span: the point is to let an analyst recognise *what kind* of
thing this was without carrying an unbounded slice of the input into the database. Shared
because "how many samples" is a presentation convention across detectors, not a property of
any one of them — the same argument that moved ``MIN_COLLECTIVE`` here."""


def sample_messages(events: Sequence[Any]) -> list[str]:
    """The first few non-empty messages of an episode, for ``evidence``."""
    return [event.message for event in events[:SAMPLE_MESSAGES] if event.message]


def group_by_gap(
    items: Sequence[T], *, gap: timedelta, timestamp: Callable[[T], datetime]
) -> list[Sequence[T]]:
    """Split time-ordered ``items`` wherever more than ``gap`` separates two neighbours.

    The third way this project forms episodes, and the one neither sibling covers.
    ``merge_runs`` collapses an already-computed per-row verdict by *index* adjacency, and
    ``_windows.find_episodes`` computes a verdict by thresholding an aggregate over a
    sliding window. This one starts from items that are all already interesting and asks
    only "did these happen at about the same time" — no mask, no threshold, no aggregate.

    Index adjacency is the wrong unit for that and the difference is not academic: a
    device's flows interleave (mqtt, smb, ssl, smb), so matches of one rule are almost never
    neighbours in the sequence, and an adjacency merge would emit one episode per match —
    the restatement that inflates an alert's ``volume`` factor (ADR-0020 D4).

    Empty in, empty out; a single item is a one-item group.
    """
    if not items:
        return []
    groups: list[Sequence[T]] = []
    start = 0
    for index in range(1, len(items)):
        if timestamp(items[index]) - timestamp(items[index - 1]) > gap:
            groups.append(items[start:index])
            start = index
    groups.append(items[start:])
    return groups


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
