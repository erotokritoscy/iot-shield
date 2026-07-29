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
            "run_at": result.run_at.isoformat(),
            "sources": [
                {"normalizer": s.normalizer, "source_format": s.source_format, "origin": s.origin}
                for s in result.sources
            ],
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
        # Confine to the server-configured directory. VALIDATE rather than sanitize: reducing
        # a path to its basename silently turns a wrong path into a different wrong path, and
        # `Path("..").name` is ".." (CPython drops "." components but keeps ".."), so the
        # `or _DEFAULT_FILENAME` fallback did not fire and the sink was handed the parent
        # directory. No escape was achieved — the open failed — but the caller got an opaque
        # OSError, which the Pipeline API maps to a 500 rather than a 422 naming the field.
        filename = Path(path).name
        if not filename.strip() or filename in (".", ".."):
            raise ValueError(
                f"sink path {path!r} does not name a file; give a plain filename"
                f" (it is written under the server's configured output directory)"
            )
        target = resources.output_dir / filename
        # The docstring's promise as a checked property rather than a claim about the edge
        # cases of Path.name — cheap, and it cannot rot if that parsing ever changes.
        if not target.resolve().is_relative_to(resources.output_dir.resolve()):
            raise ValueError(f"sink path {path!r} resolves outside the output directory")
        return FileSink(target)
    # No configured base (host/CLI run): honor the path as given.
    return FileSink(Path(path))
