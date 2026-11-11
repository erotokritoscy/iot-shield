"""HTTP API data sources (ADR-0049): an org admin's endpoint, its credentials, a JSON mapping.

The second thing a data source can be pulled from, beside a declared database (ADR-0042).
``endpoint.py`` is the configuration, ``mapping.py`` turns JSON records into
``normalized-jsonl`` lines for the ``passthrough`` normalizer, ``guard.py`` decides which URLs
the server may reach, and ``client.py`` reads the pages.
"""

from iotshield.connect.api.endpoint import ApiCredentials, ApiEndpoint, AuthKind, WindowFormat
from iotshield.connect.api.guard import ALLOWED_HOSTS_ENV, UrlRefused, check_url
from iotshield.connect.api.mapping import ApiMapping, MappingError

__all__ = [
    "ALLOWED_HOSTS_ENV",
    "ApiCredentials",
    "ApiEndpoint",
    "ApiMapping",
    "AuthKind",
    "MappingError",
    "UrlRefused",
    "WindowFormat",
    "check_url",
]
