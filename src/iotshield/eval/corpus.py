"""Labelled corpora: what a benchmark runs on, and how a researcher brings their own.

A **case** is one labelled example — the raw input text(s) a run would ingest, plus where
the faults really were. A **corpus** is a named set of cases, stored as a *bundle* on disk
(see ``bundle.py`` for the format and why it exists).

Corpora are **discovered, not registered**: everything under the corpus root that carries a
manifest is a corpus. There is no registry to edit and no loader to write, so adding a
dataset is a drop-in — which is the difference between a harness a researcher can extend and
one only its author can (ADR-0021 D5).

This is deliberately **not** a fifth port: no Protocol, no contract suite. Ports are the
pipeline's runtime joints, and nothing here ever runs at product runtime.

The root is ``data/eval/`` in the repository, overridable by ``IOTSHIELD_CORPUS_PATH`` — the
same 12-factor pattern as ``IOTSHIELD_DATABASE_URL``, and for the same reason: *where data
lives* is a deployment property. It is never a caller-supplied value, because a request that
names a path is a request to read arbitrary files (ADR-0018 §9-A).
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from iotshield.eval.bundle import (
    MANIFEST_NAME,
    BundleError,
    CaseManifest,
    CorpusManifest,
    InputManifest,
    Labels,
    discover,
    labels_name,
    manifest_json,
    payload_name,
    read_labels,
    read_manifest,
    read_payload,
    write_payload,
)
from iotshield.eval.truth import GroundTruth, Incident
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV

CORPUS_PATH_ENV = "IOTSHIELD_CORPUS_PATH"

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_ROOT = _REPO_ROOT / "data" / "eval"
"""Corpora ship *inside* the repository, so a clone of it can benchmark with no other setup."""

# Datasets we cannot redistribute but can convert locally. Listed so `--list-corpora` can
# advertise them: a researcher should learn that SKAB is one command away, not conclude the
# harness only has toy data. See eval/fetch.py.
FETCHABLE: dict[str, str] = {
    "skab": "SKAB valve faults + anomaly-free (Skoltech, GPLv3) — run: iotshield.eval fetch skab"
}


class CorpusUnavailable(RuntimeError):
    """A named corpus is not present on this machine (never fetched, or wrong root)."""


@dataclass(frozen=True)
class CaseInput:
    """One raw input a case hands to the pipeline, tagged with the format it is in.

    Tagged, not bare text, for the same reason ``SourceSpec`` is (ADR-0019): a blob has no
    intrinsic format, so it must be *declared*. Without the tag, benchmarking a stub-csv
    corpus against a syslog spec is not an error — every line is rejected as malformed, the
    detector sees nothing, and the report says ``event_f1 -``, indistinguishable from a
    detector that is simply bad. Publishing that number is exactly the failure this harness
    exists to prevent, so the mismatch is refused instead.
    """

    source_format: str
    text: str


@dataclass(frozen=True)
class Case:
    """One labelled example: what to ingest, and where the faults really were.

    ``inputs`` is a list so a case aligns 1:1 with ``PipelineSpec.sources``; a future
    multimodal corpus needs no change here, only a bundle that supplies two inputs.
    """

    name: str
    inputs: list[CaseInput]
    truth: GroundTruth
    holdout: list[CaseInput] = field(default_factory=list)


@dataclass(frozen=True)
class Corpus:
    name: str
    description: str
    cases: list[Case]
    provenance: str = ""
    licence: str = ""

    @property
    def incidents(self) -> int:
        return sum(len(case.truth.incidents) for case in self.cases)


@dataclass(frozen=True)
class CorpusInfo:
    """What a listing knows about a corpus without paying to load it.

    ``cases`` comes from the manifest, so a picker can show the size of a 16-recording
    corpus without decompressing 2 MB of readings.
    """

    name: str
    description: str
    provenance: str
    licence: str
    cases: int


def corpus_root(root: Path | None = None) -> Path:
    """The directory bundles live in: explicit argument, else env, else the repo's own."""
    if root is not None:
        return root
    configured = os.environ.get(CORPUS_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CORPUS_ROOT


def to_stub_csv(frame: pd.DataFrame, timestamps: Sequence[datetime], asset_id: str) -> CaseInput:
    """Render a wide channel frame as the long CSV the ``stub`` normalizer ingests.

    The inverse of that normalizer's fan-out: one line per (timestamp, channel) reading,
    which is the contract's one-value-per-event rule (ADR-0003 D1). Going through real CSV
    text rather than constructing events directly is deliberate — the benchmark then
    exercises normalization too, so a corpus measures the *pipeline*, not just a detector.

    The format name comes from ``normalize.stub`` rather than a literal here, so the writer
    is anchored to the reader that defines the dialect.

    Values are materialized once via ``to_numpy``; the obvious per-cell ``frame[c].iloc[i]``
    costs a pandas column selection *and* a scalar lookup per cell, measured at 22x slower
    over a SKAB recording (100ms vs 4.5ms) and dominating corpus load.
    """
    channels = list(frame.columns)
    values = frame.to_numpy()
    lines = [
        f"{moment.isoformat()},{asset_id},{channels[column]},{row[column]}"
        for moment, row in zip(timestamps, values, strict=True)
        for column in range(len(channels))
    ]
    return CaseInput(STUB_CSV, "\n".join(lines) + "\n")


def _case_from_manifest(directory: Path, entry: CaseManifest) -> Case:
    inputs = [
        CaseInput(source.source_format, read_payload(directory / source.path))
        for source in entry.inputs
    ]
    labels = read_labels(directory / entry.labels)
    truth = GroundTruth.from_incidents(
        labels.timestamps, [Incident(start, end) for start, end in labels.incidents]
    )
    return Case(name=entry.name, inputs=inputs, truth=truth)


def load_bundle(directory: Path) -> Corpus:
    """Load one bundle directory into a Corpus."""
    manifest = read_manifest(directory)
    try:
        cases = [_case_from_manifest(directory, entry) for entry in manifest.cases]
    except (OSError, BundleError) as exc:
        raise BundleError(f"corpus {manifest.name!r} in {directory}: {exc}") from exc
    return Corpus(
        name=manifest.name,
        description=manifest.description,
        cases=cases,
        provenance=manifest.provenance,
        licence=manifest.licence,
    )


def list_corpora(root: Path | None = None) -> list[CorpusInfo]:
    """Describe every bundle under the root, cheaply (manifests only, no readings)."""
    infos: list[CorpusInfo] = []
    for _, directory in discover(corpus_root(root)):
        try:
            manifest = read_manifest(directory)
        except BundleError:
            continue  # a broken manifest must not break the listing; loading it will say so
        infos.append(
            CorpusInfo(
                name=manifest.name,
                description=manifest.description,
                provenance=manifest.provenance,
                licence=manifest.licence,
                cases=len(manifest.cases),
            )
        )
    return infos


def load_corpus(name: str, root: Path | None = None) -> Corpus:
    """Load a corpus by name, or explain how to obtain it.

    An absent corpus is ``CorpusUnavailable`` rather than a plain error because the CLI
    treats it as a *skip* (exit 0): a fetchable dataset nobody has fetched yet is a normal
    state on a fresh clone, not a broken invocation.
    """
    base = corpus_root(root)
    # A bundle answers to its directory name or its manifest name; they normally agree, and
    # accepting either means renaming a folder does not break a saved command line.
    for directory_name, directory in discover(base):
        if name == directory_name or name == read_manifest(directory).name:
            return load_bundle(directory)

    for prefix, hint in FETCHABLE.items():
        if name.startswith(prefix):
            raise CorpusUnavailable(f"corpus {name!r} is not in {base}. {hint}")
    known = ", ".join(info.name for info in list_corpora(root)) or "none"
    raise CorpusUnavailable(f"unknown corpus {name!r} in {base}; available: {known}")


def write_bundle(
    directory: Path,
    corpus: Corpus,
    *,
    provenance: str | None = None,
    licence: str | None = None,
) -> CorpusManifest:
    """Write a Corpus out as a bundle — the one writer the generator and fetcher share.

    Round-trips with ``load_bundle``: everything a benchmark needs is recorded, so a corpus
    built once (generated, or converted from an external dataset) is thereafter just data.
    """
    cases: list[CaseManifest] = []
    for case in corpus.cases:
        inputs: list[InputManifest] = []
        for index, given in enumerate(case.inputs):
            relative = payload_name(case.name, given.source_format, index)
            write_payload(directory, relative, given.text)
            inputs.append(InputManifest(source_format=given.source_format, path=relative))

        labels_relative = labels_name(case.name)
        write_payload(
            directory,
            labels_relative,
            Labels(
                timestamps=[moment for moment, _ in case.truth.timeline],
                incidents=[(i.start, i.end) for i in case.truth.incidents],
            ).model_dump_json(indent=1),
        )
        cases.append(CaseManifest(name=case.name, inputs=inputs, labels=labels_relative))

    manifest = CorpusManifest(
        name=corpus.name,
        description=corpus.description,
        provenance=provenance if provenance is not None else corpus.provenance,
        licence=licence if licence is not None else corpus.licence,
        cases=cases,
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_NAME).write_text(manifest_json(manifest), encoding="utf-8")
    return manifest
