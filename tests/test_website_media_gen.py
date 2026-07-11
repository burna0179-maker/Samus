"""AI media generation (Gemini image + Veo) + the paid-media budget gate.

No network: the Gemini/Veo HTTP calls are monkeypatched. Verifies dormancy
(keyless-safe), the budget fail-closed gate, taste-governed planning, image
response parsing, and the video approval/fail-closed paths.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from backend.common.media_budget import MediaBudgetStore
from backend.website import media_gen
from backend.website.models import WebsiteBrief


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _brief(**over):
    base = dict(
        business_name="Sample Cleaning",
        business_description="Family-owned house cleaning, deep cleans and Airbnb turnovers.",
        industry="House Cleaning, Residential Cleaning",
        brand_colors=["#0E7C8B", "#5CB544"],
    )
    base.update(over)
    return WebsiteBrief(**base)


def _settings(**over):
    base = dict(
        website_media_gen_enabled=True,
        gemini_api_key="gk-test",
        media_daily_dollar_cap=2.0,
        website_media_image_model="gemini-2.5-flash-image",
        website_media_image_size="2K",
        website_media_video_enabled=False,
        website_media_video_model="veo-3.0-generate-001",
        media_video_requires_approval=True,
        media_image_cost_usd=0.04,
        media_video_cost_usd=0.75,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _img_response(png=b"\x89PNG-fake"):
    b64 = base64.b64encode(png).decode("ascii")
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": b64}}]}}
            ]
        },
    )


# ---------------------------------------------------------------------------
# media_budget
# ---------------------------------------------------------------------------


def test_budget_allows_under_cap(tmp_path):
    b = MediaBudgetStore(2.0, path=tmp_path / "b.json", today="2026-06-06")
    assert b.can_spend(0.5).allowed


def test_budget_denies_over_cap(tmp_path):
    b = MediaBudgetStore(1.0, path=tmp_path / "b.json", today="2026-06-06")
    b.record(0.8)
    d = b.can_spend(0.5)
    assert not d.allowed and "cap_exceeded" in d.reason


def test_budget_zero_cap_fails_closed(tmp_path):
    b = MediaBudgetStore(0.0, path=tmp_path / "b.json", today="2026-06-06")
    assert not b.can_spend(0.01).allowed


def test_budget_resets_next_day(tmp_path):
    p = tmp_path / "b.json"
    MediaBudgetStore(2.0, path=p, today="2026-06-06").record(2.0)
    assert MediaBudgetStore(2.0, path=p, today="2026-06-07").can_spend(1.0).allowed


def test_budget_unreadable_fails_closed(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("not json{", encoding="utf-8")
    assert not MediaBudgetStore(2.0, path=p, today="2026-06-06").can_spend(0.1).allowed


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_plan_has_images_no_video_by_default():
    plan = media_gen.build_media_plan(_brief(), settings=_settings())
    kinds = [r.kind for r in plan]
    assert "hero_image" in kinds and "og_image" in kinds
    assert not any(r.is_video for r in plan)


def test_plan_includes_video_when_enabled():
    plan = media_gen.build_media_plan(
        _brief(), settings=_settings(website_media_video_enabled=True)
    )
    assert any(r.is_video and r.kind == "hero_video" for r in plan)


def test_plan_prompt_forbids_text_in_image():
    plan = media_gen.build_media_plan(_brief(), settings=_settings())
    assert all("No text" in r.prompt for r in plan)


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------


def test_generate_image_parses_inline_data(monkeypatch):
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: _img_response(b"PNGDATA"))
    data = media_gen.generate_image("a hero", api_key="gk")
    assert data == b"PNGDATA"


def test_generate_image_non_200_raises(monkeypatch):
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: httpx.Response(403, text="denied"))
    with pytest.raises(media_gen.MediaGenError):
        media_gen.generate_image("x", api_key="gk")


def test_generate_image_no_image_raises(monkeypatch):
    monkeypatch.setattr(
        media_gen, "_http_post", lambda *a, **k: httpx.Response(200, json={"candidates": []})
    )
    with pytest.raises(media_gen.MediaGenError):
        media_gen.generate_image("x", api_key="gk")


def test_generate_image_no_key_raises():
    with pytest.raises(media_gen.MediaGenError):
        media_gen.generate_image("x", api_key="")


# ---------------------------------------------------------------------------
# orchestrator dormancy + budget
# ---------------------------------------------------------------------------


def test_generate_media_disabled_returns_nothing():
    assets, report = media_gen.generate_media(
        _brief(), settings=_settings(website_media_gen_enabled=False)
    )
    assert assets == [] and report["status"] == "disabled"


def test_generate_media_no_key_returns_nothing():
    assets, report = media_gen.generate_media(_brief(), settings=_settings(gemini_api_key=""))
    assert assets == [] and report["status"] == "no_api_key"


def test_generate_media_generates_images(monkeypatch):
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: _img_response())
    assets, report = media_gen.generate_media(_brief(), settings=_settings())
    assert report["status"] == "ok"
    assert assets and all(a.ok and not a.is_video for a in assets)
    assert report["spent_usd"] > 0


def test_generate_media_budget_caps_spend(monkeypatch):
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: _img_response())
    # cap only affords one image (0.04); rest skipped on budget.
    assets, report = media_gen.generate_media(
        _brief(), settings=_settings(media_daily_dollar_cap=0.04)
    )
    ok = [a for a in assets if a.ok]
    assert len(ok) == 1
    assert any("cap" in s for s in report["skipped"])


def test_video_skipped_without_approval(monkeypatch):
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: _img_response())
    _, report = media_gen.generate_media(
        _brief(), settings=_settings(website_media_video_enabled=True, media_daily_dollar_cap=10.0)
    )
    assert any("video_requires_approval" in s for s in report["skipped"])


def test_video_runs_when_approved(monkeypatch):
    def fake_post(url, **k):
        if ":predictLongRunning" in url:
            return httpx.Response(200, json={"name": "operations/abc"})
        return _img_response()

    def fake_get(url, **k):
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": {"generatedVideos": [{"video": {"uri": "https://v/clip.mp4"}}]},
            },
        )

    monkeypatch.setattr(media_gen, "_http_post", fake_post)
    monkeypatch.setattr(media_gen, "_http_get", fake_get)
    assets, report = media_gen.generate_media(
        _brief(),
        settings=_settings(website_media_video_enabled=True, media_daily_dollar_cap=10.0),
        video_approved=True,
    )
    vids = [a for a in assets if a.is_video]
    assert vids and vids[0].ok and vids[0].video_uri == "https://v/clip.mp4"


def test_video_failure_is_fail_closed(monkeypatch):
    def fake_post(url, **k):
        if ":predictLongRunning" in url:
            return httpx.Response(500, text="veo down")
        return _img_response()

    monkeypatch.setattr(media_gen, "_http_post", fake_post)
    assets, _ = media_gen.generate_media(
        _brief(),
        settings=_settings(website_media_video_enabled=True, media_daily_dollar_cap=10.0),
        video_approved=True,
    )
    vids = [a for a in assets if a.is_video]
    assert vids and vids[0].ok is False and vids[0].error


# ---------------------------------------------------------------------------
# media build stage (wired into the walk)
# ---------------------------------------------------------------------------


def _media_ctx(brief, settings, **state_over):
    from backend.website import stages as stages_mod
    from backend.website.models import WebsiteOrder
    from backend.website.state import WebsiteBuildState

    order = WebsiteOrder(customer_name="Harmony", brief=brief)
    st = WebsiteBuildState(order_id="wb-m", order=order, **state_over)
    return stages_mod.StageContext(state=st, order=order, settings=settings), st


def test_media_stage_disabled_passthrough():
    from backend.website import stages as stages_mod

    ctx, _ = _media_ctx(_brief(), _settings(website_media_gen_enabled=False))
    res = stages_mod._media_stage(ctx)
    assert res.ok and res.detail == {"media": "disabled"}


def test_media_stage_enabled_no_key_passthrough():
    from backend.website import stages as stages_mod

    ctx, _ = _media_ctx(_brief(), _settings(gemini_api_key=""))
    res = stages_mod._media_stage(ctx)
    assert res.ok and res.detail["media_report"]["status"] == "no_api_key"


def test_media_stage_generates_uploads_and_reports(monkeypatch):
    from backend.website import stages as stages_mod
    from backend.website.wix_client import WixClient

    # Gemini image mock
    monkeypatch.setattr(media_gen, "_http_post", lambda *a, **k: _img_response())

    # Wix media + CMS mock
    def handler(request):
        p = request.url.path
        if p.endswith("/files/generate-upload-url"):
            return httpx.Response(200, json={"uploadUrl": "https://up.test/u"})
        if p == "/u":
            return httpx.Response(
                200,
                json={
                    "file": {
                        "id": "img-1",
                        "wixUrl": "wix:image://v1/img-1/hero.png",
                        "url": "https://static/img-1.png",
                        "mediaType": "image",
                        "mimeType": "image/png",
                    }
                },
            )
        if p.endswith("/items/query"):
            return httpx.Response(
                200,
                json={
                    "dataItems": [{"id": "row1", "data": {"_id": "row1", "homeHeadline": "Mighty"}}]
                },
            )
        if "/wix-data/v2/items/" in p:
            return httpx.Response(200, json={"dataItem": {"id": "row1"}})
        return httpx.Response(404)

    wix = WixClient("k", account_id="a", transport=httpx.MockTransport(handler))
    settings = _settings(media_daily_dollar_cap=10.0)
    settings.website_content_collection_id = "SiteContent"
    ctx, _ = _media_ctx(_brief(), settings, site_id="site-1")
    ctx.wix = wix

    res = stages_mod._media_stage(ctx)
    assert res.ok
    report = res.detail["media_report"]
    assert report["status"] == "ok"
    assert report["generated"]  # images generated
    assert "hero_image" in report["publish"]["refs"]
    assert report["publish"]["cms_updated"] is True


def test_people_directive_appended_to_people_prompts():
    plan = media_gen.build_media_plan(
        _brief(), settings=_settings(website_media_people_directive="DIVERSE_TEAM_MARKER")
    )
    people = [r for r in plan if r.kind in ("hero_image", "section_image", "og_image")]
    assert people and all("DIVERSE_TEAM_MARKER" in r.prompt for r in people)


def test_no_people_directive_when_empty():
    plan = media_gen.build_media_plan(
        _brief(), settings=_settings(website_media_people_directive="")
    )
    assert all("DIVERSE_TEAM_MARKER" not in r.prompt for r in plan)
