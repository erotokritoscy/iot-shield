"""ADR-0027 D1, enforced: a label is made of the instants a behaviour ACTUALLY perturbed.

This is the law every benchmark number in the project rests on, and it was the only
load-bearing law with nothing checking it. `_validate` refuses an *empty* claim; an
over-claimed one — a behaviour returning the declared fault window instead of the measured
perturbation — passed ruff, pyright and every other test here, and every corpus that lane
produced would have labels wider than its data. That is precisely the pre-ADR-0027 bug,
re-introducible with no signal.

The check is a **clean twin**: rebuild each scenario with `faults=()`, normalize both, and
compare. It works because faults are applied last and streams are seeded by *name*, so the
unfaulted build reproduces the faulted one byte-for-byte everywhere a fault did not reach.
That makes the DATA the authority on what changed, so no behaviour has to be trusted to
report honestly about itself (ADR-0033).

Two directions, and the second is the dangerous one:

- a **phantom label** (labelled, but the bytes are identical) charges a detector for missing
  something that is not there;
- an **unlabelled change** (perturbed, but not labelled) charges a detector for correctly
  finding something real.
"""

from dataclasses import replace
from datetime import datetime
from functools import cache

import pytest

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval import Corpus
from iotshield.eval.generate import SCENARIOS, build
from iotshield.eval.generate.build import _NORMALIZER_FOR
from iotshield.eval.generate.world import Scenario
from iotshield.meter import CUMULATIVE_FIELDS
from iotshield.meter import METADATA_KEY as METER_PAYLOAD
from iotshield.pipeline.registry import build_normalizer
from tests.eval.generate.conftest import built

_NAMES = sorted(SCENARIOS)

# Which normalizer reads each renderable format is the *builder's* table, imported rather than
# restated. It is already "spelled out rather than derived, so a renderer added without a reader
# fails loudly", and the second copy that used to live here meant every new dialect had to be
# added in two places — with nothing checking they agreed and a bare KeyError if one was missed.

Instant = tuple[str, Lane, datetime]


def _clean_twin(scenario: Scenario) -> Scenario:
    return replace(
        scenario,
        recordings=tuple(replace(item, faults=()) for item in scenario.recordings),
    )


@cache
def _clean_built(name: str) -> Corpus:
    """The faultless twin, built once per session — the mirror of ``conftest.built``.

    Keyed on the name rather than the scenario because a ``Scenario`` need not be hashable, and
    for the same reason ``built`` is cached: both tests below need this corpus, and the metering
    one takes ~0.9 s to generate against ~0.25 s for the other three.
    """
    return build(_clean_twin(SCENARIOS[name]))


def _events(text: str, source_format: str) -> list[NormalizedEvent]:
    normalizer = build_normalizer(_NORMALIZER_FOR[source_format], {})
    result = normalizer.normalize(text, source_format=source_format, origin="label-law")
    assert not result.malformed, f"{source_format}: generator emitted unparseable records"
    return list(result.events)


def _observed(event: NormalizedEvent) -> str:
    """What was observed **at** this instant, as comparable text.

    For every lane but one this is the verbatim line, which is the strongest thing to compare:
    it pins an event's entire content, so a perturbation in any field is reported.

    A *cumulative* lane cannot use it. Perturb one hour of a meter and every later counter is
    permanently higher, so comparing lines reports the whole remainder of the recording as
    changed — and this law would then demand a label over all of it, marking as faulty thousands
    of hours at which nothing was observed differently and charging a detector for missing them.
    That is the failure this law exists to prevent, reached by applying it literally.

    A running total is a statement about the past, not an observation at the instant carrying it.
    What a detector reads at T is the *consumption* for T, so that is what is compared, with the
    cumulative fields subtracted (``meter.CUMULATIVE_FIELDS``, so the fact is declared beside the
    record rather than guessed here). ``message`` goes with them, and loses nothing: it is a
    rendering of the consumption and the counter, and the consumption is compared directly.
    """
    record = event.metadata.get(METER_PAYLOAD)
    if record is None:
        return event.raw
    if not isinstance(record, dict):  # pragma: no cover - the contract validates this shape
        return event.raw
    observed = {key: value for key, value in record.items() if key not in CUMULATIVE_FIELDS}
    return repr((event.value, event.severity, {**event.metadata, METER_PAYLOAD: observed}))


