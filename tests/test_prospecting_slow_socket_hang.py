"""Regression tests for the 2026-07-01 daily-prospecting hang — the exact
failure mode observed in live validation: a prospect whose "website" is served
by an origin that ACCEPTS the TCP connection but never sends a response body
(the stalled Facebook ``:443`` read), plus the thread-pool-exhaustion backstop
leak that turned a handful of such prospects into a whole-run hang.

Two independent guarantees are pinned here:

  (a) HTTP layer — an individual homepage fetch against a socket that accepts
      then never responds returns/raises within its (phase-split) timeout,
      instead of blocking indefinitely as a scalar read timeout would.

  (b) Run layer — processing SEVERAL such wedged prospects still COMPLETES and
      writes a non-empty call list from the good prospects. The prior shared
      ``ThreadPoolExecutor(max_workers=4)`` leaked a worker per wedged prospect
      and, after four, blocked the next ``submit()`` forever. The fresh
      single-use daemon thread per deadline call cannot be exhausted.

Everything is offline: the "slow" server is a loopback socket, and the SSRF
egress guard (which would otherwise block a 127.0.0.1 target) is patched off so
the real httpx client actually connects to it and exercises the real timeout.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# A loopback server that ACCEPTs connections and then never sends a response
# body — the exact "connection established, read stalls forever" failure.
# ---------------------------------------------------------------------------

class _SilentAcceptServer:
    """Accepts TCP connections on loopback and holds them open, sending nothing.

    This reproduces the observed hang: the socket connect + TLS-less handshake
    succeed, the client sends its request, and the server simply never writes a
    response line. A scalar ``read`` timeout that resets per received byte would
    still fire here (nothing is received) — but the point of the test is to
    prove the fetch is bounded end-to-end and, at the run level, that many such
    prospects cannot exhaust the deadline-guard thread budget.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_SilentAcceptServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Hold the connection open and send NOTHING. Never read/close until
            # teardown — the client's read must time out on its own.
            self._held.append(conn)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


@pytest.fixture
def _bypass_ssrf(monkeypatch):
    """Let a 127.0.0.1 loopback target through the SSRF egress guard.

    The guard exists to block loopback/RFC-1918/metadata targets; here we
    deliberately point at a loopback test server, so patch the guard to a no-op
    at every call site the audit path uses. Scoped to the test only.
    """
    noop = lambda url, *a, **k: None  # noqa: E731
    monkeypatch.setattr("backend.common.safe_fetch.assert_public_http_url", noop)
    monkeypatch.setattr("backend.prospecting.crawler.assert_public_http_url", noop)


# ---------------------------------------------------------------------------
# (a) The individual fetch is bounded — it does not hang on a silent socket.
# ---------------------------------------------------------------------------

def test_homepage_fetch_bounded_against_silent_socket(_bypass_ssrf, monkeypatch):
    """fetch_homepage against an accept-but-never-respond server returns a
    transport failure within the phase-split read budget — not a hang."""
    from backend.prospecting import crawler

    # Tight read budget so the test is fast; a real hang would blow past it.
    monkeypatch.setattr(
        crawler, "_TIMEOUT",
        crawler.bounded_timeout(connect=2.0, read=1.0, write=1.0, pool=1.0),
    )
    # Single attempt — the retry would just double the (already-bounded) wait.
    monkeypatch.setattr(crawler, "_MAX_ATTEMPTS", 1)

    with _SilentAcceptServer() as server:
        started = time.monotonic()
        page = crawler.fetch_homepage(server.url)
        elapsed = time.monotonic() - started

    # Bounded well under any run-level deadline; nowhere near an indefinite hang.
    assert elapsed < 8.0, f"fetch was not bounded (took {elapsed:.2f}s)"
    # A silent server yields a transport failure, not usable html.
    assert page["status_code"] == 0
    assert page["html"] is None
    assert "timeout" in (page["fetch_error"] or "").lower()


# ---------------------------------------------------------------------------
# (b) The run-level deadline guard cannot be exhausted by wedged prospects.
# ---------------------------------------------------------------------------

def test_deadline_guard_not_exhausted_by_many_wedged_calls():
    """More genuinely-wedged calls than the OLD pool size (4) must not stall a
    subsequent good call. The prior shared ThreadPoolExecutor(max_workers=4)
    hung on the 5th submit; the per-call daemon thread cannot be exhausted."""
    from backend.prospecting.service import (
        ProspectDeadlineExceeded,
        _run_with_deadline,
    )

    release = threading.Event()

    def _wedged():
        release.wait(timeout=30.0)  # abandoned; unwinds at teardown
        return "late"

    # Wedge far more calls than the old 4-worker pool would have.
    for _ in range(8):
        with pytest.raises(ProspectDeadlineExceeded):
            _run_with_deadline(_wedged, deadline_s=0.2)

    # A good call after 8 wedged ones must still run promptly — proof the guard
    # is not blocked waiting for a freed pool worker.
    started = time.monotonic()
    assert _run_with_deadline(lambda: "ok", deadline_s=2.0) == "ok"
    assert time.monotonic() - started < 1.0
    release.set()


