"""Normalizer port + adapters: raw input records -> NormalizedEvent | MalformedRecord.

Adapters: StubCsvNormalizer (reference/fallback); SyslogNormalizer (RFC 3164/5424
LOG-lane records, re-implemented from team4-log-normalizer — ADR-0011); FlowNormalizer
(network flow records -> FLOW lane, with a pluggable dialect per wire format — ADR-0023);
PassThroughNormalizer (input that already arrives as NormalizedEvent JSONL — validate and
envelope rather than parse, with the produced lane as a parameter — ADR-0025);
MeterNormalizer (cumulative utility-counter exports -> hourly consumption events, one
dialect per source: EOA water CSV and SensiPro hourly CSV — ADR-0028).
"""

from iotshield.normalize.flow import DIALECTS as FLOW_DIALECTS
from iotshield.normalize.flow import FlowNormalizer
from iotshield.normalize.meter import DIALECTS as METER_DIALECTS
from iotshield.normalize.meter import MeterNormalizer
from iotshield.normalize.passthrough import SOURCE_FORMAT as PASSTHROUGH_SOURCE_FORMAT
from iotshield.normalize.passthrough import PassThroughNormalizer
from iotshield.normalize.port import Normalizer, NormalizeResult
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_SOURCE_FORMAT
from iotshield.normalize.stub import StubCsvNormalizer
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG_SOURCE_FORMAT
from iotshield.normalize.syslog import SyslogNormalizer

__all__ = [
    "FLOW_DIALECTS",
    "METER_DIALECTS",
    "PASSTHROUGH_SOURCE_FORMAT",
    "STUB_SOURCE_FORMAT",
    "SYSLOG_SOURCE_FORMAT",
    "FlowNormalizer",
    "MeterNormalizer",
    "NormalizeResult",
    "Normalizer",
    "PassThroughNormalizer",
    "StubCsvNormalizer",
    "SyslogNormalizer",
]
