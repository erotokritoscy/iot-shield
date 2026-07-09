"""Normalizer port + adapters: raw input records -> NormalizedEvent | MalformedRecord.

Adapters: StubCsvNormalizer (reference/fallback); SyslogNormalizer (RFC 3164/5424
LOG-lane records, re-implemented from team4-log-normalizer — ADR-0011).
"""

from iotshield.normalize.port import Normalizer, NormalizeResult
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_SOURCE_FORMAT
from iotshield.normalize.stub import StubCsvNormalizer
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG_SOURCE_FORMAT
from iotshield.normalize.syslog import SyslogNormalizer

__all__ = [
    "STUB_SOURCE_FORMAT",
    "SYSLOG_SOURCE_FORMAT",
    "NormalizeResult",
    "Normalizer",
    "StubCsvNormalizer",
    "SyslogNormalizer",
]
