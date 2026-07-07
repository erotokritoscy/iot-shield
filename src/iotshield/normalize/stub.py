"""Reference Normalizer: minimal telemetry CSV, one event per line.

Format ("stub-csv"): ``timestamp,asset_id,metric,value`` — ISO-8601 timestamp
with timezone, float value. Blank lines are skipped (no content is not data).

Deliberately dumb and permanent: it is the reference implementation the port
suite runs against, and the pre-committed fallback if a real normalizer
integration stalls (see CLAUDE.md golden rule 5).
"""

from pydantic import ValidationError

from iotshield.contracts import Lane, MalformedRecord, NormalizedEvent
from iotshield.normalize.port import NormalizeResult

SOURCE_FORMAT = "stub-csv"


class StubCsvNormalizer:
    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        if source_format != SOURCE_FORMAT:
            raise ValueError(f"unsupported source_format: {source_format!r}")

        result = NormalizeResult()

        def reject(line: str, error: str) -> None:
            result.malformed.append(
                MalformedRecord(raw=line, error=error, source_format=source_format, origin=origin)
            )

        for line in text.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 4:
                reject(
                    line, f"expected 4 fields (timestamp,asset_id,metric,value), got {len(parts)}"
                )
                continue
            timestamp, asset_id, metric, value_text = parts
            try:
                value = float(value_text)
            except ValueError:
                reject(line, f"value is not a number: {value_text!r}")
                continue
            try:
                # Contract validation is the arbiter of well-formedness: attempt
                # construction and convert failures to rejects, so contract rules
                # (aware timestamps, non-empty asset) are never re-implemented here.
                event = NormalizedEvent.model_validate(
                    {
                        "timestamp": timestamp,
                        "asset_id": asset_id,
                        "source_type": Lane.TELEMETRY,
                        "source_format": source_format,
                        "metric": metric,
                        "value": value,
                        "raw": line,
                    }
                )
            except ValidationError as exc:
                reject(line, "; ".join(error["msg"] for error in exc.errors()))
                continue
            result.events.append(event)
        return result
