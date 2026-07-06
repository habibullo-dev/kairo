"""Network-address safety: an SSRF guard shared by web fetch and knowledge ingest.

Foundation code (no dependencies on the rest of Jarvis) so both ``tools/builtin/web``
and ``knowledge/converters`` can use it without coupling those service subtrees.

Even a single-user local assistant shouldn't let an approved *public* URL — or a
redirect from one — reach loopback / private / link-local addresses: that turns a
"fetch a blog" approval into a read of the router admin page (``192.168.x.x``) or
cloud metadata (``169.254.169.254``). :func:`check_public_http_url` validates the
scheme and resolves the host, rejecting any non-global address; :func:`safe_get`
re-runs that check on the initial URL **and every redirect hop**.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_USER_AGENT = "Jarvis/0.1 (+assistant)"


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally-routable address — everything internal is rejected."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def check_public_http_url(url: str) -> str | None:
    """Return a human-readable reason if ``url`` is unsafe to fetch, else ``None``.

    Rejects non-http(s) schemes and hosts that resolve to any non-global IP range.
    The DNS resolution here is what closes DNS-rebinding-style tricks: a hostname
    that points at ``127.0.0.1`` is caught even though the string looks public."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"only http/https URLs are allowed (got scheme {parsed.scheme or '(none)'!r})"
    try:
        host = parsed.hostname
    except ValueError as exc:
        return f"malformed URL {url!r}: {exc}"
    if not host:
        return f"no host in URL {url!r}"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return f"invalid port in URL {url!r}"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"could not resolve host {host!r}: {exc}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            return f"host {host!r} resolves to a non-public address ({ip}) — blocked (SSRF guard)"
    return None


async def safe_get(
    url: str,
    *,
    timeout_seconds: float,
    headers: dict | None = None,
    max_redirects: int = 5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """GET ``url`` with the SSRF check applied to the initial URL and every redirect.

    Redirects are followed manually (``follow_redirects=False``) so each hop's target
    is validated *before* it is fetched — auto-following would let an approved public
    URL bounce to an internal one. ``transport`` is injectable for tests (httpx
    ``MockTransport``). Raises :class:`ValueError` on an unsafe hop or redirect loop."""
    current = url
    hdrs = headers or {"User-Agent": _USER_AGENT}
    async with httpx.AsyncClient(
        timeout=timeout_seconds, follow_redirects=False, transport=transport
    ) as client:
        for _ in range(max_redirects + 1):
            problem = check_public_http_url(current)
            if problem:
                raise ValueError(problem)
            resp = await client.get(current, headers=hdrs)
            if resp.is_redirect and "location" in resp.headers:
                current = urljoin(current, resp.headers["location"])
                continue
            resp.raise_for_status()
            return resp
    raise ValueError(f"too many redirects fetching {url!r}")
