"""Sweep expansion: one spec plus a range of hyperparameter values -> N labelled specs.

The mechanism is deliberately dumb — it rewrites specs and hands them to the same
``evaluate`` a head-to-head uses — so what needs pinning is the *contract around* it: which
detectors an assignment reaches, what the labels read, and that a bad or huge sweep is
refused before anything runs rather than after.
"""

from pathlib import Path
from typing import Any

import pytest

from iotshield.eval import evaluate, load_corpus
from iotshield.eval.sweep import MAX_EXPANSION, expand, values
from iotshield.pipeline import PipelineSpec, SourceSpec, load_spec

STUB_SPEC = Path("config/bench/stub.yaml")


def spec(*detectors: dict) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {
            "sources": [{"normalizer": "stub", "source_format": "stub-csv"}],
            "detectors": list(detectors) or [{"name": "team3_zscore", "params": {}}],
            "alert_engine": {"name": "weighted"},
        }
    )


class TestExpansion:
    def test_no_sweep_returns_the_spec_untouched(self) -> None:
        original = spec()
        assert expand([("team3", original)], {}) == [("team3", original)]

    def test_one_parameter_yields_one_spec_per_value(self) -> None:
        expanded = expand([("team3", spec())], {"window_size": [20, 60]})
        assert [label for label, _ in expanded] == [
            "team3[window_size=20]",
            "team3[window_size=60]",
        ]
        assert [s.detectors[0].params["window_size"] for _, s in expanded] == [20, 60]

    def test_two_parameters_cross_product(self) -> None:
        expanded = expand([("t", spec())], {"window_size": [20, 60], "percentile": [99.0]})
        assert [label for label, _ in expanded] == [
            "t[window_size=20,percentile=99.0]",
            "t[window_size=60,percentile=99.0]",
        ]

    def test_existing_params_survive(self) -> None:
        base = spec({"name": "team3_zscore", "params": {"min_segment_length": 3}})
        _, swept = expand([("t", base)], {"window_size": [20]})[0]
        assert swept.detectors[0].params == {"min_segment_length": 3, "window_size": 20}

    def test_the_original_spec_is_not_mutated(self) -> None:
        base = spec()
        expand([("t", base)], {"window_size": [20, 60]})
        assert base.detectors[0].params == {}

    def test_only_detectors_that_declare_the_parameter_are_touched(self) -> None:
        # Sweeping across a mixed spec tunes the one that has the knob and leaves the
        # other exactly as configured — so it stays the control.
        base = spec(
            {"name": "team3_zscore", "params": {}},
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
        )
        _, swept = expand([("mix", base)], {"window_size": [20]})[0]
        assert swept.detectors[0].params == {"window_size": 20}
        assert swept.detectors[1].params == {"threshold": 80.0}

    def test_one_sweep_applies_across_several_specs(self) -> None:
        pairs = [("a", spec()), ("b", spec())]
        assert [label for label, _ in expand(pairs, {"window_size": [20, 60]})] == [
            "a[window_size=20]",
            "a[window_size=60]",
            "b[window_size=20]",
            "b[window_size=60]",
        ]

    def test_a_spec_that_accepts_nothing_swept_stays_the_control(self) -> None:
        # `--spec team3 --spec stub --sweep window_size=...`: the stub has no window, so
        # it rides along ONCE, unlabelled, as the fixed reference the swept rows move
        # against. Expanding it into N identical rows would be noise.
        pairs = [
            ("team3", spec()),
            ("stub", spec({"name": "stub_threshold", "params": {"threshold": 80.0}})),
        ]
        assert [label for label, _ in expand(pairs, {"window_size": [20, 60]})] == [
            "team3[window_size=20]",
            "team3[window_size=60]",
            "stub",
        ]


class TestRefusals:
    def test_a_parameter_no_spec_accepts_names_the_alternatives(self) -> None:
        # Still a typo if NO spec has it — validated over the union, so a control spec
        # cannot make a real sweep fail but a misspelling is still caught.
        with pytest.raises(ValueError, match=r"window_siz.*declare"):
            expand([("t", spec())], {"window_siz": [20]})

    def test_a_sweep_with_no_values_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no values"):
            expand([("t", spec())], {"window_size": []})

    def test_a_name_two_detectors_in_one_spec_accept_is_refused(self) -> None:
        # `threshold` is declared by both stub_threshold and flow_burst, so sweeping it over
        # a spec holding both would vary two knobs (different units) under one label. Fail
        # loud rather than silently — the FLOW lane made this collision reachable.
        base = spec(
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
        )
        with pytest.raises(ValueError, match="more than one detector"):
            expand([("mix", base)], {"threshold": [10, 20]})

    def test_an_oversized_cross_product_is_refused_before_running(self) -> None:
        # The cap exists because the caller is often a button: a stray comma must not
        # launch hundreds of benchmarks on someone else's server.
        with pytest.raises(ValueError, match=f"limit {MAX_EXPANSION}"):
            expand([("t", spec())], {"window_size": list(range(MAX_EXPANSION + 1))})


class TestValues:
    """Shared by the CLI's --sweep and the dashboard's box, so they cannot disagree."""

    def test_reads_the_narrowest_type_that_fits(self) -> None:
        # Typing here is what makes a label read `window_size=20`, not `window_size='20'`.
        assert values("20, 99.5,true, False , auth") == [20, 99.5, True, False, "auth"]

    def test_blank_items_and_whitespace_are_dropped(self) -> None:
        assert values(" 20 , , 60,") == [20, 60]


class TestIngestIsSharedAcrossSpecs:
    """Swept specs differ only in detector params, so the corpus is parsed once."""

    def test_the_corpus_is_normalized_once_per_case_not_once_per_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The saving the phase split (ADR-0021) exists to make possible: a 3-value sweep
        # over a 16-case corpus re-parsed 163k rows three times for identical output.
        from iotshield.eval import runner as runner_module

        calls = 0
        real = runner_module.normalize_sources

        def counting(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(runner_module, "normalize_sources", counting)
        corpus = load_corpus("synthetic")
        specs = expand([("stub", load_spec(STUB_SPEC))], {"threshold": [40.0, 80.0, 120.0]})
        evaluate(corpus, specs)
        assert len(specs) == 3
        assert calls == len(corpus.cases)  # not 3x that

    def test_specs_with_different_sources_still_ingest_separately(self) -> None:
        # The key includes the source chain, so the memo can never hand one spec another's
        # events just because they share a case name.
        from iotshield.eval.runner import _ingest_key

        corpus = load_corpus("synthetic")
        case = corpus.cases[0]
        stub = load_spec(STUB_SPEC)
        syslog = stub.model_copy(
            update={"sources": [SourceSpec(normalizer="syslog", source_format="syslog")]}
        )
        assert _ingest_key(stub, case) != _ingest_key(syslog, case)
