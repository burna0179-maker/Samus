"""Tests for Instagram carousel, story, image generation, and OAuth.

No live network call is ever made. Live-path tests assert fail-closed refusals
(missing credentials / missing stake sentence / insufficient media) which never
touch the network. Image generation tests use tmp_path for output.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from backend.social.models import PlannedPost

# ── Shared fixtures ──────────────────────────────────────────────────────────

_CRED_KEYS = (
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_BUSINESS_ACCOUNT_ID",
    "X_BEARER_TOKEN",
    "LINKEDIN_ACCESS_TOKEN",
    "LINKEDIN_AUTHOR_URN",
    "FACEBOOK_PAGE_TOKEN",
    "FACEBOOK_PAGE_ID",
)


def _reload(monkeypatch, *, dry_run: str = "true", ledger: Path | None = None):
    """Reload outreach + social adapters under controlled env. Reloading
    outreach first so social.adapters re-binds the reloaded send_post."""
    monkeypatch.setenv("SAMUS_SOCIAL_DRY_RUN", dry_run)
    if ledger is not None:
        monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(ledger))
    for key in _CRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    import backend.outreach.social_adapter as outreach_mod

    importlib.reload(outreach_mod)
    import backend.social.adapters as mod

    importlib.reload(mod)
    return mod


def _ig_post(
    fmt: str = "ig_carousel",
    *,
    body: str = "Educational carousel about AI.",
    stake: str = "",
) -> PlannedPost:
    return PlannedPost(
        week=1,
        day="Mon",
        platform="instagram",
        fmt=fmt,  # type: ignore[arg-type]
        pipeline_fn="educate",
        theme="Foundation",
        cluster="AI visibility",
        brief="brief",
        body=body,
        stake_sentence=stake,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. Carousel dispatch tests
# ═════════════════════════════════════════════════════════════════════════════


class TestCarouselDispatch:
    """dispatch_post with fmt=ig_carousel."""

    def test_carousel_dry_run(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="true", ledger=tmp_path / "p.jsonl")
        res = mod.dispatch_post(
            _ig_post("ig_carousel"),
            image_urls=["https://example.com/a.png", "https://example.com/b.png"],
        )
        assert res.sent is True
        assert res.dry_run is True
        assert res.platform == "instagram"
        assert res.post_id.startswith("dry-")

    def test_carousel_live_without_token_refuses(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        res = mod.dispatch_post(
            _ig_post("ig_carousel", stake="I personally reviewed this and vouch for it."),
            image_urls=["https://example.com/a.png", "https://example.com/b.png"],
        )
        assert res.sent is False
        assert res.error == "instagram_token_unset"

    def test_carousel_live_without_account_id_refuses(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "fake-token")
        # Re-reload so the module picks up the token but not the account id.
        import backend.social.adapters as mod2

        importlib.reload(mod2)
        res = mod2.dispatch_post(
            _ig_post("ig_carousel", stake="I personally reviewed this and vouch for it."),
            image_urls=["https://example.com/a.png", "https://example.com/b.png"],
        )
        assert res.sent is False
        assert res.error == "instagram_account_id_unset"

    def test_carousel_live_min_2_images(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "fake-token")
        monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "fake-id")
        import backend.social.adapters as mod2

        importlib.reload(mod2)
        res = mod2.dispatch_post(
            _ig_post("ig_carousel", stake="I personally reviewed this and vouch for it."),
            image_urls=["https://example.com/only-one.png"],
        )
        assert res.sent is False
        assert res.error == "instagram_carousel_min_2_images"

    def test_carousel_live_requires_stake(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        res = mod.dispatch_post(
            _ig_post("ig_carousel", stake=""),
            image_urls=["https://example.com/a.png", "https://example.com/b.png"],
        )
        assert res.sent is False
        assert res.error == "stake_sentence_required"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Story dispatch tests
# ═════════════════════════════════════════════════════════════════════════════


class TestStoryDispatch:
    """dispatch_post with fmt=ig_story."""

    def test_story_dry_run(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="true", ledger=tmp_path / "p.jsonl")
        res = mod.dispatch_post(
            _ig_post("ig_story"),
            image_url="https://example.com/story.png",
        )
        assert res.sent is True
        assert res.dry_run is True
        assert res.platform == "instagram"

    def test_story_live_without_token_refuses(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        res = mod.dispatch_post(
            _ig_post("ig_story", stake="I personally reviewed this and vouch for it."),
            image_url="https://example.com/story.png",
        )
        assert res.sent is False
        assert res.error == "instagram_token_unset"

    def test_story_live_no_media_refuses(self, monkeypatch, tmp_path):
        """No image_url or video_url after passing the stake gate."""
        mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "fake-token")
        monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "fake-id")
        import backend.social.adapters as mod2

        importlib.reload(mod2)
        res = mod2.dispatch_post(
            _ig_post("ig_story", stake="I personally reviewed this and vouch for it."),
            # Deliberately omitting image_url and video_url.
        )
        assert res.sent is False
        assert res.error == "instagram_media_required"


# ═════════════════════════════════════════════════════════════════════════════
# 3. dispatch_plan with image_urls_map
# ═════════════════════════════════════════════════════════════════════════════


class TestDispatchPlanImageUrlsMap:
    def test_dispatch_plan_with_image_urls_map(self, monkeypatch, tmp_path):
        mod = _reload(monkeypatch, dry_run="true", ledger=tmp_path / "p.jsonl")
        posts = [
            _ig_post("ig_carousel", body="First carousel."),
            _ig_post("ig_carousel", body="Second carousel."),
        ]
        results = mod.dispatch_plan(
            posts,
            image_urls_map={0: ["https://example.com/a.png", "https://example.com/b.png"]},
        )
        assert len(results) == 2
        assert results[0].sent is True
        assert results[0].dry_run is True
        assert results[1].sent is True


# ═════════════════════════════════════════════════════════════════════════════
# 4. Image generation tests
# ═════════════════════════════════════════════════════════════════════════════


class TestParseCarouselText:
    def test_parse_carousel_text_basic(self):
        from backend.social.image_gen import parse_carousel_text

        raw = (
            "Slide 1: Why AI visibility matters\nSlide 2: Three quick wins\nSlide 3: Start today\n"
        )
        slides = parse_carousel_text(raw)
        assert len(slides) == 3
        assert slides[0]["number"] == "1"
        assert slides[0]["content"] == "Why AI visibility matters"
        assert slides[1]["number"] == "2"
        assert slides[2]["number"] == "3"
        assert slides[2]["content"] == "Start today"

    def test_parse_carousel_text_with_labels(self):
        from backend.social.image_gen import parse_carousel_text

        raw = "Slide 1 (cover): The Ultimate Guide\nSlide 2 (tip): Use structured data\n"
        slides = parse_carousel_text(raw)
        assert len(slides) == 2
        assert slides[0]["label"] == "cover"
        assert slides[0]["content"] == "The Ultimate Guide"
        assert slides[1]["label"] == "tip"

    def test_parse_carousel_text_final_slide(self):
        from backend.social.image_gen import parse_carousel_text

        raw = "Final slide (CTA): Save this for later"
        slides = parse_carousel_text(raw)
        assert len(slides) == 1
        assert slides[0]["label"] == "cta"
        assert slides[0]["content"] == "Save this for later"

    def test_parse_carousel_text_empty(self):
        from backend.social.image_gen import parse_carousel_text

        slides = parse_carousel_text("")
        assert slides == []


class TestRenderCarousel:
    def test_render_carousel_no_pillow(self, monkeypatch):
        import backend.social.image_gen as ig_mod

        monkeypatch.setattr(ig_mod, "_PIL_AVAILABLE", False)
        result = ig_mod.render_carousel("Slide 1: Hello")
        assert result.ok is False
        assert result.error == "pillow_not_installed"

    def test_render_carousel_no_slides(self):
        import backend.social.image_gen as ig_mod

        result = ig_mod.render_carousel("no slide lines here at all")
        if not ig_mod._PIL_AVAILABLE:
            assert result.ok is False
            assert result.error == "pillow_not_installed"
        else:
            assert result.ok is False
            assert result.error == "no_slides_parsed"

    def test_render_carousel_success(self, tmp_path):
        pytest.importorskip("PIL")
        import backend.social.image_gen as ig_mod

        raw = (
            "Slide 1 (cover): Welcome to HustleForge\n"
            "Slide 2: Why AI visibility matters for SMBs\n"
            "Slide 3 (CTA): Follow for more tips\n"
        )
        result = ig_mod.render_carousel(raw, out_dir=tmp_path)
        assert result.ok is True
        assert len(result.slides) == 3
        for slide in result.slides:
            p = Path(slide.path)
            assert p.exists(), f"Rendered slide not found: {p}"
            assert p.suffix == ".png"
            assert slide.width == 1080
            assert slide.height == 1080


# ═════════════════════════════════════════════════════════════════════════════
# 5. OAuth tests
# ═════════════════════════════════════════════════════════════════════════════


class TestInstagramOAuth:
    def test_instagram_authorize_url(self):
        from backend.outreach.social_oauth import build_authorize_url

        url = build_authorize_url(
            "instagram",
            client_id="test-client-id",
            redirect_uri="https://example.com/callback",
            state="csrf-token-123",
        )
        assert "facebook.com" in url
        assert "dialog/oauth" in url
        assert "instagram_basic" in url
        assert "instagram_content_publish" in url
        assert "client_id=test-client-id" in url
        assert "state=csrf-token-123" in url

    def test_instagram_authorize_url_custom_scope(self):
        from backend.outreach.social_oauth import build_authorize_url

        url = build_authorize_url(
            "instagram",
            client_id="test-client-id",
            redirect_uri="https://example.com/callback",
            state="csrf-token-123",
            scope="instagram_basic",
        )
        assert "scope=instagram_basic" in url
        # The default instagram_content_publish should NOT be present when
        # a custom scope is provided.
        assert "instagram_content_publish" not in url

    def test_instagram_exchange_code_empty_code_raises(self):
        from backend.outreach.social_oauth import exchange_code

        with pytest.raises(ValueError, match="required"):
            exchange_code(
                "instagram",
                "",
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://example.com/callback",
            )

    def test_discover_requires_token(self):
        from backend.outreach.social_oauth import discover_instagram_account_id

        with pytest.raises(ValueError, match="page_access_token is required"):
            discover_instagram_account_id("")

    def test_exchange_long_lived_requires_all_params(self):
        from backend.outreach.social_oauth import exchange_for_long_lived_token

        with pytest.raises(ValueError, match="required"):
            exchange_for_long_lived_token("", "id", "secret")

        with pytest.raises(ValueError, match="required"):
            exchange_for_long_lived_token("token", "", "secret")

        with pytest.raises(ValueError, match="required"):
            exchange_for_long_lived_token("token", "id", "")
