"""The LOG lane: a device's routine chatter, the two burst shapes, and RFC 5424.

**RFC 5424, not RFC 3164, and this is not a style preference.** The BSD format omits the year
entirely, so ``normalize.syslog`` resolves it with ``datetime.now(UTC).year`` — the only
defensible choice when reading a live log. A *corpus* whose labels are pinned at a fixed epoch
and whose log payload is RFC 3164 would therefore parse into the wrong year the moment the
calendar turns over: every timestamp would miss every labelled interval, every LOG-lane
benchmark would score zero, and nothing in the output would say why. RFC 5424 carries a full
offset-bearing timestamp, so the round-trip test holds on any date.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from iotshield.contracts import Lane, NormalizedEvent, Severity
from iotshield.eval.generate.port import (
    Calibration,
    LaneBehaviour,
    Observed,
    Streams,
    one_boundary,
)
from iotshield.eval.generate.world import Clock, Device, FaultSpec
from iotshield.normalize.syslog import PRI_SEVERITY, SOURCE_FORMAT

_FACILITY = 1  # "user-level messages", the honest facility for application logs

_SEVERITY_CODE = {severity: code for code, severity in PRI_SEVERITY.items()}
"""Severity to its RFC 5424 code — the reader's table, inverted.

Genuinely derived, not a copy that claims to be: the reader takes ``pri & 0x07``, so a
priority built from this round-trips to exactly the severity intended. The forward map is
many-to-one (0, 1 and 2 all mean CRITICAL) and dict order is insertion order, so inverting
keeps the *highest* code per severity — the conventional one to emit.
"""

MIN_COUNT = 5
CALIBRATION = Calibration("log_burst", "min_count", float(MIN_COUNT))
"""``log_burst``'s default window trigger, and the unit a log fault's intensity is quoted in
(ADR-0028 D6): a storm of ``intensity`` x 5 lines. Routine chatter stays far under, which is the
property that makes this lane discriminate."""

_ROUTINE = (
    (Severity.INFO, "heartbeat ok"),
    (Severity.INFO, "telemetry batch published"),
    (Severity.DEBUG, "cache trimmed"),
)

_ERROR_STORM = (
    (Severity.ERROR, "firmware integrity check failed"),
    (Severity.ERROR, "outbound connection blocked by policy"),
    (Severity.CRITICAL, "watchdog reset imminent"),
)

_AUTH_STORM = "Failed password for root from 198.51.100.7 port 55014 ssh2"
"""Logged at INFO on purpose: sshd really does, which is why ``log_burst``'s ``auth_failure``
preset matches on the message rather than on a severity floor (ADR-0020). A corpus that logged
this at ERROR would let a severity rule pass a test it should fail."""


@dataclass(frozen=True)
class _Profile:
    every_steps: int
    app: str


_PROFILES: dict[str, _Profile] = {
    "camera": _Profile(every_steps=45, app="camerad"),
    "gateway": _Profile(every_steps=60, app="gwd"),
    "sensor": _Profile(every_steps=90, app="sensord"),
}
_DEFAULT_PROFILE = "sensor"


class LogSource(LaneBehaviour):
    """Routine chatter at a low rate, plus bursts that clear ``log_burst``'s trigger."""

    lane = Lane.LOG

    def __init__(self) -> None:
        # The dispatch table is the declaration — see `SensorRig.__init__`.
        self._handlers: dict[str, Callable[[Sequence[int]], list[tuple[int, Severity, str]]]] = {
            "error_storm": self._errors,
            "compromise": self._errors,
            "auth_storm": self._auth_failures,
        }
        # Every log shape is "too many matching lines in one window", so all three share
        # ``min_count`` as their boundary. What differs between them is which lines *match* the
        # preset, which is a property of the message and the severity rather than of the count.
        super().__init__(self._handlers, one_boundary(self._handlers, CALIBRATION))

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        profile = _PROFILES.get(device.kind, _PROFILES[_DEFAULT_PROFILE])
        rng = streams("routine")
        lines: list[tuple[int, Severity, str]] = []
        for index, step in enumerate(range(0, clock.steps, profile.every_steps)):
            severity, text = _ROUTINE[int(rng.integers(0, len(_ROUTINE)))]
            lines.append((step, severity, f"[{profile.app}] {text} seq={index}"))

        expressed: dict[FaultSpec, frozenset[datetime]] = {}
        for fault in faults:
            handler = self._handlers.get(fault.kind)
            if handler is None:
                continue
            # Read off the lines the handler produced rather than the steps it was given: a
            # fault expresses itself here by *adding log lines*, so those are the perturbation
            # and the answer key is made of exactly their instants (see `port.Observed`).
            #
            # The count is `intensity` multiples of the boundary, so 1.0 puts exactly
            # `min_count` lines in the window and the scenario never writes a raw line count
            # (ADR-0028 D6).
            injected = handler(
                clock.spread(
                    fault.start_step,
                    fault.length_steps,
                    self.boundary(fault.kind).units(fault.intensity),
                )
            )
            lines.extend(injected)
            if injected:
                expressed[fault] = frozenset(clock.at(step) for step, _, _ in injected)

        events = tuple(
            NormalizedEvent(
                timestamp=clock.at(step),
                asset_id=device.asset_id,
                source_type=Lane.LOG,
                source_format=source_format,
                severity=severity,
                message=message,
                raw="",
            )
            for step, severity, message in lines
        )
        return Observed(events=events, expressed=expressed)

    def _errors(self, steps: Sequence[int]) -> list[tuple[int, Severity, str]]:
        return [
            (step, *_ERROR_STORM[index % len(_ERROR_STORM)]) for index, step in enumerate(steps)
        ]

    def _auth_failures(self, steps: Sequence[int]) -> list[tuple[int, Severity, str]]:
        return [(step, Severity.INFO, f"[sshd] {_AUTH_STORM}") for step in steps]


class Rfc5424Renderer:
    """``<PRI>1 TIMESTAMP HOST APP PROCID MSGID SD MSG`` — the inverse of the 5424 reader.

    The reader takes ``MSG`` verbatim and the host field as the asset id, so both survive the
    round trip character for character. ``PROCID``/``MSGID`` are written as ``-`` (nil), which
    is what a real emitter does when it has neither.
    """

    lane = Lane.LOG
    source_format = SOURCE_FORMAT

    def header(self) -> tuple[str, ...]:
        return ()

    def render(self, events: Sequence[NormalizedEvent]) -> tuple[str, ...]:
        return tuple(self._line(event) for event in events)

    def _line(self, event: NormalizedEvent) -> str:
        priority = _FACILITY * 8 + _SEVERITY_CODE[event.severity]
        # A nil MSG would make the line unparseable (the reader's MSG group needs a character),
        # so an empty message becomes the nil marker rather than nothing.
        return (
            f"<{priority}>1 {_stamp(event.timestamp)} {event.asset_id} "
            f"iotshield - - - {event.message or '-'}"
        )


def _stamp(moment: datetime) -> str:
    """ISO-8601 with an offset, which is exactly what ``fromisoformat`` reads back."""
    return moment.isoformat()
