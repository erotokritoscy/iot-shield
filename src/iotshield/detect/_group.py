"""Shared detector skeleton: the port's routing laws, implemented once.

The detect-side analog of ``normalize._linewise`` and ``alert._correlate``. Before it can
judge anything, every Detector adapter owes the port the same two behaviours: ignore events
from a foreign lane (the port's defensive law) and fan out per asset in a deterministic
order (the port's law 4). Implemented here once, so a new adapter inherits both instead of
re-deriving them, and so a change to the routing laws is one edit rather than one per
adapter.

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
    """Yield ``(asset_id, events)`` per asset: foreign lanes dropped, assets in sorted order.

    Sorting the assets is what makes a detector's output deterministic no matter what order
    the events arrived in — the port's law 4, which the suite pins for every adapter.
    """
    by_asset: dict[str, list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        if event.source_type is not lane or not keep(event):
            continue
        by_asset[event.asset_id].append(event)
    for asset_id in sorted(by_asset):
        yield asset_id, by_asset[asset_id]
