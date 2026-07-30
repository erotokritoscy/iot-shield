"""Every adapter must declare a capability, and the declaration must match reality.

This is the capability analogue of the port suites: a structural guarantee that no
adapter ships undeclared, plus behavioural cross-checks that a declared shape agrees
with what the built adapter actually does — so the guard never reads a stale lie.
"""

import json
import sys
from pathlib import Path

import pytest

from iotshield.api.control import params_of
from iotshield.capabilities import Capability, formats_in, lanes_in, payloads_in
from iotshield.contracts import Lane
from iotshield.detect._artifacts import packaged_artifact_ids
from iotshield.detect.rules.loader import PACKAGED, RULES_PATH_ENV, available_rulesets
from iotshield.flow import METADATA_KEY as FLOW_METADATA_KEY
from iotshield.meter import METADATA_KEY as METER_METADATA_KEY
from iotshield.normalize import (
    FLOW_DIALECTS,
    PASSTHROUGH_SOURCE_FORMAT,
    STUB_SOURCE_FORMAT,
    SYSLOG_SOURCE_FORMAT,
)
from iotshield.pipeline.registry import (
    ALERT_ENGINES,
    DETECTORS,
    NORMALIZERS,
    AdapterSpec,
    build_detector,
    build_normalizer,
)
from tests import DEMO_DIR

_ALL_SPECS = [
    *((f"normalizer:{name}", spec) for name, spec in NORMALIZERS.items()),
    *((f"detector:{name}", spec) for name, spec in DETECTORS.items()),
    *((f"alert_engine:{name}", spec) for name, spec in ALERT_ENGINES.items()),
]


@pytest.mark.parametrize("label,spec", _ALL_SPECS, ids=[label for label, _ in _ALL_SPECS])
def test_every_adapter_declares_a_nonempty_capability(label: str, spec: AdapterSpec) -> None:
    capability = spec.capability({})  # default params
    assert isinstance(capability, Capability)
    assert capability.consumes, f"{label} declares no consumes tokens"
    assert capability.produces, f"{label} declares no produces tokens"


def test_normalizer_declared_format_matches_source_format_constant() -> None:
    assert formats_in(NORMALIZERS["stub"].capability({}).consumes) == {STUB_SOURCE_FORMAT}
    assert formats_in(NORMALIZERS["syslog"].capability({}).consumes) == {SYSLOG_SOURCE_FORMAT}
    passthrough = formats_in(NORMALIZERS["passthrough"].capability({}).consumes)
    assert passthrough == {PASSTHROUGH_SOURCE_FORMAT}


def test_flow_normalizer_declares_every_dialect_it_can_parse() -> None:
    # The first normalizer accepting more than one format: the declaration must list every
    # dialect, or the guard would reject a source the adapter handles perfectly well.
    assert formats_in(NORMALIZERS["flow"].capability({}).consumes) == set(FLOW_DIALECTS)


def test_flow_normalizer_actually_emits_its_declared_lane() -> None:
    declared = lanes_in(NORMALIZERS["flow"].capability({}).produces)
    text = (
        "start,src_ip,src_port,dst_ip,dst_port,proto\n"
        "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp\n"
    )
    result = build_normalizer("flow").normalize(text, source_format="flow-csv", origin="test")
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} <= declared


def test_stub_normalizer_actually_emits_its_declared_lane() -> None:
    declared = lanes_in(NORMALIZERS["stub"].capability({}).produces)
    result = build_normalizer("stub").normalize(
        "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0",
        source_format=STUB_SOURCE_FORMAT,
        origin="test",
    )
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} <= declared


def test_syslog_normalizer_actually_emits_its_declared_lane() -> None:
    declared = lanes_in(NORMALIZERS["syslog"].capability({}).produces)
    line = "<34>1 2026-07-07T12:00:00Z host-a app 1 ID - disk failure"
    result = build_normalizer("syslog").normalize(
        line, source_format=SYSLOG_SOURCE_FORMAT, origin="test"
    )
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} <= declared


@pytest.mark.parametrize(
    "params,expected",
    [({}, Lane.TELEMETRY), ({"lane": "log"}, Lane.LOG), ({"lane": "flow"}, Lane.FLOW)],
)
def test_passthrough_normalizer_declared_lane_matches_built_instance(
    params: dict[str, str], expected: Lane
) -> None:
    # The first param-driven NORMALIZER capability (ADR-0025): the declared lane must follow
    # params exactly as the factory does, or the guard would reason about the wrong lane. The
    # record deliberately omits source_type, so the lane it comes out on is the one the built
    # instance filled in from its param — asserting the declaration against behaviour rather
    # than against a value the input already carried.
    declared = lanes_in(NORMALIZERS["passthrough"].capability(params).produces)
    assert declared == {expected}
    record = json.dumps({"timestamp": "2026-07-07T12:00:00+00:00", "asset_id": "a", "value": 1.0})
    result = build_normalizer("passthrough", params).normalize(
        record, source_format=PASSTHROUGH_SOURCE_FORMAT, origin="test"
    )
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} == {expected}


