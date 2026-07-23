"""Typed client for the write-side Pipeline API — the orchestration UI's door to it.

Mirrors ``dashboard/client.py`` (the read-side ``ApiClient``): responses parse through
the Pipeline API's own response models, so API/UI drift fails loudly here rather than
rendering wrong (ADR-0008/0016). The dashboard never touches storage or the runner — it
composes a spec and calls this API (ADR-0015). Accepts any ``httpx.Client``, so tests can
inject the Pipeline API's ``TestClient`` in-process.
"""

from typing import TYPE_CHECKING, Any

import httpx

from iotshield.api.control import ModulesResponse, RunResponse, ValidateResponse

if TYPE_CHECKING:  # type-only: the runtime imports are deferred, see the eval methods
    from iotshield.api.evaluation import CorporaResponse, DetectorSummary
    from iotshield.eval import EvaluationReport


class ControlClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @classmethod
    def from_url(cls, base_url: str) -> "ControlClient":
        # A run can take real time (a model detector), so the timeout is generous.
        return cls(httpx.Client(base_url=base_url, timeout=120.0))

    def health(self) -> dict[str, str]:
        response = self._http.get("/health")
        response.raise_for_status()
        return response.json()

    def modules(self) -> ModulesResponse:
        response = self._http.get("/modules")
        response.raise_for_status()
        return ModulesResponse.model_validate(response.json())

    def validate(self, spec: dict[str, Any]) -> ValidateResponse:
        response = self._http.post("/runs/validate", json={"spec": spec})
        response.raise_for_status()
        return ValidateResponse.model_validate(response.json())

    def run(self, spec: dict[str, Any], *, inputs: list[dict[str, Any]]) -> RunResponse:
        # ``inputs`` are ``{"content", "origin"}`` objects, one per source in spec.sources.
        response = self._http.post("/runs", json={"spec": spec, "inputs": inputs})
        response.raise_for_status()
        return RunResponse.model_validate(response.json())

    # --- Evaluation plane (ADR-0021) -----------------------------------------
    #
    # These reach /eval, which the Pipeline API mounts only when IOTSHIELD_EVAL_API is set.
    # The response models are imported lazily inside each method so importing this client
    # never pulls in the eval package — the plane stays deletable from a product image.

    def corpora(self) -> "CorporaResponse":
        """The labelled corpora this deployment can benchmark against."""
        from iotshield.api.evaluation import CorporaResponse

        response = self._http.get("/eval/corpora")
        response.raise_for_status()
        return CorporaResponse.model_validate(response.json())

    def eval_detectors(self) -> list["DetectorSummary"]:
        """The detectors a benchmark can compare, and whether each can be trained."""
        from iotshield.api.evaluation import DetectorSummary

        response = self._http.get("/eval/detectors")
        response.raise_for_status()
        return [DetectorSummary.model_validate(item) for item in response.json()]

    def evaluate(
        self,
        *,
        corpus: str,
        specs: list[dict[str, Any]],
        fit_protocol: str,
        include_timeline: bool = True,
    ) -> "EvaluationReport":
        """Run a benchmark server-side and return the full report.

        ``specs`` are ``{"label", "spec", "sweep"}`` objects — one per detector being
        compared, since several detectors in ONE spec run together rather than competing.
        ``sweep`` maps a hyperparameter to the values to vary it across; the server
        cross-products it into one labelled spec per combination (ADR-0022), so a swept run
        and a hand-written head-to-head come back in exactly the same shape.
        """
        from iotshield.eval import EvaluationReport

        response = self._http.post(
            "/eval/run",
            json={
                "corpus": corpus,
                "specs": specs,
                "fit_protocol": fit_protocol,
                "include_timeline": include_timeline,
            },
        )
        response.raise_for_status()
        return EvaluationReport.model_validate(response.json())
