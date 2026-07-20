"""iotshield dashboard — pure API client (never touches storage; ADR-0008).

Run:    uv run streamlit run src/iotshield/dashboard/streamlit_app.py
Config: IOTSHIELD_API_URL (default http://127.0.0.1:8000)
"""

import json
import os
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
    telemetry_timeline,
)
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV_FORMAT

if TYPE_CHECKING:  # type-only: the runtime imports are deferred, see load_eval_report
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


def _bench_spec(detector: str, engine: str) -> dict[str, Any]:
    """One spec per detector — because several detectors in ONE spec run *together*.

    The researcher's mental model is "compare these detectors", but head-to-head comparison
    needs N specs of one detector each. Building them here is the difference between ticking
    two boxes and hand-writing two YAML files (ADR-0021 D6).
    """
    return {
        "sources": [{"normalizer": "stub", "source_format": STUB_CSV_FORMAT}],
        "detectors": [{"name": detector}],
        "alert_engine": {"name": engine},
    }


@st.cache_data(show_spinner=False)
def _run_benchmark(corpus: str, detectors: tuple[str, ...], engine: str, protocol: str) -> str:
    """Run a benchmark through the API and return its JSON.

    Cached on the arguments, which is what makes running one from a Streamlit page safe:
    the script re-executes on *every* interaction, so uncached work here would re-run the
    benchmark on each click. With the cache, it runs on the button press and every later
    interaction (switch spec, switch case) replays instantly. Returns JSON rather than a
    model because the cache wants a serializable value.
    """
    report = get_control_client().evaluate(
        corpus=corpus,
        specs=[{"label": name, "spec": _bench_spec(name, engine)} for name in detectors],
        fit_protocol=protocol,
        include_timeline=True,
    )
    return report.model_dump_json()


def _summary_frame(report: "EvaluationReport") -> pd.DataFrame:
    return pd.DataFrame(
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
    chart = spec_scores(
        [{"spec": spec.spec, "event_f1": spec.summary()["event_f1"]} for spec in report.specs]
    )
    if chart is not None:
        st.altair_chart(chart, width="stretch")
    st.dataframe(_summary_frame(report), width="stretch", hide_index=True)
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

    selected = report.specs[0]
    st.subheader(f"{selected.spec} — per case")
    st.dataframe(
        pd.DataFrame(
            [case.model_dump(exclude={"precision_at_k", "timeline"}) for case in selected.cases]
        ),
        width="stretch",
        hide_index=True,
    )
    for note in sorted({note for case in selected.cases for note in case.notes}):
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

    client = get_control_client()
    try:
        catalogue = client.corpora()
        detectors = client.eval_detectors()
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
    # detector here would build a spec whose format the API rightly refuses.
    choices = [item.name for item in detectors if item.lane == "lane:telemetry"]
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

    if not st.button("Run benchmark", type="primary", disabled=not chosen):
        st.caption("Pick detectors and run — results are cached, so exploring them is instant.")
        return

    from iotshield.eval import EvaluationReport

    with st.spinner(f"Benchmarking {len(chosen)} detector(s) over {corpus.cases} case(s)…"):
        try:
            payload = _run_benchmark(corpus_name, tuple(chosen), engine, protocol)
        except _API_ERRORS as exc:
            st.error(f"Benchmark failed: {exc}")
            return
    _render_report(EvaluationReport.model_validate_json(payload))


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
