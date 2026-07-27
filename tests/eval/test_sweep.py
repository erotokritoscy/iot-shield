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


class TestQualifiedNames:
    """``detector.param`` addresses one detector, because a param name is unique only inside one.

    Once a spec could hold several detectors, `threshold` stopped identifying anything: it is
    80.0 on the telemetry lane and 20 distinct peers on the flow lane. Qualifying resolves that
    rather than routing around it, which is what makes every knob of every detector in a
    multi-detector spec reachable.
    """

    def mixed(self) -> PipelineSpec:
        return spec(
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
        )

    def test_a_qualified_name_reaches_only_the_named_detector(self) -> None:
        # The whole point: both detectors declare `threshold`, and exactly one moves.
        _, swept = expand([("mix", self.mixed())], {"flow_burst.threshold": [30]})[0]
        assert swept.detectors[0].params == {"threshold": 80.0}  # untouched control
        assert swept.detectors[1].params == {"preset": "fan_out", "threshold": 30}

    def test_the_other_detectors_knob_is_reachable_too(self) -> None:
        _, swept = expand([("mix", self.mixed())], {"stub_threshold.threshold": [10]})[0]
        assert swept.detectors[0].params == {"threshold": 10}
        assert swept.detectors[1].params == {"preset": "fan_out"}

    def test_the_label_quotes_the_key_so_a_row_says_which_detector_moved(self) -> None:
        expanded = expand([("mix", self.mixed())], {"flow_burst.threshold": [10, 30]})
        assert [label for label, _ in expanded] == [
            "mix[flow_burst.threshold=10]",
            "mix[flow_burst.threshold=30]",
        ]

    def test_unqualified_names_are_completely_unchanged(self) -> None:
        """The compatibility pin: qualification is additive.

        Every committed spec, CLI invocation and published label uses the bare form, so if this
        drifts, measured numbers in the README and the ADRs stop being reproducible.
        """
        expanded = expand([("team3", spec())], {"window_size": [20, 60]})
        assert [label for label, _ in expanded] == [
            "team3[window_size=20]",
            "team3[window_size=60]",
        ]

    def test_a_qualified_name_a_spec_lacks_leaves_that_spec_as_the_control(self) -> None:
        # Same rule the unqualified form already had: a spec without the detector rides along
        # once, unlabelled, rather than expanding into N identical rows.
        pairs = [("mix", self.mixed()), ("team3", spec())]
        assert [label for label, _ in expand(pairs, {"flow_burst.threshold": [10, 30]})] == [
            "mix[flow_burst.threshold=10]",
            "mix[flow_burst.threshold=30]",
            "team3",
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

    def test_an_unqualified_name_two_detectors_accept_is_refused_with_the_remedy(self) -> None:
        # `threshold` is declared by both stub_threshold and flow_burst, so sweeping it over
        # a spec holding both would vary two knobs (different units) under one label. Still
        # refused — but the message now names the qualified form, which is a real way out
        # rather than "pick a different parameter".
        base = spec(
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
        )
        with pytest.raises(ValueError, match="more than one detector") as raised:
            expand([("mix", base)], {"threshold": [10, 20]})
        assert "stub_threshold.threshold" in str(raised.value)
        assert "flow_burst.threshold" in str(raised.value)

    def test_a_qualified_name_for_an_absent_detector_names_the_ones_present(self) -> None:
        with pytest.raises(ValueError, match=r"nosuch.*not in any spec"):
            expand([("t", spec())], {"nosuch.window_size": [20]})

    def test_a_detector_that_does_not_accept_the_parameter_is_refused(self) -> None:
        # Distinct from a typo'd parameter: the detector is real, the knob is not its.
        with pytest.raises(ValueError, match=r"'flow_burst' does not accept.*'window_size'"):
            expand(
                [("f", spec({"name": "flow_burst", "params": {}}))],
                {"flow_burst.window_size": [20]},
            )

    def test_a_repeated_adapter_cannot_be_separated_by_name(self) -> None:
        """Two instances of one adapter in one spec is legal and deliberate (ADR-0023 D9) —
        two presets in a single run. A *name* cannot tell them apart, so a qualified sweep is
        still ambiguous and is refused rather than varying both under one label."""
        base = spec(
            {"name": "log_burst", "params": {"preset": "auth_failure"}},
            {"name": "log_burst", "params": {"preset": "severity_spike"}},
        )
        with pytest.raises(ValueError, match=r"ambiguous.*appears 2 times"):
            expand([("two-presets", base)], {"log_burst.min_count": [3, 5]})

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