# ---------------------------------------------------------------------------
# (b) End-to-end: several wedged prospects, the run still writes a non-empty
# list from the good ones. Uses the service-layer fixtures from the sibling
# hang-guard suite shape (offline; discover monkeypatched to fixtures).
# ---------------------------------------------------------------------------

def _many_prospects(n_slow: int, n_fast: int):
    from backend.prospecting.models import ProspectRecord
    out = []
    for i in range(n_slow):
        out.append(ProspectRecord(
            prospect_id=f"pr_slow_{i}", account_id=f"acct_slow_{i}",
            company_name=f"Slow Co {i}", phone="(530) 222-2222",
            website_url=f"https://slow{i}.example/",
            city="Yuba City", state="CA", zipcode="95993",
            industry="finance", review_rating="4.1", review_count="9",
        ))
    for i in range(n_fast):
        out.append(ProspectRecord(
            prospect_id=f"pr_fast_{i}", account_id=f"acct_fast_{i}",
            company_name=f"Fast Co {i}", phone="(530) 111-1111",
            website_url=f"https://fast{i}.example/",
            city="Yuba City", state="CA", zipcode="95993",
            industry="finance", review_rating="4.8", review_count="20",
        ))
    return out


def test_run_completes_despite_many_wedged_prospects(tmp_path, monkeypatch):
    """Six prospects hang mid-enrichment (well past the OLD 4-worker pool),
    two enrich fine; the run still completes and writes a non-empty CSV with
    every prospect retained."""
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    import backend.prospecting.service as svc_mod
    from backend.prospecting.models import DiscoveryRequest

    store = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", store)
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", store)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"),
    )

    fixtures = _many_prospects(n_slow=6, n_fast=2)
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda *, zipcode, industries, max_results_per_zip, must_have_website:
            list(fixtures) if zipcode == "95993" else [],
    )

    # Tiny deadline so wedged prospects are skipped fast; a real hang blows past.
    monkeypatch.setattr(svc_mod, "_ENRICH_DEADLINE_S", 0.3)

    released = threading.Event()

    def _fake_fetch_homepage(url):
        if "slow" in url:
            released.wait(timeout=30.0)  # abandoned daemon thread; safe
        return {"final_url": url, "status_code": 200,
                "html": "<title>ok</title>", "fetch_error": None}

    monkeypatch.setattr(
        "backend.prospecting.crawler.fetch_homepage", _fake_fetch_homepage,
    )
    monkeypatch.setattr(
        "backend.prospecting.crawler.classify_website", lambda page: "live",
    )
    monkeypatch.setattr(
        "backend.prospecting.seo_audit.score_seo", lambda page: (55, []),
    )
    monkeypatch.setattr(
        "backend.prospecting.enrichment.enrich_from_page_with_fallback",
        lambda page, base_url, enable_facebook=True: {"owner_email": "a@x.example"},
    )

    req = DiscoveryRequest(
        campaign_name="slow_socket_test",
        zipcodes=["95993"],
        industries=["finance"],
        max_results_per_zip=20,
        enable_owner_enrichment=True,
        enable_seo_audit=True,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=False,
        enable_website_recheck=False,
        enable_signal_filter_gate=False,
    )

    from backend.prospecting.service import process_discovery

    started = time.monotonic()
    result = process_discovery(req, task_id="t-slow-socket")
    elapsed = time.monotonic() - started
    released.set()

    # 6 wedged prospects × 0.3s deadline, run sequentially, is ~1.8s — the key
    # point is it COMPLETES (the old pool would hang forever on the 5th).
    assert elapsed < 10.0, f"run not bounded (took {elapsed:.2f}s)"

    # All eight retained; the two fast ones enriched; the run wrote a list.
    assert result.prospect_count == 8
    by_id = {p.prospect_id: p for p in result.prospects}
    assert by_id["pr_fast_0"].website_status == "live"
    assert by_id["pr_fast_1"].website_status == "live"
    # Wedged prospects kept at discovery-stage state (no enrichment applied).
    assert by_id["pr_slow_0"].website_status in ("", None)

    assert Path(result.csv_path).exists()
    assert Path(result.csv_path).stat().st_size > 0
    assert result.txt_path and Path(result.txt_path).exists()
