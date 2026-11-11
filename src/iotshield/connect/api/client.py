"""Reading a window from an HTTP API: pages of JSON, mapped to ``normalized-jsonl`` lines.

Every request is bounded the way a database pull is: a timeout, a ceiling on one response's
size and on the number of pages, **no redirects** (a public endpoint must not be able to
bounce the server to an internal address), and the URL guard re-run before each page because
DNS can change between saving a source and pulling from it (``guard.py``). A next page must be
on the configured URL's origin, so a response cannot steer the server — or the credentials —
somewhere else.

A failure is ``ConnectionUnavailable`` naming the source and, where it helps, the HTTP status.
Never the response body: an upstream error page can echo the request, and the request carries
the credentials.
"""

import base64
import json
import logging
from collections.abc import Iterator
from datetime import date
from typing import Any, Final
from urllib.parse import urljoin

import httpx

from iotshield.connect.api.endpoint import ApiCredentials, ApiEndpoint
from iotshield.connect.api.guard import (
    Resolver,
    UrlRefused,
    check_url,
    same_origin,
    system_resolver,
)
from iotshield.connect.api.mapping import (
    MappingError,
    lookup,
    map_document,
    map_record,
    records_of,
)
from iotshield.connect.connections import ConnectionUnavailable

log = logging.getLogger(__name__)

TIMEOUT_S: Final = 30.0
MAX_RESPONSE_BYTES: Final = 20 * 1024 * 1024
"""One page's ceiling. A response is read in pieces and abandoned past this, not buffered."""
MAX_PAGES: Final = 1_000

_transport: httpx.BaseTransport | None = None
_resolver: Resolver = system_resolver


def use_transport(transport: httpx.BaseTransport | None, resolver: Resolver | None = None) -> None:
    """Route every API pull through ``transport`` (and ``resolver``) — for tests, which stand
    an ``httpx.MockTransport`` in for the internet. ``None`` restores the real ones."""
    global _transport, _resolver
    _transport = transport
    _resolver = resolver if resolver is not None else system_resolver


def current_resolver() -> Resolver:
    """The resolver the guard uses — the system's, or a test's (``use_transport``)."""
    return _resolver


def _headers(endpoint: ApiEndpoint, credentials: ApiCredentials) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "iotshield-connector"}
    if endpoint.auth == "basic":
        token = f"{endpoint.username or ''}:{credentials.password or ''}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(token).decode()
    elif endpoint.auth == "api_key":
        headers[endpoint.api_key_header] = endpoint.api_key_prefix + (credentials.api_key or "")
    return headers


def _failure(name: str, why: str, exc: BaseException | None = None) -> ConnectionUnavailable:
    if exc is not None:
        log.warning("pull from API source %r failed: %s (%s)", name, why, exc)
    return ConnectionUnavailable(f"could not read from {name!r}: {why}")


def _read(client: httpx.Client, name: str, url: str, params: dict[str, str] | None) -> Any:
    try:
        with client.stream("GET", url, params=params) as response:
            if response.is_redirect:
                raise _failure(name, f"the API redirected (HTTP {response.status_code})")
            if response.status_code in (401, 403):
                raise _failure(
                    name, f"the API refused the credentials (HTTP {response.status_code})"
                )
            if not response.is_success:
                raise _failure(name, f"the API answered HTTP {response.status_code}")
            body = bytearray()
            for piece in response.iter_bytes():
                body.extend(piece)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _failure(
                        name, f"a response is over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB"
                    )
    except httpx.TimeoutException as exc:
        raise _failure(name, "the API did not answer in time", exc) from exc
    except httpx.HTTPError as exc:
        raise _failure(name, "the API could not be reached", exc) from exc
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _failure(name, "the API's response is not JSON", exc) from exc


def pages(
    name: str, endpoint: ApiEndpoint, credentials: ApiCredentials, start: date, end: date
) -> Iterator[Any]:
    """Each response document of the window, in order, following ``next_path``."""
    try:
        check_url(endpoint.url, resolve=_resolver)
    except UrlRefused as exc:
        raise _failure(name, str(exc)) from exc
    with httpx.Client(
        transport=_transport,
        timeout=TIMEOUT_S,
        follow_redirects=False,
        headers=_headers(endpoint, credentials),
    ) as client:
        url: str | None = endpoint.url
        params: dict[str, str] | None = endpoint.window_params(start, end)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            assert url is not None
            document = _read(client, name, url, params)
            yield document
            if endpoint.next_path is None:
                return
            following = lookup(document, endpoint.next_path)
            if not isinstance(following, str) or not following.strip():
                return  # null, absent or empty: the last page
            url = urljoin(url, following.strip())
            if url in seen:
                raise _failure(name, "the API's next page points back to a page already read")
            seen.add(url)
            if not same_origin(url, endpoint.url):
                raise _failure(name, "the API's next page is on a different host")
            try:
                check_url(url, resolve=_resolver)
            except UrlRefused as exc:
                raise _failure(name, str(exc)) from exc
            params = None  # a next-page URL carries its own query
        raise _failure(name, f"more than {MAX_PAGES} pages in one window")


def api_stream(
    name: str, endpoint: ApiEndpoint, credentials: ApiCredentials, start: date, end: date
) -> Iterator[tuple[str | None, str]]:
    """The window as a connector stream: ``(group, normalized-jsonl line)`` pairs."""
    for document in pages(name, endpoint, credentials, start, end):
        try:
            yield from map_document(document, endpoint.mapping)
        except MappingError as exc:
            raise _failure(name, str(exc)) from exc


def api_summary(
    name: str,
    endpoint: ApiEndpoint,
    credentials: ApiCredentials,
    start: date,
    end: date,
    *,
    sample_lines: int,
) -> tuple[int, list[str]]:
    """Records in the window (before telemetry fans out per metric), and its first lines.

    One pass over the pages, so a summary costs the API what the run will, once.
    """
    total = 0
    sample: list[str] = []
    for document in pages(name, endpoint, credentials, start, end):
        try:
            records = records_of(document, endpoint.mapping)
            total += len(records)
            for record in records:
                if len(sample) >= sample_lines:
                    break
                sample.extend(map_record(record, endpoint.mapping))
        except MappingError as exc:
            raise _failure(name, str(exc)) from exc
    return total, sample[:sample_lines]
