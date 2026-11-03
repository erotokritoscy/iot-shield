"""A run's events, partitioned by asset so they can be judged a batch at a time (ADR-0047).

Why by asset, and why that is safe: every detector fans out per asset (``detect/_group.py``
is the port's routing law, and the learning adapters keep one model per asset), and every
alert bucket holds one asset (``alert/_correlate.py``). So judging assets A and B together or
apart produces the same detections and the same alerts — the only thing a batch must never do
is split *one* asset's events, which would cut a window, a burst or a baseline in two.

Why not by connector device: the key that matters is the **normalized** ``asset_id``, which is
not always what a source is organised by. A flow's asset is its *oriented* endpoint, which can
be the destination, so splitting a Biology pull by ``src_ip`` would split an asset. Bucketing
after normalization is correct for every source, uploaded or pulled, whatever its shape.

Each event goes to bucket ``crc32(asset_id) % BUCKETS`` — a stable hash, so a run is
reproducible — and stays in memory until more than ``spill_after`` are held, at which point
every bucket is appended to its own file. A run small enough never touches the disk and is
judged as exactly one batch, in the order its events were read — which is the old in-memory
run, row for row.
"""

import shutil
import tempfile
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Final

from iotshield.contracts import NormalizedEvent

BUCKETS: Final = 128
"""How finely a run is split. A batch is one or more whole buckets, so the largest batch is
about ``max(BATCH_EVENTS, events / BUCKETS)`` — and never smaller than the busiest asset,
whose history a detector must see whole."""

BATCH_EVENTS: Final = 50_000
"""About how many events are judged at once — the run's memory, not a limit on its size."""


def _bucket(asset_id: str) -> int:
    return zlib.crc32(asset_id.encode("utf-8")) % BUCKETS


class AssetPartition:
    """Events grouped into asset buckets: in memory while small, spilled to disk when not.

    Within a bucket, events keep the order they were added in, so one asset's events stay in
    ingest order across sources — which is what makes the detectors' stable sort by
    ``(asset_id, timestamp)`` give the same order batched as unbatched.
    """

    def __init__(self, *, spill_after: int = BATCH_EVENTS) -> None:
        self._spill_after = spill_after
        self._held: list[list[NormalizedEvent]] = [[] for _ in range(BUCKETS)]
        self._counts = [0] * BUCKETS
        self._holding = 0
        self._dir: Path | None = None
        # Every event in arrival order, until the first spill: a run that never spills is one
        # batch in the order it was read, exactly as before batching existed. References only,
        # and dropped at the spill, so it never costs more than the batch it describes.
        self._arrival: list[NormalizedEvent] | None = []

    @property
    def spilled(self) -> bool:
        return self._dir is not None

    def __len__(self) -> int:
        return sum(self._counts)

    def add(self, events: Iterable[NormalizedEvent]) -> None:
        for event in events:
            if self._arrival is not None:
                self._arrival.append(event)
            bucket = _bucket(event.asset_id)
            self._held[bucket].append(event)
            self._counts[bucket] += 1
            self._holding += 1
        if self._holding > self._spill_after:
            self._spill()

    def _spill(self) -> None:
        self._arrival = None
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="iotshield-run-"))
        for bucket, held in enumerate(self._held):
            if not held:
                continue
            with (self._dir / str(bucket)).open("a", encoding="utf-8") as handle:
                for event in held:
                    handle.write(event.model_dump_json())
                    handle.write("\n")
            held.clear()
        self._holding = 0

    def _load(self, bucket: int) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        if self._dir is not None:
            path = self._dir / str(bucket)
            if path.exists():
                with path.open(encoding="utf-8") as handle:
                    events.extend(NormalizedEvent.model_validate_json(line) for line in handle)
                path.unlink()
        events.extend(self._held[bucket])
        self._held[bucket] = []
        return events

    def batches(self, target: int = BATCH_EVENTS) -> Iterator[list[NormalizedEvent]]:
        """Whole buckets, gathered until a batch holds ``target`` events. Always at least one
        batch — possibly empty — so a run that read nothing is still judged once (a detector's
        ``why_silent`` may have something to say about nothing)."""
        if self._arrival is not None and len(self._arrival) <= target:
            arrival, self._arrival = self._arrival, None
            self._held = [[] for _ in range(BUCKETS)]
            yield arrival
            return
        self._arrival = None
        batch: list[NormalizedEvent] = []
        yielded = False
        for bucket in range(BUCKETS):
            if self._counts[bucket] == 0:
                continue
            batch.extend(self._load(bucket))
            if len(batch) >= target:
                yield batch
                yielded = True
                batch = []
        if batch or not yielded:
            yield batch

    def close(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
