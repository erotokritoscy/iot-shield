"""Typed API client — the dashboard's only door into the product.

Every response parses through the CONTRACT models, so dashboard code works
with validated, typed objects and any API/contract drift fails loudly at this
boundary instead of rendering wrong (ADR-0008). Accepts any httpx.Client, so
tests can inject FastAPI's TestClient (in-process, no server).
"""

from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from iotshield.api import Summary
from iotshield.contracts import (
    Asset,
    DetectionResult,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
)

_M = TypeVar("_M", bound=BaseModel)


class ApiClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @classmethod
    def from_url(cls, base_url: str) -> "ApiClient":
        return cls(httpx.Client(base_url=base_url, timeout=10.0))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._http.get(path, params=params or {})
        response.raise_for_status()
        return response.json()

    def _get_list(
        self, model: type[_M], path: str, params: dict[str, Any] | None = None
    ) -> list[_M]:
        # The one place API/contract drift fails loudly for list endpoints.
        return [model.model_validate(item) for item in self._get(path, params)]

    def health(self) -> dict[str, str]:
        return self._get("/health")

    def summary(self) -> Summary:
        return Summary.model_validate(self._get("/summary"))

    def assets(self) -> list[Asset]:
        return self._get_list(Asset, "/assets")

    def events(
        self,
        *,
        asset_id: str | None = None,
        lane: Lane | None = None,
        limit: int = 5000,
    ) -> list[NormalizedEvent]:
        params: dict[str, Any] = {"limit": limit}
        if asset_id is not None:
            params["asset_id"] = asset_id
        if lane is not None:
            params["lane"] = lane.value
        return self._get_list(NormalizedEvent, "/events", params)

    def detections(
        self,
        *,
        asset_id: str | None = None,
        lane: Lane | None = None,
        limit: int = 5000,
    ) -> list[DetectionResult]:
        params: dict[str, Any] = {"limit": limit}
        if asset_id is not None:
            params["asset_id"] = asset_id
        if lane is not None:
            params["lane"] = lane.value
        return self._get_list(DetectionResult, "/detections", params)

    def alerts(self, *, limit: int = 1000) -> list[PrioritizedAlert]:
        return self._get_list(PrioritizedAlert, "/alerts", {"limit": limit})

    def alert_detections(self, alert_id: UUID) -> list[DetectionResult]:
        return self._get_list(DetectionResult, f"/alerts/{alert_id}/detections")

    def malformed(self, *, limit: int = 1000) -> list[MalformedRecord]:
        return self._get_list(MalformedRecord, "/malformed", {"limit": limit})
