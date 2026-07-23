"""Normalizer port + adapters: raw input records -> NormalizedEvent | MalformedRecord.

Adapters: StubCsvNormalizer (reference/fallback); SyslogNormalizer (RFC 3164/5424
LOG-lane records, re-implemented from team4-log-normalizer — ADR-0011); FlowNormalizer
(network flow records -> FLOW lane, with a pluggable dialect per wire format — ADR-0023).
"""

from iotshield.normalize.flow import DIALECTS as FLOW_DIALECTS
from iotshield.normalize.flow import FlowNormalizer
from iotshield.normalize.port import Normalizer, NormalizeResult
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_SOURCE_FORMAT
from iotshield.normalize.stub import StubCsvNormalizer
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG_SOURCE_FORMAT
from iotshield.normalize.syslog import SyslogNormalizer

__all__ = [
    "FLOW_DIALECTS",
    "STUB_SOURCE_FORMAT",
    "SYSLOG_SOURCE_FORMAT",
    "FlowNormalizer",
    "NormalizeResult",
    "Normalizer",
    "StubCsvNormalizer",
    "SyslogNormalizer",
]
