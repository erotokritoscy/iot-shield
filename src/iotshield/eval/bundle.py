"""Corpus bundles: a labelled dataset as *data on disk*, not code.

The unit that makes the harness usable by someone who clones only this repository. A bundle
is a self-describing directory under ``data/eval/``:

    data/eval/<corpus>/
      manifest.json              name, description, provenance, licence, the case list
      cases/<case>.<ext>.gz      readings, in a format one of our normalizers parses
      cases/<case>.labels.json   the answer key: the row clock + fault intervals

Three decisions are worth the space (ADR-0021 D5):

**Discovered, not registered.** Corpora used to be Python loader functions in a registry, so
adding a dataset meant writing code. Scanning a directory means adding a corpus is *adding
data* — the same drop-in gesture as adding a config, and one a researcher can do without
reading our source. It also means the CLI, the API and the dashboard all see a new corpus
with no change to any of them.

**Readings stay in a real ingest format, gzipped.** A bundle holds the text a run would
actually ingest, so a benchmark exercises normalization too and measures the *pipeline*
rather than a detector in isolation. Gzip because the corpora are repetitive numeric text
(measured 4.4x on SKAB) and a repository is a bad place for megabytes that compress away.

**Labels are intervals plus the row clock.** See ``GroundTruth.from_incidents``. Storing the
clock alongside the intervals costs a little duplication and buys a labels file that is a
complete answer key on its own — the reader never has to parse readings to learn the truth,
so it needs no knowledge of their format.

Provenance and licence are *required* manifest fields, because the first thing this format
had to solve was which datasets we may redistribute at all: SKAB is GPLv3, so it ships as a
fetch step rather than committed bytes, and a bundle that cannot say where it came from is a
bundle nobody can safely reuse.
"""

import gzip
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_NAME = "manifest.json"
CASES_DIR = "cases"
LABELS_SUFFIX = ".labels.json.gz"
"""Gzipped, like the readings.

The row clock is a long run of near-identical ISO-8601 strings, which is the best case for
gzip and the worst case for storing raw: measured on the synthetic corpus, uncompressed
labels were *larger* than the compressed readings they annotate (264 KB vs 220 KB). Read
one by hand with ``gzip -dc``.
"""

# Extension per source format, so a bundle's readings are recognisable on sight rather than
# uniformly ".txt". Unknown formats fall back to .txt — the manifest is authoritative either
# way, this only affects what a human sees in a file listing.
_EXTENSIONS = {"stub-csv": "csv", "syslog": "log", "syslog-rfc3164": "log"}


class BundleError(RuntimeError):
    """A bundle directory is missing, malformed, or internally inconsistent."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputManifest(_Model):
    """One readings file: what format it is in, and where it sits inside the bundle."""

    source_format: str = Field(min_length=1)
    path: str = Field(min_length=1)


class CaseManifest(_Model):
    name: str = Field(min_length=1)
    inputs: list[InputManifest] = Field(min_length=1)
    labels: str = Field(min_length=1)


class CorpusManifest(_Model):
    """A bundle's self-description — everything needed to load and to cite it."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    provenance: str = Field(
        min_length=1, description="Where the data came from; a URL when there is one"
    )
    licence: str = Field(min_length=1, description="Terms the data is distributed under")
    cases: list[CaseManifest] = Field(min_length=1)


class Labels(_Model):
    """A case's answer key: every labelled instant, plus the fault intervals.

    ``timestamps`` is the row clock — the instants point metrics are scored over. It is
    stored rather than derived so the answer key stands alone; see the module docstring.
    """

    timestamps: list[datetime] = Field(min_length=1)
    incidents: list[tuple[datetime, datetime]] = Field(default_factory=list)


def read_payload(path: Path) -> str:
    """Read a bundle payload, transparently gunzipping when it is compressed."""
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8")


def read_manifest(directory: Path) -> CorpusManifest:
    """Parse a bundle's manifest, or say precisely what is wrong with it."""
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"no {MANIFEST_NAME} in {directory}")
    try:
        return CorpusManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError(f"invalid {manifest_path}: {exc}") from exc


def read_labels(path: Path) -> Labels:
    try:
        return Labels.model_validate_json(read_payload(path))
    except (ValueError, OSError) as exc:
        raise BundleError(f"invalid labels {path}: {exc}") from exc


def payload_name(case_name: str, source_format: str, index: int) -> str:
    """The on-disk filename for one case input — slashes flattened, format-hinted."""
    stem = case_name.replace("/", "-").replace("\\", "-").replace(" ", "_")
    suffix = f"-{index}" if index else ""
    return f"{CASES_DIR}/{stem}{suffix}.{_EXTENSIONS.get(source_format, 'txt')}.gz"


def labels_name(case_name: str) -> str:
    stem = case_name.replace("/", "-").replace("\\", "-").replace(" ", "_")
    return f"{CASES_DIR}/{stem}{LABELS_SUFFIX}"


def write_payload(directory: Path, relative: str, text: str) -> None:
    """Write one bundle payload, gzipping when the name says so."""
    target = directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".gz":
        # mtime=0 keeps the bytes reproducible: a regenerated bundle with unchanged content
        # must not show up as a diff just because the clock moved.
        target.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    else:
        target.write_text(text, encoding="utf-8")


def discover(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, directory)`` for every bundle under ``root``, in name order.

    A directory without a manifest is silently skipped rather than raising — the corpus root
    is a place people put things, and one stray folder should not break ``--list-corpora``.
    A directory *with* a broken manifest is a different matter and surfaces when loaded.
    """
    if not root.is_dir():
        return
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if (directory / MANIFEST_NAME).is_file():
            yield directory.name, directory


def manifest_json(manifest: CorpusManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
