"""Which URLs an API data source may name, and which addresses the server may reach (ADR-0049 D4).

ADR-0042 kept hosts out of requests: a data source named a connection the *deployment*
declared, so no organization could aim the server at an address of its choosing. An API
source gives that up on purpose — an org admin types the URL — so the protection moves here.
A URL is refused unless:

- it is ``https`` (plain ``http`` only for an allowlisted host: a LAN API may have no
  certificate, and that is the operator's decision, not an org admin's);
- it carries no user info (``https://user:pw@host`` would put a credential in a stored,
  displayed field — credentials have their own encrypted fields);
- **every** address its host resolves to is public: not loopback, private (RFC 1918 / ULA),
  link-local (which holds the cloud metadata service at 169.254.169.254), multicast,
  reserved or unspecified.

``IOTSHIELD_API_ALLOWED_HOSTS`` (comma-separated host names) exempts hosts from the address
check — and only from that: how ``host.docker.internal`` or a plant's LAN API is reached,
named by whoever runs the server.

The check runs when a source is saved **and** before every request, including each page a
paginated response points to, because DNS can change between the two. That is the residual
gap recorded in ADR-0049: a name that resolves to a public address for the check and to a
private one a moment later for the connection (DNS rebinding) is not stopped here. Redirects
are never followed (``client.py``), so a public endpoint cannot bounce the server inward.
"""

import ipaddress
import os
import socket
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

ALLOWED_HOSTS_ENV: Final = "IOTSHIELD_API_ALLOWED_HOSTS"

Resolver = Callable[[str, int], list[str]]
"""``(host, port) -> addresses``; injectable so tests need no DNS."""


class UrlRefused(ValueError):
    """This URL may not be reached from the server. The message is safe to show its author."""


def allowed_hosts() -> frozenset[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlRefused(f"the host {host!r} does not resolve") from exc
    return sorted({str(info[4][0]) for info in infos})


def _internal(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: str, *, resolve: Resolver = system_resolver) -> str:
    """``url`` if the server may request it, else ``UrlRefused`` saying why."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UrlRefused(f"not a valid URL: {exc}") from exc
    if parts.scheme not in ("https", "http"):
        raise UrlRefused("the URL must start with https://")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlRefused("the URL has no host")
    if parts.username is not None or parts.password is not None:
        raise UrlRefused("put credentials in the authentication fields, not in the URL")
    allowed = host in allowed_hosts()
    if parts.scheme == "http" and not allowed:
        raise UrlRefused(
            f"plain http is allowed only for hosts the server lists in {ALLOWED_HOSTS_ENV}; "
            "use https://"
        )
    if not allowed:
        addresses = resolve(host, port or (443 if parts.scheme == "https" else 80))
        if not addresses:
            raise UrlRefused(f"the host {host!r} does not resolve")
        if any(_internal(address) for address in addresses):
            raise UrlRefused(
                f"{host!r} is a private or internal address; the server's operator can allow "
                f"it in {ALLOWED_HOSTS_ENV}"
            )
    return url.strip()


def same_origin(url: str, base: str) -> bool:
    """Whether ``url`` is on the scheme, host and port of ``base`` — what a next page must be."""
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)
