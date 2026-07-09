"""iotshield dashboard — pure API client (never touches storage; ADR-0008).

Run:    uv run streamlit run src/iotshield/dashboard/streamlit_app.py
Config: IOTSHIELD_API_URL (default http://127.0.0.1:8000)
"""

import json
import os

import httpx
import pandas as pd
import streamlit as st
from pydantic import ValidationError

from iotshield.contracts import Asset, Lane, PrioritizedAlert
from iotshield.dashboard.charts import severity_bar, telemetry_timeline
from iotshield.dashboard.client import ApiClient

API_URL_ENV = "IOTSHIELD_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8000"

# Everything that can go wrong talking to the API: transport/status errors
# (httpx), a response that no longer matches a contract (ValidationError — the
# whole point of the typed client), or a non-JSON body (JSONDecodeError). The
# dashboard renders these as st.error, never a traceback (ADR-0008).
_API_ERRORS = (httpx.HTTPError, ValidationError, json.JSONDecodeError)


def get_client() -> ApiClient:
    return ApiClient.from_url(os.environ.get(API_URL_ENV, DEFAULT_API_URL))


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


def main() -> None:
    st.set_page_config(page_title="iotshield", layout="wide")
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

    page = st.sidebar.radio("View", ["Triage", "Assets & telemetry", "Data health"])
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
