"""Intake workcell FastAPI endpoint tests — including CORS preflight."""

from __future__ import annotations

from typing import Any


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


class _FakeTable:
    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item):
        self.items.append(Item)

    def scan(self, **kwargs):
        return {"Items": list(self.items)}


def _patch_table(monkeypatch, table: _FakeTable):
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "_leads_table", lambda: table)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_INTAKE_AUDIT_PATH", str(tmp_path / "intake_audit.jsonl"))


def _fresh_app(monkeypatch, *, allowed_origins=None):
    """Reload the intake app so CORSMiddleware re-reads settings.

    create_app() captures the allow-list at app-construction time, so per-
    test CORS variations need a fresh app instance. We import the module
    and call create_app() directly rather than reusing the module-level
    `app` singleton.
    """
    if allowed_origins is not None:
        monkeypatch.setenv(
            "SAMUS_INTAKE_ALLOWED_ORIGINS",
            ",".join(allowed_origins) if allowed_origins else "",
        )
    # Force settings re-bootstrap so the CORS allow-list reflects the env
    from backend.common.config import reload_settings

    reload_settings()
    from backend.intake.app import create_app

    return create_app()


def _client(monkeypatch, *, allowed_origins=None):
    from fastapi.testclient import TestClient

    return TestClient(_fresh_app(monkeypatch, allowed_origins=allowed_origins))


def _valid_body():
    return {
        "name": "Jane",
        "email": "jane@acme.com",
        "company": "Acme",
        "website_url": "https://acme.com",
        "service_interest": ["seo_audit"],
        "pain_points": "Manual follow-up is broken.",
        "monthly_budget": "$500-$2000",
        "timeline": "this_month",
    }


# ---------------------------------------------------------------------------
# POST /intake/onboarding
# ---------------------------------------------------------------------------


