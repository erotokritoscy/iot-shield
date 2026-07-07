"""Normalizer port + adapters: raw input records -> NormalizedEvent | MalformedRecord.

Adapters: StubCsvNormalizer (reference/fallback); team4 log-normalizer core (planned).
"""

from iotshield.normalize.port import Normalizer, NormalizeResult
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_SOURCE_FORMAT
from iotshield.normalize.stub import StubCsvNormalizer

__all__ = ["STUB_SOURCE_FORMAT", "NormalizeResult", "Normalizer", "StubCsvNormalizer"]
