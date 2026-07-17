"""LOG-lane burst detector: too many matching log events on one asset, too fast.

The first LOG-lane detector, and the first one we designed rather than ported
(ADR-0020). The student modules normalize logs but judge nothing: team1's
``/analytics/anomalies`` is a hardcoded ``severity in {error, critical} or |value| > 80``
scan over the latest rows, and team4 stops at parsing plus static per-metric severity
constants. Neither keeps the per-asset, per-window state a real log detector needs, so
there was nothing to port (ADR-0020 D1).

Why counting rather than a model: a log event carries no numeric channel — its signal is
``(timestamp, asset, severity, message)``. "Anomalous" therefore reduces to one of three
things: a known-bad *content* match, *too many* of something, or something *never seen*.
The volumetric family is both the highest-value (brute force, credential stuffing,
scanning and error storms are all "N of X within W") and the only one correct on run #1 —
no labels, no training corpus, no artifact — while staying explainable enough for the
rationale our alert contract mandates.

The mechanism is one parameterized rule, not a family of near-duplicate detectors: per
asset, count the events a **preset** predicate matches inside a sliding time window; when
a window holds at least ``min_count`` of them, overlapping windows merge into one episode
and one ``DetectionResult``. Two presets ship (ADR-0020 D2): ``severity_spike``
(severity >= error — format-agnostic, fires on any log source) and ``auth_failure``
(curated authentication-failure phrasings — the credential-attack story). A new category
is a ``PRESETS`` entry, not a new adapter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent, Severity
from iotshield.detect._group import group_by_asset

# Fixed log-lane detector; declared for the compatibility guard.
CAPABILITY = Capability.of(consumes={LaneToken(Lane.LOG)}, produces={DETECTIONS})

_SAMPLE_MESSAGES = 3  # how many of the burst's messages ride along as evidence

# Severity is a StrEnum, so a bare `>=` between members compares *text*
# ("critical" < "debug"). Rank explicitly so a floor orders the way an operator means it.
_SEVERITY_RANK = {
    Severity.DEBUG: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
    Severity.CRITICAL: 4,
}

# Authentication-failure phrasings across the dialects our normalizers emit (sshd, generic
# PAM, Windows security log). Curated deliberately: an operator selects a preset by name and
# never writes a regex in YAML, so a config typo cannot become a pathologically slow or
# wildly over-matching pattern (ADR-0020 D3).
_AUTH_FAILURE_PATTERNS = (
    r"failed password",
    r"authentication failure",
    r"auth(?:entication)? failed",
    r"failed login",
    r"login failed",
    r"invalid user",
    r"failed to log on",  # Windows security log, event 4625
    r"\b4625\b",
)


@dataclass(frozen=True)
class Preset:
    """A named predicate: which log events count toward a burst.

    An event matches when it clears ``severity_floor`` (if set) AND matches any of
    ``patterns`` (if any). Leaving both unset would match every log line — no shipped
    preset does, but that composition is what lets a future category be one entry here.
    """

    severity_floor: Severity | None = None
    patterns: tuple[re.Pattern[str], ...] = ()

    def matches(self, event: NormalizedEvent) -> bool:
        if (
            self.severity_floor is not None
            and _SEVERITY_RANK[event.severity] < _SEVERITY_RANK[self.severity_floor]
        ):
            return False
        if not self.patterns:
            # `any([])` is False, so a floor-only preset needs this to match at all.
            return True
        message = event.message or ""
        return any(pattern.search(message) for pattern in self.patterns)


PRESETS: dict[str, Preset] = {
    "severity_spike": Preset(severity_floor=Severity.ERROR),
    # Deliberately NO severity floor: sshd logs "Failed password" at INFO, so a
    # severity-based rule is blind to exactly the burst this preset exists to catch.
    "auth_failure": Preset(
        patterns=tuple(re.compile(pattern, re.IGNORECASE) for pattern in _AUTH_FAILURE_PATTERNS)
    ),
}

DEFAULT_PRESET = "severity_spike"
"""The format-agnostic default: it needs no message patterns, so it says something
useful about any log source the normalizers can parse."""


@dataclass
class _Burst:
    """A merged run of overlapping qualifying windows: one episode, one detection."""

    start: int
    end: int
    peak: int


class LogBurstDetector:
    def __init__(
        self,
        *,
        preset: str = DEFAULT_PRESET,
        window_minutes: float = 5.0,
        min_count: int = 5,
    ) -> None:
        if preset not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ValueError(f"unknown log_burst preset {preset!r}; known: {known}")
        if window_minutes <= 0:
            raise ValueError(f"window_minutes must be positive, got {window_minutes}")
        # Load-bearing beyond "a burst is 2+ events": it also keeps the reported threshold
        # (min_count - 1) >= 1, so the alert engine's degenerate non-positive-threshold
        # branch in detection_margin is unreachable for this detector.
        if min_count < 2:
            raise ValueError(f"min_count must be >= 2 (a burst is 2+ events), got {min_count}")
        self._preset_name = preset
        self._preset = PRESETS[preset]
        self._window = timedelta(minutes=window_minutes)
        self._min_count = min_count

    @property
    def lane(self) -> Lane:
        return Lane.LOG

    @property
    def method(self) -> str:
        # The preset rides in the method so two configured bursts (severity_spike and
        # auth_failure) stay distinguishable in a detection and in the alert rationale.
        return f"log_burst:{self._preset_name}"

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        # Foreign-lane isolation and deterministic asset order are the port's laws, shared
        # by every detector; only the "is this event judgeable" predicate is ours.
        detections: list[DetectionResult] = []
        for asset_id, matched in group_by_asset(events, lane=self.lane, keep=self._preset.matches):
            detections.extend(self._detect_asset(asset_id, matched))
        return detections

    def _detect_asset(self, asset_id: str, matched: list[NormalizedEvent]) -> list[DetectionResult]:
        # The runner hands a detector its lane in (asset, time) order (ADR-0019), but the port
        # promises a standalone caller nothing, so sort defensively — on the runner's
        # already-ordered input Timsort makes this a single O(n) pass.
        matched = sorted(matched, key=lambda event: event.timestamp)

        detections: list[DetectionResult] = []
        for burst in self._find_bursts(matched):
            span = matched[burst.start : burst.end + 1]
            detections.append(
                DetectionResult(
                    asset_id=asset_id,
                    window_start=span[0].timestamp,
                    window_end=span[-1].timestamp,
                    lane=self.lane,
                    method=self.method,
                    score=float(burst.peak),
                    # The port's law is a STRICT score > threshold, so the reported threshold
                    # is the exceedance boundary — the largest count still considered normal,
                    # one below the configured trigger. "6 failures vs threshold 4" reads
                    # correctly to an analyst and gives the alert engine a real margin.
                    threshold=float(self._min_count - 1),
                    # min_count >= 2, so an episode is several events by construction: a burst
                    # is always collective, never a point anomaly.
                    event_kind=EventKind.COLLECTIVE,
                    evidence=self._evidence(span, burst.peak),
                )
            )
        return detections

    def _find_bursts(self, matched: Sequence[NormalizedEvent]) -> list[_Burst]:
        """Merge every window holding >= ``min_count`` matches into maximal episodes.

        Two pointers walk the time-ordered matches: ``left`` trails ``right`` by at most
        ``window``, so ``right - left + 1`` is the count in the widest window ending at
        ``right``. Qualifying windows that *overlap* describe the same incident and merge;
        one whose left edge has jumped past the previous episode's end means a real quiet
        gap, so it opens a new episode. That merge is what keeps a 90-attempt brute force a
        single alert instead of 90 — the fragmentation that made team3's IsolationForest a
        poor alert citizen (ADR-0020 D4).
        """
        bursts: list[_Burst] = []
        left = 0
        for right in range(len(matched)):
            while matched[right].timestamp - matched[left].timestamp > self._window:
                left += 1
            count = right - left + 1
            if count < self._min_count:
                continue
            if bursts and left <= bursts[-1].end:
                bursts[-1].end = right  # `right` only grows, so end stays the episode's last
                bursts[-1].peak = max(bursts[-1].peak, count)
            else:
                bursts.append(_Burst(start=left, end=right, peak=count))
        return bursts

    def _evidence(self, span: Sequence[NormalizedEvent], peak: int) -> dict[str, Any]:
        # `category` is the preset name — the semantic label of what was detected. It rides
        # in evidence for now; promoting it to the alert's threat_class is a follow-on
        # (ADR-0020 D6).
        return {
            "category": self._preset_name,
            "matched_events": len(span),
            "peak_in_window": peak,
            "window_minutes": self._window.total_seconds() / 60,
            "severities": sorted({event.severity.value for event in span}),
            "sample_messages": [
                event.message for event in span[:_SAMPLE_MESSAGES] if event.message
            ],
        }
