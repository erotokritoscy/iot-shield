"""Shared detector skeleton: the port's routing laws, implemented once.

The detect-side analog of ``normalize._linewise`` and ``alert._correlate``. Before it can
judge anything, every Detector adapter owes the port the same three behaviours: ignore
events from a foreign lane (the port's defensive law), fan out per asset in a deterministic
order, and hand each asset's events over in time order (together, the port's law 4).
Implemented here once, so a new adapter inherits all three instead of re-deriving them, and
so a change to the routing laws is one edit rather than one per adapter.

Adapters differ only in which of *their own* lane's events are judgeable at all — team3
needs a metric and a numeric reading; log_burst needs its preset to match — so that
predicate is the single parameter.
"""

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence

from iotshield.contracts import Lane, NormalizedEvent


def group_by_asset(
    events: Sequence[NormalizedEvent],
    *,
    lane: Lane,
    keep: Callable[[NormalizedEvent], bool],
) -> Iterator[tuple[str, list[NormalizedEvent]]]:
    """Yield ``(asset_id, events)`` per asset: foreign lanes dropped, assets sorted, events
    within each asset in time order.

    Sorting the assets is what makes a detector's output deterministic no matter what order
    the events arrived in — the port's law 4, which the suite pins for every adapter.

    Sorting each asset's events by timestamp is the other half of that law, and it lives
    here for a sharper reason than tidiness: a windowed detector fed unordered events does
    not fail, it *lies*. ``_windows.find_episodes`` walks a trailing edge that never
    advances when a timestamp goes backwards, so the window grows without bound and every
    position qualifies — a detector that fires on everything while passing the port suite.
    Every adapter used to carry its own defensive sort; the one that forgot would have
    shipped that. A law whose omission is silent belongs in the skeleton, not in a comment.

    Timsort makes this ~O(n) on the already-ordered input the runner guarantees (ADR-0019),
    so the adapters that previously sorted for themselves pay nothing for the move.
    """
    by_asset: dict[str, list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        if event.source_type is not lane or not keep(event):
            continue
        by_asset[event.asset_id].append(event)
    for asset_id in sorted(by_asset):
        yield asset_id, sorted(by_asset[asset_id], key=lambda event: event.timestamp)