def test_post_onboarding_persists(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)
    r = client.post("/intake/onboarding", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["lead_id"].startswith("lead_")
    assert len(table.items) == 1


def test_post_onboarding_rejects_unknown_service_value(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    body = dict(_valid_body(), service_interest=["bogus"])
    r = client.post("/intake/onboarding", json=body)
    assert r.status_code == 422


def test_post_onboarding_rejects_missing_pain(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    body = dict(_valid_body(), pain_points="")
    r = client.post("/intake/onboarding", json=body)
    assert r.status_code == 422


def test_post_onboarding_dedup_returns_duplicate(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)
    first = client.post("/intake/onboarding", json=_valid_body())
    second = client.post("/intake/onboarding", json=_valid_body())
    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate"
    assert len(table.items) == 1


def test_post_onboarding_captures_xff_as_source_ip(tmp_path, monkeypatch):
    """With one trusted proxy hop the source IP is the entry the proxy saw.

    XFF ``"203.0.113.5, 10.0.0.1"`` means the workcell's single trusted proxy
    (Cloud Run / Caddy) appended ``10.0.0.1`` — the address that actually
    connected to it. The leftmost ``203.0.113.5`` is attacker-controllable and
    must NOT be trusted; the corrected ``_client_ip`` takes the rightmost
    proxy-appended entry.
    """
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)
    r = client.post(
        "/intake/onboarding",
        json=_valid_body(),
        headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"},
    )
    assert r.status_code == 200
    assert table.items[0]["source_ip"] == "10.0.0.1"


def test_post_onboarding_rejects_extra_fields(tmp_path, monkeypatch):
    """Spammer can't smuggle in admin=True via the public POST."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    body = dict(_valid_body(), admin=True)
    r = client.post("/intake/onboarding", json=body)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# CORS — preflight + actual POST
# ---------------------------------------------------------------------------


def test_cors_preflight_allows_hustleforge(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch, allowed_origins=["https://hustleforge.tech"])
    r = client.options(
        "/intake/onboarding",
        headers={
            "Origin": "https://hustleforge.tech",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "https://hustleforge.tech"
    assert "POST" in r.headers.get("access-control-allow-methods", "")
    assert "content-type" in r.headers.get("access-control-allow-headers", "").lower()


def test_cors_preflight_rejects_unknown_origin(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch, allowed_origins=["https://hustleforge.tech"])
    r = client.options(
        "/intake/onboarding",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    # Starlette CORSMiddleware returns 400 for a disallowed origin preflight
    # (browser then refuses to fire the actual POST). Either no allow-origin
    # header on the response, or a non-2xx status — both prevent the browser
    # from completing the cross-origin call.
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_post_response_includes_allow_origin(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch, allowed_origins=["https://hustleforge.tech"])
    r = client.post(
        "/intake/onboarding",
        json=_valid_body(),
        headers={"Origin": "https://hustleforge.tech"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "https://hustleforge.tech"


# ---------------------------------------------------------------------------
# /work TaskEnvelope route
# ---------------------------------------------------------------------------


def test_work_envelope_routes_submit_lead(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)
    envelope = {
        "task_id": "t1",
        "payload": _valid_body(),
        "metadata": {"action": "submit_lead"},
    }
    r = client.post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"


def test_work_envelope_rejects_unknown_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    envelope = {
        "task_id": "t1",
        "payload": {},
        "metadata": {"action": "nope"},
    }
    r = client.post("/work", json=envelope)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Finding 1 — rate limiting + CAPTCHA enforcement through the endpoint
# ---------------------------------------------------------------------------


class _CountingTable(_FakeTable):
    """_FakeTable plus a shared atomic-ADD counter for the rate limiter."""

    def __init__(self):
        super().__init__()
        self.counters: dict[str, int] = {}

    def update_item(
        self, *, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None, **kwargs
    ):
        key = Key["idempotency_key"]
        self.counters[key] = self.counters.get(key, 0) + int(ExpressionAttributeValues[":one"])
        return {"Attributes": {"request_count": self.counters[key]}}


def _patch_counter_table(monkeypatch, table):
    import backend.intake.rate_limit as rl_mod

    monkeypatch.setattr(rl_mod, "_counter_table", lambda: table)


def test_endpoint_returns_429_when_rate_limited(tmp_path, monkeypatch):
    """Public endpoint returns HTTP 429 once the per-IP minute cap is hit."""
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE", "2")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    _patch_counter_table(monkeypatch, _CountingTable())
    client = _client(monkeypatch)
    headers = {"X-Forwarded-For": "203.0.113.77"}
    # The dedup window would also swallow repeats, so vary the email to keep
    # each request a *distinct* lead and isolate the rate-limit behavior.
    bodies = [dict(_valid_body(), email=f"flood{i}@acme.com") for i in range(4)]
    statuses = [
        client.post("/intake/onboarding", json=b, headers=headers).status_code for b in bodies
    ]
    # First 2 served, then the 3rd + 4th are throttled.
    assert statuses == [200, 200, 429, 429]


def test_endpoint_429_body_has_clear_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE", "1")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    _patch_counter_table(monkeypatch, _CountingTable())
    client = _client(monkeypatch)
    headers = {"X-Forwarded-For": "203.0.113.88"}
    client.post("/intake/onboarding", json=_valid_body(), headers=headers)
    blocked = client.post(
        "/intake/onboarding",
        json=dict(_valid_body(), email="second@acme.com"),
        headers=headers,
    )
    assert blocked.status_code == 429
    detail = blocked.json()["detail"]
    assert detail["error"] == "rate_limited"
    assert detail["scope"] == "ip"
    assert "Retry-After" in blocked.headers


def test_endpoint_rate_limit_fails_open_on_backend_error(tmp_path, monkeypatch):
    """A DynamoDB outage on the limiter must not block legitimate leads."""
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE", "1")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())

    class _BrokenCounter:
        def update_item(self, **kwargs):
            raise RuntimeError("simulated DynamoDB outage")

    _patch_counter_table(monkeypatch, _BrokenCounter())
    client = _client(monkeypatch)
    headers = {"X-Forwarded-For": "203.0.113.99"}
    # Far past the 1/min cap — every request still served (fail-open).
    for i in range(5):
        r = client.post(
            "/intake/onboarding",
            json=dict(_valid_body(), email=f"open{i}@acme.com"),
            headers=headers,
        )
        assert r.status_code == 200, r.text


def test_endpoint_skips_captcha_when_secret_unset(tmp_path, monkeypatch):
    """Default deployment has no CAPTCHA secret -> no captcha_token needed."""
    monkeypatch.delenv("SAMUS_INTAKE_CAPTCHA_SECRET", raising=False)
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    r = client.post("/intake/onboarding", json=_valid_body())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


def test_endpoint_rejects_missing_captcha_when_secret_set(tmp_path, monkeypatch):
    """With a CAPTCHA secret configured, a body without captcha_token is 400."""
    monkeypatch.setenv("SAMUS_INTAKE_CAPTCHA_SECRET", "0x_secret")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())
    client = _client(monkeypatch)
    r = client.post("/intake/onboarding", json=_valid_body())
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "captcha_failed"


def test_endpoint_accepts_valid_captcha(tmp_path, monkeypatch):
    """A verified captcha_token is accepted and stripped from the lead."""
    monkeypatch.setenv("SAMUS_INTAKE_CAPTCHA_SECRET", "0x_secret")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)

    # Stub the CAPTCHA verifier so no real Turnstile call happens.
    import backend.intake.app as app_mod
    from backend.intake.captcha import CaptchaResult

    monkeypatch.setattr(
        app_mod, "verify_captcha", lambda token, source_ip="": CaptchaResult(ok=True)
    )

    client = _client(monkeypatch)
    body = dict(_valid_body(), captcha_token="turnstile-ok")
    r = client.post("/intake/onboarding", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    # captcha_token must NOT have been persisted onto the lead row.
    assert "captcha_token" not in table.items[0]


def test_endpoint_rejects_invalid_captcha(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_INTAKE_CAPTCHA_SECRET", "0x_secret")
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_table(monkeypatch, _FakeTable())

    import backend.intake.app as app_mod
    from backend.intake.captcha import CaptchaResult

    monkeypatch.setattr(
        app_mod,
        "verify_captcha",
        lambda token, source_ip="": CaptchaResult(
            ok=False,
            detail="captcha_verification_failed: bad",
        ),
    )

    client = _client(monkeypatch)
    body = dict(_valid_body(), captcha_token="turnstile-bad")
    r = client.post("/intake/onboarding", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "captcha_failed"
