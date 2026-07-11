"""SSRF egress guard — backend.common.safe_fetch (finding H3/M4).

Pins the contract of ``assert_public_http_url`` + ``safe_get``:
  - non-http(s) schemes are rejected;
  - hostnames / literal IPs that resolve into private / loopback / link-local
    / multicast / reserved / CGNAT ranges are blocked (cloud metadata at
    169.254.169.254, 127.0.0.1, localhost, RFC1918);
  - a normal public host is allowed through;
  - redirects are re-validated, so a 302 toward an internal address is caught
    before it is followed.

DNS is fully stubbed via the ``resolver`` injection seam — no live network.
"""

from __future__ import annotations

import httpx
import pytest

from backend.common import safe_fetch
from backend.common.safe_fetch import SsrfBlockedError, assert_public_http_url, safe_get


# --------------------------------------------------------------------------
# Resolver stubs — getaddrinfo returns (family, type, proto, canonname, addr)
# --------------------------------------------------------------------------


def _resolver_returning(*ips: str):
    """Build a resolver that maps every host to the given IP list."""

    def _resolve(host: str, port: int) -> list:
        return [(0, 0, 0, "", (ip, port)) for ip in ips]

    return _resolve


def _resolver_nxdomain():
    """A resolver that always fails to resolve (NXDOMAIN-style)."""
    import socket as _socket

    def _resolve(host: str, port: int) -> list:
        raise _socket.gaierror("name or service not known")

    return _resolve


# --------------------------------------------------------------------------
# assert_public_http_url — scheme guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://example.com/x",
        "ldap://internal/",
    ],
)
def test_non_http_scheme_blocked(url):
    with pytest.raises(SsrfBlockedError):
        assert_public_http_url(url, resolver=_resolver_returning("93.184.216.34"))


def test_missing_host_blocked():
    with pytest.raises(SsrfBlockedError):
        assert_public_http_url("http:///nohost", resolver=_resolver_returning("8.8.8.8"))


# --------------------------------------------------------------------------
# assert_public_http_url — literal-IP guard (no DNS involved)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # AWS / GCP instance metadata
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "172.16.0.1",  # RFC1918
        "100.64.0.1",  # CGNAT (RFC 6598)
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 unique-local
    ],
)
def test_literal_private_ip_blocked(ip):
    # Literal IPs are parsed directly; the resolver must never be consulted.
    def _explode(host, port):  # pragma: no cover - asserts resolver unused
        raise AssertionError("resolver should not be called for a literal IP")

    with pytest.raises(SsrfBlockedError):
        assert_public_http_url(f"http://{ip}/latest/meta-data/", resolver=_explode)


def test_ipv4_mapped_ipv6_loopback_blocked():
    with pytest.raises(SsrfBlockedError):
        assert_public_http_url(
            "http://[::ffff:127.0.0.1]/",
            resolver=_resolver_returning("8.8.8.8"),
        )


# --------------------------------------------------------------------------
# assert_public_http_url — hostname guard via DNS
# --------------------------------------------------------------------------


def test_hostname_resolving_to_metadata_blocked():
    """A friendly hostname that resolves to 169.254.169.254 is still blocked."""
    with pytest.raises(SsrfBlockedError):
        assert_public_http_url(
            "https://metadata.attacker.example/",
            resolver=_resolver_returning("169.254.169.254"),
        )


def test_hostname_with_one_private_address_blocked():
    """If ANY resolved address is private, the whole host is rejected."""
    with pytest.raises(SsrfBlockedError):
        assert_public_http_url(
            "https://mixed.example/",
            resolver=_resolver_returning("93.184.216.34", "10.0.0.1"),
        )


def test_public_hostname_allowed():
    """A host that resolves only to public addresses passes the guard."""
    assert_public_http_url(
        "https://example.com/page",
        resolver=_resolver_returning("93.184.216.34"),
    )


def test_unresolvable_host_is_not_blocked():
    """NXDOMAIN is a transport failure, not an SSRF block — guard lets it pass."""
    assert_public_http_url(
        "https://does-not-exist.example/",
        resolver=_resolver_nxdomain(),
    )


# --------------------------------------------------------------------------
# Bounded DNS resolution — the 2026-07-01 daily-prospecting hang fix.
# ``socket.getaddrinfo`` has no timeout; a stalled nameserver would block the
# whole sequential run. ``_default_resolver`` now enforces a wall-clock cap.
# --------------------------------------------------------------------------