def _rendered(case_inputs: object) -> dict[tuple[str, Lane, datetime, str | None], str]:
    """Every event keyed by what identifies it, valued by what was observed at it.

    Keyed on metric as well as (asset, lane, instant) because ADR-0003 D1 fans a
    multi-channel reading into one event per channel: two channels of one instant are two
    lines, and a fault may perturb one of them.
    """
    rendered: dict[tuple[str, Lane, datetime, str | None], str] = {}
    for given in case_inputs:  # type: ignore[attr-defined]
        for event in _events(given.text, given.source_format):
            key = (event.asset_id, event.source_type, event.timestamp, event.metric)
            rendered[key] = _observed(event)
    return rendered


def _labelled_instants(truth: object) -> set[Instant]:
    """Every (asset, lane, instant) an incident's spans cover, as a closed interval."""
    covered: set[Instant] = set()
    for incident in truth.incidents:  # type: ignore[attr-defined]
        for start, end in incident.spans:
            covered.add((incident.asset_id, incident.lane, start))
            covered.add((incident.asset_id, incident.lane, end))
    return covered


def _within_a_span(instant: Instant, truth: object) -> bool:
    asset_id, lane, moment = instant
    for incident in truth.incidents:  # type: ignore[attr-defined]
        if incident.asset_id != asset_id or incident.lane is not lane:
            continue
        if any(start <= moment <= end for start, end in incident.spans):
            return True
    return False


@pytest.mark.parametrize("name", _NAMES)
def test_every_label_describes_a_change_that_is_actually_in_the_bytes(name: str) -> None:
    """No phantom labels, and no unlabelled changes, at every (asset, lane, instant)."""
    faulty = built(name)
    clean = _clean_built(name)
    clean_cases = {case.name: case for case in clean.cases}

    for case in faulty.cases:
        faulty_lines = _rendered(case.inputs)
        clean_lines = _rendered(clean_cases[case.name].inputs)

        changed = {
            (asset_id, lane, moment)
            for (asset_id, lane, moment, _metric), line in faulty_lines.items()
            if clean_lines.get((asset_id, lane, moment, _metric)) != line
        }
        # An additive lane injects records the clean twin does not have at all; those keys are
        # absent from clean_lines and so already counted above. The reverse — a record the
        # clean twin has and the faulted one does not — would be a fault that DELETED data,
        # which no behaviour does and which would silently shorten a recording.
        vanished = {
            (asset_id, lane, moment)
            for (asset_id, lane, moment, _metric) in clean_lines
            if (asset_id, lane, moment, _metric) not in faulty_lines
        }
        assert not vanished, f"{name}/{case.name}: a fault removed records: {sorted(vanished)[:3]}"

        unlabelled = {item for item in changed if not _within_a_span(item, case.truth)}
        assert not unlabelled, (
            f"{name}/{case.name}: {len(unlabelled)} perturbed instant(s) carry no label, so a "
            f"detector that finds them is scored WRONG: {sorted(unlabelled)[:3]}"
        )

        # Every span ENDPOINT must be a genuinely perturbed instant. Endpoints rather than the
        # whole interval because a span's interior is the lane's own clock, which only the
        # generator enumerates — but a boundary is exactly where an over-claimed label shows
        # up first: widen a label and its edges land on untouched bytes.
        phantom = {item for item in _labelled_instants(case.truth) if item not in changed}
        assert not phantom, (
            f"{name}/{case.name}: {len(phantom)} labelled span endpoint(s) are identical to the "
            f"clean twin, so a detector is charged for missing nothing: {sorted(phantom)[:3]}"
        )


@pytest.mark.parametrize("name", _NAMES)
def test_a_clean_recording_is_byte_identical_to_its_twin(name: str) -> None:
    """The premise the check above rests on: faults are applied last and streams are seeded by
    name, so removing the faults must change nothing anywhere else."""
    faulty = built(name)
    clean = _clean_built(name)
    clean_cases = {case.name: case for case in clean.cases}

    for case in faulty.cases:
        if not case.truth.incidents:
            for original, twin in zip(case.inputs, clean_cases[case.name].inputs, strict=True):
                assert original.text == twin.text, f"{name}/{case.name} drifted without a fault"
