"""iotshield dashboard — pure API client (never touches storage; ADR-0008).

Run:    uv run streamlit run src/iotshield/dashboard/streamlit_app.py
Config: IOTSHIELD_API_URL (default http://127.0.0.1:8000)
"""

import json
import math
import os
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd
import streamlit as st
from pydantic import ValidationError

from iotshield.api.control import ModuleInfo, ParamInfo
from iotshield.contracts import Asset, DetectionResult, Lane, PrioritizedAlert
from iotshield.dashboard.charts import (
    case_timeline,
    case_variance,
    finding_of,
    severity_bar,
    spec_scores,
    sweep_curve,
    telemetry_timeline,
)
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient
from iotshield.enrichment import Enrichment, MitreTechnique, action_steps
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV_FORMAT

if TYPE_CHECKING:  # type-only: the runtime imports are deferred, see load_eval_report
    from iotshield.api.evaluation import CorporaResponse, DetectorSummary
    from iotshield.eval.report import EvaluationReport

API_URL_ENV = "IOTSHIELD_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8000"
PIPELINE_API_URL_ENV = "IOTSHIELD_PIPELINE_API_URL"
DEFAULT_PIPELINE_API_URL = "http://127.0.0.1:8100"

# The evaluation plane is off unless a deployment opts in (ADR-0021 D1). It is a research
# instrument, not a product feature: it needs labelled corpora, which production never has,
# and it is the first thing you would drop from a product image. Same env-injection pattern
# as IOTSHIELD_DATABASE_URL, so an on-prem customer simply never sees the mode.
EVAL_UI_ENV = "IOTSHIELD_EVAL_UI"
EVAL_REPORT_ENV = "IOTSHIELD_EVAL_REPORT"
DEFAULT_EVAL_REPORT = "reports/evaluation.json"

# Everything that can go wrong talking to the API: transport/status errors
# (httpx), a response that no longer matches a contract (ValidationError — the
# whole point of the typed client), or a non-JSON body (JSONDecodeError). The
# dashboard renders these as st.error, never a traceback (ADR-0008).
_API_ERRORS = (httpx.HTTPError, ValidationError, json.JSONDecodeError)


# Streamlit re-executes the whole script on every widget interaction, so a client built in
# the page body is rebuilt on every click — and `httpx.Client()` loads the CA bundle each
# time, measured at ~155ms, dwarfing the requests it then makes. Cached on the URL rather
# than on nothing, so the env var stays live while the construction happens once. This is
# also what stops a connection pool leaking per rerun.
@st.cache_resource(show_spinner=False)
def _read_client(url: str) -> ApiClient:
    return ApiClient.from_url(url)


@st.cache_resource(show_spinner=False)
def _control_client(url: str) -> ControlClient:
    return ControlClient.from_url(url)


def get_client() -> ApiClient:
    return _read_client(os.environ.get(API_URL_ENV, DEFAULT_API_URL))


def get_control_client() -> ControlClient:
    return _control_client(os.environ.get(PIPELINE_API_URL_ENV, DEFAULT_PIPELINE_API_URL))


def _asset_label(asset: Asset) -> str:
    discovered = " (auto-discovered)" if asset.metadata.get("auto_discovered") else ""
    return f"{asset.name} [{asset.asset_id}]{discovered}"


def _alert_label(alert: PrioritizedAlert) -> str:
    return (
        f"{alert.severity.value.upper()} {alert.priority_score:.1f} — {alert.asset_id}"
        f" @ {alert.window_start:%Y-%m-%d %H:%M}"
    )


def _techniques(detections: Sequence[DetectionResult]) -> list[MitreTechnique]:
    """Every technique cited across an alert's detections, deduplicated, in the order met.

    A summary of what is already on screen, not a re-derivation of the server's judgment:
    promotion picks one winning claim by precedence and that stays in ``alert/_correlate``,
    while this only answers "which techniques does this alert implicate".
    """
    seen: dict[str, MitreTechnique] = {}
    for detection in detections:
        enrichment = Enrichment.from_detection(detection)
        for technique in enrichment.mitre if enrichment else ():
            seen.setdefault(technique.technique_id, technique)
    return list(seen.values())


def triage_page(client: ApiClient) -> None:
    st.title("Alert triage")
    summary = client.summary()
    kpis = st.columns(4)
    for column, key in zip(
        kpis, ("events", "detections", "alerts", "malformed_records"), strict=True
    ):
        column.metric(key.replace("_", " "), summary.counts.get(key, 0))

    alerts = client.alerts()
    if not alerts:
        st.info("No alerts yet — run the pipeline first (see README: Run the demo pipeline).")
        return

    chart = severity_bar(alerts)
    if chart is not None:
        st.altair_chart(chart)
        st.caption("Alert count per severity.")

    st.subheader("Ranked queue")
    st.dataframe(
        pd.DataFrame(
            {
                "severity": [a.severity.value for a in alerts],
                "priority": [round(a.priority_score, 1) for a in alerts],
                "asset": [a.asset_id for a in alerts],
                "window start": [a.window_start for a in alerts],
                "threat class": [a.threat_class for a in alerts],
                "status": [a.status.value for a in alerts],
            }
        ),
        hide_index=True,
    )

    selected = st.selectbox("Inspect alert", alerts, format_func=_alert_label)
    st.markdown(f"**Rationale:** {selected.rationale}")
    evidence = client.alert_detections(selected.alert_id)
    steps = action_steps(selected.recommended_action)
    if steps:
        # A list, because the field IS one: up to six steps unioned from every rule that
        # fired (ADR-0032 D5), which an analyst works through one at a time. The contract
        # carries them joined into one string, so `action_steps` recovers what
        # `Enrichment.action_text` joined; a single-step fallback is a one-item list.
        st.markdown("**Recommended action:**")
        st.markdown("\n".join(f"- {step}" for step in steps))
    techniques = _techniques(evidence)
    if techniques:
        # `threat_class` above carries the winning technique only, and without its tactic.
        st.markdown(
            "**MITRE ATT&CK:** " + " · ".join(f"{t.tactic} — {t.label()}" for t in techniques)
        )
    st.markdown("**Score components** (the explainability receipt):")
    st.table(pd.DataFrame([selected.score_components]))

    st.markdown("**Evidence** — the detections behind this alert:")
    st.dataframe(
        pd.DataFrame(
            {
                "method": [d.method for d in evidence],
                "lane": [d.lane.value for d in evidence],
                "score": [d.score for d in evidence],
                "threshold": [d.threshold for d in evidence],
                "window start": [d.window_start for d in evidence],
                "window end": [d.window_end for d in evidence],
                "finding": [finding_of(d) for d in evidence],
            }
        ),
        hide_index=True,
    )
    # The per-detection evidence payload (top peers, ports, matched messages, ...) is what
    # makes the finding actionable — "cam-07 contacted 41 destinations" rather than a bare
    # score. It reached the API but nothing rendered it, so the analyst never saw it.
    for detection in evidence:
        if detection.evidence:
            with st.expander(f"{finding_of(detection)} — {detection.method}"):
                st.json(detection.evidence)


