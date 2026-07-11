"""Tests for backend.intake.wp_onboarding_page — the WP onboarding fallback
publisher. WordPress client fully monkeypatched; no network."""

from __future__ import annotations

import pytest

from backend.intake import wp_onboarding_page as wop
from backend.intake.wp_onboarding_page import (
    PublishPlan,
    apply_publish,
    load_fallback_html,
    plan_publish,
)

HTML = "<div>onboarding form</div>"


# ---------------------------------------------------------------------------
# asset
# ---------------------------------------------------------------------------


def test_asset_loads_and_is_self_contained():
    body = load_fallback_html()
    assert body.strip()
    # The fallback MUST hit the intake API and carry no external script/style
    # (WP themes/CSP can strip those). The onboarding POST target is NOT
    # hardcoded — it comes from schema.submit.post_url at runtime — so we only
    # assert the two endpoints the page calls literally.
    assert "/intake/form-schema" in body
    assert "/intake/telemetry" in body
    assert "post_url" in body  # submit target read from the fetched schema
    # No external resources: no <script src="…">, no stylesheet <link>.
    assert '<script src="' not in body
    assert 'rel="stylesheet"' not in body


# ---------------------------------------------------------------------------
# plan_publish
# ---------------------------------------------------------------------------


def test_plan_create_when_absent(monkeypatch):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_any_status", lambda slug: None)
    plan = plan_publish(html=HTML)
    assert plan.action == "create"
    assert plan.page_id is None
    assert plan.content_len == len(HTML)


def test_plan_update_when_content_differs(monkeypatch):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "get_page_any_status",
        lambda slug: {"id": 42, "status": "publish", "content": {"raw": "<div>OLD</div>"}},
    )
    plan = plan_publish(html=HTML)
    assert plan.action == "update"
    assert plan.page_id == 42
    assert plan.status == "publish"


def test_plan_noop_when_content_matches(monkeypatch):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "get_page_any_status",
        lambda slug: {"id": 7, "status": "draft", "content": {"raw": HTML}},
    )
    plan = plan_publish(html=HTML)
    assert plan.action == "noop"
    assert plan.page_id == 7


def test_plan_blocked_when_existence_check_errors(monkeypatch):
    # A transient 429/401/network error during the lookup must NOT become a
    # "create" plan — that would risk a duplicate on retry. It blocks instead.
    from backend.common import wordpress_client as wp

    def boom(slug):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(wp, "get_page_any_status", boom)
    plan = plan_publish(html=HTML)
    assert plan.action == "blocked"
    assert plan.page_id is None
    assert "429" in plan.reason


def test_apply_blocked_plan_writes_nothing(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp, "create_draft_page", lambda **k: pytest.fail("blocked plan must not create")
    )
    monkeypatch.setattr(
        wp, "update_page_content", lambda *a: pytest.fail("blocked plan must not update")
    )
    blocked = PublishPlan(
        action="blocked",
        slug="onboarding",
        page_id=None,
        status="",
        reason="existence check failed (503)",
        content_len=len(HTML),
    )
    result = apply_publish(blocked, html=HTML, dry_run=False)
    assert result.action == ""
    assert "existence check failed" in result.skipped_reason


# ---------------------------------------------------------------------------
# apply_publish — arming + dry-run gates
# ---------------------------------------------------------------------------


def _create_plan() -> PublishPlan:
    return PublishPlan(
        action="create",
        slug="onboarding",
        page_id=None,
        status="",
        reason="",
        content_len=len(HTML),
    )


def _update_plan(page_id: int = 42) -> PublishPlan:
    return PublishPlan(
        action="update",
        slug="onboarding",
        page_id=page_id,
        status="publish",
        reason="",
        content_len=len(HTML),
    )


def test_apply_unarmed_refuses(monkeypatch):
    monkeypatch.delenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", raising=False)
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp, "create_draft_page", lambda **k: pytest.fail("must not write when unarmed")
    )
    result = apply_publish(_create_plan(), html=HTML, dry_run=False)
    assert result.action == ""
    assert "wired-dormant" in result.skipped_reason.lower()


def test_apply_dry_run_refuses(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp, "create_draft_page", lambda **k: pytest.fail("must not write on dry_run")
    )
    result = apply_publish(_create_plan(), html=HTML, dry_run=True)
    assert result.action == ""
    assert "dry_run" in result.skipped_reason


def test_apply_noop_plan_writes_nothing(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "create_draft_page", lambda **k: pytest.fail("noop must not create"))
    monkeypatch.setattr(wp, "update_page_content", lambda *a: pytest.fail("noop must not update"))
    noop = PublishPlan(
        action="noop",
        slug="onboarding",
        page_id=1,
        status="publish",
        reason="already current",
        content_len=len(HTML),
    )
    result = apply_publish(noop, html=HTML, dry_run=False)
    assert result.action == ""
    assert result.skipped_reason == "already current"


# ---------------------------------------------------------------------------
# apply_publish — armed writes
# ---------------------------------------------------------------------------


