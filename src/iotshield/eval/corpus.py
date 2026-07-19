"""Labelled corpora: the datasets a benchmark runs on, and how a researcher adds one.

A **case** is one labelled example — the raw input text(s) a run would ingest, plus where
the faults really were. A **corpus** is a named set of cases. Corpora register in ``CORPORA``
by name, deliberately the same shape as ``DETECTORS`` and ``NORMALIZERS`` in the pipeline
registry, so "bring your own labelled dataset" is the same gesture as "bring your own
adapter" (ADR-0021 D5).

This is **not** a fifth port: no Protocol, no port contract suite. Ports are the pipeline's
runtime joints, and nothing here ever runs at product runtime — a loader is a dev-time
function, so a registry plus a dataclass is the right altitude.

Two kinds ship. ``demo_micro`` is committed and needs nothing external, so the harness works
on a fresh clone and in CI. The SKAB corpora read the read-only reference tree, which a
clone of this repo does not have — they raise ``CorpusUnavailable``, which the CLI reports
as a skip rather than a crash.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from iotshield.eval.truth import GroundTruth

# SKAB ships as semicolon-separated CSV with the labels inline; these are its label columns.
_SKAB_SEPARATOR = ";"
_SKAB_LABEL = "anomaly"
_SKAB_TIME = "datetime"
_SKAB_ASSET = "skab-testbed"
_SKAB_DROP = (_SKAB_LABEL, "changepoint")

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SKAB_PATH = _REPO_ROOT.parent / "reference" / "team3-telemetry-ad" / "Datasets" / "SKAB"
"""Where SKAB lives in the workspace. Absent in a standalone clone — hence CorpusUnavailable."""


class CorpusUnavailable(RuntimeError):
    """A corpus needs data that is not on this machine (external dataset, wrong path)."""


@dataclass(frozen=True)
class Case:
    """One labelled example: what to ingest, and where the faults really were.

    ``inputs`` is a list to align 1:1 with ``PipelineSpec.sources`` exactly as the runner
    expects, so a future multimodal corpus needs no change here — only a corpus that
    supplies two inputs per case.
    """

    name: str
    inputs: list[str]
    truth: GroundTruth
    holdout: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Corpus:
    name: str
    description: str
    cases: list[Case]

    @property
    def incidents(self) -> int:
        return sum(len(case.truth.incidents) for case in self.cases)


@dataclass(frozen=True)
class CorpusSpec:
    """A registered corpus: how to load it, and whether it needs data we do not ship."""

    loader: Callable[[Path | None], Corpus]
    description: str
    needs_external_data: bool = False


def _to_csv_text(frame: pd.DataFrame, timestamps: Sequence[datetime], asset_id: str) -> str:
    """Render a wide channel frame as the long CSV the ``stub`` normalizer ingests.

    The inverse of that normalizer's fan-out: one line per (timestamp, channel) reading,
    which is the contract's one-value-per-event rule (ADR-0003 D1). Going through real CSV
    text rather than constructing events directly is deliberate — the benchmark then
    exercises normalization too, so a corpus measures the *pipeline*, not just a detector.
    """
    lines = [
        f"{moment.isoformat()},{asset_id},{channel},{frame[channel].iloc[index]}"
        for index, moment in enumerate(timestamps)
        for channel in frame.columns
    ]
    return "\n".join(lines) + "\n"


def _skab_case(path: Path) -> Case:
    frame = pd.read_csv(path, sep=_SKAB_SEPARATOR)
    timestamps = [
        moment.to_pydatetime().replace(tzinfo=UTC) for moment in pd.to_datetime(frame[_SKAB_TIME])
    ]
    labels = (
        [bool(value) for value in frame[_SKAB_LABEL]]
        if _SKAB_LABEL in frame.columns
        else [False] * len(frame)
    )
    channels = frame.drop(columns=[_SKAB_TIME, *(c for c in _SKAB_DROP if c in frame.columns)])
    return Case(
        name=f"{path.parent.name}/{path.name}",
        inputs=[_to_csv_text(channels, timestamps, _SKAB_ASSET)],
        truth=GroundTruth.from_timeline(list(zip(timestamps, labels, strict=True))),
    )


def _skab_loader(subdirectory: str, description: str) -> Callable[[Path | None], Corpus]:
    def load(root: Path | None) -> Corpus:
        base = (root or DEFAULT_SKAB_PATH) / subdirectory
        if not base.is_dir():
            raise CorpusUnavailable(
                f"SKAB directory not found: {base}. Point --corpus-path at your SKAB "
                "checkout (the reference dataset is not shipped with this repository)."
            )
        files = sorted(base.glob("*.csv"))
        if not files:
            raise CorpusUnavailable(f"no CSV files under {base}")
        return Corpus(
            name=f"skab_{subdirectory}",
            description=description,
            cases=[_skab_case(path) for path in files],
        )

    return load


def _demo_micro(_: Path | None) -> Corpus:
    """The committed micro-corpus: the demo telemetry slice plus its labels.

    Small on purpose — it exists so the harness runs, and is tested, on a fresh clone with
    no external dataset. It is the same input ``config/demo_team3.yaml`` uses, so a
    benchmark on it is directly comparable to the demo everyone has already seen.
    """
    csv_path = _REPO_ROOT / "data" / "demo" / "demo_team3.csv"
    labels_path = _REPO_ROOT / "data" / "eval" / "demo_team3.labels.json"
    if not labels_path.is_file():
        raise CorpusUnavailable(f"labels missing: {labels_path}")
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    timeline = [
        (datetime.fromisoformat(moment), bool(flagged)) for moment, flagged in payload["timeline"]
    ]
    return Corpus(
        name="demo_micro",
        description=payload.get("description", "committed demo slice"),
        cases=[
            Case(
                name=payload.get("case", "demo_team3"),
                inputs=[csv_path.read_text(encoding="utf-8-sig")],
                truth=GroundTruth.from_timeline(timeline),
            )
        ],
    )


CORPORA: dict[str, CorpusSpec] = {
    "demo_micro": CorpusSpec(_demo_micro, "committed demo slice (1 case) - always available"),
    "skab_valve1": CorpusSpec(
        _skab_loader("valve1", "SKAB valve1 closure faults (16 recordings, one fault each)"),
        "SKAB valve1 closure faults (16 recordings)",
        needs_external_data=True,
    ),
    "skab_valve2": CorpusSpec(
        _skab_loader("valve2", "SKAB valve2 faults (4 recordings, one fault each)"),
        "SKAB valve2 faults (4 recordings)",
        needs_external_data=True,
    ),
    "skab_clean": CorpusSpec(
        _skab_loader("anomaly-free", "SKAB anomaly-free recording (no faults at all)"),
        "SKAB anomaly-free recording (false-alarm rate)",
        needs_external_data=True,
    ),
}


def load_corpus(name: str, root: Path | None = None) -> Corpus:
    """Load a registered corpus by name, or raise naming the known alternatives."""
    try:
        spec = CORPORA[name]
    except KeyError:
        known = ", ".join(sorted(CORPORA))
        raise ValueError(f"unknown corpus {name!r}; known: {known}") from None
    return spec.loader(root)
