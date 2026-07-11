"""upsell_template — per-touch email composers + variable injection."""

from __future__ import annotations

from backend.catalog.registry import CATALOG
from backend.finance.upsell_template import render_upsell_email


def _catalog_buy_url(sku_id: str) -> str:
    """Live catalog buy URL for a sku — matches upsell_template's read-through
    behaviour, so tests can't hardcode a URL that outlives the next payment-link
    rotation (see cce6f0b, 2026-07-02)."""
    entry = next(e for e in CATALOG if e.sku_id == sku_id)
    assert entry.payment_link_url, f"{sku_id} has no payment_link_url in catalog"
    return entry.payment_link_url


# ---------------------------------------------------------------------------
# Promotion code embedding (Stripe coupon auto-apply at checkout)
# ---------------------------------------------------------------------------


def test_render_with_promotion_code_embeds_in_link():
    """promotion_code threads into the buy-link as prefilled_promotion_code."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_implementation",
        touch_num=1,
        queue_event_id="abc123",
        promotion_code="AUDIT-CREDIT-FAKE001",
    )
    assert result is not None
    subject, text, html, payment_link = result
    assert "prefilled_promotion_code=AUDIT-CREDIT-FAKE001" in payment_link
    assert "client_reference_id=upsell_abc123" in payment_link
    # Body links must match the returned payment_link (no orphan stale URLs)
    assert payment_link in text
    assert payment_link in html


def test_render_without_promotion_code_leaves_link_clean():
    """When no promo code provided, the prefilled param must NOT appear."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_implementation",
        touch_num=1,
        queue_event_id="abc123",
    )
    assert result is not None
    _, text, html, payment_link = result
    assert "prefilled_promotion_code" not in payment_link
    assert "prefilled_promotion_code" not in text
    assert "prefilled_promotion_code" not in html


def test_render_with_promo_for_implementation_to_optimization_hop():
    """Second-hop composer also respects the promo code embedding."""
    result = render_upsell_email(
        source_offer_code="seo_implementation",
        target_offer_code="seo_optimization",
        touch_num=2,
        queue_event_id="xyz789",
        promotion_code="IMPL-CREDIT-ZZ9988",
    )
    assert result is not None
    _, text, html, payment_link = result
    assert "prefilled_promotion_code=IMPL-CREDIT-ZZ9988" in payment_link
    assert payment_link in text
    assert payment_link in html


# ---------------------------------------------------------------------------
# Automation funnel composers (quote-based, "reply to scope" CTA)
# ---------------------------------------------------------------------------


def test_rescue_to_buildout_touch_1_mentions_credit_and_range_no_link():
    result = render_upsell_email(
        source_offer_code="service_workflow_rescue",
        target_offer_code="service_workflow_buildout",
        touch_num=1,
    )
    assert result is not None
    subject, text, html, payment_link = result
    # Quote-based hop has no buy link
    assert payment_link == ""
    # Credit and range present in body
    assert "$500" in text
    assert "$2,500-$3,000" in text or "$2,500" in text
    # Conversational CTA
    assert "reply" in text.lower()


def test_rescue_to_buildout_all_three_touches_have_distinct_subjects():
    subjects = []
    for n in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="service_workflow_rescue",
            target_offer_code="service_workflow_buildout",
            touch_num=n,
        )
        assert result is not None
        subjects.append(result[0])
    assert len(set(subjects)) == 3, f"non-distinct subjects: {subjects}"


def test_buildout_to_aiops_touch_1_mentions_credit_and_retainer():
    result = render_upsell_email(
        source_offer_code="service_workflow_buildout",
        target_offer_code="service_ai_ops_partner_build",
        touch_num=1,
    )
    assert result is not None
    subject, text, html, payment_link = result
    assert payment_link == ""
    assert "$2,500" in text  # Buildout credit
    assert "$2,000-$5,000" in text or "$2,000" in text  # quote range
    assert "$5,000/mo" in text  # retainer reference
    assert "reply" in text.lower()


