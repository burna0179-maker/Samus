"""Tests for backend.catalog.wp_link_remediation — Samus's self-healing funnel.
Stripe + WordPress mocked; no network."""

from __future__ import annotations

import httpx

from backend.catalog.wp_link_remediation import (
    LinkFix,
    RemediationPlan,
    apply_remediations,
    plan_remediations,
)

STALE = "https://buy.stripe.com/OLD48hr"
LIVE = "https://buy.stripe.com/NEW48hr"
TEST_URL = "https://buy.stripe.com/test"


def _stripe_and_wp_mock(*, wp_pages, stripe_links):
    """One transport that answers both Stripe and the WP REST API.

    stripe_links: list of (url, active, description, amount_cents)
    wp_pages: list of {id, slug, title, content}
    """

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "api.stripe.com" in url:
            data = [
                {
                    "id": f"pl_{i}",
                    "url": u,
                    "active": a,
                    "line_items": {"data": [{"amount_total": amt, "description": desc}]},
                }
                for i, (u, a, desc, amt) in enumerate(stripe_links)
            ]
            return httpx.Response(200, json={"data": data, "has_more": False})
        if "/pages" in url:
            page_num = dict(req.url.params).get("page", "1")
            if page_num == "1":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": p["id"],
                            "slug": p["slug"],
                            "title": {"rendered": p["title"]},
                            "content": {"rendered": p["content"]},
                        }
                        for p in wp_pages
                    ],
                )
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        if "/posts" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


_DEFAULT_STRIPE = [
    (STALE, False, "48-Hour Workflow Rescue", 50000),
    (LIVE, True, "48-Hour Workflow Rescue", 50000),
]


# ---------------------------------------------------------------------------
# plan_remediations
# ---------------------------------------------------------------------------


def test_plan_resolves_unique_successor():
    pages = [
        {
            "id": 300,
            "slug": "workflow-rescue",
            "title": "Rescue",
            "content": f'<a href="{STALE}">Buy</a>',
        }
    ]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=_DEFAULT_STRIPE) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert len(plan.auto) == 1
    fix = plan.auto[0]
    assert fix.page_id == 300
    assert fix.stale_url == STALE
    assert fix.live_url == LIVE
    assert plan.manual == []


def test_plan_test_placeholder_is_manual():
    pages = [
        {"id": 248, "slug": "home", "title": "Home", "content": f'<a href="{TEST_URL}">Buy</a>'}
    ]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=_DEFAULT_STRIPE) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert plan.auto == []
    assert len(plan.manual) == 1
    assert "no record" in plan.manual[0].reason


def test_plan_ambiguous_successor_is_manual():
    two_live = _DEFAULT_STRIPE + [
        ("https://buy.stripe.com/NEW48hr2", True, "48-Hour Workflow Rescue", 50000),
    ]
    pages = [{"id": 300, "slug": "r", "title": "R", "content": f'<a href="{STALE}">Buy</a>'}]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=two_live) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert plan.auto == []
    assert "ambiguous" in plan.manual[0].reason


def test_plan_retired_product_is_manual():
    no_successor = [(STALE, False, "Retired Product", 9900)]
    pages = [{"id": 300, "slug": "r", "title": "R", "content": f'<a href="{STALE}">Buy</a>'}]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=no_successor) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert plan.auto == []
    assert "retired" in plan.manual[0].reason


def test_plan_dedupes_repeated_url_on_one_page():
    pages = [
        {
            "id": 248,
            "slug": "home",
            "title": "Home",
            "content": f'<a href="{TEST_URL}">A</a><a href="{TEST_URL}">B</a>',
        }
    ]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=_DEFAULT_STRIPE) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert len(plan.manual) == 1


def test_plan_no_key_is_empty():
    assert plan_remediations(api_key="").fixes == []


def test_plan_live_links_not_flagged():
    pages = [{"id": 300, "slug": "r", "title": "R", "content": f'<a href="{LIVE}">Buy</a>'}]
    with _stripe_and_wp_mock(wp_pages=pages, stripe_links=_DEFAULT_STRIPE) as c:
        plan = plan_remediations(api_key="sk_test", http_client=c)
    assert plan.fixes == []


# ---------------------------------------------------------------------------
# apply_remediations — arming + dry-run + surgical write
# ---------------------------------------------------------------------------


def _fix(page_id=300, stale=STALE, live=LIVE):
    return LinkFix(
        page_id=page_id,
        post_type="page",
        slug="s",
        title="T",
        stale_url=stale,
        live_url=live,
        action="replace",
        reason="",
    )


def test_apply_unarmed_refuses(monkeypatch):
    monkeypatch.delenv("SAMUS_WP_REMEDIATION_ENABLED", raising=False)
    result = apply_remediations(RemediationPlan(fixes=[_fix()]), dry_run=False)
    assert result.applied == []
    assert "wired-dormant" in result.skipped_reason


