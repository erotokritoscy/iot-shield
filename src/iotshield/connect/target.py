"""What a pull reads from: a declared database or an HTTP API — one shape to the run (ADR-0049 D5).

A run, the pull route and the device list used to hold a ``(Connection, ConnectorSpec)`` pair,
because a database was the only thing a data source could be pulled from. An API source is
the second, so the pair becomes a **pull target**: what it is read with, how to check a window
before reading anything, how to stream it in chunks, how to summarize it. The run sees no
difference between the two — which is the point, since ADR-0047's batching, the saved run's
``pull`` and Compose's restore were all written against "a pull", not against SQL.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from iotshield.connect.api.client import api_stream, api_summary
from iotshield.connect.api.endpoint import ApiCredentials, ApiEndpoint
from iotshield.connect.api.mapping import SOURCE_FORMAT as API_FORMAT
from iotshield.connect.connections import Connection
from iotshield.connect.port import (
    SAMPLE_LINES,
    ConnectorSpec,
    Device,
    PullRefused,
    PullSummary,
    check_pull,
    chunked,
    list_devices,
    pull_chunks,
    pull_count,
    pull_summary,
    pull_window,
    render,
)

API_NORMALIZER = "passthrough"
"""An API source's lines are already normalized events, so the run only validates them."""


class PullTarget(Protocol):
    @property
    def normalizer(self) -> str: ...
    @property
    def source_format(self) -> str: ...
    @property
    def normalizer_params(self) -> Mapping[str, str]:
        """Params the normalizer must be run with to read this target (an API's lane)."""
        ...

    def check(self, start: date, end: date, params: Mapping[str, Any]) -> None:
        """``PullRefused`` for a window or params this target cannot serve — reading nothing."""
        ...

    def chunks(self, start: date, end: date, params: Mapping[str, Any]) -> Iterator[str]: ...
    def count(self, start: date, end: date, params: Mapping[str, Any]) -> int | None:
        """The window's rows without reading them, or None when that would cost a read."""
        ...

    @property
    def header_lines(self) -> int:
        """Lines every chunk repeats before its rows."""
        ...

    def summary(self, start: date, end: date, params: Mapping[str, Any]) -> PullSummary: ...
    def devices(self, params: Mapping[str, Any]) -> list[Device]: ...


@dataclass(frozen=True)
class DatabaseTarget:
    """A connection declared in ``config/connections.yaml``, read by its connector (ADR-0042)."""

    spec: ConnectorSpec
    connection: Connection
    normalizer_params: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def normalizer(self) -> str:
        return self.spec.normalizer

    @property
    def source_format(self) -> str:
        return self.spec.source_format

    def check(self, start: date, end: date, params: Mapping[str, Any]) -> None:
        check_pull(self.spec, start, end, params)

    def chunks(self, start: date, end: date, params: Mapping[str, Any]) -> Iterator[str]:
        return pull_chunks(self.spec, self.connection, start, end, params)

    def count(self, start: date, end: date, params: Mapping[str, Any]) -> int | None:
        return pull_count(self.spec, self.connection, start, end, params)

    @property
    def header_lines(self) -> int:
        return 1 if self.spec.header else 0

    def summary(self, start: date, end: date, params: Mapping[str, Any]) -> PullSummary:
        return pull_summary(self.spec, self.connection, start, end, params)

    def devices(self, params: Mapping[str, Any]) -> list[Device]:
        return list_devices(self.spec, self.connection, params)


@dataclass(frozen=True)
class ApiTarget:
    """An org admin's HTTP API, mapped to ``normalized-jsonl`` (ADR-0049)."""

    name: str
    endpoint: ApiEndpoint
    credentials: ApiCredentials

    @property
    def normalizer(self) -> str:
        return API_NORMALIZER

    @property
    def source_format(self) -> str:
        return API_FORMAT

    @property
    def normalizer_params(self) -> Mapping[str, str]:
        return {"lane": self.endpoint.mapping.lane}

    def check(self, start: date, end: date, params: Mapping[str, Any]) -> None:
        pull_window(start, end)
        if params:
            raise PullRefused(
                f"an API source takes no pull parameters (got {', '.join(sorted(params))})"
            )

    def chunks(self, start: date, end: date, params: Mapping[str, Any]) -> Iterator[str]:
        self.check(start, end, params)
        return chunked("", api_stream(self.name, self.endpoint, self.credentials, start, end))

    def count(self, start: date, end: date, params: Mapping[str, Any]) -> int | None:
        # An API can only be counted by paging through it, which is the read itself.
        return None

    @property
    def header_lines(self) -> int:
        return 0

    def summary(self, start: date, end: date, params: Mapping[str, Any]) -> PullSummary:
        self.check(start, end, params)
        rows, sample = api_summary(
            self.name, self.endpoint, self.credentials, start, end, sample_lines=SAMPLE_LINES
        )
        return PullSummary(
            rows=rows,
            sample=render("", sample),
            normalizer=self.normalizer,
            source_format=self.source_format,
        )

    def devices(self, params: Mapping[str, Any]) -> list[Device]:
        return []
