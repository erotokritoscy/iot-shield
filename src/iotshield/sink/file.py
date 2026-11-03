"""File result sink: append a run's full result as one JSON line (JSONL).

The proof that the store is pluggable (framing B): a deployment can run with no Postgres
and no dashboard and still capture every run losslessly to a file any downstream tool can
ingest. One self-contained JSON object per run (metadata + all payload arrays), serialized
through the contracts' own pydantic JSON dump (timestamps -> ISO-8601, UUIDs -> str), so
there is no bespoke serialization to drift from the contracts. When an ``output_dir`` is
provided (server deployments), only the filename component of ``path`` is used, under that
directory — so a caller-supplied path can never escape onto arbitrary server paths. When the
run also names an organization (ADR-0039), that directory is ``<output_dir>/<org_id>/``, so one
tenant's exports never land beside another's.

**Still one line per run when the run arrives in batches (ADR-0047).** Each array's items are
appended to a part file beside the target as the batches come in, and ``finish`` writes the
line by copying the parts through in order — so the line is byte-identical to the one a
single ``json.dumps`` of the whole run produced, and is never held in memory whole. Only the
assets are kept (one per device, deduplicated across batches), and only until the line is
written. The parts are deleted on ``finish`` and on ``abort``, so a failed run appends nothing.
"""

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, TextIO

from iotshield.contracts import Asset
from iotshield.sink import RunHeader, RunResult, RunTotals, SinkResources, write_result

_DEFAULT_FILENAME = "iotshield-results.jsonl"
_ARRAYS = ("events", "malformed", "detections", "alerts")


class FileSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self, header: RunHeader) -> "_FileWriter":
        return _FileWriter(self._path, header)

    def write(self, result: RunResult) -> None:
        """A complete result as a one-batch run."""
        write_result(self, result)


class _FileWriter:
    def __init__(self, path: Path, header: RunHeader) -> None:
        self._path = path
        self._header = header
        path.parent.mkdir(parents=True, exist_ok=True)
        self._dir = Path(tempfile.mkdtemp(prefix=".iotshield-run-", dir=path.parent))
        self._parts: dict[str, TextIO] = {
            name: (self._dir / name).open("w", encoding="utf-8") for name in _ARRAYS
        }
        self._written: dict[str, int] = dict.fromkeys(_ARRAYS, 0)
        self._registered: dict[str, Asset] = {}
        self._discovered: dict[str, Asset] = {}

    def add(self, part: RunResult) -> None:
        for asset in part.assets:
            if asset.metadata.get("auto_discovered"):
                if asset.asset_id not in self._registered:
                    self._discovered.setdefault(asset.asset_id, asset)
            else:
                self._registered.setdefault(asset.asset_id, asset)
                self._discovered.pop(asset.asset_id, None)
        for name, items in (
            ("events", part.events),
            ("malformed", part.malformed),
            ("detections", part.detections),
            ("alerts", part.alerts),
        ):
            handle = self._parts[name]
            for item in items:
                if self._written[name]:
                    handle.write(", ")
                handle.write(json.dumps(item.model_dump(mode="json")))
                self._written[name] += 1

    def finish(self, totals: RunTotals) -> None:
        try:
            for handle in self._parts.values():
                handle.close()
            header = self._header
            # Registrations first, then discovered devices by id: the order `resolve_assets`
            # gives a whole run, so a batched run's line is the same as an unbatched one's.
            assets = [
                *self._registered.values(),
                *sorted(self._discovered.values(), key=lambda asset: asset.asset_id),
            ]
            head: dict[str, Any] = {
                "run_id": str(header.run_id),
                "run_at": header.run_at.isoformat(),
                "sources": [_source(s) for s in header.sources],
                "warnings": totals.warnings,
                "assets": [asset.model_dump(mode="json") for asset in assets],
            }
            with self._path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(head)[:-1])  # the object, still open for the arrays
                for name in _ARRAYS:
                    out.write(f', "{name}": [')
                    with (self._dir / name).open(encoding="utf-8") as part:
                        shutil.copyfileobj(part, out)
                    out.write("]")
                out.write("}\n")
        finally:
            self._cleanup()

    def abort(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        for handle in self._parts.values():
            handle.close()
        shutil.rmtree(self._dir, ignore_errors=True)


def _source(source: Any) -> dict[str, Any]:
    record = {
        "normalizer": source.normalizer,
        "source_format": source.source_format,
        "origin": source.origin,
    }
    if source.pull is not None:
        record["pull"] = source.pull
    return record


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
        # The org id is a slug (`accounts.slugify`), so it is a safe single path component.
        base = resources.output_dir
        if resources.org_id is not None:
            base = base / resources.org_id
        target = base / filename
        # The docstring's promise as a checked property rather than a claim about the edge
        # cases of Path.name — cheap, and it cannot rot if that parsing ever changes.
        if not target.resolve().is_relative_to(base.resolve()):
            raise ValueError(f"sink path {path!r} resolves outside the output directory")
        return FileSink(target)
    # No configured base (host/CLI run): honor the path as given.
    return FileSink(Path(path))