@pytest.mark.parametrize(
    "params,expected",
    [({}, Lane.TELEMETRY), ({"lane": "telemetry"}, Lane.TELEMETRY), ({"lane": "flow"}, Lane.FLOW)],
)
def test_stub_detector_declared_lane_matches_built_instance(
    params: dict[str, str], expected: Lane
) -> None:
    # The param-driven case: the declaration must follow params exactly as the
    # factory does, or the guard would warn (or stay silent) about the wrong lane.
    declared = lanes_in(DETECTORS["stub_threshold"].capability(params).consumes)
    assert declared == {expected}
    assert build_detector("stub_threshold", params).lane == expected


def test_team3_detector_declared_lane_matches_built_instance() -> None:
    declared = lanes_in(DETECTORS["team3_zscore"].capability({}).consumes)
    assert declared == {Lane.TELEMETRY}
    assert build_detector("team3_zscore", {}).lane == Lane.TELEMETRY


def _choices_of(spec: AdapterSpec, param: str) -> list[str] | None:
    """The choices `/modules` publishes for one param — built exactly as the API builds them,
    so a test cannot pass against a vocabulary the catalogue never sends."""
    return next(p.choices for p in params_of(spec) if p.name == param)


def _default_of(spec: AdapterSpec, param: str) -> object:
    return next(p.default for p in params_of(spec) if p.name == param)


def test_declared_lane_choices_are_the_value_carrying_lanes() -> None:
    # stub_threshold compares event.value, which only telemetry and flow carry — a LOG event
    # has none, so `lane: log` would validate clean and never fire. The offered vocabulary is
    # the value-carrying lanes, not every Lane; this pins that (and catches a new such lane).
    value_carrying = [Lane.TELEMETRY.value, Lane.FLOW.value]
    assert _choices_of(DETECTORS["stub_threshold"], "lane") == value_carrying


def test_learning_adapters_params_type_mirrors_the_factory_signature() -> None:
    """A hyperparameter field with no matching factory argument is silently unreachable from
    config (ADR-0022 Consequences). Pin the mirror so it cannot drift."""
    import inspect

    for name, spec in DETECTORS.items():
        if spec.params_type is None:
            continue
        factory_args = set(inspect.signature(spec.factory).parameters) - {"self"}
        artifact = {spec.artifact_param} if spec.artifact_param else set()
        fields = set(spec.params_type.model_fields)
        # Every factory arg except the artifact selector is a hyperparameter field, and every
        # field is a factory arg. Either gap means a knob that config or fitting cannot reach.
        assert fields == factory_args - artifact, (
            f"{name}: params_type {fields} vs factory {factory_args - artifact}"
        )


def test_every_preset_adapter_declares_its_presets_as_choices() -> None:
    """Same guard on the other side: a new PRESETS entry the Literal does not list would be
    unreachable from the UI even though the adapter supports it.

    Discovered from the registry rather than named by hand, so the law covers the *next*
    preset adapter automatically — naming them would only guarantee it for the ones
    somebody remembered to add here.
    """
    checked: list[str] = []
    for name in DETECTORS:
        module = sys.modules[type(build_detector(name, {})).__module__]
        presets = getattr(module, "PRESETS", None)
        if presets is None:
            continue  # an adapter with no preset vocabulary has nothing to mirror
        assert set(_choices_of(DETECTORS[name], "preset") or []) == set(presets), name
        checked.append(name)
    # Guard the guard: if the discovery above ever stops finding anything, this test would
    # pass vacuously while asserting nothing at all.
    assert set(checked) == {"log_burst", "flow_burst"}


def test_flow_burst_declared_lane_matches_built_instance() -> None:
    # Fixed-lane detector: the declaration must hold for every preset, since a preset
    # changes *what* it measures but never *which* lane it reads.
    assert lanes_in(DETECTORS["flow_burst"].capability({}).consumes) == {Lane.FLOW}
    for preset in ("fan_out", "port_scan", "exfiltration"):
        assert build_detector("flow_burst", {"preset": preset}).lane == Lane.FLOW