def test_default_resolver_times_out_on_slow_getaddrinfo(monkeypatch):
    """A getaddrinfo that never returns is bounded and reported as gaierror."""
    import socket as _socket
    import threading
    import time

    monkeypatch.setattr(safe_fetch, "_DNS_RESOLVE_TIMEOUT", 0.2)

    released = threading.Event()

    def _hanging_getaddrinfo(host, port, *a, **kw):
        # Simulate a black-holed nameserver: block well past the deadline.
        released.wait(timeout=5.0)
        return [(0, 0, 0, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", _hanging_getaddrinfo)

    started = time.monotonic()
    with pytest.raises(_socket.gaierror):
        safe_fetch._default_resolver("slow.example", 443)
    elapsed = time.monotonic() - started

    # Bounded: returns ~immediately after the 0.2s cap, NOT after the 5s block.
    assert elapsed < 2.0, f"resolver did not honour the timeout (took {elapsed:.2f}s)"
    released.set()  # let the abandoned worker finish cleanly


def test_slow_dns_degrades_to_unresolved_not_blocked(monkeypatch):
    """A timed-out DNS lookup surfaces as 'unresolvable' — guard lets the fetch
    proceed to fail at the (bounded) transport layer, not hang and not 500."""
    import time

    monkeypatch.setattr(safe_fetch, "_DNS_RESOLVE_TIMEOUT", 0.2)

    def _hanging_getaddrinfo(host, port, *a, **kw):
        time.sleep(5.0)
        return [(0, 0, 0, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", _hanging_getaddrinfo)

    started = time.monotonic()
    # Unresolvable == not an SSRF block (module contract), so this returns
    # cleanly rather than raising SsrfBlockedError or hanging.
    assert_public_http_url("https://slow.example/")
    assert time.monotonic() - started < 2.0


# --------------------------------------------------------------------------
# safe_get — full guarded GET + redirect re-validation
# --------------------------------------------------------------------------


class _StubClient:
    """httpx.Client stand-in. Serves canned responses keyed by URL."""

    def __init__(self, routes: dict[str, httpx.Response]):
        self._routes = routes

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        url = str(url)
        if url in self._routes:
            return self._routes[url]
        return httpx.Response(404, text="", request=httpx.Request("GET", url))


def _response(
    status: int, *, url: str, location: str | None = None, text: str = ""
) -> httpx.Response:
    headers = {"location": location} if location else {}
    return httpx.Response(
        status,
        text=text,
        headers=headers,
        request=httpx.Request("GET", url),
    )


def test_safe_get_returns_response_for_public_host():
    url = "https://example.com/"
    client = _StubClient({url: _response(200, url=url, text="hello")})
    resp = safe_get(
        url,
        resolver=_resolver_returning("93.184.216.34"),
        client_factory=client,
    )
    assert resp.status_code == 200
    assert resp.text == "hello"


def test_safe_get_blocks_initial_private_target():
    client = _StubClient({})
    with pytest.raises(SsrfBlockedError):
        safe_get(
            "http://169.254.169.254/latest/meta-data/",
            resolver=_resolver_returning("169.254.169.254"),
            client_factory=client,
        )


def test_safe_get_revalidates_redirect_hop():
    """A 302 toward an internal address is blocked before being followed."""
    start = "https://example.com/"
    # The redirect target is an internal host; the guard must catch it.
    client = _StubClient(
        {
            start: _response(302, url=start, location="http://10.0.0.9/secret"),
        }
    )

    def _resolver(host: str, port: int) -> list:
        if host == "example.com":
            return [(0, 0, 0, "", ("93.184.216.34", port))]
        return [(0, 0, 0, "", ("10.0.0.9", port))]

    with pytest.raises(SsrfBlockedError):
        safe_get(start, resolver=_resolver, client_factory=client)


def test_safe_get_follows_redirect_to_public_host():
    start = "https://example.com/"
    final = "https://example.com/final"
    client = _StubClient(
        {
            start: _response(302, url=start, location="/final"),
            final: _response(200, url=final, text="done"),
        }
    )
    resp = safe_get(
        start,
        resolver=_resolver_returning("93.184.216.34"),
        client_factory=client,
    )
    assert resp.status_code == 200
    assert resp.text == "done"


def test_safe_get_redirect_loop_raises():
    start = "https://example.com/"
    client = _StubClient(
        {
            start: _response(302, url=start, location="https://example.com/"),
        }
    )
    with pytest.raises(SsrfBlockedError):
        safe_get(
            start,
            max_redirects=3,
            resolver=_resolver_returning("93.184.216.34"),
            client_factory=client,
        )


def test_ssrf_blocked_error_is_value_error():
    """SsrfBlockedError subclasses ValueError so generic handlers degrade."""
    assert issubclass(SsrfBlockedError, ValueError)


# --------------------------------------------------------------------------
# Browser-equivalent request identity (2026-05-21 crawler false-positive sweep)
# --------------------------------------------------------------------------


def test_browser_user_agent_is_not_a_bot_ua():
    """The shared UA must look like a browser — a self-identifying bot UA is
    what common WAFs 403, mis-flagging healthy sites as broken."""
    ua = safe_fetch.BROWSER_USER_AGENT
    low = ua.lower()
    assert "bot" not in low
    assert "samus" not in low
    assert "Mozilla/5.0" in ua and "Chrome/" in ua


def test_browser_headers_carry_ua_and_accept_language():
    h = safe_fetch.BROWSER_HEADERS
    assert h["User-Agent"] == safe_fetch.BROWSER_USER_AGENT
    assert h["Accept-Language"].startswith("en-US")
    assert "text/html" in h["Accept"]


def test_http2_available_is_a_bool():
    """HTTP2_AVAILABLE is a plain bool — opportunistic, never raises on import."""
    assert isinstance(safe_fetch.HTTP2_AVAILABLE, bool)


def test_crawler_and_seo_fetchers_share_one_browser_identity():
    """The prospecting crawler and both SEO fetchers must present the same
    browser UA — one source of truth, no module drifting back to a bot UA."""
    from backend.prospecting import crawler
    from backend.seo import audit, security_audit

    assert crawler._DEFAULT_HEADERS is safe_fetch.BROWSER_HEADERS
    assert audit._USER_AGENT == safe_fetch.BROWSER_USER_AGENT
    assert security_audit._USER_AGENT == safe_fetch.BROWSER_USER_AGENT
