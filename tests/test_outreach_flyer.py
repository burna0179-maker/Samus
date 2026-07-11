"""Tests for backend.outreach.flyer — the matched buy-now flyer.

Uses the real catalog (SKUs carry live Stripe links); no network."""

from __future__ import annotations

from backend.outreach import flyer


def _row(**over):
    base = {
        "prospect_id": "pr_1",
        "owner_email": "o@x.com",
        "owner_name": "Dana Reyes",
        "company_name": "Acme Dental",
        "security_grade": "F",
        "seo_score": "80",
        "callsheet_finding": "a security warning grading the site an F",
    }
    base.update(over)
    return base


def test_match_offer_default_is_seo_audit_with_live_link():
    offer = flyer.match_offer(_row())
    assert offer is not None
    assert offer.sku_id == "seo_audit"
    assert offer.payment_link.startswith("https://buy.stripe.com/")
    assert offer.price_usd == 149.0
    assert "graded F" in offer.why  # finding-tied reason


def test_match_offer_routes_404():
    offer = flyer.match_offer(_row(callsheet_finding="several 404 broken link errors"))
    assert offer.sku_id == "addon_404_audit"
    assert offer.payment_link.startswith("https://buy.stripe.com/")


def test_match_offer_routes_dns_ssl():
    offer = flyer.match_offer(_row(security_grade="", callsheet_finding="expired ssl certificate"))
    assert offer.sku_id == "addon_dns_health"


def test_match_offer_routes_email_deliverability():
    offer = flyer.match_offer(
        _row(security_grade="", callsheet_finding="your email lands in the spam folder")
    )
    assert offer.sku_id == "addon_email_deliverability"


def test_buy_url_carries_attribution():
    offer = flyer.match_offer(_row())
    url = flyer.buy_url(offer, prospect_id="pr_42", email="o@x.com")
    assert "client_reference_id=out_pr_42" in url  # namespaced for the webhook
    assert "utm_campaign=flyer" in url
    assert "prefilled_email=o%40x.com" in url  # url-encoded @
    # appended with the right separator
    assert url.startswith(offer.payment_link)


def test_render_flyer_is_spam_safe_and_compliant():
    offer = flyer.match_offer(_row())
    link = flyer.buy_url(offer, prospect_id="pr_1", email="o@x.com")
    html = flyer.render_flyer_html(
        company="Acme Dental",
        first_name="Dana",
        offer=offer,
        buy_link=link,
        postal_address="2290 Cheim Blvd, Marysville, CA 95901",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        stake="Alex flagged Acme Dental because your security grade is F.",
    )
    # ONE buy-now CTA to the live checkout
    assert "buy.stripe.com" in html
    # spam-safe: no external images at all
    assert "<img" not in html.lower()
    assert "http://" not in html  # no insecure remote resources
    # CAN-SPAM footer present
    assert "2290 Cheim Blvd" in html
    assert "https://hustleforge.tech/unsubscribe" in html
    assert "Unsubscribe" in html
    # personalized
    assert "Dana" in html and "Acme Dental" in html


def test_featured_offer_leads_when_no_specific_finding():
    offer = flyer.match_offer(_row(), featured_sku="service_workflow_rescue")
    assert offer.kind == "featured"
    assert offer.sku_id == "service_workflow_rescue"
    assert offer.price_usd == 500.0
    assert offer.payment_link.startswith("https://buy.stripe.com/")
    assert "manual task" in offer.headline.lower()


def test_featured_falls_back_to_addon_on_specific_finding():
    # a concrete 404 finding is more directly fixed by the cheap add-on
    offer = flyer.match_offer(
        _row(callsheet_finding="several 404 broken link errors"),
        featured_sku="service_workflow_rescue",
    )
    assert offer.kind == "matched"
    assert offer.sku_id == "addon_404_audit"


def test_render_featured_flyer_uses_proven_copy():
    offer = flyer.match_offer(_row(), featured_sku="service_workflow_rescue")
    link = flyer.buy_url(offer, prospect_id="pr_1", email="o@x.com")
    html = flyer.render_flyer_html(
        company="Acme Agency",
        first_name="Dana",
        offer=offer,
        buy_link=link,
        postal_address="2290 Cheim Blvd",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
    )
    assert "Stop doing that manual task by Friday" in html
    assert "Start My 48-Hour Automation" in html
    assert "48 hours" in html
    assert "buy.stripe.com" in html
    assert "<img" not in html.lower()  # spam-safe
    assert "2290 Cheim Blvd" in html  # CAN-SPAM
    assert "Unsubscribe" in html


def test_render_flyer_escapes_company():
    offer = flyer.match_offer(_row())
    html = flyer.render_flyer_html(
        company="<script>evil()</script>",
        first_name="Dana",
        offer=offer,
        buy_link="https://buy.stripe.com/x",
        postal_address="addr",
        unsubscribe_url="https://u",
    )
    assert "<script>evil" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Vibrant email design upgrade (2026-07-07) — rich HTML/CSS, no raster images.
# ---------------------------------------------------------------------------


def _render(**over):
    row = _row(**over)
    offer = flyer.match_offer(row, featured_sku=None)
    return flyer.render_flyer_html(
        company=row["company_name"],
        first_name=row["owner_name"],
        offer=offer,
        buy_link="https://buy.stripe.com/test",
        postal_address="2290 Cheim Blvd",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        stake=over.get("stake", "We noticed something worth a look."),
    )


