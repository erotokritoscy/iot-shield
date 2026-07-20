"""Shared detector skeleton: "too much of something, too fast", implemented once.

A whole family of detectors asks one question — *per asset, over a sliding time window,
does some aggregate over the events exceed a threshold?* Only the aggregate varies:

- count of matching log lines           -> a burst (``log_burst``, ADR-0020)
- distinct peers contacted              -> fan-out / worm propagation (ADR-0024)
- distinct peer ports                   -> port scanning
- bytes sent                            -> exfiltration

Everything else — walking the window, deciding when a qualifying window starts a *new*
episode rather than extending the current one, and tracking the peak — is identical, and
is precisely the part that is easy to get subtly wrong. So it lives here, and an adapter
supplies ~15 lines of ``Aggregate``.

Why the merge is load-bearing (ADR-0020 D4, measured)
-----------------------------------------------------
Qualifying windows overlap heavily: 90 attempts inside one window qualify at almost every
position, describing *one* incident. Emitting a detection per position would restate a
single finding many times — and the alert engine's ``volume`` factor counts detections in
a bucket as *independent corroboration*, so the same evidence would arrive twice, once
through ``score`` and again through the count, measurably inflating priority. Merging is
what makes one incident one judgment, which is the precondition ``volume`` depends on.

This is the third member of the detect-side skeleton, alongside ``_group.py`` (the port's
routing laws) and ``_runs.py`` (row-mask -> spans). ``_runs.merge_runs`` is its sibling,
not its rival: that one collapses an already-computed per-row verdict, this one computes a
verdict over a *time* window where row adjacency is not the relevant unit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, Protocol, TypeVar

# Classic TypeVar rather than PEP 695 syntax: the project floor is Python 3.11 and type
# parameter lists arrived in 3.12. Contravariant because an aggregate only ever *consumes*
# items, so an Aggregate[NormalizedEvent] is usable wherever a narrower one is asked for.
T = TypeVar("T")
T_contra = TypeVar("T_contra", contravariant=True)


class Aggregate(Protocol[T_contra]):
    """An incrementally maintained quantity over the events currently in the window.

    Incremental (``add``/``remove``) rather than recomputed per position so a window walk
    stays linear: recomputing "distinct peers" from scratch at every position would make a
    long capture quadratic.

    Implementations must satisfy: after the same multiset of ``add``/``remove`` calls, they
    report the same value — the Detector port's determinism law rests on it.
    """

    def add(self, item: T_contra) -> None: ...
    def remove(self, item: T_contra) -> None: ...
    def value(self) -> float: ...


@dataclass
class Episode:
    """One merged run of overlapping qualifying windows: one incident, one judgment."""

    start: int
    end: int
    peak: float


class CountAggregate(Generic[T]):
    """How many events are in the window. The volumetric baseline (``log_burst``)."""

    def __init__(self) -> None:
        self._count = 0

    def add(self, item: T) -> None:
        self._count += 1

    def remove(self, item: T) -> None:
        self._count -= 1

    def value(self) -> float:
        return float(self._count)


class DistinctAggregate(Generic[T]):
    """How many *distinct* keys are in the window — cardinality, not volume.

    The distinction is the whole signal for scanning: one host contacted a thousand times
    is a busy client, a thousand hosts contacted once each is a scan. A ``Counter``
    (multiset) rather than a set, because the same key may legitimately appear several
    times in the window and must only leave the tally when its last occurrence slides out.

    Keys that are ``None`` are not counted: a portless protocol has no port to be distinct.
    """

    def __init__(self, key: Callable[[T], Hashable | None]) -> None:
        self._key = key
        self._counts: Counter[Hashable] = Counter()

    def add(self, item: T) -> None:
        key = self._key(item)
        if key is not None:
            self._counts[key] += 1

    def remove(self, item: T) -> None:
        key = self._key(item)
        if key is None:
            return
        # One lookup, one write: the obvious `-= 1` then re-read costs three dict
        # operations on a path that runs once per event leaving the window.
        remaining = self._counts[key] - 1
        if remaining > 0:
            self._counts[key] = remaining
        else:
            del self._counts[key]

    def value(self) -> float:
        return float(len(self._counts))


class SumAggregate(Generic[T]):
    """The running total of a per-event quantity (bytes, for exfiltration).

    Kept as an integer internally: byte counts are integers, and a float running sum would
    accumulate drift over a long capture and make the port's determinism law depend on the
    order events happened to arrive in.
    """

    def __init__(self, quantity: Callable[[T], int]) -> None:
        self._quantity = quantity
        self._total = 0

    def add(self, item: T) -> None:
        self._total += self._quantity(item)

    def remove(self, item: T) -> None:
        self._total -= self._quantity(item)

    def value(self) -> float:
        return float(self._total)


def find_episodes(
    items: Sequence[T],
    *,
    timestamp: Callable[[T], datetime],
    window: timedelta,
    threshold: float,
    aggregate: Callable[[], Aggregate[T]],
) -> list[Episode]:
    """Merge every window whose aggregate exceeds ``threshold`` into maximal episodes.

    ``items`` must be time-ordered. Two pointers walk them: ``left`` trails ``right`` by at
    most ``window``, so the aggregate always describes the widest window ending at
    ``right``. A qualifying window that *overlaps* the previous episode is the same
    incident and extends it; one whose left edge has passed the previous episode's end
    means a real quiet gap, and opens a new one.

    The comparison is a strict ``>``, matching the Detector port's law that an emitted
    detection has ``score > threshold``: what a caller passes here is the *exceedance
    boundary* — the largest value still considered normal — so the number an analyst reads
    on the detection is the same number that decided it.

    ``aggregate`` is a factory rather than an instance so one call cannot inherit residue
    from another; the returned episodes index into ``items``.
    """
    episodes: list[Episode] = []
    if not items:
        return episodes

    tally = aggregate()
    left = 0
    previous_ts: datetime | None = None
    for right in range(len(items)):
        tally.add(items[right])
        # Hoisted: the right edge is invariant across the trailing-edge advance, and this
        # is the innermost loop of every volumetric detector.
        right_ts = timestamp(items[right])
        # Unordered input does not degrade this walk, it inverts it: a backwards timestamp
        # makes the trailing edge stop advancing, so the window grows without bound and
        # every position qualifies — a detector that fires on everything. That is far worse
        # than a crash, so refuse instead. One comparison per item against a value already
        # in hand; `group_by_asset` guarantees the order, and this catches the caller that
        # bypasses it.
        if previous_ts is not None and right_ts < previous_ts:
            raise ValueError(
                f"find_episodes requires time-ordered items; item {right} goes backwards "
                f"({right_ts.isoformat()} after {previous_ts.isoformat()}). "
                f"Group events with detect._group.group_by_asset, which orders them."
            )
        previous_ts = right_ts
        while right_ts - timestamp(items[left]) > window:
            tally.remove(items[left])
            left += 1

        current = tally.value()
        if current <= threshold:
            continue
        if episodes and left <= episodes[-1].end:
            # `right` only grows, so end stays the episode's last qualifying position.
            episodes[-1].end = right
            episodes[-1].peak = max(episodes[-1].peak, current)
        else:
            episodes.append(Episode(start=left, end=right, peak=current))
    return episodes
