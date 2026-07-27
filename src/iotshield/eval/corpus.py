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
from dataclasses import dataclass, field
from pathlib import Path

from iotshield.contracts import Lane
from iotshield.eval.bundle import (
    MANIFEST_NAME,
    AssetLabel,
    BundleError,
    CaseManifest,
    CorpusManifest,
    IncidentLabel,
    InputManifest,
    Labels,
    discover,
    labels_name,
    manifest_json,
    payload_name,
    read_labels,
    read_manifest,
    read_payload,
    source_formats,
    write_payload,
)
from iotshield.eval.truth import AssetTruth, GroundTruth, Incident

CORPUS_PATH_ENV = "IOTSHIELD_CORPUS_PATH"

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_ROOT = _REPO_ROOT / "data" / "eval"
"""Corpora ship *inside* the repository, so a clone of it can benchmark with no other setup."""


def fetchable() -> dict[str, str]:
    """Datasets convertible locally but not shipped: corpus name -> how to obtain it.

    Derived from ``fetch.DATASETS`` rather than restated, so a dataset is described in one
    place. Imported inside the function because ``fetch`` imports this module — and the
    dependency genuinely runs that way, since a converter builds ``Corpus`` objects.
    """
    from iotshield.eval.fetch import DATASETS

    return {
        name: f"{dataset.describe} — run: python -m iotshield.eval fetch {key}"
        for key, dataset in DATASETS.items()
        for name in dataset.corpora
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
    source_formats: tuple[str, ...] = ()
    """The input shape a spec must declare to benchmark this corpus, from the manifest.

    Carried here so a caller composing a spec on someone's behalf — the dashboard's Evaluate
    page — can build one that *fits*. Without it the page hardcoded a single stub-csv source,
    so a multi-lane corpus appeared in its picker and then failed alignment on run.
    """


def corpus_root(root: Path | None = None) -> Path:
    """The directory bundles live in: explicit argument, else env, else the repo's own."""
    if root is not None:
        return root
    configured = os.environ.get(CORPUS_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CORPUS_ROOT


# Rendering readings into wire text lives with the lane that defines the format:
# `generate.telemetry.to_stub_csv` and its `StubCsvRenderer` (ADR-0026). It sat here while the
# generator was one file; keeping it would give stub-csv two writers free to drift apart,
# which is exactly what a renderer being *the inverse of a normalizer* rules out. This module
# owns the corpus container and its bundle IO, and knows nothing about formats.


def _asset_truth(label: AssetLabel) -> AssetTruth:
    """One asset's stored labels back into truth, re-deriving its row flags from intervals."""
    return AssetTruth.from_incidents(
        label.asset_id,
        label.clock,
        [
            Incident(
                asset_id=label.asset_id,
                start=incident.start,
                end=incident.end,
                kind=incident.kind,
                fault_id=incident.fault_id,
                lanes=frozenset(Lane(lane) for lane in incident.lanes),
            )
            for incident in label.incidents
        ],
    )


def _asset_label(asset: AssetTruth) -> AssetLabel:
    """One asset's truth as stored labels — the inverse of ``_asset_truth``, beside it.

    A named function rather than a comprehension nested in a write call, so the two halves of
    one mapping read as the pair they are.
    """
    return AssetLabel(
        asset_id=asset.asset_id,
        clock=[moment for moment, _ in asset.timeline],
        incidents=[
            IncidentLabel(
                start=incident.start,
                end=incident.end,
                kind=incident.kind,
                fault_id=incident.fault_id,
                # Sorted so a regenerated bundle is byte-identical: a frozenset has no order,
                # and gzip would show the churn.
                lanes=sorted(incident.lanes),
            )
            for incident in asset.incidents
        ],
    )


def _case_from_manifest(directory: Path, entry: CaseManifest) -> Case:
    inputs = [
        CaseInput(source.source_format, read_payload(directory / source.path))
        for source in entry.inputs
    ]
    labels = read_labels(directory / entry.labels)
    return Case(
        name=entry.name,
        inputs=inputs,
        truth=GroundTruth.from_assets([_asset_truth(label) for label in labels.assets]),
    )


def _corpus_from(directory: Path, manifest: CorpusManifest) -> Corpus:
    """Build a Corpus from an already-parsed manifest, so callers never re-read it."""
    source_formats(manifest)  # refuse a mixed-shape corpus here rather than mid-benchmark
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


def load_bundle(directory: Path) -> Corpus:
    """Load one bundle directory into a Corpus."""
    return _corpus_from(directory, read_manifest(directory))


def list_corpora(root: Path | None = None) -> list[CorpusInfo]:
    """Describe every bundle under the root, cheaply (manifests only, no readings)."""
    infos: list[CorpusInfo] = []
    for _, directory in discover(corpus_root(root)):
        try:
            manifest = read_manifest(directory)
        except BundleError:
            continue  # a broken manifest must not break the listing; loading it will say so
        try:
            shape = source_formats(manifest)
        except BundleError:
            continue  # unbenchmarkable; the same reason a broken manifest is skipped here
        infos.append(
            CorpusInfo(
                name=manifest.name,
                description=manifest.description,
                provenance=manifest.provenance,
                licence=manifest.licence,
                cases=len(manifest.cases),
                source_formats=shape,
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
    # One pass: parse each manifest once, keep the names seen for the error message, and
    # reuse the matched manifest rather than reading it a second time. A broken sibling
    # bundle is skipped here for the same reason list_corpora tolerates it — one bad
    # directory must not make every other corpus unloadable.
    seen: list[str] = []
    for directory_name, directory in discover(base):
        try:
            manifest = read_manifest(directory)
        except BundleError:
            continue
        # A bundle answers to its directory name or its manifest name; they normally agree,
        # and accepting either means renaming a folder does not break a saved command line.
        if name in (directory_name, manifest.name):
            return _corpus_from(directory, manifest)
        seen.append(manifest.name)

    hint = fetchable().get(name)
    if hint is not None:
        raise CorpusUnavailable(f"corpus {name!r} is not in {base}. {hint}")
    raise CorpusUnavailable(
        f"unknown corpus {name!r} in {base}; available: {', '.join(seen) or 'none'}"
    )


def write_bundle(directory: Path, corpus: Corpus) -> CorpusManifest:
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
            Labels(assets=[_asset_label(asset) for asset in case.truth.assets]).model_dump_json(
                indent=1
            ),
        )
        cases.append(CaseManifest(name=case.name, inputs=inputs, labels=labels_relative))

    manifest = CorpusManifest(
        name=corpus.name,
        description=corpus.description,
        provenance=corpus.provenance,
        licence=corpus.licence,
        cases=cases,
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_NAME).write_text(manifest_json(manifest), encoding="utf-8")
    return manifest