def test_vibrant_render_is_rich_structured_html():
    html = _render()
    # Table-based email-safe layout (multiple nested tables) + gradient cards +
    # the hidden inbox preheader — the markers that distinguish the vibrant
    # design from the old 1.5KB inline stub.
    assert html.count("<table") >= 4
    assert "linear-gradient" in html
    assert "display:none" in html  # preheader
    assert "HustleForge" in html
    assert len(html) > 3000  # was ~1.5KB before the upgrade


def test_vibrant_render_still_image_free_and_compliant():
    """The richness must NOT come from raster images (deliverability) and must
    keep the CAN-SPAM footer — same guarantees as the pre-upgrade flyer."""
    html = _render()
    assert "<img" not in html.lower()
    assert "<script" not in html.lower()
    assert "2290 Cheim Blvd" in html  # postal address (CAN-SPAM)
    assert "Unsubscribe" in html
    assert "https://hustleforge.tech/unsubscribe" in html


def test_vibrant_render_escapes_injection_in_company_and_stake():
    """Offer/prospect content is attacker-influenced (company names, findings);
    it must be HTML-escaped so a crafted value can't inject markup."""
    row = _row(company_name="Acme <script>alert(1)</script> Co")
    offer = flyer.match_offer(row, featured_sku=None)
    html = flyer.render_flyer_html(
        company=row["company_name"],
        first_name=row["owner_name"],
        offer=offer,
        buy_link="https://buy.stripe.com/test",
        postal_address="2290 Cheim Blvd",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        stake="<img src=x onerror=alert(1)>",
    )
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror" not in html
    assert "&lt;script&gt;" in html  # crafted company present, escaped


def test_vibrant_featured_render_has_feature_grid():
    row = _row(callsheet_finding="")  # no specific finding -> featured leads
    offer = flyer.match_offer(row, featured_sku="service_workflow_rescue")
    assert offer.kind == "featured"
    html = flyer.render_flyer_html(
        company="Acme",
        first_name="Dana",
        offer=offer,
        buy_link="https://buy.stripe.com/test",
        postal_address="2290 Cheim Blvd",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        stake="",
    )
    # featured offers carry bullets -> the 2-col check grid renders
    assert "&#10003;" in html  # check mark
    assert html.count("<table") >= 5


def test_send_message_forwards_html_body_to_backend(monkeypatch):
    """The service layer must forward html_body to email_backend.send_email so
    the vibrant flyer actually reaches SendGrid as the text/html part. Guards
    the 2026-07-07 fix for the autonomous cash_engine text-only leak."""
    import backend.common.email_backend as eb
    from backend.outreach import service as svc
    from backend.outreach.models import OutreachMessageRequest

    captured = {}

    def _fake_send_email(to, subject, body, **kw):
        captured["html_body"] = kw.get("html_body")
        captured["body"] = body
        return {"message_id": "fake123", "status": "sent"}

    # send_message imports send_email locally from email_backend, so patch there.
    monkeypatch.setattr(eb, "send_email", _fake_send_email)
    # Neutralize suppression / compliance side-lookups that need AWS/DDB.
    monkeypatch.setattr(svc, "_check_harm_suppression", lambda req: None, raising=False)

    svc.send_message(
        OutreachMessageRequest(
            prospect_id="pr_test",
            channel="email",
            template_id="cash_engine_initial",
            body="plain text fallback",
            html_body="<body>VIBRANT</body>",
            to="t@example.com",
            subject="Test",
            company="Acme",
        )
    )

    assert captured["html_body"] == "<body>VIBRANT</body>"
    assert captured["body"] == "plain text fallback"  # multipart text part intact


# ---------------------------------------------------------------------------
# flyer_html_for — the ONE reusable attach every promo path calls.
# ---------------------------------------------------------------------------


class _FakeProspect:
    company_name = "Acme Dental"
    website_url = "https://acme.example"
    owner_email = "o@acme.example"
    owner_name = "Dana Reyes"
    security_grade = "F"
    seo_score = 80


def test_flyer_html_for_from_object_prospect():
    html = flyer.flyer_html_for(
        prospect_id="pr_9",
        to_email="o@acme.example",
        prospect=_FakeProspect(),
        stake="We found an F security grade.",
    )
    assert html  # non-empty
    assert "buy.stripe.com" in html  # buy button present (impulse purchase)
    assert "client_reference_id=out_pr_9" in html  # attribution on the CTA
    assert "<table" in html  # vibrant structure
    assert "<img" not in html.lower()  # image-free


def test_flyer_html_for_from_row_dict():
    html = flyer.flyer_html_for(
        prospect_id="pr_7",
        to_email="x@y.com",
        row={"company_name": "Beta LLC", "security_grade": "D"},
        stake="",
    )
    assert html
    assert "Beta LLC" in html
    assert "buy.stripe.com" in html


def test_flyer_html_for_failsoft_returns_empty(monkeypatch):
    """A render fault must return '' (caller sends text), never raise."""
    monkeypatch.setattr(
        flyer, "match_offer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = flyer.flyer_html_for(prospect_id="p", to_email="t@x.com", row={"company_name": "X"})
    assert out == ""
