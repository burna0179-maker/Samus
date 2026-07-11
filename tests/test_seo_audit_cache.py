"""Unit tests for the WIRED-DORMANT SEO ``input_hash`` durable result cache.

Proves the four contract points:
  1. cache HIT returns the prior result WITHOUT recomputing (flag on),
  2. TTL EXPIRY triggers a recompute,
  3. force_refresh BYPASSES the cache,
  4. flag OFF (default) = unchanged behaviour (no read, no write, always recompute).

The cache is keyed on ``events._deterministic_hash`` of the request payload —
the SAME ``input_hash`` recorded in ``seo_audit.jsonl`` — so a cache key lines
up 1:1 with the ledger evidence that surfaced the duplication.
"""

from __future__ import annotations


from backend.common.settings import reload_settings


def _enable_cache(monkeypatch, tmp_path, ttl_seconds: int | None = None):
    """Turn the flag on for THIS test + point the sidecar at a tmp file."""
    monkeypatch.setenv("SAMUS_AUDIT_CACHE_ENABLED", "true")
    monkeypatch.setenv("SAMUS_SEO_AUDIT_CACHE_PATH", str(tmp_path / "seo_audit_cache.jsonl"))
    if ttl_seconds is not None:
        monkeypatch.setenv("SAMUS_AUDIT_CACHE_TTL_SECONDS", str(ttl_seconds))
    reload_settings()  # rebind seo_audit_cache_enabled from the env we just set


# --------------------------------------------------------------------------
# cached_or_compute — the core primitive audit_site delegates to
# --------------------------------------------------------------------------


def test_flag_off_always_computes_and_never_touches_disk(tmp_path, monkeypatch):
    """Default (flag unset) = pre-feature behaviour: compute runs every time,
    nothing is read or written."""
    from backend.seo import audit_cache

    # Explicitly ensure the flag is off (conftest already reloads, but be
    # defensive against ambient env).
    monkeypatch.delenv("SAMUS_AUDIT_CACHE_ENABLED", raising=False)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_CACHE_PATH", str(tmp_path / "seo_audit_cache.jsonl"))
    reload_settings()

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"score": 42}

    payload = {"url": "https://acme.example.com", "keywords": ["plumbing"]}

    r1, from_cache1 = audit_cache.cached_or_compute(payload, _compute)
    r2, from_cache2 = audit_cache.cached_or_compute(payload, _compute)

    assert r1 == r2 == {"score": 42}
    assert from_cache1 is False and from_cache2 is False
    assert calls["n"] == 2  # computed BOTH times — no dedupe when off
    # Sidecar never created.
    assert not (tmp_path / "seo_audit_cache.jsonl").exists()


def test_cache_hit_returns_prior_result_without_recompute(tmp_path, monkeypatch):
    _enable_cache(monkeypatch, tmp_path)
    from backend.seo import audit_cache

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"score": 88, "run": calls["n"]}

    payload = {"url": "https://acme.example.com", "keywords": ["plumbing"]}

    r1, from_cache1 = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache1 is False
    assert calls["n"] == 1

    # Second identical request within TTL -> HIT, compute NOT called again.
    r2, from_cache2 = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache2 is True
    assert calls["n"] == 1  # no recompute
    assert r2 == r1 == {"score": 88, "run": 1}


def test_ttl_expiry_triggers_recompute(tmp_path, monkeypatch):
    # 1-second TTL so we can age it out without real waiting via a patched clock.
    _enable_cache(monkeypatch, tmp_path, ttl_seconds=1)
    from backend.seo import audit_cache

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"run": calls["n"]}

    payload = {"url": "https://acme.example.com"}

    # First run at t=1000 writes a cache entry stamped cached_at=1000.
    monkeypatch.setattr(audit_cache.time, "time", lambda: 1000.0)
    r1, from_cache1 = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache1 is False and calls["n"] == 1

    # Still fresh at t=1000.5 -> HIT.
    monkeypatch.setattr(audit_cache.time, "time", lambda: 1000.5)
    _, from_cache_fresh = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache_fresh is True and calls["n"] == 1

    # Past TTL at t=1002 (age 2s >= 1s TTL) -> MISS -> recompute.
    monkeypatch.setattr(audit_cache.time, "time", lambda: 1002.0)
    r3, from_cache3 = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache3 is False
    assert calls["n"] == 2
    assert r3 == {"run": 2}


