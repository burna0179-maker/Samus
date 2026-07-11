"""TikTok content campaigns — sourced product -> 9:16 reel -> (dormant) post.

Reuses the reel engine (:mod:`backend.social.video.pipeline.produce_reel`) to turn
a :class:`TrendingProduct` into a vertical video, then posts it via TikTok's
**Content Posting API**. Posting is **DRY-RUN by default** (``tiktok_dry_run``) and
dormant without ``tiktok_content_posting_enabled`` + ``tiktok_content_access_token``
(which require an operator-approved TikTok app). Mirrors the fail-closed/dry-run
contract of :mod:`backend.social.adapters`.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.tiktok.models import CampaignResult, TrendingProduct

_LOG = logging.getLogger("samus.tiktok.campaigns")

_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_TIMEOUT = 30.0


def build_caption(product: TrendingProduct) -> str:
    """Deterministic, $0 caption + hashtags from the product."""
    base = f"{product.title} — you need to see this 👀"
    tags = ["#TikTokMadeMeBuyIt", "#tiktokshop", "#trending"]
    if product.category:
        tags.append("#" + "".join(w.capitalize() for w in product.category.split()))
    return f"{base}\n\n{' '.join(tags)}"


def build_product_reel(product: TrendingProduct, *, settings: Any, use_llm: bool = False) -> CampaignResult:
    """Generate a 9:16 reel for ``product`` via the reel engine.

    Returns a :class:`CampaignResult` (not yet posted). Surfaces the reel engine's
    own dormancy (``social_reel_enabled``) as a fail-closed status.
    """
    from backend.social.models import BlogInput
    from backend.social.video.pipeline import produce_reel

    blog = BlogInput(
        title=product.title,
        summary=f"{product.title} is trending"
                + (f" in {product.category}" if product.category else "")
                + (f" — {product.sales}+ sold." if product.sales else "."),
        key_points=[
            f"Why {product.title} is blowing up right now",
            "What makes it actually worth it",
            "Where to grab one before it sells out",
        ],
        cluster=product.category or "trending products",
    )
    reel = produce_reel(blog, settings=settings, use_llm=use_llm)
    if not reel.ok:
        return CampaignResult(ok=False, product_title=product.title,
                              status=reel.status or "reel_failed", error=reel.error)
    return CampaignResult(ok=True, product_title=product.title, reel_path=reel.mp4_path,
                          caption=build_caption(product), status="reel_ready")


def _http_post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    """Thin wrapper — the test monkeypatch point."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.post(url, json=json, headers=headers)


def post_to_tiktok(result: CampaignResult, *, settings: Any) -> CampaignResult:
    """Post a built reel to TikTok via the Content Posting API.

    DRY-RUN by default and dormant without the flag + token. The live path
    initializes a video upload (the approved-app, multi-step flow); it is only
    reachable when an operator arms it. Never raises.
    """
    if not result.ok or not result.reel_path:
        result.status = "no_reel_to_post"
        return result

    if bool(getattr(settings, "tiktok_dry_run", True)) or not bool(
        getattr(settings, "tiktok_content_posting_enabled", False)
    ):
        result.dry_run = True
        result.status = "dry_run"
        return result

    token = (getattr(settings, "tiktok_content_access_token", "") or "").strip()
    if not token:
        result.status = "no_access_token"
        return result

    # LIVE (operator-armed, approved app). Content Posting API step 1: init the
    # upload; subsequent chunked upload + publish happen on the returned URL.
    try:
        resp = _http_post(
            _INIT_URL,
            json={"post_info": {"title": result.caption, "privacy_level": "PUBLIC_TO_EVERYONE"},
                  "source_info": {"source": "FILE_UPLOAD"}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            result.status = f"http_{resp.status_code}"
            result.error = resp.text[:200]
            return result
        publish_id = str((resp.json().get("data") or {}).get("publish_id") or "")
        result.posted = True
        result.post_id = publish_id
        result.status = "posted"
        return result
    except (httpx.HTTPError, ValueError) as exc:
        _LOG.warning("tiktok post failed: %s", exc)
        result.status = "post_failed"
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def run_campaign(product: TrendingProduct, *, settings: Any, use_llm: bool = False) -> CampaignResult:
    """Build the reel and (dry-run by default) post it. One-call convenience."""
    result = build_product_reel(product, settings=settings, use_llm=use_llm)
    if not result.ok:
        return result
    return post_to_tiktok(result, settings=settings)


__all__ = ["build_caption", "build_product_reel", "post_to_tiktok", "run_campaign"]
