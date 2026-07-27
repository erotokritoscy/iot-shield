"""Corpora as bundles: the committed one, discovery, and the round-trip.

The property under test is the one that made bundles worth building: we push ``iotshield/``
only, so a researcher who clones it must get working corpora with no other setup. Every test
here runs against data that ships in the repository.
"""

import json
from pathlib import Path

import pytest

from iotshield.eval import Corpus, CorpusUnavailable, list_corpora, load_bundle, load_corpus
from iotshield.eval.bundle import MANIFEST_NAME, BundleError, read_manifest
from iotshield.eval.corpus import CORPUS_PATH_ENV, DEFAULT_CORPUS_ROOT, corpus_root, write_bundle

COMMITTED = "synthetic"


class TestCommittedCorpus:
    """Ships in the repository, so this suite never skips and a fresh clone can benchmark."""

    def test_the_committed_corpus_is_present(self) -> None:
        assert (DEFAULT_CORPUS_ROOT / COMMITTED / MANIFEST_NAME).is_file()

    def test_it_loads_with_readings_labels_and_faults(self) -> None:
        corpus = load_corpus(COMMITTED)
        assert corpus.cases
        assert corpus.incidents > 0
        for case in corpus.cases:
            assert case.inputs and case.inputs[0].text.strip()
            # Every case names its roster, and every listed asset carries its own row clock
            # to score point metrics over (ADR-0026).
            assert case.truth.assets
            assert all(asset.clock for asset in case.truth.assets)

    def test_it_carries_provenance_and_licence(self) -> None:
        # A bundle that cannot say where it came from is one nobody can safely reuse — the
        # whole reason SKAB ships as a fetch step rather than committed bytes.
        corpus = load_corpus(COMMITTED)
        assert corpus.provenance and corpus.licence

    def test_it_contains_clean_recordings_as_well_as_faulty_ones(self) -> None:
        # Both are needed: faults score recall, clean recordings score false alarms, and a
        # corpus of only faults cannot measure the second at all.
        corpus = load_corpus(COMMITTED)
        assert any(case.truth.is_clean for case in corpus.cases)
        assert any(not case.truth.is_clean for case in corpus.cases)

    def test_inputs_are_tagged_with_a_real_source_format(self) -> None:
        for case in load_corpus(COMMITTED).cases:
            assert case.inputs[0].source_format


class TestDiscovery:
    def test_listing_finds_the_committed_corpus(self) -> None:
        assert COMMITTED in {info.name for info in list_corpora()}

    def test_listing_reads_manifests_only(self, tmp_path: Path) -> None:
        # Cheap by construction: a picker must be able to show a 16-recording corpus's size
        # without decompressing megabytes of readings.
        corpus = load_corpus(COMMITTED)
        write_bundle(tmp_path / "copy", corpus)
        (info,) = list_corpora(tmp_path)
        assert info.cases == len(corpus.cases)

    def test_a_directory_without_a_manifest_is_ignored(self, tmp_path: Path) -> None:
        # The corpus root is a place people put things; one stray folder must not break a
        # listing.
        (tmp_path / "not-a-corpus").mkdir()
        assert list_corpora(tmp_path) == []

    def test_unknown_corpus_names_what_is_available(self, tmp_path: Path) -> None:
        write_bundle(tmp_path / "only", load_corpus(COMMITTED))
        with pytest.raises(CorpusUnavailable, match="available: "):
            load_corpus("nope", tmp_path)

    def test_a_fetchable_corpus_says_how_to_get_it(self, tmp_path: Path) -> None:
        # Not "unknown": a dataset nobody has fetched yet is a normal state on a fresh
        # clone, and the message must be the command rather than a dead end.
        with pytest.raises(CorpusUnavailable, match="fetch skab"):
            load_corpus("skab_valve1", tmp_path)

    def test_a_broken_bundle_is_listed_with_its_reason_rather_than_hidden(
        self, tmp_path: Path
    ) -> None:
        """Listed-but-unloadable, because vanishing is worse than being visibly broken.

        A broken bundle used to be skipped by every listing, so it was absent from `eval
        list`, from `/eval/corpora` and from the dashboard picker — and the only feedback was
        a later "unknown corpus" error that never mentioned the directory sitting on disk.
        """
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        (info,) = list_corpora(tmp_path)
        assert info.name == "broken" and info.cases == 0
        assert "invalid" in info.error
        with pytest.raises(BundleError):
            read_manifest(tmp_path / "broken")

    def test_asking_for_a_broken_bundle_by_name_raises_rather_than_saying_unknown(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(BundleError):
            load_corpus("broken", tmp_path)


class TestRoundTrip:
    def test_write_then_load_preserves_everything_a_benchmark_needs(self, tmp_path: Path) -> None:
        original = load_corpus(COMMITTED)
        write_bundle(tmp_path / "copy", original)
        restored = load_bundle(tmp_path / "copy")

        assert restored.name == original.name
        assert restored.incidents == original.incidents
        assert len(restored.cases) == len(original.cases)
        for before, after in zip(original.cases, restored.cases, strict=True):
            assert after.name == before.name
            assert [i.text for i in after.inputs] == [i.text for i in before.inputs]
            assert [i.source_format for i in after.inputs] == [
                i.source_format for i in before.inputs
            ]
            # Every part of the answer key survives: the roster, each asset's own row clock,
            # and the intervals with their descriptive kind/fault_id/lanes.
            assert after.truth.assets == before.truth.assets

    def test_writing_is_reproducible(self, tmp_path: Path) -> None:
        # Byte-identical on rewrite, so regenerating a committed bundle is not a diff. Gzip
        # stamps an mtime by default, which would make every rebuild look like a change.
        corpus = load_corpus(COMMITTED)
        write_bundle(tmp_path / "a", corpus)
        write_bundle(tmp_path / "b", corpus)
        for left in sorted((tmp_path / "a").rglob("*")):
            if left.is_file():
                right = tmp_path / "b" / left.relative_to(tmp_path / "a")
                assert left.read_bytes() == right.read_bytes(), left.name

    def test_readings_and_labels_are_compressed(self, tmp_path: Path) -> None:
        # Uncompressed labels were measured *larger* than the readings they annotate; a
        # repository is a bad place for megabytes that compress away.
        write_bundle(tmp_path / "copy", load_corpus(COMMITTED))
        payloads = list((tmp_path / "copy" / "cases").iterdir())
        assert payloads and all(path.suffix == ".gz" for path in payloads)


class TestCorpusRoot:
    def test_defaults_to_the_repository(self) -> None:
        assert corpus_root() == DEFAULT_CORPUS_ROOT

    def test_env_overrides_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The 12-factor seam a container needs — and the reason a caller never names a path:
        # a request carrying one is a request to read arbitrary files.
        monkeypatch.setenv(CORPUS_PATH_ENV, str(tmp_path))
        assert corpus_root() == tmp_path

    def test_an_explicit_argument_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CORPUS_PATH_ENV, str(tmp_path / "env"))
        assert corpus_root(tmp_path / "explicit") == tmp_path / "explicit"

    def test_a_missing_root_lists_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        assert list_corpora(tmp_path / "absent") == []