def assets_page(client: ApiClient) -> None:
    st.title("Assets & telemetry")
    assets = client.assets()
    if not assets:
        st.info("No assets yet — the inventory populates from data (asset discovery).")
        return

    selected = st.selectbox("Asset", assets, format_func=_asset_label)
    st.caption(
        f"type: {selected.asset_type} · criticality: {selected.criticality.value}"
        + (f" · location: {selected.location}" if selected.location else "")
    )

    events = client.events(asset_id=selected.asset_id, lane=Lane.TELEMETRY)
    # The chart is telemetry-only on purpose: it plots telemetry values, and overlaying a
    # flow or log detection's window on a value series they don't belong to would be
    # misleading. Only the telemetry detections are drawn as bands.
    telemetry_detections = client.detections(asset_id=selected.asset_id, lane=Lane.TELEMETRY)
    chart = telemetry_timeline(events, telemetry_detections)
    if chart is None:
        st.info("No telemetry with numeric values for this asset.")
    else:
        st.altair_chart(chart, width="stretch")
        st.caption(
            "Blue line: telemetry values. Red bands/rules: telemetry detection windows "
            "(all lanes are in the table below)."
        )

    # The table lists EVERY lane, so a flow- or log-only asset is not invisible here — its
    # alert sits in Triage while this page used to show it "no telemetry" and nothing else.
    detections = client.detections(asset_id=selected.asset_id)
    if detections:
        st.subheader("Detections")
        st.dataframe(
            pd.DataFrame(
                {
                    "method": [d.method for d in detections],
                    "lane": [d.lane.value for d in detections],
                    "score": [d.score for d in detections],
                    "threshold": [d.threshold for d in detections],
                    "window start": [d.window_start for d in detections],
                    # Last, so the numeric block stays adjacent, but present: `method` no
                    # longer identifies a finding, so without this the table lists several
                    # rows that differ only in score.
                    "finding": [finding_of(d) for d in detections],
                }
            ),
            hide_index=True,
        )
    else:
        st.info("No detections for this asset.")


def data_health_page(client: ApiClient) -> None:
    st.title("Data health")
    summary = client.summary()
    st.table(pd.DataFrame([summary.counts]))

    st.subheader("Malformed inputs (captured, never dropped)")
    malformed = client.malformed()
    if not malformed:
        st.success("No malformed inputs recorded.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "raw": [m.raw for m in malformed],
                "error": [m.error for m in malformed],
                "source format": [m.source_format for m in malformed],
                "origin": [m.origin for m in malformed],
            }
        ),
        hide_index=True,
    )


def _untouched(value: Any, param: ParamInfo) -> bool:
    """Whether the operator left this widget at what the catalogue reported.

    A form must be able to say "not given", and a widget cannot: Streamlit hands back a
    value whether or not anyone touched it. Sending all of them makes every run an explicit
    request for every parameter, which is wrong wherever "not given" carries meaning — and
    it does, in three different ways: a preset owns the real default (ADR-0024 D3), a
    learning adapter reads it from its artifact, or an absent value means "learn it from the
    data" (ADR-0022). The clearest case: an untouched `team3_zscore` row submitted its whole
    prefilled hyperparameter block alongside the artifact id, which is exactly the
    half-configured request ADR-0022 D4 refuses — so picking that detector and pressing run
    could not succeed.

    Comparing against the reported default is what lets the page express absence, and it is
    faithful: a value equal to the default produces an identical adapter either way.
    """
    if param.default is None:
        return value is None or value == ""
    return value == param.default or str(value) == str(param.default)


def _param_widgets(prefix: str, params: list[ParamInfo]) -> dict[str, Any]:
    # One input per adapter param, typed off the /modules schema. Values are still
    # validated by the guard and the contracts, so free-text entry is safe here. Only
    # parameters the operator actually moved are sent — see _untouched.
    values: dict[str, Any] = {}

    def submit(param: ParamInfo, value: Any) -> None:
        if not _untouched(value, param):
            values[param.name] = value

    for param in params:
        key = f"{prefix}_{param.name}"
        if param.choices:
            # A closed vocabulary the factory declared (a preset, a lane): offer it rather
            # than making the operator type it from memory into a free-text box, where a
            # typo surfaces only as a guard error after the fact (ADR-0023 D9).
            default = str(param.default) if param.default is not None else None
            index = param.choices.index(default) if default in param.choices else 0
            submit(param, st.selectbox(param.name, param.choices, index=index, key=key))
        elif param.type in ("float", "int"):
            # A numeric whose own default is None is *optional*: it must render empty and
            # be omitted when untouched, exactly as the free-text branch below does. Coercing
            # None to 0 instead would silently submit a value the adapter never offered —
            # and for a param whose real default is preset-owned (flow_burst's window and
            # threshold, ADR-0024 D3) that fabricated 0 is refused at construction, turning
            # "pick a detector and press run" into a failed run.
            whole = param.type == "int"
            # Keep value and step the same Python type — Streamlit infers the widget's mode
            # from them and rejects a mix.
            default = None if param.default is None else (int if whole else float)(param.default)
            number = st.number_input(param.name, value=default, step=1 if whole else None, key=key)
            if number is not None:
                submit(param, int(number) if whole else float(number))
        elif param.type == "bool":
            submit(param, st.checkbox(param.name, value=bool(param.default), key=key))
        else:
            default = "" if param.default is None else str(param.default)
            submit(param, st.text_input(param.name, value=default, key=key))
    return values


def _preferred_index(names: list[str], *ranked: Callable[[str], bool]) -> int:
    """Preselect by the first predicate that matches anything, in order of preference.

    No picker here may preselect "whichever name sorts first": that was silently wrong for
    detectors the moment a second lane arrived (ADR-0020), and wrong for sources once
    ``flow`` was registered, since it sorts before ``stub`` — a fresh session would have
    opened on a flow chain fed telemetry CSV.

    Each predicate is tried in turn and only a real match wins, so a second-choice rule can
    back up a first-choice one. Falling back to 0 when nothing matches is deliberate: the
    guard reports a mismatched chain, which is more useful than a row contributing no
    adapter at all.
    """
    for fits in ranked:
        match = next((index for index, name in enumerate(names) if fits(name)), None)
        if match is not None:
            return match
    return 0


def _feeds(detector: ModuleInfo, produced: set[str]) -> bool:
    """Whether this detector consumes a lane the page's sources actually produce."""
    return bool(set(detector.consumes) & produced)


def _ready(detector: ModuleInfo) -> bool:
    """Whether this detector can judge without an artifact being named first.

    Declared, not name-sniffed: ``artifact_param`` is published by ``/modules`` precisely so
    a caller can tell "needs trained weights" from "ready to run" (ADR-0022). Read as a
    typed attribute rather than via ``getattr`` — a default would silently restore the very
    regression the field exists to prevent, and against an older API the honest failure is
    the response model's, not a preselection that quietly reverts.
    """
    return detector.artifact_param is None