def test_force_refresh_bypasses_cache(tmp_path, monkeypatch):
    _enable_cache(monkeypatch, tmp_path)
    from backend.seo import audit_cache

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"run": calls["n"]}

    payload = {"url": "https://acme.example.com"}

    r1, _ = audit_cache.cached_or_compute(payload, _compute)
    assert calls["n"] == 1

    # force_refresh -> ignore the fresh entry, recompute, and re-warm.
    r2, from_cache2 = audit_cache.cached_or_compute(payload, _compute, force_refresh=True)
    assert from_cache2 is False
    assert calls["n"] == 2
    assert r2 == {"run": 2}

    # A subsequent NON-forced call now sees the re-warmed (run=2) entry.
    r3, from_cache3 = audit_cache.cached_or_compute(payload, _compute)
    assert from_cache3 is True
    assert calls["n"] == 2
    assert r3 == {"run": 2}


def test_different_input_hash_is_a_miss(tmp_path, monkeypatch):
    _enable_cache(monkeypatch, tmp_path)
    from backend.seo import audit_cache

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"run": calls["n"]}

    audit_cache.cached_or_compute({"url": "https://a.example.com"}, _compute)
    # Different payload -> different input_hash -> MISS -> recompute.
    _, from_cache = audit_cache.cached_or_compute({"url": "https://b.example.com"}, _compute)
    assert from_cache is False
    assert calls["n"] == 2


def test_corrupt_sidecar_falls_back_to_recompute(tmp_path, monkeypatch):
    """A garbage cache file must degrade to a miss, never raise."""
    cache_file = tmp_path / "seo_audit_cache.jsonl"
    cache_file.write_text("not json at all\n{broken\n", encoding="utf-8")
    _enable_cache(monkeypatch, tmp_path)
    from backend.seo import audit_cache

    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"ok": True}

    result, from_cache = audit_cache.cached_or_compute(
        {"url": "https://acme.example.com"}, _compute
    )
    assert from_cache is False
    assert result == {"ok": True}
    assert calls["n"] == 1


def test_input_hash_matches_ledger_deterministic_hash(monkeypatch):
    """The cache key must equal events._deterministic_hash so it lines up with
    the input_hash recorded in seo_audit.jsonl."""
    from backend.seo import audit_cache
    from backend.common import events

    payload = {"url": "https://acme.example.com", "keywords": ["a", "b"]}
    assert audit_cache.compute_input_hash(payload) == events._deterministic_hash(payload)


# --------------------------------------------------------------------------
# audit_site integration — the cache short-circuits the real recompute
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, html, status=200):
        self.text = html
        self.status_code = status
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        return None


class _AuditClient:
    calls = 0

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _AuditClient.calls += 1
        return _Resp("<html><head><title>x</title></head><body><h1>x</h1></body></html>")


def _patch_audit_fetch(monkeypatch):
    import backend.seo.audit as audit_mod

    _AuditClient.calls = 0
    monkeypatch.setattr(audit_mod.httpx, "Client", _AuditClient)


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_audit_site_flag_on_second_call_skips_fetch(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _enable_cache(monkeypatch, tmp_path)

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    req = AuditRequest(url="https://acme.example.com", keywords=["plumbing"])

    r1 = audit_site(req)
    fetches_after_first = _AuditClient.calls
    assert fetches_after_first >= 1  # the real audit performed a fetch

    # Simulate a cold process: drop the in-process idempotency store so ONLY
    # the durable cache can dedupe (this is the Cloud-Run restart scenario).
    _reset_idempotency(monkeypatch)
    r2 = audit_site(req)

    # NO additional fetches — served entirely from the durable cache.
    assert _AuditClient.calls == fetches_after_first
    assert r2.url == r1.url
    assert r2.seo_score == r1.seo_score


def test_audit_site_flag_off_recomputes_across_cold_start(tmp_path, monkeypatch):
    """Flag off + cold in-process store => the site is fetched again (the
    exact pre-feature duplication we are leaving unchanged by default)."""
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("SAMUS_AUDIT_CACHE_ENABLED", raising=False)
    reload_settings()

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    req = AuditRequest(url="https://acme.example.com")

    audit_site(req)
    fetches_after_first = _AuditClient.calls
    assert fetches_after_first >= 1

    _reset_idempotency(monkeypatch)  # cold restart
    audit_site(req)
    # Fetched AGAIN across the cold start — unchanged legacy behaviour.
    assert _AuditClient.calls == fetches_after_first * 2


def test_audit_site_force_refresh_re_fetches(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _enable_cache(monkeypatch, tmp_path)

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    req = AuditRequest(url="https://acme.example.com")

    audit_site(req)
    fetches_after_first = _AuditClient.calls
    assert fetches_after_first >= 1

    _reset_idempotency(monkeypatch)
    # force_refresh bypasses BOTH caches -> real fetch again.
    audit_site(req, force_refresh=True)
    assert _AuditClient.calls == fetches_after_first * 2