def test_a_param_with_no_closed_vocabulary_reports_no_choices() -> None:
    assert _choices_of(DETECTORS["stub_threshold"], "threshold") is None


# --- discovered vocabularies: shipped artifacts and shelved rule sets ---------
# (ADR-0035 D1 for the channel, ADR-0037 for the second thing that travels down it)


@pytest.mark.parametrize("name", sorted(n for n, s in DETECTORS.items() if s.artifact_param))
def test_a_learning_adapter_publishes_the_artifact_ids_it_ships(name: str) -> None:
    """Every id the catalogue offers must be one ``load_artifact`` resolves.

    The whole value of a dropdown over a text box is that its members are guaranteed good; an
    id published but unloadable would be worse than free text, because it looks authoritative.
    Building each one is the only way to check that, and it is affordable for two artifacts.
    """
    spec = DETECTORS[name]
    param = spec.artifact_param
    assert param is not None  # the parametrization selected on it
    ids = spec.discovered[param]()
    assert ids, f"{name} declares an artifact_param but enumerates nothing"
    assert _choices_of(spec, param) == list(ids)
    for artifact_id in ids:
        assert build_detector(name, {param: artifact_id}) is not None


def test_an_adapter_that_does_not_learn_enumerates_no_artifacts() -> None:
    """The channel is opt-in, so the API catalogues need no None-handling of their own."""
    assert DETECTORS["stub_threshold"].discovered == {}
    assert DETECTORS["log_burst"].discovered == {}


@pytest.mark.parametrize("label,spec", _ALL_SPECS, ids=[label for label, _ in _ALL_SPECS])
def test_a_declared_default_is_one_of_the_params_own_choices(label: str, spec: AdapterSpec) -> None:
    """A default outside its own vocabulary is a form that opens on a value it cannot show.

    Neither ``Select`` nor ``st.selectbox`` renders an option that is not in its list, so such
    a param *displays* the first choice while holding the default — the display-vs-value
    incoherence ADR-0036 D2 removed for artifacts, here as a law for every adapter. ``None``
    is exempt: that is "not given", which ADR-0033 says opens an optional vocabulary blank.
    """
    for param in params_of(spec):
        if param.choices is None or param.default is None:
            continue
        assert param.default in param.choices, (
            f"{label}: {param.name} defaults to {param.default!r}, "
            f"which is not one of {param.choices}"
        )


