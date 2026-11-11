"""Reading a window from an HTTP API source (ADR-0049): auth, window, pages, and the limits.

An ``httpx.MockTransport`` stands in for the internet and a fake resolver for DNS, through
the same ``use_transport`` hook the route tests use.
"""

import base64
import json
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx
import pytest

from iotshield.connect import ApiTarget, ConnectionUnavailable, PullRefused
from iotshield.connect.api import ApiCredentials, ApiEndpoint
from iotshield.connect.api import client as api_client
from iotshield.connect.api.client import pages

Handler = Callable[[httpx.Request], httpx.Response]

BASE = {
    "url": "https://api.example.com/v1/readings",
    "start_param": "from",
    "end_param": "to",
    "mapping": {
        "lane": "telemetry",
        "records": "items",
        "timestamp": "ts",
        "asset_id": "id",
        "metrics": {"Temperature": "t"},
    },
}


def endpoint(**changes: Any) -> ApiEndpoint:
    return ApiEndpoint.model_validate(BASE | changes)


def public(host: str, port: int) -> list[str]:
    return {"api.example.com": ["93.184.216.34"], "evil.example.com": ["10.0.0.1"]}.get(host, [])


@pytest.fixture()
def serve() -> Iterator[Callable[[Handler], list[httpx.Request]]]:
    def install(handler: Handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        api_client.use_transport(httpx.MockTransport(record), public)
        return seen

    yield install
    api_client.use_transport(None)


def items(*records: dict[str, Any], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"items": list(records), **extra})


DAY = date(2026, 11, 1)


class TestRequests:
    def test_sends_the_window_in_the_declared_format(self, serve) -> None:  # type: ignore[no-untyped-def]
        seen = serve(lambda request: items())
        for fmt, expected in [
            ("date", ("2026-11-01", "2026-11-02")),
            ("iso", ("2026-11-01T00:00:00Z", "2026-11-03T00:00:00Z")),
            ("epoch_s", ("1793491200", "1793664000")),
            ("epoch_ms", ("1793491200000", "1793664000000")),
        ]:
            list(pages("x", endpoint(window_format=fmt), ApiCredentials(), DAY, date(2026, 11, 2)))
            params = seen[-1].url.params
            assert (params["from"], params["to"]) == expected, fmt

    def test_basic_auth_and_api_key_headers(self, serve) -> None:  # type: ignore[no-untyped-def]
        seen = serve(lambda request: items())
        basic = endpoint(auth="basic", username="ops")
        list(pages("x", basic, ApiCredentials(password="s3cret"), DAY, DAY))
        assert (
            seen[-1].headers["Authorization"] == "Basic " + base64.b64encode(b"ops:s3cret").decode()
        )

        keyed = endpoint(auth="api_key", api_key_header="X-API-Key", api_key_prefix="")
        list(pages("x", keyed, ApiCredentials(api_key="k-1"), DAY, DAY))
        assert seen[-1].headers["X-API-Key"] == "k-1"
        assert "Authorization" not in seen[-1].headers

        bearer = endpoint(auth="api_key")
        list(pages("x", bearer, ApiCredentials(api_key="k-2"), DAY, DAY))
        assert seen[-1].headers["Authorization"] == "Bearer k-2"

    def test_follows_next_page_links_on_the_same_origin(self, serve) -> None:  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page")
            if page is None:
                return items({"ts": "2026-11-01T00:00:00Z", "id": "a", "t": 1}, next="?page=2")
            return items({"ts": "2026-11-01T01:00:00Z", "id": "a", "t": 2}, next=None)

        seen = serve(handler)
        target = ApiTarget(
            name="x", endpoint=endpoint(next_path="next"), credentials=ApiCredentials()
        )
        text = "".join(target.chunks(DAY, DAY, {}))
        assert [json.loads(line)["value"] for line in text.splitlines()] == [1, 2]
        assert len(seen) == 2
        assert "from" not in seen[1].url.params  # the next link carries its own query


