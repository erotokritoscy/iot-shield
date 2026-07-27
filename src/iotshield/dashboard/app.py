"""iotshield dashboard — pure API client (never touches storage; ADR-0008).

Run:    uv run streamlit run src/iotshield/dashboard/streamlit_app.py
Config: IOTSHIELD_API_URL (default http://127.0.0.1:8000)
"""

import json
import math
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd
import streamlit as st
from pydantic import ValidationError

from iotshield.api.control import ParamInfo
from iotshield.contracts import Asset, Lane, PrioritizedAlert
from iotshield.dashboard.charts import (
    case_timeline,
    case_variance,
    severity_bar,
    spec_scores,
    sweep_curve,
    telemetry_timeline,
)
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient
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
    if selected.recommended_action:
        st.markdown(f"**Recommended action:** {selected.recommended_action}")
    st.markdown("**Score components** (the explainability receipt):")
    st.table(pd.DataFrame([selected.score_components]))

    st.markdown("**Evidence** — the detections behind this alert:")
    evidence = client.alert_detections(selected.alert_id)
    st.dataframe(
        pd.DataFrame(
            {
                "method": [d.method for d in evidence],
                "lane": [d.lane.value for d in evidence],
                "score": [d.score for d in evidence],
                "threshold": [d.threshold for d in evidence],
                "window start": [d.window_start for d in evidence],
                "window end": [d.window_end for d in evidence],
            }
        ),
        hide_index=True,
    )
    # The per-detection evidence payload (top peers, ports, matched messages, ...) is what
    # makes the finding actionable — "cam-07 contacted 41 destinations" rather than a bare
    # score. It reached the API but nothing rendered it, so the analyst never saw it.
    for detection in evidence:
        if detection.evidence:
            with st.expander(f"{detection.method} — why it fired"):
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


def _first_index(names: list[str], fits: Callable[[str], bool]) -> int:
    """Preselect the first name that fits, else fall back to the first entry.

    Both pickers need this and neither may preselect "whichever name sorts first": that was
    silently wrong for detectors the moment a second lane arrived (ADR-0020), and it became
    wrong for sources when ``flow`` was registered, since it sorts before ``stub`` — a fresh
    session would have opened on a flow chain fed telemetry CSV. Falling back to 0 rather
    than to nothing is deliberate: the guard reports a mismatched chain, which is more
    useful than a row that contributes no adapter at all.
    """
    return next((index for index, name in enumerate(names) if fits(name)), 0)


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
        index=_first_index(names, lambda name: STUB_CSV_FORMAT in normalizers[name].formats),
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
        # Preselect a detector the page's sources can actually feed. Streamlit honours
        # `index=` only on a widget's first render, so this reads the sources at their
        # defaults; switching the source afterwards leaves the operator's choice alone (and
        # the guard speaks up if it no longer fits). Matching rendered tokens is safe only
        # because normalizers produce lane tokens and detectors consume them — the typed
        # version of this joint is `compat.lanes_in`.
        index=(
            _first_index(names, lambda n: bool(set(detectors[n].consumes) & produced))
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
        "Relearn 'normal' from each case's own data before its first fault — what a real "
        "deployment does. Detectors that cannot train are used as configured and say so."
    ),
    "holdout": "Relearn from a separate clean recording the corpus supplies.",
}


def _normalizer_for(source_format: str, normalizers: dict[str, Any]) -> str:
    """Which registered normalizer accepts this format — asked of the registry, not assumed.

    Follows the capability declarations the same way ``_source_widgets`` does, so a new
    normalizer becomes usable here the moment it is registered.
    """
    return next(
        (name for name in sorted(normalizers) if source_format in normalizers[name].formats),
        "",
    )


