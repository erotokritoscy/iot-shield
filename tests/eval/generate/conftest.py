"""One scenario factory, and memoised builds.

Two things every test here needs. A ``Scenario`` literal is 15 lines, and spelling it out per
test meant six near-identical copies that a new field would have to visit one at a time. And
building a corpus is expensive — the three shipped ones cost ~0.25 s each, and the suite was
rebuilding them ~30 times plus re-running ~16 full benchmarks.

Caching is sound *by construction* here rather than by assumption: determinism is the property
the rest of this package asserts, so a second build of the same scenario is the same bytes.
"""

from functools import cache

import pytest

from iotshield.eval import Corpus
from iotshield.eval.generate import SCENARIOS, Clock, Device, FaultSpec, Recording, Scenario, build
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV

CLOCK = Clock(steps=20)
"""Short by default: a fixture asserting a structural property does not need 900 rows."""


def scenario_of(
    *devices: Device,
    formats: tuple[str, ...] = (STUB_CSV,),
    faults: tuple[FaultSpec, ...] = (),
    extra: tuple[Recording, ...] = (),
    clock: Clock = CLOCK,
    name: str = "fixture",
    seed: int = 7,
) -> Scenario:
    """A one-recording scenario, with every axis a test might vary exposed as a default."""
    return Scenario(
        name=name,
        description="fixture",
        formats=formats,
        recordings=(
            Recording(name="only", clock=clock, devices=devices, faults=faults),
            *extra,
        ),
        seed=seed,
    )


@cache
def built(name: str) -> Corpus:
    """A shipped corpus, built once per session."""
    return build(SCENARIOS[name])


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def shipped(request: pytest.FixtureRequest) -> Corpus:
    """Each shipped corpus in turn — so a structural law covers a scenario added later."""
    return built(request.param)
