"""Converting a third-party dataset into bundles, without redistributing it.

SKAB is GPLv3, so it ships as a conversion step rather than committed bytes (ADR-0021 D5).
These tests build a miniature SKAB-shaped tree so they run on any machine — the point under
test is the *converter*, not whether this particular laptop has a SKAB checkout.
"""

from pathlib import Path

import pytest

from iotshield.eval import CorpusUnavailable, list_corpora, load_corpus
from iotshield.eval.fetch import DATASETS, SKAB_URL, fetch

HEADER = "datetime;Pressure;Temperature;anomaly;changepoint"


def write_skab(root: Path, subdirectory: str, name: str, *, faulty_rows: range | None) -> None:
    """A miniature recording in SKAB's own dialect: semicolon CSV, labels inline."""
    rows = [HEADER]
    for index in range(40):
        flagged = faulty_rows is not None and index in faulty_rows
        pressure = 90.0 if flagged else 10.0
        rows.append(f"2020-03-09 10:{index:02d}:00;{pressure};75.0;{1 if flagged else 0};0.0")
    target = root / subdirectory
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture
def skab_source(tmp_path: Path) -> Path:
    source = tmp_path / "SKAB"
    write_skab(source, "valve1", "0.csv", faulty_rows=range(10, 20))
    write_skab(source, "valve1", "1.csv", faulty_rows=range(25, 30))
    write_skab(source, "anomaly-free", "anomaly-free.csv", faulty_rows=None)
    return source


class TestConversion:
    def test_it_writes_one_bundle_per_subset(self, skab_source: Path, tmp_path: Path) -> None:
        root = tmp_path / "corpora"
        written = dict(fetch("skab", root, skab_source))
        assert written == {"skab_valve1": 2, "skab_clean": 1}

    def test_the_converted_corpora_are_then_discoverable(
        self, skab_source: Path, tmp_path: Path
    ) -> None:
        # The payoff of one format: after conversion SKAB is data like any other corpus,
        # with no loader and no registry entry anywhere.
        root = tmp_path / "corpora"
        fetch("skab", root, skab_source)
        assert {info.name for info in list_corpora(root)} == {"skab_valve1", "skab_clean"}

    def test_labels_survive_as_intervals(self, skab_source: Path, tmp_path: Path) -> None:
        root = tmp_path / "corpora"
        fetch("skab", root, skab_source)
        corpus = load_corpus("skab_valve1", root)
        by_name = {case.name: case for case in corpus.cases}
        # Rows 10-19 flagged -> one contiguous incident, not ten.
        assert len(by_name["valve1-0"].truth.incidents) == 1
        assert len(by_name["valve1-1"].truth.incidents) == 1

    def test_a_clean_recording_converts_to_zero_incidents(
        self, skab_source: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "corpora"
        fetch("skab", root, skab_source)
        (case,) = load_corpus("skab_clean", root).cases
        assert case.truth.is_clean
        # Still in the roster with a row clock, which is what makes it *asserted clean*
        # rather than unknown — the difference between scoring false alarms and ignoring them.
        assert case.truth.clean_assets == frozenset({"skab-testbed"})
        assert all(asset.clock for asset in case.truth.assets)

    def test_the_label_column_is_not_fed_to_the_pipeline(
        self, skab_source: Path, tmp_path: Path
    ) -> None:
        # The answer key must never reach the detector, or every benchmark is cheating.
        root = tmp_path / "corpora"
        fetch("skab", root, skab_source)
        text = load_corpus("skab_valve1", root).cases[0].inputs[0].text
        assert "anomaly" not in text and "changepoint" not in text

    def test_provenance_and_licence_are_recorded(self, skab_source: Path, tmp_path: Path) -> None:
        # The whole reason this path exists: a bundle must be able to say what it is.
        root = tmp_path / "corpora"
        fetch("skab", root, skab_source)
        corpus = load_corpus("skab_valve1", root)
        assert SKAB_URL in corpus.provenance
        assert "GPL-3.0" in corpus.licence


class TestFailures:
    def test_an_unknown_dataset_names_the_known_ones(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="known: skab"):
            fetch("nope", tmp_path, tmp_path)

    def test_a_missing_source_points_at_the_upstream_project(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusUnavailable, match=SKAB_URL):
            fetch("skab", tmp_path, tmp_path / "absent")

    def test_a_source_without_the_expected_subsets_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(CorpusUnavailable, match="no SKAB subsets"):
            fetch("skab", tmp_path / "out", empty)


def test_every_registered_dataset_declares_its_terms() -> None:
    # A dataset we convert but cannot redistribute has to carry its licence forward, or the
    # bundles it produces are unusable by anyone else.
    for name, dataset in DATASETS.items():
        assert dataset.url and dataset.licence and dataset.describe, name
