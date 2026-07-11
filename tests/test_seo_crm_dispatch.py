"""Phase 5 — seo workcell dispatches artifact registration to samus-crm.

When an audit completes for a request that carries a ``prospect_id`` the
service fires a best-effort POST /crm/artifacts dispatch. The dispatch is
guarded so a CRM outage / mis-config never breaks the audit pipeline.
"""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


class _Resp:
    def __init__(self, html, status=200):
        self.text = html
        self.status_code = status
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        return None


class _AuditClient:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        return _Resp("<html><head><title>x</title></head><body><h1>x</h1></body></html>")


def _patch_audit_fetch(monkeypatch):
    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _AuditClient)


def _stub_settings(monkeypatch, *, gateway_url="http://gateway.local", shared_hmac_key="secret"):
    """Make backend.seo.service.get_settings() return a stub with the
    gateway wired (producers dispatch via gateway -> SQS, not directly
    to samus-crm)."""

    class _S:
        gateway_urls = {"gateway": gateway_url}

    s = _S()
    s.shared_hmac_key = shared_hmac_key
    import backend.seo.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


def _capture_dispatches(monkeypatch):
    """Capture signed_post_json calls instead of dispatching to background.

    We swap _dispatch_artifact_to_crm with a synchronous capture so the test
    doesn't have to deal with daemon-thread timing. The production code path
    that builds the payload is exercised because we monkeypatch only the
    final dispatch helper, not the call site.
    """
    captured: list[dict] = []
    import backend.seo.service as svc

    monkeypatch.setattr(svc, "_dispatch_artifact_to_crm", lambda payload: captured.append(payload))
    return captured


def test_audit_with_prospect_id_fires_crm_dispatch(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    captured = _capture_dispatches(monkeypatch)

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    result = audit_site(
        AuditRequest(
            url="https://acme.example.com",
            keywords=["plumbing"],
            prospect_id="pr_acme",
        )
    )
    assert result.url == "https://acme.example.com"
    assert len(captured) == 1
    payload = captured[0]
    assert payload["kind"] == "seo_audit"
    assert payload["owner_entity_kind"] == "prospect"
    assert payload["owner_entity_id"] == "pr_acme"
    assert payload["source"] == "seo"
    assert payload["created_by"] == "samus-seo"
    assert "url" in payload["inline_data"]


def test_audit_without_prospect_id_skips_crm_dispatch(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    captured = _capture_dispatches(monkeypatch)

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    # No prospect_id -> ad-hoc URL audit, no CRM linkage to register.
    audit_site(AuditRequest(url="https://lone.example.com"))
    assert captured == []


def test_dispatch_helper_skips_when_gateway_url_unset(tmp_path, monkeypatch):
    """A missing gateway URL is a no-op, not an exception. The dispatch
    routes through the gateway now (gateway -> SQS), so without a
    GATEWAY_URL there's nowhere to enqueue and the helper short-circuits."""
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    # Empty gateway URL: helper must early-return cleanly.
    _stub_settings(monkeypatch, gateway_url="", shared_hmac_key="secret")
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    # Don't swap _dispatch_artifact_to_crm — exercise the real helper's
    # early-return path. Patch signed_post_json_sync so a stray call
    # would loudly fail the test.
    fired: list = []
    import backend.seo.service as svc

    monkeypatch.setattr(svc, "signed_post_json_sync", lambda *a, **kw: fired.append((a, kw)))

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    result = audit_site(
        AuditRequest(
            url="https://safe.example.com",
            prospect_id="pr_x",
        )
    )
    assert result.url == "https://safe.example.com"
    assert fired == []  # helper short-circuited before any dispatch


def test_dispatch_failure_does_not_break_audit(tmp_path, monkeypatch):
    """signed_post_json_sync blowing up must not propagate out of
    audit_site. The helper wraps the call in try/except + logs."""
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    import backend.seo.service as svc

    def _raising_dispatch(*args, **kwargs):
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(svc, "signed_post_json_sync", _raising_dispatch)

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    result = audit_site(
        AuditRequest(
            url="https://still-works.example.com",
            prospect_id="pr_q",
        )
    )
    assert result.url == "https://still-works.example.com"
