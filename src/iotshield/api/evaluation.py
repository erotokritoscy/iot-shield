"""The evaluation API: benchmarking over HTTP, for the dashboard's Evaluate mode.

**This module is mounted only when ``IOTSHIELD_EVAL_API`` is set.** Not guarded inside the
handlers — *absent*. A product deployment has no ``/eval`` route to reach, probe, or exploit,
because the router was never registered. That is the difference between a feature flag and
the separable third plane ADR-0021 describes, and this file is the thing you delete to drop
the plane entirely.

It reverses ADR-0021 D6, which said "no API endpoint" and "the dashboard never launches a
run". Both were correct when written and stopped being correct when the measurement changed:
a full 16-recording benchmark went from 12.9 s to 3.2 s after the efficiency pass, so
"a caller could pin a worker for minutes" no longer describes reality. Synchronous is right
at this size; the trigger for a job/poll pattern is a corpus that takes longer than ~30 s.

Two constraints the design keeps:

- **No database.** Nothing here takes a session or an engine; a benchmark computes and
  returns. That the router needs no resources is the third plane visible in the wiring.
- **The caller never names a path.** Corpora are addressed by *name* and resolved against
  the server's ``IOTSHIELD_CORPUS_PATH``. A request carrying a filesystem path is a request
  to read arbitrary files — the same rule that keeps ``database_url`` server-side
  (ADR-0018 §9-A).
"""

from functools import cache
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from iotshield.api._eval_gate import EVAL_API_ENV, eval_api_enabled
from iotshield.api.control import ParamInfo, describe_params
from iotshield.capabilities import formats_in, lanes_in
from iotshield.contracts import Lane
from iotshield.detect import Fittable
from iotshield.eval import CorpusUnavailable, EvaluationReport, FitProtocol, evaluate, load_corpus
from iotshield.eval.corpus import corpus_root, fetchable, list_corpora
from iotshield.eval.sweep import expand
from iotshield.pipeline import PipelineSpec
from iotshield.pipeline.registry import DETECTORS, NORMALIZERS, build_detector

__all__ = ["EVAL_API_ENV", "create_eval_router", "eval_api_enabled"]


class CorpusSummary(BaseModel):
    """One corpus as a picker sees it — described without paying to load its readings."""

    name: str
    description: str
    provenance: str
    licence: str
    cases: int
    source_formats: list[str] = Field(
        default_factory=list,
        description=(
            "The input shape a spec must declare to benchmark this corpus: one source format "
            "per position, in order. A UI composing a spec needs it to build one that fits."
        ),
    )
    lanes: list[str] = Field(
        default_factory=list,
        description=(
            "The detector lanes this corpus can actually feed. A UI picking detectors needs "
            "it: one that consumes none of these lanes is handed no events and scores a row "
            "of zeros it never had the chance to earn."
        ),
    )


class CorporaResponse(BaseModel):
    root: str
    corpora: list[CorpusSummary]
    fetchable: dict[str, str] = Field(
        default_factory=dict,
        description="Datasets convertible locally but not shipped; value is how to get them",
    )


class DetectorSummary(BaseModel):
    """A benchmarkable detector. ``fittable`` tells the UI whether fit protocols apply.

    ``lanes`` is *decoded* server-side into bare ``Lane`` values, mirroring
    ``ModuleInfo.formats``: capability tokens render to wire form only at the API boundary,
    so a client never has to know the ``lane:`` prefix scheme (``capabilities.py``). Plural
    because ``consumes`` is a set — a detector may legitimately consume more than one lane,
    and picking one arbitrarily would misreport it.

    ``params`` and ``artifact_param`` are the ADR-0022 declaration, carried here so the
    Evaluate page needs *one* call rather than joining this against ``/modules``: which
    hyperparameters exist (with real defaults, so a form can prefill them), and which
    param — if any — names a cached fit. A UI reads the latter to offer "use trained
    weights" and "fit from scratch with my own hyperparameters" as two exclusive modes,
    instead of letting someone combine them and hit the atomicity error.
    """

    name: str
    lanes: list[str]
    fittable: bool
    params: list[ParamInfo] = Field(default_factory=list)
    artifact_param: str | None = None


class EvalRunRequest(BaseModel):
    corpus: str = Field(min_length=1)
    specs: list["LabelledSpec"] = Field(min_length=1)
    fit_protocol: FitProtocol = FitProtocol.NONE
    include_timeline: bool = False


class LabelledSpec(BaseModel):
    """A spec plus the name it appears under in the report, optionally swept.

    ``sweep`` maps a detector hyperparameter to the values to vary it across; the server
    cross-products them into one labelled spec per combination before evaluating
    (ADR-0022). Expanding server-side rather than in the client is what keeps the CLI and
    the dashboard producing identical labels and obeying one expansion cap.
    """

    label: str = Field(min_length=1)
    spec: PipelineSpec
    sweep: dict[str, list[Any]] = Field(default_factory=dict)