def test_apply_dry_run_refuses(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    result = apply_remediations(RemediationPlan(fixes=[_fix()]), dry_run=True)
    assert result.applied == []
    assert "dry_run" in result.skipped_reason


def test_apply_armed_replaces_url(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    raw = f'<!-- wp:button --><a href="{STALE}">Buy</a><!-- /wp:button -->'
    written = {}

    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_raw", lambda pid: {"id": pid, "content": {"raw": raw}})

    def fake_update(pid, content):
        written["id"], written["content"] = pid, content
        return {"id": pid, "slug": "s"}

    monkeypatch.setattr(wp, "update_page_content", fake_update)

    result = apply_remediations(RemediationPlan(fixes=[_fix()]), dry_run=False)
    assert len(result.applied) == 1
    assert result.failed == []
    assert written["id"] == 300
    assert LIVE in written["content"]
    assert STALE not in written["content"]
    # block markup preserved — surgical string replace only
    assert "<!-- wp:button -->" in written["content"]


def test_apply_two_fixes_one_page_single_write(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    stale2, live2 = "https://buy.stripe.com/OLDseo", "https://buy.stripe.com/NEWseo"
    raw = f'<a href="{STALE}">A</a><a href="{stale2}">B</a>'
    writes = []

    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_raw", lambda pid: {"id": pid, "content": {"raw": raw}})
    monkeypatch.setattr(
        wp, "update_page_content", lambda pid, content: writes.append((pid, content)) or {"id": pid}
    )

    plan = RemediationPlan(
        fixes=[
            _fix(),
            _fix(stale=stale2, live=live2),
        ]
    )
    result = apply_remediations(plan, dry_run=False)
    assert len(result.applied) == 2
    assert len(writes) == 1  # one write per page, not per fix
    assert LIVE in writes[0][1] and live2 in writes[0][1]


def test_apply_boundary_replace_does_not_corrupt_prefix_sharing_link(monkeypatch):
    """A stale token that is a PREFIX of a DIFFERENT live link on the same page
    must not be rewritten inside that longer link. Bare str.replace bug:
    replacing …/ABC would mangle a live …/ABCDEF into {live}DEF on the page."""
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    stale = "https://buy.stripe.com/ABC"
    live = "https://buy.stripe.com/NEWabc"
    other_live = "https://buy.stripe.com/ABCDEF"  # different link, shares prefix
    raw = f'<a href="{stale}">stale</a><a href="{other_live}">keep</a>'
    written = {}

    from backend.common import wordpress_client as wp

    monkeypatch.setattr(wp, "get_page_raw", lambda pid: {"id": pid, "content": {"raw": raw}})
    monkeypatch.setattr(
        wp,
        "update_page_content",
        lambda pid, content: written.update(id=pid, content=content) or {"id": pid},
    )

    result = apply_remediations(
        RemediationPlan(fixes=[_fix(stale=stale, live=live)]), dry_run=False
    )
    assert len(result.applied) == 1
    assert result.failed == []
    assert live in written["content"]  # stale link replaced
    assert other_live in written["content"]  # longer live link untouched
    assert "NEWabcDEF" not in written["content"]  # no prefix-corruption artifact


def test_apply_stale_url_missing_from_raw_is_failed(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    from backend.common import wordpress_client as wp

    monkeypatch.setattr(
        wp, "get_page_raw", lambda pid: {"id": pid, "content": {"raw": "<p>edited meanwhile</p>"}}
    )
    called = []
    monkeypatch.setattr(wp, "update_page_content", lambda pid, content: called.append(pid))

    result = apply_remediations(RemediationPlan(fixes=[_fix()]), dry_run=False)
    assert result.applied == []
    assert len(result.failed) == 1
    assert "not found" in result.failed[0][1]
    assert called == []  # nothing written


def test_apply_page_error_isolated(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    from backend.common import wordpress_client as wp

    def get_raw(pid):
        if pid == 300:
            raise RuntimeError("boom")
        return {"id": pid, "content": {"raw": f'<a href="{STALE}">x</a>'}}

    monkeypatch.setattr(wp, "get_page_raw", get_raw)
    monkeypatch.setattr(wp, "update_page_content", lambda pid, content: {"id": pid})

    plan = RemediationPlan(fixes=[_fix(page_id=300), _fix(page_id=301)])
    result = apply_remediations(plan, dry_run=False)
    assert [f.page_id for f in result.applied] == [301]
    assert [f.page_id for f, _ in result.failed] == [300]


def test_apply_manual_fixes_never_written(monkeypatch):
    monkeypatch.setenv("SAMUS_WP_REMEDIATION_ENABLED", "1")
    from backend.common import wordpress_client as wp

    called = []
    monkeypatch.setattr(wp, "get_page_raw", lambda pid: called.append(pid))
    manual = LinkFix(
        page_id=1,
        post_type="page",
        slug="s",
        title="T",
        stale_url=TEST_URL,
        live_url="",
        action="manual",
        reason="r",
    )
    result = apply_remediations(RemediationPlan(fixes=[manual]), dry_run=False)
    assert result.applied == []
    assert called == []
