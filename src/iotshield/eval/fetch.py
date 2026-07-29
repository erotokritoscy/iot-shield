"""Converting third-party datasets into bundles — the tier we may use but not redistribute.

SKAB (the Skoltech Anomaly Benchmark) is the dataset every measured result in ADR-0021 came
from, and it is licensed **GPLv3** — copyleft, with share-alike obligations on derivative
works. This repository declares no licence and its stated direction is a commercial product,
so committing SKAB's bytes (or our labels derived from its ``anomaly`` column) would bake a
licensing question into git history, where it is expensive to undo.

So SKAB ships as a *conversion step* instead: point this at a checkout, get bundles under the
corpus root, gitignored. One command, run once, and thereafter SKAB is just data like any
other corpus. The manifest records provenance and licence, so a bundle can always say what it
is and where it came from.

    uv run python -m iotshield.eval fetch skab --source ../reference/.../Datasets/SKAB

Obtaining the source is deliberately *not* automated. A silent network download into a
research repository is the kind of convenience that later turns out to have fetched something
nobody reviewed; naming a local checkout keeps the human in the loop, and the URL is printed
when the path is missing.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

import pandas as pd

from iotshield.eval.corpus import Case, Corpus, CorpusUnavailable, write_bundle
from iotshield.eval.generate.telemetry import to_stub_csv
from iotshield.eval.truth import GroundTruth
from iotshield.pipeline.registry import lookup

SKAB_URL = "https://github.com/waico/SKAB"
SKAB_LICENCE = f"GPL-3.0 — (c) Skoltech (waico/SKAB). Not redistributed; see {SKAB_URL}"

# SKAB ships semicolon-separated CSV with the labels inline.
_SEPARATOR = ";"
_LABEL = "anomaly"
_TIME = "datetime"
_ASSET = "skab-testbed"
_DROP = (_LABEL, "changepoint")

_SKAB_SUBSETS = {
    "skab_valve1": ("valve1", "SKAB valve1 closure faults (one fault per recording)"),
    "skab_valve2": ("valve2", "SKAB valve2 faults (one fault per recording)"),
    "skab_clean": ("anomaly-free", "SKAB anomaly-free recording (no faults at all)"),
}


@dataclass(frozen=True)
class Dataset:
    """A convertible third-party dataset: how to find it, and how to turn it into bundles.

    ``corpora`` names what conversion produces, so ``load_corpus`` can recognise an
    un-fetched corpus by *exact name* rather than a prefix guess — and so this registry is
    the single description of a dataset rather than one of two.
    """

    describe: str
    url: str
    licence: str
    corpora: tuple[str, ...]
    default_source: Path | None
    convert: Callable[[Path], list[Corpus]]


def fetch_hint(corpus_name: str) -> str | None:
    """How to obtain a corpus this machine does not have, or None if we cannot get it."""
    for name, dataset in DATASETS.items():
        if corpus_name in dataset.corpora:
            return f"{dataset.describe} — run: python -m iotshield.eval fetch {name}"
    return None


def _skab_case(path: Path) -> Case:
    frame = pd.read_csv(path, sep=_SEPARATOR)
    timestamps = [
        moment.to_pydatetime().replace(tzinfo=UTC) for moment in pd.to_datetime(frame[_TIME])
    ]
    flags = (
        [bool(value) for value in frame[_LABEL]]
        if _LABEL in frame.columns
        else [False] * len(frame)
    )
    channels = frame.drop(columns=[_TIME, *(c for c in _DROP if c in frame.columns)])
    return Case(
        name=f"{path.parent.name}-{path.stem}",
        inputs=[to_stub_csv(channels, timestamps, _ASSET)],
        # from_timeline owns the collapse of per-row flags into incidents; re-deriving it
        # here would give the answer key a second definition of "one incident", which is
        # exactly what truth.py exists to prevent.
        #
        # The roster is exactly {_ASSET}: SKAB is one testbed, and its `anomaly` column is
        # exhaustive for it, so "listed with no incident in this window" really does mean
        # clean rather than unknown (ADR-0026).
        truth=GroundTruth.from_timeline(list(zip(timestamps, flags, strict=True)), asset_id=_ASSET),
    )


def _convert_skab(source: Path) -> list[Corpus]:
    corpora: list[Corpus] = []
    for name, (subdirectory, description) in _SKAB_SUBSETS.items():
        base = source / subdirectory
        if not base.is_dir():
            continue
        files = sorted(base.glob("*.csv"))
        if not files:
            continue
        corpora.append(
            Corpus(
                name=name,
                description=description,
                cases=[_skab_case(path) for path in files],
                provenance=f"SKAB {subdirectory} — {SKAB_URL}",
                licence=SKAB_LICENCE,
            )
        )
    if not corpora:
        raise CorpusUnavailable(
            f"no SKAB subsets found under {source} "
            f"(expected valve1/ valve2/ anomaly-free/). Clone {SKAB_URL} and pass --source."
        )
    return corpora


# Where SKAB sits in the internship workspace, as a convenience default only — a standalone
# clone of this repository has no such tree and must pass --source.
_WORKSPACE_SKAB = (
    Path(__file__).resolve().parents[3].parent
    / "reference"
    / "team3-telemetry-ad"
    / "Datasets"
    / "SKAB"
)

DATASETS: dict[str, Dataset] = {
    "skab": Dataset(
        describe="SKAB — Skoltech Anomaly Benchmark (valve faults + anomaly-free)",
        url=SKAB_URL,
        licence=SKAB_LICENCE,
        corpora=tuple(_SKAB_SUBSETS),
        default_source=_WORKSPACE_SKAB,
        convert=_convert_skab,
    )
}


def fetch(name: str, root: Path, source: Path | None = None) -> list[tuple[str, int]]:
    """Convert a named dataset into bundles under ``root``; return (corpus, cases) pairs."""
    dataset = lookup("dataset", DATASETS, name)
    chosen = source or dataset.default_source
    if chosen is None or not chosen.is_dir():
        raise CorpusUnavailable(
            f"{name}: source directory not found ({chosen}). "
            f"Obtain it from {dataset.url} and pass --source <path>."
        )

    written: list[tuple[str, int]] = []
    for corpus in dataset.convert(chosen):
        write_bundle(root / corpus.name, corpus)
        written.append((corpus.name, len(corpus.cases)))
    return written
