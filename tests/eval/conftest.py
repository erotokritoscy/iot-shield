"""Shared fixtures for the evaluation suite: one clock, one way to build an answer key."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from iotshield.contracts import AlertSeverity, DetectionResult, Lane, PrioritizedAlert
from iotshield.eval import AssetTruth, GroundTruth

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

ASSET = "pump-02"
"""The default asset under test, matching what the detection/alert fixtures carry.

Named rather than repeated: an answer key about one device and findings about another is a
silent zero, and one constant makes that mismatch impossible to introduce by accident.
"""


def at(second: int) -> datetime:
    """A moment on the corpus clock, ``second`` seconds in."""
    return T0 + timedelta(seconds=second)


def rows(flags: list[bool]) -> list[tuple[datetime, bool]]:
    """One-second-apart labelled rows: ``[False, True, True]`` is one incident."""
    return [(at(index), flag) for index, flag in enumerate(flags)]


def truth_of(flags: list[bool], asset_id: str = ASSET) -> GroundTruth:
    """A single-asset answer key — the shape every pre-ADR-0026 corpus has."""
    return GroundTruth.from_timeline(rows(flags), asset_id=asset_id)


def detection(
    asset_id: str = ASSET, start: int = 0, end: int = 0, lane: Lane = Lane.TELEMETRY
) -> DetectionResult:
    """One detection on the shared clock. Lives here so both scoring suites share one factory.

    Each suite had its own near-identical pair, which meant two places to update whenever a
    contract field became required.
    """
    return DetectionResult(
        asset_id=asset_id,
        lane=lane,
        method="test",
        score=10.0,
        threshold=1.0,
        window_start=at(start),
        window_end=at(end),
    )


def alert(
    asset_id: str = ASSET,
    start: int = 0,
    end: int = 0,
    priority: float = 50.0,
    backing: Sequence[DetectionResult] = (),
) -> PrioritizedAlert:
    """One alert, optionally citing the detections that produced it.

    ``backing`` matters only for the lanes-per-alert metric; when it is empty a placeholder
    detection id is cited, because the contract requires at least one.
    """
    return PrioritizedAlert(
        asset_id=asset_id,
        severity=AlertSeverity.HIGH,
        priority_score=priority,
        rationale="test",
        detection_ids=[d.detection_id for d in backing]
        or [detection(asset_id, start, end).detection_id],
        window_start=at(start),
        window_end=at(end),
    )


def fleet_truth(**flags_by_asset: list[bool]) -> GroundTruth:
    """A multi-asset answer key: every named asset is in the roster, so all are adjudicated.

    ``fleet_truth(scanner=[...], gateway=[False] * 5)`` is the shape that matters — one
    device with a fault and one *asserted clean*, which is what makes a detection on the
    second an unambiguous false alarm rather than an unknown.
    """
    return GroundTruth.from_assets(
        [
            AssetTruth.from_timeline(asset_id, rows(flags))
            for asset_id, flags in flags_by_asset.items()
        ]
    )