def _bench_spec(
    detector: str,
    params: dict[str, Any],
    engine: str,
    source_formats: Sequence[str],
    normalizers: dict[str, Any],
) -> dict[str, Any]:
    """One spec per detector — because several detectors in ONE spec run *together*.

    The researcher's mental model is "compare these detectors", but head-to-head comparison
    needs N specs of one detector each. Building them here is the difference between ticking
    two boxes and hand-writing two YAML files (ADR-0021 D6).

    The sources come from **the corpus's own declared shape**, one per position. This used to
    be a hardcoded single stub-csv source, which was correct only while every corpus was
    single-lane telemetry: a benchmark checks a case's inputs against the spec's ``sources``
    positionally and by format, so a flow or multi-lane corpus would appear in the picker and
    then be refused on run (ADR-0026).
    """
    return {
        "sources": [
            {
                "normalizer": _normalizer_for(source_format, normalizers),
                "source_format": source_format,
            }
            for source_format in source_formats
        ],
        "detectors": [{"name": detector, "params": params}],
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
listed beside ``_bench_spec``'s hardcoded normalizer as a known deferral.
"""


def _sweep_widgets(prefix: str, params: list[ParamInfo]) -> dict[str, list[Any]]:
    """Pick one numeric hyperparameter and the values to vary it across.

    One parameter, not many: a 2-D sweep is a surface, and a head-to-head table of 24 rows
    is not how anyone reads one. The server cross-products anyway (the CLI can sweep two),
    so this is a UI judgment about legibility, not a limit on the mechanism.
    """
    # Deferred so the eval plane stays deletable; shared so the dashboard and the CLI
    # cannot disagree about whitespace, empty items or value types.
    from iotshield.eval.sweep import values as parse_values

    numeric = [param.name for param in params if param.type in ("int", "float")]
    if not numeric:
        return {}
    chosen = st.selectbox("Sweep a hyperparameter", ["(no sweep)", *numeric], key=f"{prefix}_sw")
    if chosen == "(no sweep)":
        return {}
    default = next((p.default for p in params if p.name == chosen), None)
    raw = st.text_input(
        f"{chosen} values (comma-separated)", value=str(default or ""), key=f"{prefix}_swv"
    )
    swept = parse_values(raw)
    if not swept:
        return {}
    st.caption(f"-> {len(swept)} run(s), one per value, compared head to head")
    return {chosen: swept}


def _eval_catalogue() -> tuple["CorporaResponse", list["DetectorSummary"]]:
    """The Evaluate page's two catalogue calls.

    Deliberately NOT ``st.cache_data``: caching the *result* would replay a stale
    catalogue after the Pipeline API goes away, defeating the saved-report fallback below
    — which is the page's whole degradation story. The server builds this response once
    (``api/evaluation.py``), so the per-rerun cost is two local round-trips against a
    precomputed payload, not repeated introspection.
    """
    client = get_control_client()
    return client.corpora(), client.eval_detectors()


def _run_count(sweep: dict[str, list[Any]]) -> int:
    """How many benchmarks one detector's plan costs: one per swept value, else one."""
    return math.prod(len(values) for values in sweep.values()) if sweep else 1


def _detector_plan(summary: "DetectorSummary", protocol: str) -> tuple[dict[str, Any], str | None]:
    """One detector's benchmark configuration, plus anything blocking the run.

    Renders the ADR-0022 split as the page's central choice. A detector with no cached-fit
    param (a rule, like ``stub_threshold``) shows only its knobs; one that has an artifact
    shows the two modes, and picking "from scratch" makes the fit protocol load-bearing —
    which the returned problem says out loud, because an untrained model under protocol
    ``none`` scores a row of zeros that reads like a bad detector rather than a skipped one.

    The blocker rides beside the entry rather than inside it: the entry is serialized whole
    as the run's cache key, and a UI-only field in there would make an error message part
    of the identity of a benchmark.
    """
    prefix = f"eval_{summary.name}"
    tunables = [param for param in summary.params if param.name != summary.artifact_param]
    params: dict[str, Any]
    sweep: dict[str, list[Any]] = {}
    problem: str | None = None

    if summary.artifact_param is None:
        params = _param_widgets(prefix, summary.params)
        sweep = _sweep_widgets(prefix, tunables)
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
            if not chosen:
                problem = (
                    f"**{summary.name}** is set to use trained weights but no artifact is "
                    "named. Enter an artifact id, or switch it to fit from scratch."
                )
        else:
            params = _param_widgets(prefix, tunables)
            sweep = _sweep_widgets(prefix, tunables)
            if protocol == "none":
                problem = (
                    f"**{summary.name}** has no trained weights in this mode, and the fit "
                    "protocol is `none` — it would judge nothing. Pick `prefix` or "
                    "`holdout` above, or switch it back to trained weights."
                )

    return {"name": summary.name, "params": params, "sweep": sweep}, problem


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
def _run_benchmark(
    corpus: str, plan_json: str, engine: str, protocol: str, formats_json: str
) -> str:
    """Run a benchmark through the API and return its JSON.

    Cached on the arguments, which is what makes running one from a Streamlit page safe:
    the script re-executes on *every* interaction, so uncached work here would re-run the
    benchmark on each click. With the cache, it runs on the button press and every later
    interaction (switch spec, switch case) replays instantly. Returns JSON rather than a
    model because the cache wants a serializable value — and takes the plan as JSON for
    the same reason, since it is the cache key as well as the request.

    Sweeps are expanded *server-side* (ADR-0022): the plan carries the values, the API
    cross-products them into labelled specs, and this page never has to know that four
    rows in the report came from one detector.
    """
    plan = json.loads(plan_json)
    # The corpus's input shape is part of the cache key, not looked up here: two corpora with
    # different shapes must never share a cached result, and the catalogue is a live fetch.
    source_formats = json.loads(formats_json)
    normalizers = {info.name: info for info in get_control_client().modules().normalizers}
    report = get_control_client().evaluate(
        corpus=corpus,
        specs=[
            {
                "label": item["name"],
                "spec": _bench_spec(
                    item["name"], item["params"], engine, source_formats, normalizers
                ),
                "sweep": item["sweep"],
            }
            for item in plan
        ],
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
        "what papers report (per-row coverage). `compression_ratio` is detections-per-incident "
        "over alerts-per-incident — what the correlation stage absorbed. An empty cell means "
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

    columns = st.columns([2, 2, 3])
    corpus_name = columns[0].selectbox(
        "Corpus", [item.name for item in catalogue.corpora], key="eval_corpus"
    )
    corpus = next(item for item in catalogue.corpora if item.name == corpus_name)
    engine = columns[1].selectbox("Alert engine", ["weighted", "stub"], key="eval_engine")
    protocol = columns[2].radio(
        "Fit protocol", list(PROTOCOL_HELP), key="eval_protocol", horizontal=True
    )
    st.caption(f"{corpus.cases} case(s) · {corpus.description}")
    if corpus.licence:
        st.caption(f"Provenance: {corpus.provenance} · Licence: {corpus.licence}")

    # Only telemetry detectors can judge the telemetry corpora we ship; offering a log
    # detector here would build a spec whose format the API rightly refuses. The lane comes
    # back decoded, so this compares against the Lane enum rather than a wire prefix.
    choices = [item.name for item in detectors if Lane.TELEMETRY.value in item.lanes]
    fittable = {item.name for item in detectors if item.fittable}
    chosen = st.multiselect(
        "Detectors to compare", choices, default=choices[:2], key="eval_detectors"
    )
    st.caption(PROTOCOL_HELP[protocol])
    if protocol != "none" and chosen and not set(chosen) & fittable:
        st.info(
            "None of the selected detectors can be trained; they will be evaluated as "
            "configured and the report will say so."
        )

    # Each detector's own hyperparameters, weights source and sweep. In an expander because
    # the defaults are the common case: the page still opens as "tick two boxes and run".
    by_name = {item.name: item for item in detectors}
    plan = []
    problems = []
    for name in chosen:
        with st.expander(f"{name} — hyperparameters", expanded=False):
            entry, problem = _detector_plan(by_name[name], protocol)
        plan.append(entry)
        problems.append(problem)
    for problem in filter(None, problems):
        st.error(problem)

    total = sum(_run_count(entry["sweep"]) for entry in plan)
    if total > len(plan):
        st.caption(f"This will run **{total}** benchmark(s) — one per swept value.")

    # Latch the run into session state rather than rendering only on the click. A bare
    # `if not st.button(): return` renders the report *only* on the click rerun — so the
    # first touch of a selectbox inside it (the timeline's spec/case pickers) reruns with
    # the button False and wipes the page. Latching is what lets the cache below pay off:
    # exploring a finished report replays from cache instead of clearing it.
    # The plan is carried as JSON so the whole selection stays a hashable cache key.
    selection = (
        corpus_name,
        json.dumps(plan, sort_keys=True),
        engine,
        protocol,
        # The corpus's input shape rides in the cache key: two corpora of different shapes
        # produce different specs, so a shared cache entry would replay the wrong run.
        json.dumps(list(corpus.source_formats)),
    )
    blocked = not chosen or any(problems)
    if st.button("Run benchmark", type="primary", disabled=blocked):
        st.session_state[_EVAL_RUN_KEY] = selection

    latched = st.session_state.get(_EVAL_RUN_KEY)
    if latched is None:
        st.caption("Pick detectors and run — results are cached, so exploring them is instant.")
        return

    # Changing a control after a run leaves the latched report on screen, which reads as
    # "the control did nothing" — the fit protocol especially, since switching it silently
    # showed the previous protocol's numbers. Say so rather than re-running unasked: a
    # benchmark is not something to trigger on a stray click.
    if latched != selection:
        previous = ", ".join(item["name"] for item in json.loads(latched[1]))
        st.warning(
            f"Showing the previous run (corpus **{latched[0]}**, {previous}, "
            f"fit protocol **{latched[3]}**). "
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
        evaluate_page(Path(os.environ.get(EVAL_REPORT_ENV, DEFAULT_EVAL_REPORT)))
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
