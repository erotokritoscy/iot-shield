"""iotshield dashboard — pure API client (never touches storage; ADR-0008).

Run:    uv run streamlit run src/iotshield/dashboard/streamlit_app.py
Config: IOTSHIELD_API_URL (default http://127.0.0.1:8000)
"""

import json
import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st
from pydantic import ValidationError

from iotshield.api.control import ParamInfo
from iotshield.contracts import Asset, Lane, PrioritizedAlert
from iotshield.dashboard.charts import severity_bar, telemetry_timeline
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient

API_URL_ENV = "IOTSHIELD_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8000"
PIPELINE_API_URL_ENV = "IOTSHIELD_PIPELINE_API_URL"
DEFAULT_PIPELINE_API_URL = "http://127.0.0.1:8100"

# Everything that can go wrong talking to the API: transport/status errors
# (httpx), a response that no longer matches a contract (ValidationError — the
# whole point of the typed client), or a non-JSON body (JSONDecodeError). The
# dashboard renders these as st.error, never a traceback (ADR-0008).
_API_ERRORS = (httpx.HTTPError, ValidationError, json.JSONDecodeError)


def get_client() -> ApiClient:
    return ApiClient.from_url(os.environ.get(API_URL_ENV, DEFAULT_API_URL))


def get_control_client() -> ControlClient:
    return ControlClient.from_url(os.environ.get(PIPELINE_API_URL_ENV, DEFAULT_PIPELINE_API_URL))


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
    # Telemetry-only lane: the timeline plots telemetry values, so overlaying
    # detections from other lanes (e.g. logs) would draw red bands over a series
    # they don't belong to.
    detections = client.detections(asset_id=selected.asset_id, lane=Lane.TELEMETRY)
    chart = telemetry_timeline(events, detections)
    if chart is None:
        st.info("No telemetry with numeric values for this asset.")
    else:
        st.altair_chart(chart, width="stretch")
        st.caption(
            "Blue line: telemetry values. Red bands/rules: detection windows "
            "(details in the table below)."
        )

    if detections:
        st.subheader("Detections")
        st.dataframe(
            pd.DataFrame(
                {
                    "method": [d.method for d in detections],
                    "score": [d.score for d in detections],
                    "threshold": [d.threshold for d in detections],
                    "window start": [d.window_start for d in detections],
                }
            ),
            hide_index=True,
        )


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


def _param_widgets(prefix: str, params: list[ParamInfo]) -> dict[str, Any]:
    # One input per adapter param, typed off the /modules schema. Values are still
    # validated by the guard and the contracts, so free-text entry is safe here.
    values: dict[str, Any] = {}
    for param in params:
        key = f"{prefix}_{param.name}"
        if param.type == "float":
            values[param.name] = st.number_input(
                param.name, value=float(param.default or 0.0), key=key
            )
        elif param.type == "int":
            values[param.name] = st.number_input(
                param.name, value=int(param.default or 0), step=1, key=key
            )
        else:
            default = "" if param.default is None else str(param.default)
            values[param.name] = st.text_input(param.name, value=default, key=key)
    return values


def _source_widgets(index: int, normalizers: dict[str, Any]) -> tuple[dict[str, str], str, str]:
    """One input source: normalizer + format picker, and its content (upload or paste).

    Returns the source descriptor (what the guard/spec need), the raw content, and a
    provenance origin. A run carries a list of these, so one run can mix modalities (a
    telemetry CSV alongside syslog) that pool into the detector lanes (ADR-0019).
    """
    st.markdown(f"**Source {index + 1}**")
    normalizer_name = st.selectbox("Normalizer", sorted(normalizers), key=f"src_{index}_norm")
    formats = normalizers[normalizer_name].formats
    source_format = (
        st.selectbox("Input format", formats, key=f"src_{index}_fmt")
        if len(formats) > 1
        else (formats[0] if formats else "")
    )
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
    return {"normalizer": normalizer_name, "source_format": source_format}, content, origin


def _default_detectors(detectors: dict[str, Any], produced: set[str]) -> list[str]:
    """The detector to preselect: one the page's sources can actually feed.

    Registry-driven rather than hardcoded, which is the whole point: "whichever name sorts
    first" silently became wrong the moment the registry held a detector for a second lane
    (ADR-0020), preselecting a *log* detector beside the default telemetry source — a chain
    the guard immediately warns about. Streamlit honours ``default=`` only on a widget's
    first render, so this reads the sources at their defaults too; switching the source
    afterwards leaves the operator's own selection alone (and the guard speaks up if it no
    longer fits). Returning nothing when nothing fits is deliberate — the caller turns that
    into "pick a detector", which beats preselecting one the guard would flag.

    Matching rendered tokens is safe only because normalizers produce lane tokens and
    detectors consume them; the typed version of this joint is ``compat.lanes_in``. If the
    UI ever needs another axis, decode lanes server-side beside ``ModuleInfo.formats``
    rather than growing a string scheme here.
    """
    return [name for name in sorted(detectors) if set(detectors[name].consumes) & produced][:1]


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
    sources: list[dict[str, str]] = []
    inputs: list[dict[str, str]] = []
    for index in range(count):
        descriptor, content, origin = _source_widgets(index, normalizers)
        sources.append(descriptor)
        inputs.append({"content": content, "origin": origin})

    st.subheader("2 · Detectors")
    produced_lanes = {
        token for source in sources for token in normalizers[source["normalizer"]].produces
    }
    selected = st.multiselect(
        "Detectors",
        sorted(detectors),
        default=_default_detectors(detectors, produced_lanes),
        key="compose_detectors",
    )
    detector_specs: list[dict[str, Any]] = []
    for name in selected:
        with st.expander(f"{name} parameters"):
            params = _param_widgets(f"det_{name}", detectors[name].params)
        detector_specs.append({"name": name, "params": params})

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


def main() -> None:
    st.set_page_config(page_title="iotshield", layout="wide")
    st.sidebar.title("iotshield")
    # Two modes, mirroring the two planes: Operate (write — compose & run, backed by
    # the Pipeline API) vs Monitor (read — browse results, backed by the read API).
    # Grouping them keeps the analyst's daily view free of the operator's controls;
    # a future split into two deployables is then just promoting the Operate mode.
    mode = st.sidebar.radio("Mode", ["Monitor", "Operate"])

    # Operate talks only to the Pipeline API and handles its own reachability, so it
    # returns before the read-side health gate the Monitor views depend on.
    if mode == "Operate":
        compose_page(get_control_client())
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
