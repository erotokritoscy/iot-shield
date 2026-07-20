"""The ingest memo must not let one spec be scored against another spec's events.

`evaluate()` shares an ingest cache across the specs of one call, so two specs that ingest
*differently* must key differently. Keying on the normalizer's name alone was correct only
while normalizers took no params; once they did (ADR-0023 D8), two flow specs differing only
in `home_networks` collided and the second was scored against the first's events — a
benchmark publishing a number it never measured, which is the one failure this harness
exists to prevent.
"""

from iotshield.eval.corpus import Case, CaseInput
from iotshield.eval.runner import _ingest_key
from iotshield.pipeline import PipelineSpec
from tests.eval.conftest import truth_of

CASE = Case(name="c", inputs=[CaseInput("flow-csv", "")], truth=truth_of([False, True]))


def spec_with(**source: object) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {
            "sources": [{"normalizer": "flow", "source_format": "flow-csv", **source}],
            "detectors": [{"name": "flow_burst"}],
        }
    )


def test_differing_normalizer_params_do_not_share_an_ingest_entry() -> None:
    narrow = spec_with(params={"home_networks": ["10.0.0.0/8"]})
    wide = spec_with(params={"home_networks": ["0.0.0.0/0"]})
    assert _ingest_key(narrow, CASE) != _ingest_key(wide, CASE)


def test_identical_sources_still_share_one_ingest_entry() -> None:
    # The memo must keep paying off — this is what makes a sweep cheap, since a sweep varies
    # only detector params and those never influence ingest.
    first = spec_with(params={"home_networks": ["10.0.0.0/8"]})
    second = spec_with(params={"home_networks": ["10.0.0.0/8"]})
    assert _ingest_key(first, CASE) == _ingest_key(second, CASE)


def test_param_order_does_not_split_the_cache() -> None:
    # Shares config_fingerprint's canonicalization, so two equal mappings written in a
    # different order are one entry rather than two.
    a = spec_with(params={"home_networks": ["10.0.0.0/8"], "unused": 1})
    b = spec_with(params={"unused": 1, "home_networks": ["10.0.0.0/8"]})
    assert _ingest_key(a, CASE) == _ingest_key(b, CASE)


def test_detector_params_are_still_excluded() -> None:
    """The memo's whole point: a sweep varies detectors and reuses one ingest."""
    base = spec_with()
    swept = PipelineSpec.model_validate(
        {
            "sources": [{"normalizer": "flow", "source_format": "flow-csv"}],
            "detectors": [{"name": "flow_burst", "params": {"threshold": 99}}],
        }
    )
    assert _ingest_key(base, CASE) == _ingest_key(swept, CASE)