@cache
def _lanes_fed_by(source_formats: tuple[str, ...]) -> tuple[str, ...]:
    """Which detector lanes a corpus of this shape can feed, given the normalizers registered.

    A corpus's declared formats plus the registry determine it: each format is read by whichever
    normalizer accepts it, and *that* normalizer's produced lane is what a detector must consume
    to be handed anything at all. Union across positions, because a multi-lane corpus feeds
    several and a spec may judge all of them in one run.

    This join belongs on the server for the same reason ``DetectorSummary.lanes`` is decoded
    here: the ``lane:`` prefix is a private wire detail of ``capabilities.py``, rendered only at
    this boundary. A UI deriving corpus lanes for itself would have to parse that prefix, which
    is exactly the scheme-scattered-through-the-code that typed tokens exist to prevent — so the
    two sides of the comparison are published in the same decoded vocabulary and a client merely
    intersects them.

    Cached with ``DETECTORS``-style justification: ``NORMALIZERS`` is a module-level constant
    with static capabilities, so the answer cannot change within a process.
    """
    lanes: set[Lane] = set()
    for source_format in source_formats:
        for spec in NORMALIZERS.values():
            capability = spec.capability({})
            if source_format in formats_in(capability.consumes):
                lanes |= lanes_in(capability.produces)
    return tuple(sorted(lane.value for lane in lanes))


@cache
def _is_fittable(name: str) -> bool:
    """Whether a registered detector implements the optional training capability.

    Constructing it is the only way to ask — ``Fittable`` is a structural protocol, not a
    registry declaration (ADR-0021 D3). Acceptable here because this endpoint is a catalogue
    call, not an inner loop; if it ever moves onto a hot path, the answer belongs in
    ``AdapterSpec`` beside ``SinkSpec.requires_database``. Cached because ``DETECTORS`` is a
    module-level constant with static factories, so the answer cannot change in a process.
    """
    try:
        return isinstance(build_detector(name, {}), Fittable)
    except ValueError:
        # A detector whose defaults cannot construct (a missing artifact, say) is not
        # benchmarkable as-is; report it as unfittable rather than failing the catalogue.
        return False


def _detector_summaries() -> list[DetectorSummary]:
    """The benchmarkable-detector catalogue, built once.

    ``DETECTORS`` is a module-level constant with static factories and static capabilities,
    so this cannot change in a process — the same invariant ``/modules`` relies on for
    ``_MODULES_RESPONSE``. Worth building once because the Evaluate page re-fetches it on
    every Streamlit rerun (i.e. every widget click), and the answer costs a signature
    introspection per parameter per detector.
    """
    return [
        DetectorSummary(
            name=name,
            lanes=sorted(lane.value for lane in lanes_in(spec.capability({}).consumes)),
            fittable=_is_fittable(name),
            params=describe_params(spec.factory, spec.params_type),
            artifact_param=spec.artifact_param,
        )
        for name, spec in sorted(DETECTORS.items())
    ]


def create_eval_router() -> APIRouter:
    router = APIRouter(prefix="/eval", tags=["evaluation"])
    detector_catalogue = _detector_summaries()

    @router.get("/corpora")
    def corpora() -> CorporaResponse:
        """What can be benchmarked here, and what could be with one more command."""
        return CorporaResponse(
            root=str(corpus_root()),
            corpora=[
                CorpusSummary(
                    name=info.name,
                    description=info.description,
                    provenance=info.provenance,
                    licence=info.licence,
                    cases=info.cases,
                    source_formats=list(info.source_formats),
                    lanes=list(_lanes_fed_by(tuple(info.source_formats))),
                )
                for info in list_corpora()
            ],
            fetchable=fetchable(),
        )

    @router.get("/detectors")
    def detectors() -> list[DetectorSummary]:
        """The detectors a benchmark can compare, with their training capability."""
        return detector_catalogue

    @router.post("/run")
    def run(request: EvalRunRequest) -> EvaluationReport:
        """Benchmark the given specs over a corpus and return the full report.

        Synchronous by design (see the module docstring). Errors are mapped by *kind*: an
        absent corpus is 404 (ask for something else), a corpus/spec mismatch is 422 (the
        request is incoherent) — never a 500, because both are ordinary user situations.
        """
        try:
            corpus = load_corpus(request.corpus)
        except CorpusUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            # Sweeps expand before evaluation, into ordinary labelled specs — so a swept
            # run and a hand-written head-to-head are the same request downstream.
            specs = [
                pair
                for entry in request.specs
                for pair in expand([(entry.label, entry.spec)], entry.sweep)
            ]
            return evaluate(
                corpus,
                specs,
                protocol=request.fit_protocol,
                include_timeline=request.include_timeline,
            )
        except ValueError as exc:
            # Two incoherent-request kinds share this mapping: a sweep naming a parameter
            # no detector accepts (or exceeding the expansion cap), and _check_alignment's
            # corpus/spec mismatch. Refusing beats scoring — a mismatch reads exactly like
            # a bad detector.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


EvalRunRequest.model_rebuild()