def _source_widgets(index: int, normalizers: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """One input source: normalizer + format picker, and its content (upload or paste).

    Returns the source descriptor (what the guard/spec need), the raw content, and a
    provenance origin. A run carries a list of these, so one run can mix modalities (a
    telemetry CSV alongside syslog) that pool into the detector lanes (ADR-0019).
    """
    st.markdown(f"**Source {index + 1}**")
    names = sorted(normalizers)
    normalizer_name = st.selectbox(
        "Normalizer",
        names,
        # By the format the reference normalizer accepts, not by its name — follows the
        # registry rather than restating it.
        index=_preferred_index(names, lambda name: STUB_CSV_FORMAT in normalizers[name].formats),
        key=f"src_{index}_norm",
    )
    formats = normalizers[normalizer_name].formats
    source_format = (
        st.selectbox("Input format", formats, key=f"src_{index}_fmt")
        if len(formats) > 1
        else (formats[0] if formats else "")
    )
    with st.expander(f"{normalizer_name} parameters"):
        params = _param_widgets(f"src_{index}", normalizers[normalizer_name].params)
    uploaded = st.file_uploader("Upload a file", key=f"src_{index}_file")
    pasted = st.text_area(
        "…or paste records",
        key=f"src_{index}_text",
        height=120,
        placeholder="CSV for stub, log lines for syslog.",
    )
    if uploaded is not None:
        content = uploaded.getvalue().decode("utf-8", errors="replace")
        origin = uploaded.name
    else:
        content, origin = pasted, f"source-{index + 1}"
    return (
        {"normalizer": normalizer_name, "source_format": source_format, "params": params},
        content,
        origin,
    )


def _detector_widgets(index: int, detectors: dict[str, Any], produced: set[str]) -> dict[str, Any]:
    """One detector instance: which adapter, and its params (ADR-0023).

    The mirror of ``_source_widgets``, and for the same reason. ``PipelineSpec.detectors``
    is a *list* of instances, but this page used to render it as a multiselect over
    *names* — a set. That was lossy in one specific, costly way: names are unique, and the
    parameter widgets were keyed by name, so an operator could never configure the same
    adapter twice with different params. Every preset-bearing detector (``log_burst``, and
    now the flow detectors) was therefore limited to exactly one preset per run from the
    UI, while the YAML and API paths had always allowed several.

    Keying by *index* instead of by name is the whole fix; everything else here is the
    shape ``_source_widgets`` already established.
    """
    names = sorted(detectors)
    name = st.selectbox(
        f"Detector {index + 1}",
        names,
        # Preselect a detector the page's sources can actually feed AND that is ready to
        # run. Streamlit honours `index=` only on a widget's first render, so this reads the
        # sources at their defaults; switching the source afterwards leaves the operator's
        # choice alone (and the guard speaks up if it no longer fits). Matching rendered
        # tokens is safe only because normalizers produce lane tokens and detectors consume
        # them — the typed version of this joint is `compat.lanes_in`.
        #
        # "Ready to run" is the second condition, and it is not cosmetic: a detector with an
        # `artifact_param` is *untrained* until one is named, and a run refuses an untrained
        # detector outright (ADR-0022). Preferring one that needs no cached fit is what stops
        # the page opening on a chain that cannot run — which is exactly what happened when
        # `meter_iforest` joined the registry and sorted ahead of `stub_threshold`. Declared,
        # not name-sniffed: `artifact_param` is published by /modules for this purpose.
        index=(
            _preferred_index(
                names,
                lambda n: _feeds(detectors[n], produced) and _ready(detectors[n]),
                lambda n: _feeds(detectors[n], produced),
            )
            if index == 0
            else 0
        ),
        key=f"det_{index}_name",
    )
    with st.expander(f"{name} parameters"):
        params = _param_widgets(f"det_{index}", detectors[name].params)
    return {"name": name, "params": params}


def compose_page(client: ControlClient) -> None:
    st.title("Compose & run a pipeline")
    st.caption(
        "Pick input sources and modules, validate the chain, then trigger a run. "
        "Results appear under Monitor → Triage."
    )
    try:
        catalogue = client.modules()
    except _API_ERRORS as exc:
        st.error(
            f"Pipeline API unreachable ({exc}). Start it with: "
            "IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api.control"
        )
        return

    normalizers = {module.name: module for module in catalogue.normalizers}
    detectors = {module.name: module for module in catalogue.detectors}
    engines = {module.name: module for module in catalogue.alert_engines}
    sinks = {module.name: module for module in catalogue.sinks}
    if not (normalizers and detectors and engines and sinks):
        st.error("The Pipeline API reports no adapters — nothing to compose.")
        return

    st.subheader("1 · Input sources")
    st.caption("Add one source per file/format; each is routed to its normalizer and pooled.")
    count = int(
        st.number_input(
            "How many input sources?", min_value=1, max_value=10, value=1, step=1, key="compose_n"
        )
    )
    sources: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    for index in range(count):
        descriptor, content, origin = _source_widgets(index, normalizers)
        sources.append(descriptor)
        inputs.append({"content": content, "origin": origin})

    st.subheader("2 · Detectors")
    st.caption(
        "Add one row per detector instance. The same adapter may appear more than once — "
        "that is how you run two presets (e.g. a fan-out and an exfiltration rule) at once."
    )
    produced_lanes = {
        token for source in sources for token in normalizers[source["normalizer"]].produces
    }
    detector_count = int(
        st.number_input(
            "How many detectors?", min_value=1, max_value=10, value=1, step=1, key="compose_det_n"
        )
    )
    detector_specs: list[dict[str, Any]] = [
        _detector_widgets(index, detectors, produced_lanes) for index in range(detector_count)
    ]

    st.subheader("3 · Alert engine")
    engine_name = st.selectbox("Alert engine", sorted(engines), key="compose_engine")
    with st.expander(f"{engine_name} parameters"):
        engine_params = _param_widgets(f"eng_{engine_name}", engines[engine_name].params)

    st.subheader("4 · Result sinks")
    default_sinks = ["postgres"] if "postgres" in sinks else sorted(sinks)[:1]
    selected_sinks = st.multiselect(
        "Sinks",
        sorted(sinks),
        default=default_sinks,
        key="compose_sinks",
        help="Where results go. The 'postgres' store is what the Triage view reads.",
    )
    sink_specs: list[dict[str, Any]] = []
    for name in selected_sinks:
        with st.expander(f"{name} parameters"):
            params = _param_widgets(f"sink_{name}", sinks[name].params)
        sink_specs.append({"name": name, "params": params})

    if not detector_specs:
        st.info("Pick at least one detector to continue.")
        return
    if not sink_specs:
        st.info("Pick at least one sink so results have somewhere to go.")
        return

    spec: dict[str, Any] = {
        "sources": sources,
        "detectors": detector_specs,
        "alert_engine": {"name": engine_name, "params": engine_params},
        "sinks": sink_specs,
    }

    st.subheader("5 · Validate & run")
    try:
        validation = client.validate(spec)
    except _API_ERRORS as exc:
        st.error(f"Validation request failed: {exc}")
        return
    for issue in validation.issues:
        line = f"[{issue.stage}] {issue.message}"
        if issue.level == "error":
            st.error(line)
        else:
            st.warning(line)
    if validation.ok and not validation.issues:
        st.success("Chain is compatible.")

    ready = validation.ok and all(item["content"].strip() for item in inputs)
    if st.button("Run pipeline", disabled=not ready, key="compose_run"):
        try:
            result = client.run(spec, inputs=inputs)
        except _API_ERRORS as exc:
            st.error(f"Run failed: {exc}")
            return
        st.success("Run complete.")
        columns = st.columns(4)
        for column, key in zip(
            columns, ("events", "malformed", "detections", "alerts"), strict=True
        ):
            column.metric(key, getattr(result, key))
        for warning in result.warnings:
            st.warning(warning)
        st.caption(f"Written to: {', '.join(result.destinations) or 'nothing'}")
        # A receipt, not a results view: the ranked, explained alerts live on the read
        # plane. Hand off to Triage rather than half-rendering them here, so the run
        # surface (write) and the browse surface (read) each do one job. Triage reads the
        # postgres store only, so the hand-off is shown only when that sink was written.
        wrote_postgres = "postgres" in selected_sinks
        if result.alerts and wrote_postgres:
            st.info(
                f"{result.alerts} alert(s) written — see them ranked and explained "
                "under **Monitor → Triage**."
            )
        elif not wrote_postgres:
            st.caption("No postgres sink selected — results are in the chosen sink(s), not Triage.")


def eval_ui_enabled() -> bool:
    return os.environ.get(EVAL_UI_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def load_eval_report(path: Path) -> "EvaluationReport | None":
    """Read a saved benchmark report, or None if there is not a valid one there yet.

    ``iotshield.eval`` is imported here rather than at module scope on purpose: the
    evaluation plane is meant to be *droppable* from a product image (ADR-0021 D1), and a
    top-level import would make the dashboard fail at startup without it — turning the
    runtime env gate into a hard dependency it was supposed to avoid.
    """
    from iotshield.eval.report import EvaluationReport

    if not path.is_file():
        return None
    try:
        return EvaluationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError, OSError):
        return None


PROTOCOL_HELP = {
    "none": "Judge with each detector exactly as configured (its shipped artifact).",
    "prefix": (
        "Relearn 'normal' from a reserved head of each asset's clock, which is then NOT "
        "scored — what a real deployment does. Detectors that cannot train are used as "
        "configured and say so."
    ),
}
"""The fit protocols, in the order the radio shows them.

There is no ``holdout``: no bundle format can carry a holdout recording, so the option could
only ever produce a table of zeros (``eval.fit``). Keyed off this dict rather than off
``FitProtocol`` so the page keeps its own help text, but a protocol added to the enum and not
here would simply not be offered — which is the safe direction.
"""


def _normalizer_for(source_format: str, normalizers: dict[str, Any]) -> str:
    """Which registered normalizer accepts this format — asked of the registry, not assumed.

    Follows the capability declarations the same way ``_source_widgets`` does, so a new
    normalizer becomes usable here the moment it is registered.
    """
    return next(
        (name for name in sorted(normalizers) if source_format in normalizers[name].formats),
        "",
    )


def _composed_spec(
    detectors: Sequence[dict[str, Any]],
    engine: str,
    source_formats: Sequence[str],
    normalizers: dict[str, Any],
) -> dict[str, Any]:
    """One spec from one *entry* — whose detectors run together, in a single pipeline.

    This composes a spec from what is on screen, which is the right thing for an exploratory
    comparison and the *wrong* thing for reproducing a committed benchmark: it carries no
    ``assets:`` block and no source or engine parameters, so it is a lookalike of
    ``config/bench/*.yaml`` rather than the thing itself, and the two published different
    numbers for what read as the same run. Reproducing a committed benchmark is now its own
    mode, fed by ``/eval/specs`` (``BenchSpecSummary``); this stays what it always was, and
    the page shows the spec it built either way.

    The unit of comparison is the entry, not the detector. This used to build exactly one
    detector per spec, which made head-to-head easy and made the other experiment impossible:
    several detectors in ONE spec pool their detections into a single alert-engine run, which
    is what produces cross-lane corroboration. With a detector per spec, every alert cited one
    lane by construction, so the corroboration measurement ADR-0026 exists to publish could
    not be expressed from the UI at any setting.

    Both shapes now fall out of the same control. One detector per entry is head-to-head,
    unchanged; three detectors in one entry approximates ``config/bench/trimodal_all.yaml``.
    Nothing combines anything on the user's behalf — the entry says what runs together.

    The sources come from **the corpus's own declared shape**, one per position. Hardcoding a
    single stub-csv source was correct only while every corpus was single-lane telemetry: a
    benchmark checks a case's inputs against the spec's ``sources`` positionally and by
    format, so a flow or multi-lane corpus would appear in the picker and then be refused on
    run (ADR-0026).
    """
    return {
        "sources": [
            {
                "normalizer": _normalizer_for(source_format, normalizers),
                "source_format": source_format,
            }
            for source_format in source_formats
        ],
        "detectors": list(detectors),
        "alert_engine": {"name": engine},
    }


_EVAL_RUN_KEY = "eval_latched_run"

# Modes for a detector that can load a cached fit (ADR-0022). Two exclusive choices rather
# than one form, because an artifact is atomic: its weights are aligned to the features its
# own hyperparameters produced, so editing one knob while keeping the weights is refused at
# construction. Offering the modes separately means the UI never builds that request.
_TRAINED = "Use trained weights"
_SCRATCH = "Fit from scratch with my hyperparameters"

_DEFAULT_ARTIFACT = {"team3_zscore": "skab_zscore"}
"""Which artifact to prefill per detector, so the page is runnable on arrival.

A UI convenience, not a contract: the *factory* default is deliberately ``None`` (no
artifact means untrained, ADR-0022), so there is nothing on the wire to prefill from. A
blank box is still a legal state — it is reported as a blocker rather than silently
building an untrained detector. This table disappears the day ``/eval/detectors``
enumerates each adapter's shipped artifacts, which is the real fix and is why it is
listed beside ``_composed_spec``'s hardcoded normalizer as a known deferral.
"""


_NO_SWEEP = "(no sweep)"


def _sweep_widgets(prefix: str, offered: dict[str, ParamInfo]) -> dict[str, list[Any]]:
    """Pick one numeric hyperparameter and the values to vary it across.

    ``offered`` is keyed by the *swept key* — the qualified ``flow_burst.threshold`` when the
    entry holds several detectors, the bare ``threshold`` when it holds one (see
    ``_offered_knobs``). The key is what the server parses and what the report's label quotes,
    so the menu shows exactly the text that will identify the run.

    One parameter, not many: a 2-D sweep is a surface, and a head-to-head table of 24 rows
    is not how anyone reads one. The server cross-products anyway (the CLI can sweep two),
    so this is a UI judgment about legibility, not a limit on the mechanism.
    """
    # Deferred so the eval plane stays deletable; shared so the dashboard and the CLI
    # cannot disagree about whitespace, empty items or value types.
    from iotshield.eval.sweep import values as parse_values

    if not offered:
        return {}
    key = f"{prefix}_sw"
    options = [_NO_SWEEP, *offered]
    # Editing an entry changes which keys exist, and Streamlit keeps a selectbox's value across
    # that change — but *silently resets it to the first option* when the value is gone, unlike
    # a multiselect, which raises. So a configured sweep would vanish with no message and the
    # run would quietly measure an unswept spec. Say so instead, and reset deliberately.
    stale = st.session_state.get(key)
    if stale is not None and stale not in options:
        st.warning(
            f"The **{stale}** sweep no longer applies to this entry's detectors and has been "
            "cleared. Pick one below if you still want a sweep."
        )
        st.session_state[key] = _NO_SWEEP
    chosen = st.selectbox("Sweep a hyperparameter", options, key=key)
    if chosen == _NO_SWEEP:
        return {}
    default = offered[chosen].default
    raw = st.text_input(
        f"{chosen} values (comma-separated)", value=str(default or ""), key=f"{prefix}_swv"
    )
    swept = parse_values(raw)
    if not swept:
        # Reachable and not an error: several params default to None because their real value
        # comes from a preset, so the box opens empty and there is nothing to sweep yet.
        st.caption(f"Enter comma-separated values to sweep {chosen}.")
        return {}
    st.caption(f"-> {len(swept)} run(s), one per value, compared head to head")
    return {chosen: swept}


def _eval_catalogue() -> tuple["CorporaResponse", list["DetectorSummary"]]:
    """What the Evaluate page needs *before* the user has chosen a mode.

    Deliberately NOT ``st.cache_data``: caching the *result* would replay a stale catalogue
    after the Pipeline API goes away, defeating the saved-report fallback below — which is the
    page's whole degradation story.

    Only these two. The bench-spec list and the normalizer registry each serve exactly one of
    two mutually exclusive modes, and Streamlit re-runs this whole script on every widget
    click — so fetching both here meant one guaranteed-dead round trip per keystroke, and
    ``/eval/specs`` in particular re-walks the corpus root and re-parses every bench YAML
    (measured ~21 ms). Each mode fetches its own.
    """
    client = get_control_client()
    return client.corpora(), client.eval_detectors()


def _run_count(sweep: dict[str, list[Any]]) -> int:
    """How many benchmarks one entry costs: one per swept value, else one."""
    return math.prod(len(values) for values in sweep.values()) if sweep else 1


def _detector_plan(
    summary: "DetectorSummary", protocol: str, prefix: str
) -> tuple[dict[str, Any], str | None, list[ParamInfo]]:
    """One detector's benchmark configuration, what blocks it, and what it offers to sweep.

    Renders the ADR-0022 split as the page's central choice. A detector with no cached-fit
    param (a rule, like ``stub_threshold``) shows only its knobs; one that has an artifact
    shows the two modes, and picking "from scratch" makes the fit protocol load-bearing —
    which the returned problem says out loud, because an untrained model under protocol
    ``none`` scores a row of zeros that reads like a bad detector rather than a skipped one.

    ``prefix`` is supplied by the caller rather than derived from the name, because the same
    detector may now sit in two entries at once (comparing two of its presets is the obvious
    reason to want that). Widget keys have to separate those instances, exactly as
    ``_detector_widgets`` keys the Compose page's rows by index.

    The third return value is what this detector contributes to its *entry's* sweep menu:
    empty in trained mode, since an artifact is atomic and sweeping a knob beside it is the
    request construction refuses. The entry decides what is finally offered, because
    ambiguity is a property of the set (see ``_entry_widgets``).

    The blocker rides beside the plan rather than inside it: the plan is serialized whole as
    the run's cache key, and a UI-only field in there would make an error message part of the
    identity of a benchmark.
    """
    tunables = [param for param in summary.params if param.name != summary.artifact_param]
    params: dict[str, Any]
    sweepable: list[ParamInfo] = []
    problem: str | None = None

    if summary.artifact_param is None:
        params = _param_widgets(prefix, summary.params)
        sweepable = tunables
    else:
        mode = st.radio(
            "Weights",
            [_TRAINED, _SCRATCH],
            key=f"{prefix}_mode",
            horizontal=True,
            label_visibility="collapsed",
        )
        if mode == _TRAINED:
            artifact = next((p for p in summary.params if p.name == summary.artifact_param), None)
            shipped = "" if artifact is None or artifact.default is None else str(artifact.default)
            chosen = st.text_input(
                summary.artifact_param,
                value=shipped or _DEFAULT_ARTIFACT.get(summary.name, ""),
                key=f"{prefix}_art",
                placeholder="artifact id",
                help=(
                    "A committed artifact id. It supplies both the weights and the "
                    "hyperparameters they were fitted with — the two travel together."
                ),
            )
            params = {summary.artifact_param: chosen}
            # Why this detector contributes nothing to the entry's sweep menu. Without it the
            # menu silently gains three options when this radio moves, and the radio lives
            # inside a collapsed expander, so the cause is invisible.
            st.caption(
                f"Trained weights are atomic: the artifact supplies **{summary.name}**'s "
                "hyperparameters too, so there is nothing here to edit or sweep. Switch to "
                f"*{_SCRATCH}* to choose them yourself."
            )
            if not chosen:
                problem = (
                    f"**{summary.name}** is set to use trained weights but no artifact is "
                    "named. Enter an artifact id, or switch it to fit from scratch."
                )
        else:
            params = _param_widgets(prefix, tunables)
            sweepable = tunables
            if protocol == "none":
                problem = (
                    f"**{summary.name}** has no trained weights in this mode, and the fit "
                    "protocol is `none` — it would judge nothing. Pick `prefix` above, or "
                    "switch it back to trained weights."
                )

    return {"name": summary.name, "params": params}, problem, sweepable


def _offered_knobs(by_detector: list[tuple[str, list[ParamInfo]]]) -> dict[str, ParamInfo]:
    """The entry's sweepable knobs, keyed by the text the server will parse.

    Numeric only, because a sweep draws a curve: a preset or a channel list has no axis to
    vary along.

    Qualified with the detector name as soon as an entry holds more than one, which is what
    makes every knob reachable. This menu used to *hide* any name two of the entry's detectors
    shared — ``threshold`` on ``stub_threshold + flow_burst`` — mirroring a server refusal, so
    the dropdown shrank from four entries to one with no explanation. Naming the detector
    removes the ambiguity instead of routing around it.

    A single-detector entry keeps bare names, so the common head-to-head case reads
    ``stub_threshold[threshold=40]`` exactly as it always has and no published label moves.
    """
    qualify = len(by_detector) > 1
    return {
        f"{name}.{param.name}" if qualify else param.name: param
        for name, params in by_detector
        for param in params
        if param.type in ("int", "float")
    }


def _entry_widgets(
    index: int,
    eligible: list[str],
    summaries: dict[str, "DetectorSummary"],
    protocol: str,
) -> tuple[dict[str, Any], list[str]]:
    """One entry: the detectors that run *together*, their params, and one sweep across them.

    An entry becomes exactly one spec, so it is also one row of the head-to-head table. One
    detector per entry is the classic comparison; several is a multi-lane pipeline whose
    alerts can cite more than one lane.

    The sweep lives here rather than on each detector because a spec carries one sweep: the
    server expands an entry into one labelled spec per value, and which knobs are legal to
    vary depends on the whole set, not on any one member.
    """
    st.markdown(f"**Entry {index + 1}**")
    key = f"eval_e{index}_dets"
    # Switching corpus changes which detectors are eligible, and Streamlit keeps a widget's
    # value in session state across that change — so a flow detector picked on `fleet_scan`
    # would survive into `synthetic`, where it is not an option, and Streamlit raises rather
    # than silently dropping it. Pruning before the widget is instantiated is the supported
    # way to reconcile the two, and it keeps whatever remains valid instead of resetting the
    # whole entry.
    if key in st.session_state:
        st.session_state[key] = [name for name in st.session_state[key] if name in eligible]
    # Default to one detector per entry, walking down the eligible list, so a fresh page is
    # the head-to-head everyone expects and multi-detector entries are opt-in.
    default = [eligible[index]] if index < len(eligible) else []
    chosen = st.multiselect(
        f"Detectors in entry {index + 1}",
        eligible,
        default=default,
        key=key,
        help="Two or more run together in one pipeline, so their alerts can corroborate.",
    )

    detectors: list[dict[str, Any]] = []
    problems: list[str] = []
    offered: list[tuple[str, list[ParamInfo]]] = []
    for name in chosen:
        with st.expander(f"{name} — hyperparameters", expanded=False):
            plan, problem, sweepable = _detector_plan(
                summaries[name], protocol, f"eval_e{index}_{name}"
            )
        detectors.append(plan)
        if problem is not None:
            problems.append(problem)
        offered.append((name, sweepable))

    sweep = _sweep_widgets(f"eval_e{index}", _offered_knobs(offered)) if detectors else {}
    return {"label": " + ".join(chosen), "detectors": detectors, "sweep": sweep}, problems


def _labelled(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty entries and make the surviving labels unique.

    A label is a spec's identity in the report, and two entries holding the same detectors
    would otherwise produce two rows with one name — legitimate to want (the same detector
    under two fit protocols is a real experiment) but unreadable unless they are told apart.
    An entry with nothing selected is a transient UI state, not an error, so it is dropped
    rather than blocking the run.
    """
    kept = [entry for entry in entries if entry["detectors"]]
    seen: Counter[str] = Counter()
    for entry in kept:
        seen[entry["label"]] += 1
        if seen[entry["label"]] > 1:
            entry["label"] = f"{entry['label']} ({seen[entry['label']]})"
    return kept


_SHIPPED_SPECS = "Run a committed bench spec"
_COMPOSE_SPECS = "Compose my own"


def _shipped_widgets(corpus_name: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Pick one or more committed ``config/bench/*.yaml`` and run them **verbatim**.

    The point of the mode: what runs is the file, not a reconstruction of it, so a number on
    this page and the same number from the CLI are the same measurement. The specs arrive from
    ``/eval/specs`` already parsed, and the page forwards the spec object untouched — it never
    names a path, and it never edits what it was given.

    Only specs whose source shape matches the corpus are offered, which is the pairing the
    server derives (``eval.bench``). Offering the rest would put a guaranteed alignment failure
    in a dropdown.
    """
    published = get_control_client().eval_specs()
    # A spec the server could not load is published with its reason rather than omitted, so
    # say so here. Dropping it silently is what let a typo an operator introduced while the
    # server was running read as "this corpus has no matching spec" — blaming the corpus for
    # a broken file (ADR-0033).
    broken = [
        f"Bench spec `{item.name}` could not be loaded: {item.error}"
        for item in published
        if item.error
    ]
    fitting = [item for item in published if item.spec is not None and corpus_name in item.corpora]
    if not fitting:
        return [], [
            *broken,
            f"No committed bench spec matches `{corpus_name}`'s input shape. Compose one "
            "instead, or add a spec whose `sources` match.",
        ]
    chosen = st.multiselect(
        "Bench specs",
        [item.name for item in fitting],
        default=[fitting[0].name],
        key="eval_bench_specs",
        help="Each becomes one row of the comparison, exactly as the CLI would run it.",
    )
    picked = [item for item in fitting if item.name in chosen]
    for item in picked:
        st.caption(f"**{item.name}** — {', '.join(item.detectors)}")
    return [
        # No sweep: an artifact is atomic and a committed spec is a fixed measurement. Sweeping
        # one belongs in the compose mode, where the hyperparameters are on screen (ADR-0022).
        {"label": item.name, "spec": item.spec.model_dump(mode="json"), "sweep": {}}
        for item in picked
        if item.spec is not None
    ], broken


def _compose_widgets(
    detectors: list["DetectorSummary"], corpus: Any, protocol: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build specs from detectors picked on screen — the exploratory mode.

    The alert engine is chosen here rather than beside the corpus, because it is only
    meaningful here: a committed bench spec brings its own, so a picker outside this branch is
    a control the user can change while nothing happens.
    """
    # Which detectors this corpus can actually feed, from the lanes the API says it yields.
    # Hardcoding the telemetry lane here was correct only while every corpus was telemetry:
    # once ADR-0026 shipped a flow corpus and a trimodal one, it hid the only detectors that
    # could judge them and offered ones guaranteed to score zero. Both sides of this
    # intersection arrive decoded from /eval, so the page never parses a `lane:` prefix.
    eligible = [item.name for item in detectors if set(item.lanes) & set(corpus.lanes)]
    if not eligible:
        return [], [
            f"No registered detector consumes this corpus's lane(s): "
            f"{', '.join(corpus.lanes) or 'none'}."
        ]

    engine = st.selectbox("Alert engine", ["weighted", "stub"], key="eval_engine")
    st.subheader("Entries")
    st.caption(
        "One entry is one row of the comparison. Put a single detector in each to compare "
        "them head to head; put several in one to run them **together** in a single "
        "pipeline, which is how alerts come to cite more than one lane."
    )
    entry_count = int(
        st.number_input(
            "How many entries?",
            min_value=1,
            max_value=6,
            value=min(2, len(eligible)),
            step=1,
            key="eval_entry_n",
        )
    )
    summaries = {item.name: item for item in detectors}
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for index in range(entry_count):
        entry, entry_problems = _entry_widgets(index, eligible, summaries, protocol)
        entries.append(entry)
        problems.extend(entry_problems)
    plan = _labelled(entries)

    fittable = {item.name for item in detectors if item.fittable}
    picked = {detector["name"] for entry in plan for detector in entry["detectors"]}
    if protocol != "none" and picked and not picked & fittable:
        st.info(
            "None of the selected detectors can be trained; they will be evaluated as "
            "configured and the report will say so."
        )
    # The normalizer registry is fetched here rather than with the page's catalogue: only this
    # mode composes a spec, so asking for it up front was a round trip the shipped-spec mode
    # never used, on every widget click.
    normalizers = {info.name: info for info in get_control_client().modules().normalizers}
    return [
        {
            "label": entry["label"],
            "spec": _composed_spec(entry["detectors"], engine, corpus.source_formats, normalizers),
            "sweep": entry["sweep"],
        }
        for entry in plan
    ], problems


@st.cache_data(show_spinner=False)
def _parse_report(payload: str) -> "EvaluationReport":
    """Parse a cached benchmark payload — cached too, since reruns re-read the same string.

    The run itself is cached, but the *parse* sat outside it, so every widget interaction
    re-parsed the report (measured 9ms on an 880KB report with timelines). Returning the
    model straight from ``_run_benchmark`` would be worse: ``cache_data`` round-trips
    through pickle, measured slower than the JSON parse.
    """
    from iotshield.eval import EvaluationReport

    return EvaluationReport.model_validate_json(payload)


@st.cache_data(show_spinner=False)
def _run_benchmark(corpus: str, specs_json: str, protocol: str) -> str:
    """Run a benchmark through the API and return its JSON.

    Cached on the arguments, which is what makes running one from a Streamlit page safe:
    the script re-executes on *every* interaction, so uncached work here would re-run the
    benchmark on each click. With the cache, it runs on the button press and every later
    interaction (switch spec, switch case) replays instantly. Returns JSON rather than a
    model because the cache wants a serializable value.

    ``specs_json`` is the **finished request body**, not a plan to be turned into one. Composing
    the spec in here made the cache key a description of a spec rather than the spec, so two
    different specs could collide on one entry and — worse — nothing on screen could show what
    actually ran. It is now literally the payload, which makes the key exact and the echo free.

    Sweeps are expanded *server-side* (ADR-0022): the payload carries the values, the API
    cross-products them into labelled specs, and this page never has to know that four
    rows in the report came from one entry.
    """
    report = get_control_client().evaluate(
        corpus=corpus,
        specs=json.loads(specs_json),
        fit_protocol=protocol,
        include_timeline=True,
    )
    return report.model_dump_json()


def _render_timeline(report: "EvaluationReport") -> None:
    """One case's truth / detections / alerts against a shared clock."""
    st.subheader("What actually happened")
    columns = st.columns(2)
    chosen_spec = columns[0].selectbox(
        "Spec", [spec.spec for spec in report.specs], key="eval_timeline_spec"
    )
    selected = next(spec for spec in report.specs if spec.spec == chosen_spec)
    chosen_case = columns[1].selectbox(
        "Case", [case.case for case in selected.cases], key="eval_timeline_case"
    )
    case = next(item for item in selected.cases if item.case == chosen_case)

    if case.timeline is None:
        st.info("This report carries no timelines — re-run with them enabled.")
        return

    from iotshield.eval.report import TIMELINE_SPAN_CAP

    chart = case_timeline(case.timeline.truth, case.timeline.detections, case.timeline.alerts)
    if chart is None:
        st.info("Nothing to draw: no faults, no detections and no alerts on this case.")
    else:
        st.altair_chart(chart, width="stretch")
    if case.timeline.truncated:
        st.warning(
            f"Truncated at {TIMELINE_SPAN_CAP} spans per lane — this detector emits more "
            "detections than a chart can show, which is itself the finding."
        )
    st.caption(
        "**truth** is where the fault really was; **detections** are what the detector "
        "emitted; **alerts** are what the correlation stage compressed them into. The gap "
        "between the truth band's left edge and the first detection is the latency."
    )


def _render_report(report: "EvaluationReport") -> None:
    """Draw a report: head-to-head, per-case variance, one case's timeline, the table."""
    st.caption(
        f"`{report.corpus}` — {report.corpus_description} · {report.cases} case(s), "
        f"{report.incidents} incident(s) · fit protocol **{report.fit_protocol}** · "
        f"generated {report.generated_at:%Y-%m-%d %H:%M} UTC"
    )

    st.subheader("Head to head")
    # One construction: spec.summary() is the whole row, and the chart is a slice of it.
    summary = pd.DataFrame(
        [
            {
                "spec": spec.spec,
                "detectors": ", ".join(spec.detectors),
                **spec.summary(),
                "fitted": f"{spec.cases_fitted}/{len(spec.cases)}",
            }
            for spec in report.specs
        ]
    )
    scores = [{"spec": row["spec"], "event_f1": row["event_f1"]} for _, row in summary.iterrows()]
    # A sweep gets a curve, everything else gets bars: over a quantitative axis the reader
    # sees whether the parameter matters and where it peaks, which is the question the
    # sweep was run to answer and the one a bar chart of decoded labels hides (ADR-0022).
    curve = sweep_curve(scores)
    chart = curve if curve is not None else spec_scores(scores)
    if chart is not None:
        st.altair_chart(chart, width="stretch")
    if curve is not None:
        st.caption(
            "One point per swept value, all other settings held fixed. A flat line means "
            "the hyperparameter does not matter on this corpus — a real result, and a "
            "cheaper one than tuning it."
        )
    st.dataframe(summary, width="stretch", hide_index=True)
    st.caption(
        "`event_f1` is what an analyst experiences (one finding per fault); `point_f1` is "
        "what papers report (per-row coverage). `compression_ratio` is scoreable detections "
        "over scoreable alerts — what the correlation stage absorbed. An empty cell means "
        "*undefined*, not zero — a clean recording has no faults to recall."
    )

    if report.cases > 1:
        st.subheader("Consistency across cases")
        variance = case_variance(
            [
                {"spec": spec.spec, "case": case.case, "event_f1": case.event_f1}
                for spec in report.specs
                for case in spec.cases
            ]
        )
        if variance is not None:
            st.altair_chart(variance, width="stretch")
        st.caption(
            "One dot per recording. A mean hides the shape: a detector scoring 1.0 on most "
            "cases and 0 on a few is a different problem from one that is mediocre "
            "everywhere, and only this view tells them apart."
        )

    _render_timeline(report)

    # Every spec, not just the first: comparing them is the page's whole purpose, and a
    # table pinned to `specs[0]` silently hid the second detector's cases and notes.
    st.subheader("Per case")
    st.dataframe(
        pd.DataFrame(
            [
                {"spec": spec.spec, **case.model_dump(exclude={"precision_at_k", "timeline"})}
                for spec in report.specs
                for case in spec.cases
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    for note in sorted(
        {note for spec in report.specs for case in spec.cases for note in case.notes}
    ):
        st.warning(note)


def _saved_report_fallback(report_path: Path) -> None:
    """No Pipeline API: render a report the CLI wrote, or explain how to make one."""
    report = load_eval_report(report_path)
    if report is None:
        st.info(
            f"Pipeline API unreachable and no saved report at `{report_path}`. Either start "
            "the Pipeline API with IOTSHIELD_EVAL_API=1, or generate a report:"
        )
        st.code(
            "uv run python -m iotshield.eval --corpus synthetic --with-timeline "
            "--spec config/bench/team3.yaml --out " + str(report_path),
            language="bash",
        )
        return
    st.warning("Pipeline API unreachable — showing the last saved report.")
    _render_report(report)


def evaluate_page(report_path: Path) -> None:
    """Compose and run a benchmark, then visualise it.

    Runs are **button-gated and cached** — see ``_run_benchmark`` for why that is what makes
    running one from Streamlit safe. The dashboard stays a pure API client (ADR-0008): it
    POSTs to the Pipeline API and renders, it never computes. When that API is unreachable
    the page falls back to a saved report, so the CLI -> file -> dashboard path keeps
    working and the page degrades instead of breaking.
    """
    st.title("Evaluate — detector benchmarks")
    st.caption(
        "How detectors score against labelled corpora. A research instrument: it needs "
        "ground truth, which production data by definition never has."
    )

    try:
        catalogue, detectors = _eval_catalogue()
    except _API_ERRORS:
        _saved_report_fallback(report_path)
        return

    if not catalogue.corpora:
        st.warning(f"No corpora in `{catalogue.root}`. Build the committed one with:")
        st.code("uv run python -m iotshield.eval generate", language="bash")
        for hint in catalogue.fetchable.values():
            st.caption(hint)
        return

    # A bundle that cannot be loaded is shown as broken rather than hidden — it is listed with
    # its reason (which names the fix) and kept out of the picker, so "my corpus vanished" is
    # never the only feedback.
    for broken in [item for item in catalogue.corpora if item.error]:
        st.error(f"`{broken.name}` cannot be loaded: {broken.error}")
    usable = [item for item in catalogue.corpora if not item.error]
    if not usable:
        return

    columns = st.columns([2, 3])
    corpus_name = columns[0].selectbox("Corpus", [item.name for item in usable], key="eval_corpus")
    corpus = next(item for item in usable if item.name == corpus_name)
    protocol = columns[1].radio(
        "Fit protocol", list(PROTOCOL_HELP), key="eval_protocol", horizontal=True
    )
    st.caption(f"{corpus.cases} case(s) · {corpus.description}")
    if corpus.licence:
        st.caption(f"Provenance: {corpus.provenance} · Licence: {corpus.licence}")

    st.caption(PROTOCOL_HELP[protocol])

    mode = st.radio(
        "Specs", [_SHIPPED_SPECS, _COMPOSE_SPECS], key="eval_spec_mode", horizontal=True
    )
    if mode == _SHIPPED_SPECS:
        request_specs, problems = _shipped_widgets(corpus_name)
    else:
        request_specs, problems = _compose_widgets(detectors, corpus, protocol)

    for problem in problems:
        st.error(problem)
    if request_specs:
        # The exact request, on the page that made it. Neither surface used to display the
        # spec it ran, so a dashboard number and a CLI number that disagreed could only be
        # reconciled by reading the page's source.
        with st.expander(
            f"Spec sent to the API ({len(request_specs)} entr(y/ies))", expanded=False
        ):
            st.json(request_specs)

    total = sum(_run_count(entry["sweep"]) for entry in request_specs)
    if total > len(request_specs):
        st.caption(f"This will run **{total}** benchmark(s) — one per swept value.")

    # Latch the run into session state rather than rendering only on the click. A bare
    # `if not st.button(): return` renders the report *only* on the click rerun — so the
    # first touch of a selectbox inside it (the timeline's spec/case pickers) reruns with
    # the button False and wipes the page. Latching is what lets the cache below pay off:
    # exploring a finished report replays from cache instead of clearing it.
    # The request rides as JSON so the whole selection stays a hashable cache key — and since
    # it IS the request body, two different specs can no longer collide on one entry.
    selection = (corpus_name, json.dumps(request_specs, sort_keys=True), protocol)
    blocked = not request_specs or bool(problems)
    if st.button("Run benchmark", type="primary", disabled=blocked):
        st.session_state[_EVAL_RUN_KEY] = selection

    latched = st.session_state.get(_EVAL_RUN_KEY)
    if latched is None:
        st.caption("Pick specs and run — results are cached, so exploring them is instant.")
        return

    # Changing a control after a run leaves the latched report on screen, which reads as
    # "the control did nothing" — the fit protocol especially, since switching it silently
    # showed the previous protocol's numbers. Say so rather than re-running unasked: a
    # benchmark is not something to trigger on a stray click.
    if latched != selection:
        previous = ", ".join(entry["label"] for entry in json.loads(latched[1]))
        st.warning(
            f"Showing the previous run (corpus **{latched[0]}**, {previous}, "
            f"fit protocol **{latched[2]}**). "
            "Press **Run benchmark** to apply the settings above."
        )

    with st.spinner("Benchmarking…"):
        try:
            payload = _run_benchmark(*latched)
        except _API_ERRORS as exc:
            st.error(f"Benchmark failed: {exc}")
            return
    _render_report(_parse_report(payload))


def main() -> None:
    st.set_page_config(page_title="iotshield", layout="wide")
    st.sidebar.title("iotshield")
    # Modes mirror the planes: Operate (write — compose & run, backed by the Pipeline API)
    # vs Monitor (read — browse results, backed by the read API). Grouping them keeps the
    # analyst's daily view free of the operator's controls; a future split into two
    # deployables is then just promoting the Operate mode.
    #
    # Evaluate is a third plane, off unless a deployment opts in (ADR-0021): Monitor and
    # Operate *use* the pipeline, while Evaluate *judges* it — a different question, over
    # labelled corpora rather than production data, for a researcher rather than an analyst.
    modes = ["Monitor", "Operate"] + (["Evaluate"] if eval_ui_enabled() else [])
    mode = st.sidebar.radio("Mode", modes)

    # Operate talks only to the Pipeline API and handles its own reachability; Evaluate
    # reads a file and talks to nothing. Both return before the read-side health gate the
    # Monitor views depend on.
    if mode == "Operate":
        compose_page(get_control_client())
        return
    if mode == "Evaluate":
        report_path = Path(os.environ.get(EVAL_REPORT_ENV, DEFAULT_EVAL_REPORT))
        # Boundary around the WHOLE page, not around its first call. evaluate_page guards
        # `_eval_catalogue` and `_run_benchmark` individually, but the two spec-composition
        # calls between them were unguarded — and one of those is the DEFAULT mode, so a
        # restart of pipeline-api mid-session raised a raw traceback where ADR-0008 requires
        # a friendly error, and the saved-report fallback that exists for exactly this never
        # ran. Guarding here rather than adding a third inner try keeps one boundary for the
        # page, which is what stops the next call being forgotten again (ADR-0033).
        try:
            evaluate_page(report_path)
        except _API_ERRORS:
            _saved_report_fallback(report_path)
        return

    page = st.sidebar.radio("View", ["Triage", "Assets & telemetry", "Data health"])

    client = get_client()
    try:
        client.health()
    except _API_ERRORS:
        api_url = os.environ.get(API_URL_ENV, DEFAULT_API_URL)
        st.error(
            f"API unreachable at {api_url}. Start it with: "
            "IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"
        )
        st.stop()

    try:
        if page == "Triage":
            triage_page(client)
        elif page == "Assets & telemetry":
            assets_page(client)
        else:
            data_health_page(client)
    except _API_ERRORS as exc:
        # The health check above covers "API never started"; this boundary covers
        # a single endpoint erroring, the API dying mid-rerun, or a response that
        # fails contract validation — friendly error, never a traceback (ADR-0008).
        st.error(f"API request failed while rendering this page: {exc}")