def test_the_rules_detector_offers_the_shelf_as_choices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one discovered vocabulary that can change while the process runs (ADR-0037).

    Resolved per catalogue call rather than at import, so a rule set dropped onto the shelf is
    offered without a restart — asserted here end to end (env var -> listing -> the choices
    ``/modules`` publishes), because that is the property an eager snapshot would silently lose.
    """
    assert _choices_of(DETECTORS["rules"], "ruleset") == list(available_rulesets())
    assert _default_of(DETECTORS["rules"], "ruleset") == PACKAGED

    # Never compiled during a listing, so any file with the right suffix marks a rule set.
    (tmp_path / "site-rules").mkdir()
    (tmp_path / "site-rules" / "a-rule.yml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(RULES_PATH_ENV, str(tmp_path))
    assert _choices_of(DETECTORS["rules"], "ruleset") == [PACKAGED, "site-rules"]


def test_an_unknown_artifact_package_lists_nothing_rather_than_raising() -> None:
    """`/modules` answers for every adapter, so one packaged without its artifacts must not
    take the whole catalogue down."""
    assert packaged_artifact_ids("iotshield.detect.nonexistent_artifacts") == ()


def test_log_burst_detector_declared_lane_matches_built_instance() -> None:
    # Fixed-lane detector: the declaration must hold for every preset, not just the
    # default, since the preset changes *what* it counts but never *which* lane it reads.
    assert lanes_in(DETECTORS["log_burst"].capability({}).consumes) == {Lane.LOG}
    for preset in ("severity_spike", "auth_failure"):
        assert build_detector("log_burst", {"preset": preset}).lane == Lane.LOG


# --- payload tokens: the lane vocabulary axis (ADR-0035 D2) --------------------


def test_a_payload_token_names_the_metadata_key_its_vocabulary_module_owns() -> None:
    """The token IS the key, so a payload nothing writes is a payload no event carries.

    A ``PayloadToken`` carries a bare string, which is the one thing typed tokens cannot
    check for us. Pinning it to the vocabulary module's own constant is what keeps a
    declaration from drifting into a value no writer ever produces — the failure mode that
    would make this axis quietly permissive instead of quietly restrictive.
    """
    assert payloads_in(NORMALIZERS["meter"].capability({}).produces) == {METER_METADATA_KEY}
    assert payloads_in(NORMALIZERS["flow"].capability({}).produces) == {FLOW_METADATA_KEY}
    assert payloads_in(DETECTORS["meter_iforest"].capability({}).consumes) == {METER_METADATA_KEY}
    assert payloads_in(DETECTORS["flow_burst"].capability({}).consumes) == {FLOW_METADATA_KEY}


def test_every_payload_a_detector_needs_is_produced_by_some_normalizer() -> None:
    """Closes the loop: a detector demanding a payload nothing emits is unreachable.

    The guard would report it forever and no configuration could satisfy it — a typo in a
    payload string is indistinguishable from a detector nobody can use. Discovered over the
    registries rather than listed, so a new adapter is covered on arrival.
    """
    available: set[str] = set()
    for spec in NORMALIZERS.values():
        available |= payloads_in(spec.capability({}).produces)
    for name, spec in DETECTORS.items():
        needed = payloads_in(spec.capability({}).consumes)
        assert needed <= available, (
            f"{name} needs payload(s) {needed - available}, which nothing emits"
        )


@pytest.mark.parametrize(
    "normalizer,source_format,path",
    [("meter", "eoa-meter-csv", "demo_meter.csv"), ("flow", "zeek-conn", "demo_flow.log")],
)
def test_a_normalizer_actually_emits_the_payload_it_declares(
    normalizer: str, source_format: str, path: str
) -> None:
    """The behavioural half, matching the declared-lane checks above.

    A declaration the adapter does not honour is worse than none: the guard would pass a
    chain whose detector then discards every event, which is precisely the silence ADR-0035
    exists to make visible.
    """
    declared = payloads_in(NORMALIZERS[normalizer].capability({}).produces)
    text = (DEMO_DIR / path).read_text(encoding="utf-8")
    result = build_normalizer(normalizer).normalize(
        text, source_format=source_format, origin="test"
    )
    assert result.events, "the sample parsed"
    for key in declared:
        assert all(key in event.metadata for event in result.events)


def test_the_rules_detector_needs_a_payload_only_on_the_lane_that_reads_one() -> None:
    """One adapter, two plug shapes — the clearest case that this axis is per-configuration.

    ``rules(lane: flow)`` reads every flow field through ``FlowRecord``; ``rules(lane: log)``
    reads the contract's own fields. Declaring the payload per *adapter* would have made the
    log configuration permanently unsatisfiable.
    """
    assert payloads_in(DETECTORS["rules"].capability({"lane": "flow"}).consumes) == {
        FLOW_METADATA_KEY
    }
    assert payloads_in(DETECTORS["rules"].capability({"lane": "log"}).consumes) == set()
    assert payloads_in(DETECTORS["rules"].capability({"lane": "telemetry"}).consumes) == set()


def test_the_passthrough_payload_vocabulary_is_the_one_normalizers_actually_produce() -> None:
    """The registry's ``payload`` Literal must offer exactly the payloads that exist.

    A payload name is a bare string — the one thing typed tokens cannot check — so it is
    pinned the same way the ``lane`` Literal is pinned to ``Lane`` and the preset Literals to
    ``PRESETS``. Offering a member nothing produces gives an operator a dropdown entry that
    silently satisfies no detector; omitting one hides a legitimate declaration.
    """
    produced: set[str] = set()
    for name, spec in NORMALIZERS.items():
        if name == "passthrough":
            continue  # its own vocabulary is what we are checking
        produced |= payloads_in(spec.capability({}).produces)
    offered = _choices_of(NORMALIZERS["passthrough"], "payload")
    assert offered is not None, "the payload param must publish a closed vocabulary"
    assert set(offered) == produced
    # Optional, so it renders as "not given" rather than preselecting a claim nobody made.
    assert _default_of(NORMALIZERS["passthrough"], "payload") is None


def test_passthrough_declares_the_payload_its_operator_claims() -> None:
    """It envelopes what a partner already emits, so its shape is the operator's claim.

    Same reasoning ADR-0025 applied to ``lane``: the parser validates the envelope, not the
    vocabulary inside ``metadata``, so it cannot discover a payload and must be told. Without
    this a partner emitting MeterReadings as JSONL could never satisfy ``meter_iforest``.
    """
    assert payloads_in(NORMALIZERS["passthrough"].capability({}).produces) == set()
    declared = NORMALIZERS["passthrough"].capability({"payload": "meter"})
    assert payloads_in(declared.produces) == {METER_METADATA_KEY}
