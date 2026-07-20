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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from iotshield.api._eval_gate import EVAL_API_ENV, eval_api_enabled
from iotshield.detect import Fittable
from iotshield.eval import CorpusUnavailable, EvaluationReport, FitProtocol, evaluate, load_corpus
from iotshield.eval.corpus import FETCHABLE, corpus_root, list_corpora
from iotshield.pipeline import PipelineSpec
from iotshield.pipeline.registry import DETECTORS, build_detector

__all__ = ["EVAL_API_ENV", "create_eval_router", "eval_api_enabled"]


class CorpusSummary(BaseModel):
    """One corpus as a picker sees it — described without paying to load its readings."""

    name: str
    description: str
    provenance: str
    licence: str
    cases: int


class CorporaResponse(BaseModel):
    root: str
    corpora: list[CorpusSummary]
    fetchable: dict[str, str] = Field(
        default_factory=dict,
        description="Datasets convertible locally but not shipped; value is how to get them",
    )


class DetectorSummary(BaseModel):
    """A benchmarkable detector. ``fittable`` tells the UI whether fit protocols apply."""

    name: str
    lane: str
    fittable: bool


class EvalRunRequest(BaseModel):
    corpus: str = Field(min_length=1)
    specs: list["LabelledSpec"] = Field(min_length=1)
    fit_protocol: FitProtocol = FitProtocol.NONE
    include_timeline: bool = False


class LabelledSpec(BaseModel):
    """A spec plus the name it appears under in the report."""

    label: str = Field(min_length=1)
    spec: PipelineSpec


def _is_fittable(name: str) -> bool:
    """Whether a registered detector implements the optional training capability.

    Constructing it is the only way to ask — ``Fittable`` is a structural protocol, not a
    registry declaration (ADR-0021 D3). Acceptable here because this endpoint is a catalogue
    call, not an inner loop; if it ever moves onto a hot path, the answer belongs in
    ``AdapterSpec`` beside ``SinkSpec.requires_database``.
    """
    try:
        return isinstance(build_detector(name, {}), Fittable)
    except ValueError:
        # A detector whose defaults cannot construct (a missing artifact, say) is not
        # benchmarkable as-is; report it as unfittable rather than failing the catalogue.
        return False


def create_eval_router() -> APIRouter:
    router = APIRouter(prefix="/eval", tags=["evaluation"])

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
                )
                for info in list_corpora()
            ],
            fetchable=dict(FETCHABLE),
        )

    @router.get("/detectors")
    def detectors() -> list[DetectorSummary]:
        """The detectors a benchmark can compare, with their training capability."""
        summaries: list[DetectorSummary] = []
        for name, spec in sorted(DETECTORS.items()):
            capability = spec.capability({})
            lanes = sorted(token.render() for token in capability.consumes)
            summaries.append(
                DetectorSummary(
                    name=name, lane=lanes[0] if lanes else "", fittable=_is_fittable(name)
                )
            )
        return summaries

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
            return evaluate(
                corpus,
                [(entry.label, entry.spec) for entry in request.specs],
                protocol=request.fit_protocol,
                include_timeline=request.include_timeline,
            )
        except ValueError as exc:
            # _check_alignment: the corpus does not match what the spec says it ingests.
            # Refusing beats scoring, because a mismatch reads exactly like a bad detector.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


EvalRunRequest.model_rebuild()
