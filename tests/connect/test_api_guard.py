"""Which URLs an API data source may name (ADR-0049 D4) — the SSRF guard ADR-0042 relied on
"no host in a request" for, now that an org admin types one."""

import pytest

from iotshield.connect.api.guard import ALLOWED_HOSTS_ENV, UrlRefused, check_url, same_origin


def resolving_to(*addresses: str):  # type: ignore[no-untyped-def]
    def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


PUBLIC = resolving_to("93.184.216.34")


def test_a_public_https_url_passes() -> None:
    assert check_url(" https://api.example.com/v1/readings ", resolve=PUBLIC) == (
        "https://api.example.com/v1/readings"
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # the cloud metadata service
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1%eth0",
        "::ffff:127.0.0.1",
        "100.64.0.1",  # carrier-grade NAT: not global
        "224.0.0.1",
    ],
)
def test_an_internal_address_is_refused(address: str) -> None:
    with pytest.raises(UrlRefused, match="private or internal"):
        check_url("https://api.example.com/", resolve=resolving_to(address))


def test_one_internal_address_among_public_ones_is_enough_to_refuse() -> None:
    with pytest.raises(UrlRefused):
        check_url("https://api.example.com/", resolve=resolving_to("93.184.216.34", "10.0.0.1"))


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("ftp://api.example.com/", "https://"),
        ("file:///etc/passwd", "https://"),
        ("http://api.example.com/", "plain http"),
        ("https:///nohost", "no host"),
        ("https://user:pw@api.example.com/", "credentials"),
        ("https://api.example.com:99999/", "valid URL"),
    ],
)
def test_refuses_what_is_not_a_plain_public_https_url(url: str, why: str) -> None:
    with pytest.raises(UrlRefused, match=why):
        check_url(url, resolve=PUBLIC)


def test_an_unresolvable_host_is_refused() -> None:
    with pytest.raises(UrlRefused, match="does not resolve"):
        check_url("https://api.example.com/", resolve=resolving_to())


def test_the_operator_may_allow_an_internal_host_even_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "host.docker.internal, plant-api.lan")
    loopback = resolving_to("127.0.0.1")
    assert check_url("http://host.docker.internal:9000/x", resolve=loopback)
    assert check_url("https://PLANT-API.lan/x", resolve=loopback)
    with pytest.raises(UrlRefused):
        check_url("https://other.lan/x", resolve=loopback)


def test_same_origin_compares_scheme_host_and_port() -> None:
    base = "https://api.example.com/v1/items"
    assert same_origin("https://api.example.com/v1/items?page=2", base)
    assert not same_origin("https://evil.example.com/v1/items", base)
    assert not same_origin("http://api.example.com/v1/items", base)
    assert not same_origin("https://api.example.com:8443/v1/items", base)