def test_buildout_to_aiops_all_three_touches_have_distinct_subjects():
    subjects = []
    for n in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="service_workflow_buildout",
            target_offer_code="service_ai_ops_partner_build",
            touch_num=n,
        )
        assert result is not None
        subjects.append(result[0])
    assert len(set(subjects)) == 3, f"non-distinct subjects: {subjects}"


def test_render_quote_based_with_promo_does_not_inject_link():
    """Even if a promo code is somehow passed, a quote-based target has no
    base buy link, so the returned payment_link stays empty (no orphan ? param)."""
    result = render_upsell_email(
        source_offer_code="service_workflow_rescue",
        target_offer_code="service_workflow_buildout",
        touch_num=1,
        promotion_code="SHOULD-NOT-LAND",
    )
    assert result is not None
    _, text, html, payment_link = result
    assert payment_link == ""
    assert "SHOULD-NOT-LAND" not in text
    assert "SHOULD-NOT-LAND" not in html


def test_render_seo_audit_touch_1_returns_4_tuple_with_link():
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_implementation",
        touch_num=1,
    )
    assert result is not None
    subject, text, html, payment_link = result
    assert payment_link == _catalog_buy_url("service_seo_implementation")
    assert "SEO" in subject
    assert "audit" in text.lower()
    assert "$200" in text  # one-time fix price
    assert "<p>" in html
    assert payment_link in text
    assert payment_link in html


def test_render_touch_1_2_3_have_distinct_subjects():
    """Sequence must not be three copies of the same email."""
    subjects = []
    for n in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="seo_audit",
            target_offer_code="seo_optimization",
            touch_num=n,
        )
        assert result is not None
        subjects.append(result[0])
    assert len(set(subjects)) == 3, f"duplicate subjects: {subjects}"


def test_render_touch_1_references_audit_explicitly():
    """Touch 1 = 'five days after the audit landed' framing."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=1,
    )
    _, text, _, _ = result
    assert "audit" in text.lower()
    assert "five" in text.lower()


def test_render_touch_3_signals_last_touch():
    """Touch 3 = 'last note from me' framing — no fourth email promise."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=3,
    )
    _, text, _, _ = result
    assert "last" in text.lower() or "fourth" in text.lower()


def test_render_includes_public_page_link_for_trust():
    """Body must include the hustleforge.tech link, not just the buy link.
    Operator-confirmed pattern: prospects need a corroborating page before
    they trust a bare Stripe URL.
    """
    for touch_num in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="seo_audit",
            target_offer_code="seo_optimization",
            touch_num=touch_num,
        )
        _, text, html, _ = result
        assert "hustleforge.tech/seo-optimization" in text
        assert "hustleforge.tech/seo-optimization" in html


def test_render_unknown_source_returns_none():
    """No composer set → caller (runner) marks the row as failed cleanly."""
    assert (
        render_upsell_email(
            source_offer_code="not_in_map",
            target_offer_code="seo_optimization",
            touch_num=1,
        )
        is None
    )


def test_render_unknown_touch_returns_none():
    """Cadence override beyond 3 touches must not raise."""
    assert (
        render_upsell_email(
            source_offer_code="seo_audit",
            target_offer_code="seo_optimization",
            touch_num=99,
        )
        is None
    )


def test_render_html_has_table_or_list_structure():
    """Trust-corroboration links should be visually scannable."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=1,
    )
    _, _, html, _ = result
    # Must include some form of list (ul/ol) so links don't run together
    assert ("<ul>" in html) or ("<ol>" in html)


def test_render_signs_as_morgan():
    """Sign-off must be human, not corporate."""
    for touch_num in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="seo_audit",
            target_offer_code="seo_optimization",
            touch_num=touch_num,
        )
        _, text, _, _ = result
        assert "Morgan" in text


# ---------------------------------------------------------------------------
# Cut 3 — client_reference_id UTM injection
# ---------------------------------------------------------------------------


def test_render_appends_client_reference_id_when_queue_event_id_set():
    """Cut 3: queue_event_id non-empty -> URL has ?client_reference_id=upsell_<id>."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=1,
        queue_event_id="abc123",
    )
    assert result is not None
    _, text, html, payment_link = result
    expected_param = "client_reference_id=upsell_abc123"
    assert expected_param in payment_link
    # Must also appear in both text + html bodies (the link is interpolated)
    assert expected_param in text
    assert expected_param in html


