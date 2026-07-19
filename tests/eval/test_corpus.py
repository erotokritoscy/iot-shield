"""Corpora: the committed micro-corpus, and graceful absence of the external ones.

The availability behaviour is the load-bearing part. We push ``iotshield/`` only, so a
researcher who clones this repo has no ``reference/`` tree and therefore no SKAB — the
harness must skip cleanly rather than crash, and one corpus must work with nothing external
so the tooling is demoable and testable on a fresh clone.
"""

from pathlib import Path

import pytest

from iotshield.eval import CORPORA, CorpusUnavailable, load_corpus
from iotshield.eval.corpus import DEFAULT_SKAB_PATH

EXTERNAL = sorted(name for name, spec in CORPORA.items() if spec.needs_external_data)


class TestRegistry:
    def test_unknown_corpus_names_the_alternatives(self) -> None:
        with pytest.raises(ValueError, match="unknown corpus"):
            load_corpus("does-not-exist")

    def test_every_registered_corpus_is_described(self) -> None:
        # The description is the researcher's menu — an entry without one is unusable.
        assert all(spec.description for spec in CORPORA.values())

    def test_at_least_one_corpus_needs_no_external_data(self) -> None:
        assert any(not spec.needs_external_data for spec in CORPORA.values())


class TestMicroCorpus:
    """The committed corpus: always available, so this suite never skips."""

    def test_loads_with_labels_and_content(self) -> None:
        corpus = load_corpus("demo_micro")
        (case,) = corpus.cases
        assert case.inputs and case.inputs[0].text.strip()
        assert len(case.truth.timeline) == 300
        assert case.truth.anomalous_rows == 230

    def test_is_one_contiguous_incident(self) -> None:
        # The slice was cut to contain a single sustained valve fault; if that ever stops
        # being true, every "1 incident" expectation in the docs quietly rots.
        (case,) = load_corpus("demo_micro").cases
        assert len(case.truth.incidents) == 1

    def test_inputs_align_with_a_single_source_spec(self) -> None:
        (case,) = load_corpus("demo_micro").cases
        assert len(case.inputs) == 1


@pytest.mark.parametrize("name", EXTERNAL)
class TestExternalCorpora:
    def test_missing_data_raises_corpus_unavailable(self, name: str, tmp_path: Path) -> None:
        # Not a crash and not a ValueError: the CLI distinguishes "you asked for something
        # impossible" (exit 2) from "that dataset is not on this machine" (skip, exit 0).
        with pytest.raises(CorpusUnavailable):
            load_corpus(name, tmp_path)

    def test_loads_when_the_reference_tree_is_present(self, name: str) -> None:
        if not DEFAULT_SKAB_PATH.is_dir():
            pytest.skip("SKAB not present (expected in a standalone clone)")
        corpus = load_corpus(name)
        assert corpus.cases
        assert all(case.truth.timeline for case in corpus.cases)
        # Every case ingests as real CSV text, so a benchmark exercises normalization too.
        assert all(case.inputs[0].text.count(",") for case in corpus.cases)