class TestRefusals:
    @pytest.mark.parametrize(
        ("response", "why"),
        [
            (httpx.Response(401), "refused the credentials"),
            (httpx.Response(500, text="Traceback: password=hunter2"), "HTTP 500"),
            (httpx.Response(302, headers={"Location": "http://169.254.169.254/"}), "redirected"),
            (httpx.Response(200, text="<html>"), "not JSON"),
            (httpx.Response(200, json={"items": {}}), "expected a list of records"),
        ],
    )
    def test_a_failure_names_the_source_never_the_body(
        self,
        serve,
        response: httpx.Response,
        why: str,  # type: ignore[no-untyped-def]
    ) -> None:
        serve(lambda request: response)
        target = ApiTarget(name="Plant API", endpoint=endpoint(), credentials=ApiCredentials())
        with pytest.raises(ConnectionUnavailable, match=why) as caught:
            list(target.chunks(DAY, DAY, {}))
        assert "Plant API" in str(caught.value)
        assert "hunter2" not in str(caught.value)

    def test_a_next_page_on_another_host_is_refused(self, serve) -> None:  # type: ignore[no-untyped-def]
        serve(lambda request: items(next="https://evil.example.com/steal"))
        with pytest.raises(ConnectionUnavailable, match="different host"):
            list(pages("x", endpoint(next_path="next"), ApiCredentials(), DAY, DAY))

    def test_a_next_page_loop_is_refused(self, serve) -> None:  # type: ignore[no-untyped-def]
        serve(lambda request: items(next="?page=1"))
        with pytest.raises(ConnectionUnavailable, match="already read"):
            list(pages("x", endpoint(next_path="next"), ApiCredentials(), DAY, DAY))

    def test_an_oversized_response_is_abandoned(
        self,
        serve,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setattr(api_client, "MAX_RESPONSE_BYTES", 100)
        serve(lambda request: items(*[{"ts": "x", "id": "a", "t": 1}] * 20))
        with pytest.raises(ConnectionUnavailable, match="over"):
            list(pages("x", endpoint(), ApiCredentials(), DAY, DAY))

    def test_a_url_that_now_resolves_inward_is_refused_at_pull_time(self, serve) -> None:  # type: ignore[no-untyped-def]
        seen = serve(lambda request: items())
        inward = endpoint(url="https://evil.example.com/v1")
        with pytest.raises(ConnectionUnavailable, match="private or internal"):
            list(pages("x", inward, ApiCredentials(), DAY, DAY))
        assert seen == []  # nothing was sent

    def test_the_window_and_params_are_checked_before_any_request(self, serve) -> None:  # type: ignore[no-untyped-def]
        seen = serve(lambda request: items())
        target = ApiTarget(name="x", endpoint=endpoint(), credentials=ApiCredentials())
        with pytest.raises(PullRefused, match="at most 92"):
            target.chunks(date(2026, 1, 1), date(2026, 12, 31), {})
        with pytest.raises(PullRefused, match="no pull parameters"):
            target.check(DAY, DAY, {"utility": "water"})
        assert seen == []


class TestSummary:
    def test_counts_records_and_samples_mapped_lines_in_one_pass(self, serve) -> None:  # type: ignore[no-untyped-def]
        seen = serve(
            lambda request: items(
                *[{"ts": "2026-11-01T00:00:00Z", "id": "a", "t": n} for n in range(5)]
            )
        )
        target = ApiTarget(name="x", endpoint=endpoint(), credentials=ApiCredentials())
        summary = target.summary(DAY, DAY, {})
        assert (summary.rows, summary.normalizer, summary.source_format) == (
            5,
            "passthrough",
            "normalized-jsonl",
        )
        assert len(summary.sample.splitlines()) == 5
        assert len(seen) == 1
        assert target.normalizer_params == {"lane": "telemetry"}


class TestEndpointModel:
    @pytest.mark.parametrize(
        "changes",
        [
            {"auth": "basic"},  # no user name
            {"start_param": "x", "end_param": "x"},
            {"api_key_header": "X Bad"},
            {"api_key_prefix": "Bearer\r\nX-Injected: 1"},
            {"url": "  "},
        ],
    )
    def test_refuses_a_configuration_that_cannot_work(self, changes: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            endpoint(**changes)
