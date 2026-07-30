"""Synthetic corpora: labelled data we own outright, for every lane the product judges.

The reason this exists is licensing, not convenience. SKAB — the obvious benchmark, and the
one every measured result in ADR-0021 came from — is GPLv3, so it cannot ship inside a
repository headed for a commercial track. Without something to replace it, a researcher who
clones this repository gets a harness with one tiny corpus, which is a demo, not a benchmark.

Generation solves that completely: **the faults we inject are the labels**, so there is no
annotation step, no third-party data, and no licence question. It also does things a fixed
dataset cannot — arbitrary size, controllable difficulty, and fault shapes chosen to
*discriminate* rather than whatever a testbed happened to do.

## The shape of this package (ADR-0026)

The first version fused three concerns in one function: what the world did, how it is written
down, and what the answer key says. That is why it could only ever produce single-asset
telemetry — every extension hit all three at once. They are now three things:

===============  =========================================================================
``world``        devices, a clock, and declared faults. No formats, no channels, no IPs.
``port``         ``Behaviour`` (a lane's normal + what a fault does to it) and ``Renderer``
                 (events -> wire text). The two extension points.
``build``        simulate -> render -> **derive** the answer key from what was expressed.
``rng``          one private random stream per thing, so the corpus can grow.
``faults``       a fault is a name plus which lanes can show it.
``scenarios``    the shipped corpora, as data.
===============  =========================================================================

To add a **lane**: one ``Behaviour``, one ``Renderer``, two registry lines. To add a **wire
dialect**: one ``Renderer``. To add a **fault shape**: a ``FAULTS`` entry plus a handler in the
behaviours that should show it. To add a **corpus**: a ``Scenario``. Nothing else, in any case
— and the meta-tests in ``tests/eval/generate/`` then hold the new thing to every law.

Output is written once and committed, so loading it at benchmark time is a file read rather
than a re-run of this code — the same treatment as the committed team3 artifact (ADR-0012),
and for the same reason: generated inputs must be *fixed*, or every benchmark silently
measures a different dataset.
"""

from pathlib import Path

from iotshield.eval.corpus import Corpus
from iotshield.eval.corpus import write_bundle as _write_bundle
from iotshield.eval.generate.build import build
from iotshield.eval.generate.faults import FAULTS, lanes_of
from iotshield.eval.generate.flow import FlowCsvRenderer, NetworkHost, ZeekConnRenderer
from iotshield.eval.generate.meter import MeterFleet, SensiproHourlyRenderer
from iotshield.eval.generate.port import BEHAVIOURS, RENDERERS, Renderer
from iotshield.eval.generate.rng import stream
from iotshield.eval.generate.scenarios import SCENARIOS
from iotshield.eval.generate.syslog import LogSource, Rfc5424Renderer
from iotshield.eval.generate.telemetry import SensorRig, StubCsvRenderer
from iotshield.eval.generate.world import (
    Clock,
    Device,
    FaultSpec,
    GenerationError,
    Recording,
    Scenario,
)
from iotshield.pipeline.registry import lookup

# Registration, in one place rather than by import side effect in each module: a registry
# populated from six files is a registry nobody can read, and an unimported adapter would be
# silently absent. Mirrors how `pipeline.registry` names every adapter explicitly.
#
# Both keys are read off the component itself, so adding a lane really is the one line the
# package docstring promises — restating the lane here would be a second place to get it wrong.
for _behaviour in (SensorRig(), LogSource(), NetworkHost(), MeterFleet()):
    BEHAVIOURS[(_behaviour.lane, _behaviour.payload)] = _behaviour
for _renderer in (
    StubCsvRenderer(),
    Rfc5424Renderer(),
    FlowCsvRenderer(),
    ZeekConnRenderer(),
    SensiproHourlyRenderer(),
):
    RENDERERS[_renderer.source_format] = _renderer


def write(root: Path, name: str) -> Corpus:
    """Build one scenario and write its bundle under ``root``. Same input, same bytes."""
    scenario = lookup("scenario", SCENARIOS, name)
    corpus = build(scenario)
    _write_bundle(root / scenario.name, corpus)
    return corpus


def write_all(root: Path) -> list[Corpus]:
    """Write every shipped scenario — what a bare ``generate`` does."""
    return [write(root, name) for name in SCENARIOS]


# Only what a caller outside this package actually reaches for. The port vocabulary
# (`Behaviour`, `Calibration`, `Observed`, `Streams`) is imported from `generate.port` by the
# lane modules that implement it, so re-exporting it here would only advertise a path nobody
# takes; `to_stub_csv` likewise, whose one caller uses the submodule.
__all__ = [
    "BEHAVIOURS",
    "FAULTS",
    "RENDERERS",
    "SCENARIOS",
    "Clock",
    "Device",
    "FaultSpec",
    "GenerationError",
    "Recording",
    "Renderer",
    "Scenario",
    "build",
    "lanes_of",
    "stream",
    "write",
    "write_all",
]
