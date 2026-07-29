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
from dataclasses import dataclass
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
    contained,
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
    """A named corpus is not present on this machine (never fetched, or wrong root).

    Deliberately *not* an error for the CLI: a fetchable dataset nobody has fetched yet is a
    normal state on a fresh clone, so a benchmark over it is skipped with exit 0.
    """


class UnknownCorpus(CorpusUnavailable):
    """No corpus of that name exists anywhere — a typo, not a missing download.

    The distinction the CLI could not make. Both raised ``CorpusUnavailable`` and the CLI
    treats that as a skip, so ``--corpus sythetic`` printed nothing and returned **0**: a CI
    benchmark job with a mistyped name passed green having measured nothing. The information
    to tell them apart was already computed at the raise site (a fetchable name has a hint, an
    unknown one does not); only the type was missing. The API maps both to 404, which is still
    right — subclassing keeps that mapping untouched.
    """


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
    # There is deliberately no `holdout` here. It existed as a field with no manifest
    # counterpart, so nothing on disk could ever populate it while three surfaces offered
    # `--fit-protocol holdout` as a working choice (see ``eval.fit``).


@dataclass(frozen=True)
class Corpus:
    name: str
    description: str
    cases: list[Case]
    provenance: str = ""
    licence: str = ""

    @property
    def incidents(self) -> int:
        """Labelled (fault, lane) projections across every case — the event level's total."""
        return sum(len(case.truth.incidents) for case in self.cases)

    @property
    def faults(self) -> int:
        """Distinct causes across every case — the alert level's total (ADR-0028 D2).

        Reported beside ``incidents`` rather than instead of it, because the gap between the
        two *is* the corpus's multimodality: they are equal on a single-lane corpus, and
        ``device_trimodal`` carries 7 projections of 3 faults.
        """
        return sum(len(case.truth.faults) for case in self.cases)


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

    error: str = ""
    """Why this bundle cannot be loaded, when it cannot — empty for a healthy corpus.

    A broken bundle used to be ``continue``d out of every listing, so a stale answer key or a
    malformed manifest simply *vanished*: it was absent from ``eval list``, from
    ``/eval/corpora`` and from the dashboard picker, and the follow-up error said "unknown
    corpus" without ever mentioning the directory sitting right there. Listing it with its
    reason turns three silent disappearances into one message, and the reason is already
    written — see ``bundle.read_labels``, which names the exact regenerate command.
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
    """One asset's stored labels back into truth, re-deriving its flags from the intervals."""
    return AssetTruth.from_incidents(
        label.asset_id,
        {Lane(lane): moments for lane, moments in label.clocks.items()},
        [
            Incident(
                asset_id=label.asset_id,
                lane=Lane(incident.lane),
                spans=tuple(incident.spans),
                kind=incident.kind,
                fault_id=incident.fault_id,
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
        # Sorted by lane so a regenerated bundle is byte-identical, the same reason `lanes`
        # below is sorted: a dict keyed off a set has no order gzip would forgive.
        clocks={
            lane.value: [moment for moment, _ in rows]
            for lane, rows in sorted(asset.timelines.items())
        },
        # Sorted by (lane, start) so a regenerated bundle is byte-identical however the
        # generator happened to visit the lanes. The v3 predecessor needed a sort *inside* each
        # label as well, because a lane set has no order; one incident naming one lane removes
        # that second ordering problem rather than solving it again.
        incidents=[
            IncidentLabel(
                spans=list(incident.spans),
                lane=incident.lane.value,
                kind=incident.kind,
                fault_id=incident.fault_id,
            )
            for incident in sorted(asset.incidents, key=lambda i: (i.lane.value, i.spans))
        ],
    )


def _case_from_manifest(directory: Path, entry: CaseManifest) -> Case:
    # Every path a manifest names is resolved through `contained`: a bundle is untrusted data
    # and may only reference files inside itself (see `bundle.contained`).
    inputs = [
        CaseInput(source.source_format, read_payload(contained(directory, source.path)))
        for source in entry.inputs
    ]
    labels = read_labels(contained(directory, entry.labels))
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


def _describe(directory_name: str, directory: Path) -> CorpusInfo:
    """One bundle as a listing sees it — including one that cannot be loaded.

    A broken bundle is *listed with its reason* rather than skipped. It is still a corpus
    someone put there; hiding it means the only feedback is an "unknown corpus" error that
    does not mention it.
    """
    try:
        manifest = read_manifest(directory)
        shape = source_formats(manifest)
    except BundleError as exc:
        return CorpusInfo(
            name=directory_name,
            description="(cannot be loaded)",
            provenance="",
            licence="",
            cases=0,
            error=str(exc),
        )
    return CorpusInfo(
        name=manifest.name,
        description=manifest.description,
        provenance=manifest.provenance,
        licence=manifest.licence,
        cases=len(manifest.cases),
        source_formats=shape,
    )


def list_corpora(root: Path | None = None) -> list[CorpusInfo]:
    """Describe every bundle under the root, cheaply (manifests only, no readings)."""
    return [_describe(name, directory) for name, directory in discover(corpus_root(root))]


def load_corpus(name: str, root: Path | None = None) -> Corpus:
    """Load a corpus by name, or explain how to obtain it.

    Three outcomes, and they are three different situations. A corpus that loads. One that is
    *fetchable but not here*, which is ``CorpusUnavailable`` and a normal state on a fresh
    clone. And a name that exists nowhere, which is ``UnknownCorpus`` — a typo, not a missing
    download. A broken bundle raises its own ``BundleError`` (a ``ValueError``) rather than
    disappearing, so the reader is told the directory is there and why it will not load.
    """
    base = corpus_root(root)
    # One pass: parse each manifest once, keep the names seen for the error message, and
    # reuse the matched manifest rather than reading it a second time. A broken *sibling*
    # bundle is skipped — one bad directory must not make every other corpus unloadable —
    # but a broken bundle that IS the one asked for raises, because silence there is how a
    # stale answer key turned into "unknown corpus 'x'" with no mention of x sitting on disk.
    seen: list[str] = []
    for directory_name, directory in discover(base):
        try:
            manifest = read_manifest(directory)
        except BundleError:
            if directory_name == name:
                raise
            continue
        # A bundle answers to its directory name or its manifest name; they normally agree,
        # and accepting either means renaming a folder does not break a saved command line.
        if name in (directory_name, manifest.name):
            return _corpus_from(directory, manifest)
        seen.append(manifest.name)

    hint = fetchable().get(name)
    if hint is not None:
        raise CorpusUnavailable(f"corpus {name!r} is not in {base}. {hint}")
    raise UnknownCorpus(
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
    # Through `write_payload` like every other file above, rather than its own `write_text`.
    # A bundle's text files must be LF on every platform (`bundle.NEWLINE`), and a second write
    # site is a second place to forget that — which is exactly how the CRLF reproducibility bug
    # got in. `MANIFEST_NAME` has no `.gz` suffix, so this takes the plain-text branch.
    write_payload(directory, MANIFEST_NAME, manifest_json(manifest))
    return manifest
