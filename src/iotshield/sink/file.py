"""File result sink: append a run's full result as one JSON line (JSONL).

The proof that the store is pluggable (framing B): a deployment can run with no Postgres
and no dashboard and still capture every run losslessly to a file any downstream tool can
ingest. One self-contained JSON object per run (metadata + all payload arrays), serialized
through the contracts' own pydantic JSON dump (timestamps -> ISO-8601, UUIDs -> str), so
there is no bespoke serialization to drift from the contracts. When an ``output_dir`` is
provided (server deployments), only the filename component of ``path`` is used, under that
directory — so a caller-supplied path can never escape onto arbitrary server paths.
"""

import json
from pathlib import Path

from iotshield.sink import RunResult, SinkResources

_DEFAULT_FILENAME = "iotshield-results.jsonl"


class FileSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, result: RunResult) -> None:
        record = {
            "origin": result.origin,
            "source_format": result.source_format,
            "normalizer": result.normalizer,
            "run_at": result.run_at.isoformat(),
            "warnings": result.warnings,
            "assets": [asset.model_dump(mode="json") for asset in result.assets],
            "events": [event.model_dump(mode="json") for event in result.events],
            "malformed": [bad.model_dump(mode="json") for bad in result.malformed],
            "detections": [det.model_dump(mode="json") for det in result.detections],
            "alerts": [alert.model_dump(mode="json") for alert in result.alerts],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def build(resources: SinkResources, path: str = _DEFAULT_FILENAME) -> FileSink:
    if resources.output_dir is not None:
        # Confine to the server-configured directory: only the basename is honored, so a
        # caller-supplied path (e.g. from the UI) cannot traverse out of it.
        filename = Path(path).name or _DEFAULT_FILENAME
        return FileSink(resources.output_dir / filename)
    # No configured base (host/CLI run): honor the path as given.
    return FileSink(Path(path))