def test_a_corpus_can_be_dropped_in_without_touching_code(tmp_path: Path) -> None:
    """The extensibility claim, tested: adding a corpus is adding *data*.

    No registry entry, no loader function, no import — write a bundle into the root and it
    is discoverable and loadable. This is the difference between a harness a researcher can
    extend and one only its author can.
    """
    source = load_corpus(COMMITTED)
    mine = Corpus(
        name="my-dataset",
        description="a researcher's own labelled data",
        cases=source.cases[:2],
        provenance="somewhere",
        licence="mine",
    )
    write_bundle(tmp_path / "my-dataset", mine)

    assert "my-dataset" in {info.name for info in list_corpora(tmp_path)}
    assert len(load_corpus("my-dataset", tmp_path).cases) == 2


class TestBundlesAreUntrustedData:
    """A bundle is a drop-in a third party supplies, so it may only read itself.

    That is the premise of "discovered, not registered": `data/eval` is a bind mount in
    docker-compose, and the eval API loads whatever is under the corpus root. Joining a
    manifest's `path` to the bundle directory without a containment check made `../secret.txt`
    a way to read any file the server can and return it inside a benchmark report.
    """

    def _bundle_naming(self, tmp_path: Path, payload_path: str) -> Path:
        directory = tmp_path / "hostile"
        (directory / "cases").mkdir(parents=True)
        (directory / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "name": "hostile",
                    "description": "d",
                    "provenance": "p",
                    "licence": "l",
                    "cases": [
                        {
                            "name": "c",
                            "inputs": [{"source_format": "stub-csv", "path": payload_path}],
                            "labels": "cases/c.labels.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return directory

    def test_a_relative_escape_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "SECRET.txt").write_text("host secret", encoding="utf-8")
        directory = self._bundle_naming(tmp_path, "../SECRET.txt")
        with pytest.raises(BundleError, match="outside its bundle"):
            load_bundle(directory)

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        secret = tmp_path / "SECRET.txt"
        secret.write_text("host secret", encoding="utf-8")
        directory = self._bundle_naming(tmp_path, str(secret))
        with pytest.raises(BundleError, match="outside its bundle"):
            load_bundle(directory)

    def test_an_ordinary_payload_path_still_loads(self, tmp_path: Path) -> None:
        # Guard the guard: a containment check that refused everything would pass the two
        # tests above while breaking every real bundle.
        original = load_corpus(COMMITTED)
        write_bundle(tmp_path / "copy", original)
        assert load_bundle(tmp_path / "copy").cases