def test_apply_armed_creates_draft(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": 100, "slug": kwargs.get("slug"), "status": "draft"}

    monkeypatch.setattr(wp, "create_draft_page", fake_create)
    monkeypatch.setattr(
        wp, "update_page_content", lambda *a: pytest.fail("create path must not update")
    )

    result = apply_publish(_create_plan(), html=HTML, dry_run=False)
    assert result.action == "create"
    assert result.page_id == 100
    assert captured["slug"] == "onboarding"
    assert captured["content"] == HTML
    assert captured["title"] == wop.ONBOARDING_TITLE


def test_apply_armed_updates_in_place(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    calls = []

    def fake_update(page_id, content):
        calls.append((page_id, content))
        return {"id": page_id}

    monkeypatch.setattr(wp, "update_page_content", fake_update)
    monkeypatch.setattr(
        wp, "create_draft_page", lambda **k: pytest.fail("update path must not create")
    )

    result = apply_publish(_update_plan(55), html=HTML, dry_run=False)
    assert result.action == "update"
    assert result.page_id == 55
    assert calls == [(55, HTML)]


def test_apply_write_failure_captured_not_raised(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    def boom(**kwargs):
        raise RuntimeError("WordPress API error 503")

    monkeypatch.setattr(wp, "create_draft_page", boom)
    result = apply_publish(_create_plan(), html=HTML, dry_run=False)
    assert result.action == ""
    assert "503" in result.error


def test_apply_update_without_page_id_is_error(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp, "update_page_content", lambda *a: pytest.fail("must not update without id")
    )
    plan = PublishPlan(
        action="update",
        slug="onboarding",
        page_id=None,
        status="publish",
        reason="",
        content_len=len(HTML),
    )
    result = apply_publish(plan, html=HTML, dry_run=False)
    assert result.action == ""
    assert "page_id" in result.error


def test_force_armed_overrides_env(monkeypatch):
    monkeypatch.delenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", raising=False)
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "create_draft_page", lambda **k: {"id": 9, "status": "draft"})
    result = apply_publish(_create_plan(), html=HTML, dry_run=False, force_armed=True)
    assert result.action == "create"
    assert result.page_id == 9


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_plan_only_never_writes(monkeypatch, capsys):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_any_status", lambda slug: None)
    monkeypatch.setattr(
        wp, "create_draft_page", lambda **k: pytest.fail("plan-only must not write")
    )
    rc = wop.main([])
    assert rc == 0
    assert "plan: create" in capsys.readouterr().out


def test_cli_apply_creates(monkeypatch, capsys):
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_any_status", lambda slug: None)
    monkeypatch.setattr(wp, "create_draft_page", lambda **k: {"id": 321, "status": "draft"})
    rc = wop.main(["--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "applied: create" in out
    assert "321" in out


def test_cli_blocked_lookup_exits_nonzero(monkeypatch, capsys):
    # Existence check fails -> plan blocked -> CLI must exit non-zero so a
    # scheduled run surfaces the credential/permission problem.
    monkeypatch.setenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "1")
    from backend.common import wordpress_client as wp

    def boom(slug):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(wp, "get_page_any_status", boom)
    monkeypatch.setattr(wp, "create_draft_page", lambda **k: pytest.fail("blocked must not create"))
    rc = wop.main(["--apply"])
    assert rc == 1
    assert "blocked" in capsys.readouterr().out


def test_cli_apply_unarmed_exits_zero_and_skips(monkeypatch, capsys):
    monkeypatch.delenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", raising=False)
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_any_status", lambda slug: None)
    monkeypatch.setattr(wp, "create_draft_page", lambda **k: pytest.fail("unarmed must not write"))
    rc = wop.main(["--apply"])
    assert rc == 0
    assert "apply skipped" in capsys.readouterr().out


def test_cli_whoami_role_sufficient(monkeypatch, capsys):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "whoami",
        lambda: {"ok": True, "id": 3, "name": "Samus", "slug": "samus", "roles": ["editor"]},
    )
    rc = wop.main(["--whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "editor" in out
    assert "can create/edit pages" in out


def test_cli_whoami_role_too_low(monkeypatch, capsys):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "whoami",
        lambda: {"ok": True, "id": 3, "name": "Samus", "slug": "samus", "roles": ["author"]},
    )
    rc = wop.main(["--whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CANNOT create/edit pages" in out


def test_cli_whoami_not_logged_in_guides_username_first(monkeypatch, capsys):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "whoami",
        lambda: {
            "ok": False,
            "status": 401,
            "code": "rest_not_logged_in",
            "message": "not logged in",
        },
    )
    rc = wop.main(["--whoami"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "whoami FAILED" in out
    assert "NO valid credentials" in out
    assert "USERNAME mismatch" in out


def test_cli_whoami_rate_limited_is_not_an_auth_verdict(monkeypatch, capsys):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "whoami",
        lambda: {"ok": False, "status": 429, "code": "", "message": "Too Many Requests"},
    )
    rc = wop.main(["--whoami"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "RATE-LIMITED" in out
    assert "invalid" not in out.lower()


def test_cli_whoami_wrong_password_named(monkeypatch, capsys):
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp,
        "whoami",
        lambda: {
            "ok": False,
            "status": 401,
            "code": "incorrect_password",
            "message": "invalid application password",
        },
    )
    rc = wop.main(["--whoami"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out
