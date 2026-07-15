"""SSRF guard tests: scheme + address validation, and per-hop redirect revalidation.

Offline — literal IPs and 'localhost' resolve without a network call; redirect
behavior is driven through an httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from jarvis import net

# --- check_public_http_url -------------------------------------------------


def test_public_literal_ip_allowed() -> None:
    assert net.check_public_http_url("http://1.1.1.1/") is None
    assert net.check_public_http_url("https://8.8.8.8/path") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",  # loopback
        "http://10.0.0.1/",  # private (RFC1918)
        "http://192.168.1.1/admin",  # private
        "http://169.254.169.254/latest/meta-data/",  # link-local (cloud metadata)
        "http://localhost:8080/",  # resolves to loopback
    ],
)
def test_non_public_hosts_rejected(url: str) -> None:
    reason = net.check_public_http_url(url)
    assert reason is not None
    assert "non-public" in reason or "resolve" in reason


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com/x",
        r"\\server\share\file",  # UNC path — urlparse gives no scheme
        "gopher://x",
    ],
)
def test_non_http_schemes_rejected(url: str) -> None:
    assert net.check_public_http_url(url) is not None


# --- safe_get: redirect revalidation ---------------------------------------


async def test_safe_get_returns_public_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["User-Agent"] == "Kira/0.1 (+assistant)"
        return httpx.Response(200, text="hello")

    transport = httpx.MockTransport(handler)
    resp = await net.safe_get("http://1.1.1.1/", timeout_seconds=5, transport=transport)
    assert resp.text == "hello"


async def test_safe_get_blocks_redirect_to_private() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "1.1.1.1":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="should not reach here")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="non-public"):
        await net.safe_get("http://1.1.1.1/", timeout_seconds=5, transport=transport)


async def test_safe_get_rejects_unsafe_initial_url() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text="x"))
    with pytest.raises(ValueError, match="non-public"):
        await net.safe_get("http://10.0.0.1/", timeout_seconds=5, transport=transport)


async def test_safe_get_redirect_loop_bounded() -> None:
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(302, headers={"location": "http://1.1.1.1/next"})
    )
    with pytest.raises(ValueError, match="too many redirects"):
        await net.safe_get(
            "http://1.1.1.1/", timeout_seconds=5, max_redirects=2, transport=transport
        )
