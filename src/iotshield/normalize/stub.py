"""Reference Normalizer: minimal telemetry CSV, one event per line.

Format ("stub-csv"): ``timestamp,asset_id,metric,value`` — ISO-8601 timestamp
with timezone, float value. Blank lines are skipped (no content is not data).

Deliberately dumb and permanent: it is the reference implementation the port
suite runs against, and the pre-committed fallback if a real normalizer
integration stalls (see CLAUDE.md golden rule 5). Its per-line parse is all that
is stub-specific; the port's laws (conservation, verbatim raw, unsupported-format
raises) live once in ``normalize_linewise``.
"""

from typing import Any

from iotshield.capabilities import Capability, FormatToken, LaneToken
from iotshield.contracts import Lane
from iotshield.normalize._linewise import LineParseError, normalize_linewise
from iotshield.normalize.port import NormalizeResult

SOURCE_FORMAT = "stub-csv"

# Consumes the stub CSV format; every event it emits is telemetry-lane. Declared
# here so the compatibility guard can read it without constructing the adapter.
CAPABILITY = Capability.of(
    consumes={FormatToken(SOURCE_FORMAT)}, produces={LaneToken(Lane.TELEMETRY)}
)


def _parse_line(line: str) -> dict[str, Any]:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 4:
        raise LineParseError(
            f"expected 4 fields (timestamp,asset_id,metric,value), got {len(parts)}"
        )
    timestamp, asset_id, metric, value_text = parts
    try:
        value = float(value_text)
    except ValueError as exc:
        raise LineParseError(f"value is not a number: {value_text!r}") from exc
    # The remaining well-formedness rules (aware timestamp, non-empty asset,
    # finite value) are the contract's job — normalize_linewise validates and
    # rejects, so they are never re-checked here.
    return {
        "timestamp": timestamp,
        "asset_id": asset_id,
        "source_type": Lane.TELEMETRY,
        "source_format": SOURCE_FORMAT,
        "metric": metric,
        "value": value,
    }


class StubCsvNormalizer:
    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        return normalize_linewise(
            text,
            source_format=source_format,
            expected_format=SOURCE_FORMAT,
            origin=origin,
            parse_line=_parse_line,
        )
