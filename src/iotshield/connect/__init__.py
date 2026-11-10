"""Connectors: pull a time range out of a source's own database as text a normalizer reads.

The step *before* normalization for a source that lives in a database rather than a file
(ADR-0042). A connector queries one known schema and renders the rows in a format an existing
normalizer already parses, so the normalizer stays a pure text-to-events function and is never
taught SQL — ``normalize/meter.py`` named that boundary when SensiPro first arrived: "reading it
directly is not a normalizer's job ... adding a database reader would be a new port".

Two connectors ship, one per source the iotShield prototype read:

- ``sensipro`` (OT) — SensiPro's ``ascendinghourlyreading`` hourly rollup joined to
  ``devices`` for the unit, rendered as ``sensipro-hourly-csv`` for the ``meter`` normalizer.
- ``biology`` (IT) — the Biology capture's ``packets`` table (cicflowmeter rows), rendered as
  ``cicflowmeter-csv`` for the ``flow`` normalizer.

A data source may instead be pulled from an **HTTP API** an org admin registers (ADR-0049,
``api/``): JSON mapped to ``normalized-jsonl`` for the ``passthrough`` normalizer. Both kinds
reach a run as a ``target.PullTarget``.

*Where* a database is — host, user, the variable holding its password — is a deployment fact
declared in ``config/connections.yaml`` and never accepted from a request (``connections.py``).
"""

from iotshield.connect.connections import (
    CONNECTIONS_PATH_ENV,
    Connection,
    ConnectionUnavailable,
    load_connections,
)
from iotshield.connect.port import (
    MAX_PULL_DAYS,
    ConnectorSpec,
    Device,
    Pulled,
    PullRefused,
    PullSummary,
    check_pull,
    list_devices,
    pull,
    pull_chunks,
    pull_summary,
)
from iotshield.connect.registry import CONNECTORS
from iotshield.connect.target import API_NORMALIZER, ApiTarget, DatabaseTarget, PullTarget

__all__ = [
    "API_NORMALIZER",
    "CONNECTIONS_PATH_ENV",
    "CONNECTORS",
    "MAX_PULL_DAYS",
    "ApiTarget",
    "Connection",
    "ConnectionUnavailable",
    "ConnectorSpec",
    "DatabaseTarget",
    "Device",
    "PullRefused",
    "PullSummary",
    "PullTarget",
    "Pulled",
    "check_pull",
    "list_devices",
    "load_connections",
    "pull",
    "pull_chunks",
    "pull_summary",
]
