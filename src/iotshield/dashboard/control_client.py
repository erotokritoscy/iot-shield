"""Typed client for the write-side Pipeline API — the orchestration UI's door to it.

Mirrors ``dashboard/client.py`` (the read-side ``ApiClient``): responses parse through
the Pipeline API's own response models, so API/UI drift fails loudly here rather than
rendering wrong (ADR-0008/0016). The dashboard never touches storage or the runner — it
composes a spec and calls this API (ADR-0015). Accepts any ``httpx.Client``, so tests can
inject the Pipeline API's ``TestClient`` in-process.
"""

from typing import Any

import httpx

from iotshield.api.control import ModulesResponse, RunResponse, ValidateResponse


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

    def run(
        self, spec: dict[str, Any], *, input_text: str, origin: str = "dashboard"
    ) -> RunResponse:
        response = self._http.post(
            "/runs", json={"spec": spec, "input": input_text, "origin": origin}
        )
        response.raise_for_status()
        return RunResponse.model_validate(response.json())
