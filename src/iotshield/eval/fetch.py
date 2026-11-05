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
from typing import cast

import pandas as pd

from iotshield._paths import REPO_ROOT
from iotshield.convert._rates import HEADER as RATE_HEADER
from iotshield.convert._rates import SOURCE_FORMAT as RATE_FORMAT
from iotshield.convert._rates import counter_rows, representable
from iotshield.detect.team3.train import DEFAULT_ASSET_ID
from iotshield.eval.corpus import Case, CaseInput, Corpus, CorpusUnavailable, write_bundle
from iotshield.eval.generate.telemetry import to_stub_csv
from iotshield.eval.truth import GroundTruth
from iotshield.pipeline.registry import lookup

SKAB_URL = "https://github.com/waico/SKAB"
SKAB_LICENCE = f"GPL-3.0 — (c) Skoltech (waico/SKAB). Not redistributed; see {SKAB_URL}"

# SKAB ships semicolon-separated CSV with the labels inline.
_SEPARATOR = ";"
_LABEL = "anomaly"
_TIME = "datetime"
_DROP = (_LABEL, "changepoint")

# Imported rather than retyped, because since ADR-0034 the two must agree or the shipped
# artifact silently judges nothing: a model is filed under an asset id, and an id these
# bundles do not use makes `team3_zscore` skip every recording and report zeros. Nothing
# else would catch the drift — SKAB bundles are gitignored (GPLv3), so an absent corpus is
# a CI *skip*, and the mismatch would surface only as an unexplained empty benchmark.
# eval -> detect is the legal direction of the dependency arrow (ADR-0021).
_ASSET = DEFAULT_ASSET_ID

# LEAD reports a per-hour reading rather than a counter, so it arrives through the same rate
# conversion as WBN (ADR-0048 D1). It is the only meter corpus whose labels are not ours: a human
# marked these hours, so it is what can falsify a number the generator produced.
LEAD_URL = "https://www.kaggle.com/competitions/energy-anomaly-detection"
LEAD_LICENCE = (
    f"LEAD1.0 (Gulati & Arjunan). Terms not reviewed by this project; not redistributed. {LEAD_URL}"
)
_LEAD_FILE = "train.csv"
_LEAD_BUILDING = "building_id"
_LEAD_TIME = "timestamp"
_LEAD_READING = "meter_reading"
_LEAD_LABEL = "anomaly"
_LEAD_UNIT = "kwh"
_LEAD_SAMPLE_EVERY = 10

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


def _lead_case(building: int, rows: pd.DataFrame) -> Case:
    """One building as one case, its hour flags collapsed into incidents by ``from_timeline``.

    A blank reading is a gap rather than a row: 6% of LEAD's cells are empty, and rejecting each
    as malformed would bury the corpus's notes under something the dataset says plainly. Its flag
    goes with it, because truth is indexed by what was observed (ADR-0028 D1), and so does the
    flag of a reading a counter cannot carry (``representable``). The roster is the one building,
    and the column is exhaustive for it, so an unflagged hour is asserted clean.
    """
    asset = f"LEAD_{building}"
    observed = rows.dropna(subset=[_LEAD_READING]).sort_values(_LEAD_TIME)
    stamps = [
        moment.to_pydatetime().replace(tzinfo=UTC)
        for moment in pd.to_datetime(observed[_LEAD_TIME])
    ]
    flag_at = dict(zip(stamps, (bool(value) for value in observed[_LEAD_LABEL]), strict=True))
    kept = representable(
        list(zip(stamps, observed[_LEAD_READING].astype(float).tolist(), strict=True))
    )
    lines = counter_rows(kept, meter_id=asset, unit=_LEAD_UNIT)
    return Case(
        name=f"building-{building}",
        inputs=[CaseInput(RATE_FORMAT, "\n".join([RATE_HEADER, *lines]) + "\n")],
        truth=GroundTruth.from_timeline(
            [(stamp, flag_at[stamp]) for stamp, _ in kept], asset_id=asset
        ),
    )


def _convert_lead(source: Path) -> list[Corpus]:
    path = source / _LEAD_FILE
    if not path.is_file():
        raise CorpusUnavailable(
            f"{_LEAD_FILE} not found under {source}. Obtain LEAD1.0 from {LEAD_URL} and pass "
            "--source <directory holding it>."
        )
    frame = pd.read_csv(path)
    cases = [
        _lead_case(int(cast("int", building)), rows)
        for building, rows in frame.groupby(_LEAD_BUILDING, sort=True)
        if bool(rows[_LEAD_READING].notna().any())
    ]
    provenance = f"LEAD1.0 (Gulati & Arjunan, CIKM 2022) — {LEAD_URL}"
    return [
        Corpus(
            name="lead",
            description=(
                "LEAD1.0: a year of hourly electricity on commercial buildings, with "
                "human-labelled anomalous hours"
            ),
            cases=cases,
            provenance=provenance,
            licence=LEAD_LICENCE,
        ),
        Corpus(
            name="lead_sample",
            description=f"Every {_LEAD_SAMPLE_EVERY}th building of LEAD1.0, for a quick run",
            cases=cases[::_LEAD_SAMPLE_EVERY],
            provenance=provenance,
            licence=LEAD_LICENCE,
        ),
    ]


# Where SKAB sits in the internship workspace, as a convenience default only — a standalone
# clone of this repository has no such tree and must pass --source. LEAD likewise.
_WORKSPACE_SKAB = REPO_ROOT.parent / "reference" / "team3-telemetry-ad" / "Datasets" / "SKAB"
_WORKSPACE_LEAD = (
    REPO_ROOT.parent
    / "ML-Based-Anomaly-and-Attack-Detection-for-Smart-Meter-Telemetry"
    / "data/raw"
)

DATASETS: dict[str, Dataset] = {
    "lead": Dataset(
        describe="LEAD1.0 — hourly building electricity with human-labelled anomalies",
        url=LEAD_URL,
        licence=LEAD_LICENCE,
        corpora=("lead", "lead_sample"),
        default_source=_WORKSPACE_LEAD,
        convert=_convert_lead,
    ),
    "skab": Dataset(
        describe="SKAB — Skoltech Anomaly Benchmark (valve faults + anomaly-free)",
        url=SKAB_URL,
        licence=SKAB_LICENCE,
        corpora=tuple(_SKAB_SUBSETS),
        default_source=_WORKSPACE_SKAB,
        convert=_convert_skab,
    ),
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
