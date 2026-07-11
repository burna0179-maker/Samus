"""SSRF-guarded outbound HTTP fetch helper (security hardening, finding H3/M4).

Several workcells fetch a URL that ultimately came from outside the trust
boundary — the SEO audit fetches an operator/prospect-supplied site URL, and
the feedback workcell GETs an SNS ``SubscribeURL``. A bare ``httpx`` GET on
such a URL is a server-side request forgery (CWE-918) sink: an attacker who
controls the URL can point it at ``169.254.169.254`` (cloud instance metadata),
``127.0.0.1`` / ``localhost`` (sibling workcells on the same host), or any
RFC1918 address and have the server fetch it on their behalf — sometimes with
the response body reflected straight back to them.

This module centralizes the egress guard:

  - :func:`assert_public_http_url` validates a single URL — the scheme must be
    ``http``/``https`` and every IP the hostname resolves to must be public
    (not private / loopback / link-local / multicast / reserved, and not in
    the CGNAT range 100.64.0.0/10). It is cheap to call and is the building
    block callers re-run on every redirect ``Location``.

  - :func:`safe_get` is the full guarded GET: it validates the URL, then walks
    redirects manually with ``follow_redirects=False`` and re-validates every
    hop, so a 302 to ``http://169.254.169.254/`` is caught before it is
    followed. DNS-rebind is mitigated by re-resolving + re-checking immediately
    before each request.

A blocked URL raises :class:`SsrfBlockedError` (a subclass of ``ValueError``)
so callers can turn it into a clean ``4xx`` / audit error instead of a 500.

Design notes:
  - DNS *resolution failure* (NXDOMAIN, no network) is **not** treated as an
    SSRF block — a host that resolves to nothing cannot reach an internal
    service, so the guard lets the fetch proceed and fail naturally at the
    transport layer. Only a host that *successfully resolves to a forbidden
    address* is blocked. Literal-IP URLs are parsed directly (no DNS) and are
    always checked.
  - ``resolver`` and ``client_factory`` are injection seams for tests, mirroring
    the ``http_get`` seam already used in :mod:`backend.feedback.sns_signature`.
  - Hard dependencies are ``httpx``, ``ipaddress``, ``socket`` only. ``h2`` is
    an *optional* extra (``httpx[http2]``) detected opportunistically below —
    absent it, outbound fetches transparently use HTTP/1.1.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import socket
from typing import Callable

import httpx

_LOG = logging.getLogger("samus.common.safe_fetch")

# DNS-resolution wall-clock ceiling (seconds). ``socket.getaddrinfo`` has NO
# native per-call timeout — it blocks for the whole system-resolver retry
# ladder (glibc default ``timeout:5 attempts:2`` PER nameserver, longer with
# multiple/black-holed nameservers or DNSSEC). Because the guard resolves the
# host BEFORE the httpx request, the httpx transport timeout does NOT cover it:
# a single prospect whose domain points at a stalled authoritative nameserver
# would hang the entire sequential prospecting run indefinitely (observed
# 2026-07-01: the 07:30 daily run stalled 15+ min mid-discovery and the
# scheduled task's 20-min limit killed it before any call_list CSV was
# written). We cap the lookup at ``_DNS_RESOLVE_TIMEOUT`` and treat an
# over-budget resolution as "unresolvable" — the same conservative outcome the
# guard already produces for NXDOMAIN (fetch proceeds and fails naturally at
# the transport layer, which now has its own bounded timeout).
_DNS_RESOLVE_TIMEOUT = 5.0

# One shared daemon-thread pool for the bounded DNS lookups. Daemon threads so
# a stuck ``getaddrinfo`` (the very case we are bounding) never blocks process
# exit; a small cap keeps a batch run from spawning unbounded threads.
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="safe-fetch-dns",
)

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# --- browser-equivalent request identity ----------------------------------
# A self-identifying bot User-Agent is rejected outright (HTTP 403) by common
# WAFs — the 2026-05-21 crawler false-positive sweep found healthy dealer /
# realty sites mis-read as "broken" purely because of the bot UA. Outbound
# fetches of third-party customer / prospect sites present these
# browser-equivalent headers so a UA-only WAF rule lets them through. A
# JS-challenge WAF still blocks — that is reported honestly upstream as
# ``access_blocked`` rather than retried into a wrong verdict.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# HTTP/2 transport capability. Real browsers negotiate HTTP/2; some WAFs
# fingerprint a plain HTTP/1.1 request as a bot. ``httpx`` only enables HTTP/2
# when the ``h2`` package is installed — detect it once so callers can pass
# ``http2=HTTP2_AVAILABLE`` and degrade cleanly to HTTP/1.1 when it is absent.
try:  # pragma: no cover - trivial import guard
    import h2 as _h2  # noqa: F401

    HTTP2_AVAILABLE = True
except ImportError:  # pragma: no cover - h2 is an optional extra
    HTTP2_AVAILABLE = False

# Carrier-grade NAT range (RFC 6598). ``ipaddress`` does not flag this as
# ``.is_private`` so it needs an explicit check — a metadata-style service
# can legitimately live behind CGNAT inside a provider network.
_CGNAT_NETWORK_V4 = ipaddress.ip_network("100.64.0.0/10")

# Per-request transport budget. Callers may override via the ``timeout`` arg.
#
# A *structured* ``httpx.Timeout`` rather than a bare scalar is deliberate and
# load-bearing (2026-07-01 hang, second root cause). A scalar float applies the
# same value to connect/read/write/pool, but httpx's ``read`` timeout is the
# gap between *successive* received chunks — it is reset on every byte. A
# hostile / broken origin that ACCEPTS the connection then trickles one byte
# every few seconds (the observed stalled Facebook ``:443`` read) never trips a
# scalar read timeout and can hold a worker for minutes-to-forever. Splitting
# the budget — a short connect ceiling, a bounded read gap, and (crucially) a
# ``pool`` ceiling so ``client.get`` cannot block indefinitely waiting for a
# free keep-alive connection — bounds each phase independently. Callers still
# pass their own ``httpx.Timeout`` for per-endpoint tuning (PageSpeed's
# 25s Lighthouse render, the crawler's snappier budget).
_DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=5.0, read=15.0, write=10.0, pool=5.0,
)


def bounded_timeout(
    *,
    connect: float = 5.0,
    read: float = 15.0,
    write: float = 10.0,
    pool: float = 5.0,
) -> httpx.Timeout:
    """Build a phase-split ``httpx.Timeout`` for an outbound third-party fetch.

    One source of truth so every hot fetch site (SEO homepage/robots, PageSpeed,
    the security probes, the prospecting crawler) bounds connect / read / write /
    pool independently instead of a scalar that a slow-trickle read can defeat.
    """
    return httpx.Timeout(connect=connect, read=read, write=write, pool=pool)

# Redirect hops walked + re-validated before giving up. SNS / SEO targets do
# not need deep redirect chains; a small ceiling also bounds the guard cost.
_MAX_REDIRECTS = 5

# Default outbound-body ceiling (S3). Third-party site fetches (SEO audit,
# prospecting crawl) are HTML pages of a few hundred KiB; 32 MiB is generous
# headroom while still bounding peak memory against a hostile target. Callers
# pass ``max_bytes=None`` to opt out (none currently do).
_DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# Resolver injection seam (tests swap this). Signature mirrors the subset of
# ``socket.getaddrinfo`` we use: ``(host, port) -> list[addrinfo tuples]``.
Resolver = Callable[[str, int], list]

# Factory injection seam — defaults to ``httpx.Client``. Callers that are
# themselves monkeypatched in tests (e.g. ``backend.seo.audit``) pass their
# own module's ``httpx.Client`` so existing patches still land.
ClientFactory = Callable[..., httpx.Client]


class SsrfBlockedError(ValueError):
    """Raised when a URL trips the SSRF egress guard.

    Subclasses :class:`ValueError` so callers that already catch ``ValueError``
    on bad input degrade gracefully, while a dedicated type lets security-aware
    handlers distinguish a blocked egress from an ordinary validation error.
    """


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is in any range a guarded fetch must never reach."""
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # CGNAT (RFC 6598) — not covered by ``.is_private``.
    if ip.version == 4 and ip in _CGNAT_NETWORK_V4:
        return True
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) must be checked as its
    # embedded IPv4 address, otherwise loopback slips through.
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and _is_forbidden_ip(mapped):
            return True
    return False


