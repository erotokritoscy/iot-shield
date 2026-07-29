"""Severity ordering, defined once for every adapter that compares severities.

``Severity`` is a ``StrEnum``, so a bare ``>=`` between members compares *text* — and
"critical" < "debug" alphabetically, which is the exact inverse of what an operator means.
Every adapter that wants "at least a warning" therefore needs an explicit rank, and two
copies of that table would eventually disagree about where WARNING sits.

Extracted from ``log_burst`` (which had the only copy) when the rule detector became the
second adapter to need it: rules address ``severity`` as an ordered *tier*, so
``severity: {operator: ">=", value: error}`` has to mean what it says.
"""

from iotshield.contracts import Severity

SEVERITY_RANK: dict[Severity, int] = {
    Severity.DEBUG: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
    Severity.CRITICAL: 4,
}
