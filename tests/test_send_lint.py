"""Pre-send subject↔CTA misalignment lint.

The headline test is :func:`test_kelly_case_detected` — the exact subject /
button combo from the 2026-06-30 17:14Z Kelly Zimmerman send. This file
exists so that send never ships unflagged again.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.common.config as config_mod
import backend.outreach.send_lint as send_lint
from backend.catalog.registry import CATALOG


def _buy_url(sku_id: str) -> str:
    """Live payment link for a SKU, straight from the catalog.

    Payment links rotate when Stripe checkouts are archived/recreated (see
    cce6f0b — 4 links repointed 2026-07-02); hardcoded URLs go stale. The
    lint resolves buttons *through* the catalog, so the tests must source
    URLs from the same place."""
    entry = next(e for e in CATALOG if e.sku_id == sku_id)
    assert entry.payment_link_url, f"{sku_id} has no payment_link_url in catalog"
    return entry.payment_link_url


def _settings(mode: str = "audit"):
    def _gs():
        return SimpleNamespace(outreach_send_lint_mode=mode)
    _gs.cache_clear = lambda: None
    return _gs


@pytest.fixture(autouse=True)
def _audit_mode(monkeypatch):
    """Default to AUDIT — lint runs but never blocks."""
    monkeypatch.setattr(config_mod, "get_settings", _settings("audit"))


def test_off_mode_skips_lint_returns_aligned(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", _settings("off"))
    rep = send_lint.lint_send(
        subject="anything", html_body="<a href='https://buy.stripe.com/x'>x</a>",
    )
    assert rep.skipped is True
    assert rep.aligned is True


def test_kelly_case_detected():
    """The exact misalignment from the 2026-06-30 17:14Z send.

    Subject says 'receptionist build, scoped' (Workflow System Buildout),
    button sells the 48-Hour Workflow Rescue ($500). Two different SKUs."""
    rep = send_lint.lint_send(
        subject="Kelly — your receptionist build, scoped",
        html_body=(
            f'<a href="{_buy_url("service_workflow_rescue")}'
            '?client_reference_id=op_test">Lock it in</a>'
        ),
    )
    assert rep.aligned is False, "Kelly's misalignment must be caught"
    assert "service_workflow_rescue" in rep.button_skus
    assert "service_workflow_buildout" in rep.subject_implied_skus
    assert any("service_workflow_rescue" in w for w in rep.warnings)
    assert any("service_workflow_buildout" in w for w in rep.warnings)


def test_aligned_send_passes():
    """Subject + button both reference the SEO Audit — should pass."""
    rep = send_lint.lint_send(
        subject="Your HustleForge SEO Audit — $149 checkout inside",
        html_body=(
            f'<a href="{_buy_url("seo_audit")}'
            '?client_reference_id=op_test">Get the audit</a>'
        ),
    )
    assert rep.aligned is True, f"Expected aligned, got warnings: {rep.warnings}"
    assert "seo_audit" in rep.button_skus
    assert "seo_audit" in rep.subject_implied_skus


def test_no_buy_url_is_trivially_aligned():
    """A transactional / scope-call email with no buy URL passes lint."""
    rep = send_lint.lint_send(
        subject="Quick question about your site",
        html_body="<p>Reply with two times this week.</p>",
    )
    assert rep.aligned is True
    assert rep.button_skus == []


def test_unmapped_url_warns_but_does_not_block():
    """A buy.stripe.com URL not in the catalog logs a warning, lint passes."""
    rep = send_lint.lint_send(
        subject="Cool offer",
        html_body='<a href="https://buy.stripe.com/UNKNOWN_LINK">x</a>',
    )
    assert any("not in catalog" in w for w in rep.warnings)
    # Unmapped → no resolved button SKUs → trivially aligned (no comparison to make)
    assert rep.aligned is True


def test_claimed_sku_enforces_both_directions():
    """When the operator declares the intended SKU, the lint requires it
    in BOTH the buttons and the subject."""
    rep = send_lint.lint_send(
        subject="Your HustleForge SEO Audit — $149",
        html_body=f'<a href="{_buy_url("service_workflow_rescue")}">x</a>',
        claimed_sku="seo_audit",
    )
    assert rep.aligned is False
    assert any("claimed_sku 'seo_audit'" in w for w in rep.warnings)


def test_send_promotional_audit_mode_does_not_raise(monkeypatch):
    """AUDIT: warnings logged, send proceeds."""
    monkeypatch.setattr(config_mod, "get_settings", _settings("audit"))
    sent = []
    import backend.common.email_backend as eb
    monkeypatch.setattr(eb, "send_email", lambda **kw: (sent.append(kw) or
                        {"message_id": "x", "channel": "email",
                         "to": kw["to"], "ts": "x"}))
    resp = send_lint.send_promotional(
        to="kelly@example.com",
        subject="Kelly — your receptionist build, scoped",
        body="text",
        html_body=f'<a href="{_buy_url("service_workflow_rescue")}">x</a>',
    )
    assert len(sent) == 1
    assert resp["lint"]["aligned"] is False
    assert any("service_workflow_buildout" in w for w in resp["lint"]["warnings"])


def test_send_promotional_enforce_mode_raises(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", _settings("enforce"))
    sent = []
    import backend.common.email_backend as eb
    monkeypatch.setattr(eb, "send_email", lambda **kw: sent.append(kw))
    with pytest.raises(send_lint.SendLintBlocked):
        send_lint.send_promotional(
            to="kelly@example.com",
            subject="Kelly — your receptionist build, scoped",
            body="text",
            html_body=f'<a href="{_buy_url("service_workflow_rescue")}">x</a>',
        )
    assert sent == []  # blocked before reaching the backend


def test_send_promotional_enforce_passes_aligned(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", _settings("enforce"))
    sent = []
    import backend.common.email_backend as eb
    monkeypatch.setattr(eb, "send_email", lambda **kw: (sent.append(kw) or
                        {"message_id": "x", "channel": "email",
                         "to": kw["to"], "ts": "x"}))
    resp = send_lint.send_promotional(
        to="prospect@example.com",
        subject="Your HustleForge SEO Audit — $149",
        body="text",
        html_body=f'<a href="{_buy_url("seo_audit")}">go</a>',
        claimed_sku="seo_audit",
    )
    assert len(sent) == 1
    assert resp["lint"]["aligned"] is True