def _resolved_addresses(
    host: str, port: int, resolver: Resolver,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    """Resolve ``host`` to its IPs. Returns ``None`` on resolution failure.

    A literal IP is parsed directly with no DNS. ``None`` means the host could
    not be resolved at all — the caller treats that as "not an SSRF block, let
    the transport fail naturally", not as "allowed-because-unknown".
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass  # not a literal IP — fall through to DNS

    try:
        infos = resolver(host, port)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        _LOG.debug("safe_fetch: host %r did not resolve: %s", host, exc)
        return None

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        # getaddrinfo tuple: (family, type, proto, canonname, sockaddr)
        try:
            sockaddr = info[4]
            addr_str = sockaddr[0]
        except (IndexError, TypeError):
            continue
        try:
            addrs.append(ipaddress.ip_address(addr_str))
        except ValueError:
            continue
    return addrs


def _default_resolver(host: str, port: int) -> list:
    """Default DNS resolver — ``socket.getaddrinfo`` for a TCP stream.

    Wrapped in a wall-clock deadline (``_DNS_RESOLVE_TIMEOUT``) because
    ``getaddrinfo`` itself has no timeout knob and can otherwise block for
    minutes on a stalled/black-holed nameserver — the root cause of the
    2026-07-01 daily-prospecting hang. A lookup that overruns the budget is
    surfaced as :class:`socket.gaierror` so :func:`_resolved_addresses`
    treats it as an ordinary resolution failure ("not an SSRF block, let the
    transport fail naturally") rather than hanging the caller.
    """
    future = _DNS_EXECUTOR.submit(
        socket.getaddrinfo, host, port, type=socket.SOCK_STREAM,
    )
    try:
        return future.result(timeout=_DNS_RESOLVE_TIMEOUT)
    except concurrent.futures.TimeoutError as exc:
        # Let the (daemon) worker keep running to completion in the
        # background; we abandon the result. Report as a resolution failure.
        future.cancel()
        _LOG.warning(
            "safe_fetch: DNS resolution for %r exceeded %.1fs — treating as "
            "unresolved", host, _DNS_RESOLVE_TIMEOUT,
        )
        raise socket.gaierror(
            f"getaddrinfo timed out after {_DNS_RESOLVE_TIMEOUT}s"
        ) from exc


def assert_public_http_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Validate a single URL for outbound fetch. Raises :class:`SsrfBlockedError`.

    Checks, in order:
      1. scheme is ``http`` or ``https``;
      2. a hostname is present;
      3. every IP the hostname resolves to is a public address.

    A hostname that fails to resolve is **not** rejected here — see the module
    docstring. This function does not perform any HTTP request.
    """
    resolver = resolver or _default_resolver

    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise SsrfBlockedError(f"malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SsrfBlockedError(
            f"scheme {scheme!r} not permitted (only http/https)"
        )

    host = parsed.host or ""
    if not host:
        raise SsrfBlockedError("URL has no host")

    port = parsed.port or (443 if scheme == "https" else 80)

    addrs = _resolved_addresses(host, port, resolver)
    if addrs is None:
        # Unresolvable — cannot reach any internal service; let the transport
        # layer surface the failure normally.
        return
    if not addrs:
        raise SsrfBlockedError(f"host {host!r} produced no usable addresses")

    for ip in addrs:
        if _is_forbidden_ip(ip):
            raise SsrfBlockedError(
                f"host {host!r} resolves to non-public address {ip} — blocked"
            )


def safe_get(
    url: str,
    *,
    timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int | None = _DEFAULT_MAX_BYTES,
    resolver: Resolver | None = None,
    client_factory: ClientFactory | None = None,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """SSRF-guarded HTTP GET.

    Validates ``url`` (scheme + resolved-IP allowlist), then issues the GET
    with ``follow_redirects=False`` and walks any redirect manually, re-running
    the guard on every ``Location`` hop. The IP guard re-runs (and so
    re-resolves DNS) immediately before each request, which bounds the
    DNS-rebind window to the gap between check and connect.

    Raises :class:`SsrfBlockedError` if the initial URL or any redirect target
    trips the guard. Transport errors propagate as the usual ``httpx`` errors.

    Two transport modes:

      * ``client`` (preferred for hot call sites) — a caller-owned reusable
        ``httpx.Client`` is supplied; ``safe_get`` calls ``.get`` on it
        directly and does **not** close it (lifetime belongs to the caller).
        The client must have been constructed with ``follow_redirects=False``
        so the manual redirect-validation loop here remains in control;
        ``timeout`` on this path is per-request when the underlying client
        supports it, else the client's bound timeout wins.
      * ``client_factory`` / default — one-shot ``httpx.Client`` constructed
        per call inside a ``with`` block (legacy pre-keepalive shape).
        Callers whose own module is monkeypatched in tests pass their
        module's ``httpx.Client`` as ``client_factory`` so those patches
        continue to apply.

    Supplying both ``client`` and ``client_factory`` is undefined — ``client``
    wins; the factory is ignored.
    """
    resolver = resolver or _default_resolver
    factory = client_factory or httpx.Client

    current = url
    for _hop in range(max_redirects + 1):
        # Guard runs (and re-resolves) right before every request — the
        # initial URL and each redirect Location alike.
        assert_public_http_url(current, resolver=resolver)

        if client is not None:
            # Caller-managed reusable client: never enter a ``with`` block,
            # never close. Pass ``headers`` per-request; the client's
            # follow_redirects must already be False (caller's responsibility)
            # for the manual redirect walk below to stay in control.
            #
            # Pass ``timeout`` per-request too (2026-07-01 hang fix): previously
            # this path dropped the caller's ``timeout`` entirely and inherited
            # only whatever scalar the shared client was built with, so a caller
            # asking for a phase-split ``httpx.Timeout`` silently got none of it.
            # httpx applies a per-request timeout over the client default; guard
            # the ``.get`` for minimal test-double clients whose signature omits
            # the kwarg.
            try:
                response = client.get(
                    current, headers=headers or {}, timeout=timeout,
                )
            except TypeError:
                # Test-double client.get without a ``timeout`` kwarg — fall back
                # to the client's own bound timeout.
                response = client.get(current, headers=headers or {})
        else:
            with factory(timeout=timeout, follow_redirects=False) as one_shot:
                response = one_shot.get(current, headers=headers or {})

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return response  # redirect status with no target — hand back
            # Resolve relative redirects against the URL that produced them.
            current = str(httpx.URL(current).join(location))
            continue
        # S3: bound the final response body. ``url`` came from outside the
        # trust boundary (operator/prospect-supplied site, SNS SubscribeURL),
        # so a hostile target could return a multi-GB body to OOM the worker.
        # Post-hoc guard on the body httpx already buffered (a no-op when
        # ``max_bytes`` is None) — the response is handed back unchanged so the
        # caller's ``.content`` / ``.text`` access is untouched.
        # ``check_httpx_size`` raises ``ResponseTooLarge`` (a subclass-free
        # Exception) which callers already treat as a fetch failure alongside
        # ``SsrfBlockedError``.
        if max_bytes is not None:
            from .net_limits import check_httpx_size
            check_httpx_size(response, max_bytes=max_bytes, source="safe_get")
        return response

    raise SsrfBlockedError(
        f"exceeded {max_redirects} redirects while fetching {url!r}"
    )


__all__ = [
    "BROWSER_HEADERS",
    "BROWSER_USER_AGENT",
    "HTTP2_AVAILABLE",
    "SsrfBlockedError",
    "assert_public_http_url",
    "safe_get",
]