def test_render_omits_param_when_queue_event_id_none():
    """Default behavior preserved when no queue_event_id passed."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=1,
    )
    _, text, html, payment_link = result
    assert "client_reference_id" not in payment_link
    assert "client_reference_id" not in text
    assert "client_reference_id" not in html


def test_render_omits_param_when_queue_event_id_empty_string():
    """Empty string treated same as None (defensive)."""
    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_optimization",
        touch_num=2,
        queue_event_id="",
    )
    _, _, _, payment_link = result
    assert "client_reference_id" not in payment_link


def test_render_uses_ampersand_when_url_already_has_query_string():
    """Defensive: if a base payment link ever grows a query param of its
    own, the append must use & not ? (would otherwise yield '?a=1?b=2')."""
    from backend.finance.upsell_template import _append_query_param

    out = _append_query_param("https://example.com/buy?foo=bar", "x", "y")
    assert out == "https://example.com/buy?foo=bar&x=y"


def test_append_query_param_url_encodes_value():
    """client_reference_id values are hex, but defensive: helper must escape."""
    from backend.finance.upsell_template import _append_query_param

    out = _append_query_param("https://example.com/buy", "ref", "upsell_a b")
    # urlencode quotes the space to '+' (form-encoded)
    assert out == "https://example.com/buy?ref=upsell_a+b"


# ---------------------------------------------------------------------------
# CAN-SPAM footer (postal address + unsubscribe) — required before enforce
# ---------------------------------------------------------------------------


def test_render_appends_canspam_footer(monkeypatch):
    """Commercial upsell emails must carry a postal address + unsubscribe so
    they clear the ComplianceGuard (and are CAN-SPAM compliant)."""
    monkeypatch.setenv("SAMUS_SENDER_POSTAL_ADDRESS", "HustleForge LLC, Marysville, CA 95901")
    monkeypatch.setenv("SAMUS_UNSUBSCRIBE_URL", "https://hustleforge.tech/unsubscribe")
    from backend.common.settings import reload_settings

    reload_settings()

    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_implementation",
        touch_num=1,
    )
    assert result is not None
    _, text, html, _ = result
    assert "Marysville, CA 95901" in text
    assert "Marysville, CA 95901" in html
    assert "https://hustleforge.tech/unsubscribe" in text
    assert "https://hustleforge.tech/unsubscribe" in html
    assert "Unsubscribe:" in text


def test_canspam_footer_clears_compliance_guard(monkeypatch):
    """End-to-end: a rendered upsell body passes the ComplianceGuard as
    commercial mail (the gate that enforce mode runs)."""
    monkeypatch.setenv("SAMUS_SENDER_POSTAL_ADDRESS", "HustleForge LLC, Marysville, CA 95901")
    monkeypatch.setenv("SAMUS_UNSUBSCRIBE_URL", "https://hustleforge.tech/unsubscribe")
    from backend.common.settings import reload_settings

    reload_settings()
    import backend.common.compliance_guard as cg

    monkeypatch.setattr(cg, "is_email_suppressed", lambda e: False)

    result = render_upsell_email(
        source_offer_code="seo_audit",
        target_offer_code="seo_implementation",
        touch_num=1,
    )
    subject, text, html, _ = result
    verdict = cg.evaluate(
        cg.ComplianceMessage(
            to="customer@example.com",
            subject=subject,
            body=text,
            html_body=html,
            kind="commercial",
        )
    )
    assert verdict.ok is True, verdict.reasons


def test_render_all_three_touches_inject_param():
    """Every touch in the cadence must inject the param, not just touch 1."""
    for touch_num in (1, 2, 3):
        result = render_upsell_email(
            source_offer_code="seo_audit",
            target_offer_code="seo_optimization",
            touch_num=touch_num,
            queue_event_id="evt_xyz",
        )
        _, text, html, payment_link = result
        assert "client_reference_id=upsell_evt_xyz" in payment_link
        assert "client_reference_id=upsell_evt_xyz" in text
        assert "client_reference_id=upsell_evt_xyz" in html
