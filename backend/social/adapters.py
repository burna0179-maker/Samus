"""Unified social dispatch across LinkedIn, Facebook, Instagram, and X.

LinkedIn + Facebook delegate to the battle-tested
``outreach.social_adapter.send_post`` (its DRY-RUN gate, guardrails, and ledger
are the single source of truth for those platforms). Instagram + X are new
live senders implemented here, mirroring the same contract:

    DRY-RUN short-circuit -> G1 stake gate -> G2 moderation -> token gate ->
    live send -> JSONL audit.

**DRY-RUN BY DEFAULT.** Nothing reaches a live network unless an operator sets
``SAMUS_SOCIAL_DRY_RUN=false`` *and* seeds the platform credentials. Every
outcome returns a clean :class:`SocialDispatchResult` — never an exception,
never a silent send. The same ``SAMUS_SOCIAL_LEDGER_PATH`` ledger is shared
with the outreach adapter so all social activity lands in one audit trail.

TOS NOTE: automated posting to LinkedIn/Instagram is account-ban territory
without an approved app. Activation is a deliberate per-platform operator
decision, not a default.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.outreach.social_adapter import (
    SocialPost as _OutreachPost,
    moderate_post as _moderate_post,
    send_post as _outreach_send_post,
)
from backend.social.models import Platform, PlannedPost, SocialDispatchResult

_LOG = logging.getLogger("samus.social.adapters")

# ---------------------------------------------------------------------------
# Config (read at import; tests reload the module to re-evaluate)
# ---------------------------------------------------------------------------

_DRY_RUN: bool = os.getenv("SAMUS_SOCIAL_DRY_RUN", "true").lower() not in (
    "false",
    "0",
    "no",
)
_INSTAGRAM_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
_INSTAGRAM_ACCOUNT_ID: str = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
_X_BEARER_TOKEN: str = os.getenv("X_BEARER_TOKEN", "")
_LEDGER_PATH: Path = Path(
    os.getenv("SAMUS_SOCIAL_LEDGER_PATH", "/opt/samus/data/social_posts.jsonl")
)

_FACEBOOK_GRAPH_VERSION = "v19.0"
_X_TWEETS_URL = "https://api.twitter.com/2/tweets"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_BACKOFF_SECONDS = (1.0, 2.0)
_FACEBOOK_RATE_LIMIT_CODES = {4, 17, 32, 613}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(post: PlannedPost, result: SocialDispatchResult, image_url: str) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "social.calendar",
        "platform": post.platform,
        "fmt": post.fmt,
        "pipeline_fn": post.pipeline_fn,
        "week": post.week,
        "day": post.day,
        "sent": result.sent,
        "post_id": result.post_id,
        "error": result.error,
        "dry_run": result.dry_run,
        "body_preview": post.body[:120],
        "image_url": image_url,
        "scheduled_at": post.scheduled_at,
        "has_stake_sentence": bool(post.stake_sentence.strip()),
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        _LOG.warning("social adapters audit append failed: %s", exc)


# ---------------------------------------------------------------------------
# Guardrails (reuse the outreach moderation + stake logic — single source)
# ---------------------------------------------------------------------------


def _guardrail_reason(post: PlannedPost) -> str | None:
    """Return a refusal reason, or ``None`` to proceed. Fail-closed."""
    if not post.stake_sentence or not post.stake_sentence.strip():
        return "stake_sentence_required"
    if not post.body or not post.body.strip():
        return "empty_body"
    # Reuse the outreach banned-phrase + prohibited-term moderator. The platform
    # value is irrelevant to moderation, so a LinkedIn vehicle is fine.
    vehicle = _OutreachPost(
        platform="linkedin", body=post.body, stake_sentence=post.stake_sentence
    )
    ok, reason = _moderate_post(vehicle)
    if not ok:
        return f"moderation_blocked: {reason}"
    return None


# ---------------------------------------------------------------------------
# Instagram live sender (DORMANT) — two-step container + publish
# ---------------------------------------------------------------------------


def _send_instagram(
    post: PlannedPost, image_url: str, video_url: str = ""
) -> SocialDispatchResult:
    """Instagram Graph API content publishing: create a media container, then
    publish it. Photo feed posts require a public ``image_url``; **Reels**
    require a public ``video_url`` (and a processing-poll before publish).

    A ``video_url`` is preferred when present (the reel engine produces an MP4
    that an operator has hosted at a public URL); otherwise this falls back to
    the photo path. Fail-closed on missing token / account id / media. DORMANT:
    only reachable when ``SAMUS_SOCIAL_DRY_RUN=false`` AND credentials present.
    """
    if not _INSTAGRAM_TOKEN:
        return SocialDispatchResult(False, "instagram", error="instagram_token_unset")
    if not _INSTAGRAM_ACCOUNT_ID:
        return SocialDispatchResult(
            False, "instagram", error="instagram_account_id_unset"
        )
    if not video_url and not image_url:
        return SocialDispatchResult(
            False, "instagram", error="instagram_media_required"
        )

    base = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/{_INSTAGRAM_ACCOUNT_ID}"
    if video_url:
        create_data = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": post.body,
            "access_token": _INSTAGRAM_TOKEN,
        }
    else:
        create_data = {
            "image_url": image_url,
            "caption": post.body,
            "access_token": _INSTAGRAM_TOKEN,
        }

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            create = client.post(f"{base}/media", data=create_data)
            code = _facebook_error_code(create)
            if create.status_code == 429 or code in _FACEBOOK_RATE_LIMIT_CODES:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_rate_limited"
                )
            if create.status_code != 200:
                return SocialDispatchResult(
                    False, "instagram", error=f"instagram_http_{create.status_code}"
                )
            creation_id = str((create.json() or {}).get("id") or "")
            if not creation_id:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_no_creation_id"
                )

            # Reels are processed asynchronously — the container must report
            # FINISHED before media_publish, or the publish 400s.
            if video_url:
                ready = _wait_instagram_container(client, base, creation_id)
                if ready is not None:
                    return ready

            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": creation_id, "access_token": _INSTAGRAM_TOKEN},
            )
            if publish.status_code != 200:
                return SocialDispatchResult(
                    False, "instagram", error=f"instagram_publish_{publish.status_code}"
                )
            media_id = str((publish.json() or {}).get("id") or "")
            return SocialDispatchResult(True, "instagram", post_id=media_id)
    except httpx.HTTPError as exc:
        _LOG.warning("instagram send transport error: %s", exc)
        return SocialDispatchResult(
            False, "instagram", error=f"instagram_send_failed:{type(exc).__name__}"
        )


_IG_CONTAINER_POLL_MAX = 12
_IG_CONTAINER_POLL_INTERVAL_S = 5.0


def _send_instagram_carousel(
    post: PlannedPost, image_urls: list[str]
) -> SocialDispatchResult:
    """Instagram Graph API carousel publishing: create N child containers,
    one carousel parent container, then publish. Requires at least 2 public
    ``image_urls``. Fail-closed on missing credentials or insufficient media.
    DORMANT.
    """
    if not _INSTAGRAM_TOKEN:
        return SocialDispatchResult(False, "instagram", error="instagram_token_unset")
    if not _INSTAGRAM_ACCOUNT_ID:
        return SocialDispatchResult(
            False, "instagram", error="instagram_account_id_unset"
        )
    if len(image_urls) < 2:
        return SocialDispatchResult(
            False, "instagram", error="instagram_carousel_min_2_images"
        )

    base = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/{_INSTAGRAM_ACCOUNT_ID}"

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            child_ids: list[str] = []
            for url in image_urls:
                child_data = {
                    "image_url": url,
                    "is_carousel_item": "true",
                    "access_token": _INSTAGRAM_TOKEN,
                }
                resp = client.post(f"{base}/media", data=child_data)
                code = _facebook_error_code(resp)
                if resp.status_code == 429 or code in _FACEBOOK_RATE_LIMIT_CODES:
                    return SocialDispatchResult(
                        False, "instagram", error="instagram_rate_limited"
                    )
                if resp.status_code != 200:
                    return SocialDispatchResult(
                        False,
                        "instagram",
                        error=f"instagram_child_http_{resp.status_code}",
                    )
                child_id = str((resp.json() or {}).get("id") or "")
                if not child_id:
                    return SocialDispatchResult(
                        False, "instagram", error="instagram_no_child_id"
                    )
                child_ids.append(child_id)

            carousel_data = {
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": post.body,
                "access_token": _INSTAGRAM_TOKEN,
            }
            create = client.post(f"{base}/media", data=carousel_data)
            code = _facebook_error_code(create)
            if create.status_code == 429 or code in _FACEBOOK_RATE_LIMIT_CODES:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_rate_limited"
                )
            if create.status_code != 200:
                return SocialDispatchResult(
                    False,
                    "instagram",
                    error=f"instagram_carousel_http_{create.status_code}",
                )
            carousel_id = str((create.json() or {}).get("id") or "")
            if not carousel_id:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_no_carousel_id"
                )

            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": carousel_id, "access_token": _INSTAGRAM_TOKEN},
            )
            if publish.status_code != 200:
                return SocialDispatchResult(
                    False,
                    "instagram",
                    error=f"instagram_publish_{publish.status_code}",
                )
            media_id = str((publish.json() or {}).get("id") or "")
            return SocialDispatchResult(True, "instagram", post_id=media_id)
    except httpx.HTTPError as exc:
        _LOG.warning("instagram carousel send transport error: %s", exc)
        return SocialDispatchResult(
            False, "instagram", error=f"instagram_send_failed:{type(exc).__name__}"
        )


def _send_instagram_story(
    post: PlannedPost, image_url: str = "", video_url: str = ""
) -> SocialDispatchResult:
    """Instagram Graph API story publishing: create a STORIES container then
    publish. Accepts either a public ``image_url`` or ``video_url``; video
    stories require async processing (polled via ``_wait_instagram_container``).
    Fail-closed on missing credentials or media. DORMANT.
    """
    if not _INSTAGRAM_TOKEN:
        return SocialDispatchResult(False, "instagram", error="instagram_token_unset")
    if not _INSTAGRAM_ACCOUNT_ID:
        return SocialDispatchResult(
            False, "instagram", error="instagram_account_id_unset"
        )
    if not image_url and not video_url:
        return SocialDispatchResult(
            False, "instagram", error="instagram_media_required"
        )

    base = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/{_INSTAGRAM_ACCOUNT_ID}"
    create_data: dict[str, str] = {
        "media_type": "STORIES",
        "access_token": _INSTAGRAM_TOKEN,
    }
    if video_url:
        create_data["video_url"] = video_url
    else:
        create_data["image_url"] = image_url

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            create = client.post(f"{base}/media", data=create_data)
            code = _facebook_error_code(create)
            if create.status_code == 429 or code in _FACEBOOK_RATE_LIMIT_CODES:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_rate_limited"
                )
            if create.status_code != 200:
                return SocialDispatchResult(
                    False, "instagram", error=f"instagram_http_{create.status_code}"
                )
            creation_id = str((create.json() or {}).get("id") or "")
            if not creation_id:
                return SocialDispatchResult(
                    False, "instagram", error="instagram_no_creation_id"
                )

            if video_url:
                ready = _wait_instagram_container(client, base, creation_id)
                if ready is not None:
                    return ready

            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": creation_id, "access_token": _INSTAGRAM_TOKEN},
            )
            if publish.status_code != 200:
                return SocialDispatchResult(
                    False, "instagram", error=f"instagram_publish_{publish.status_code}"
                )
            media_id = str((publish.json() or {}).get("id") or "")
            return SocialDispatchResult(True, "instagram", post_id=media_id)
    except httpx.HTTPError as exc:
        _LOG.warning("instagram story send transport error: %s", exc)
        return SocialDispatchResult(
            False, "instagram", error=f"instagram_send_failed:{type(exc).__name__}"
        )


def _wait_instagram_container(
    client: httpx.Client, base: str, creation_id: str
) -> SocialDispatchResult | None:
    """Poll a Reels container until ``status_code=FINISHED``.

    Returns ``None`` when ready (caller proceeds to publish), or a failure
    :class:`SocialDispatchResult` on ERROR / timeout. Fail-closed.
    """
    for _ in range(_IG_CONTAINER_POLL_MAX):
        status = client.get(
            f"{base.rsplit('/', 1)[0]}/{creation_id}",
            params={"fields": "status_code", "access_token": _INSTAGRAM_TOKEN},
        )
        if status.status_code != 200:
            return SocialDispatchResult(
                False, "instagram", error=f"instagram_status_http_{status.status_code}"
            )
        code = str((status.json() or {}).get("status_code") or "")
        if code == "FINISHED":
            return None
        if code == "ERROR":
            return SocialDispatchResult(False, "instagram", error="instagram_container_error")
        time.sleep(_IG_CONTAINER_POLL_INTERVAL_S)
    return SocialDispatchResult(False, "instagram", error="instagram_container_timeout")


def _facebook_error_code(resp: httpx.Response) -> int | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, int):
                return code
    return None


# ---------------------------------------------------------------------------
# X (Twitter) live sender (DORMANT)
# ---------------------------------------------------------------------------


def _send_x(post: PlannedPost) -> SocialDispatchResult:
    """Post a single tweet via X API v2 ``POST /2/tweets``.

    For a thread, ``post.body`` is the lead tweet; reply-chaining the rest is a
    future enhancement. Fail-closed on missing bearer token. DORMANT.
    """
    if not _X_BEARER_TOKEN:
        return SocialDispatchResult(False, "x", error="x_token_unset")

    headers = {
        "Authorization": f"Bearer {_X_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    # X caps a tweet at 280 chars; send the lead line of a thread body.
    text = post.body.split("\n\n", 1)[0][:280]

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(_X_TWEETS_URL, json={"text": text}, headers=headers)
        except httpx.HTTPError as exc:
            _LOG.warning("x send transport error: %s", exc)
            return SocialDispatchResult(
                False, "x", error=f"x_send_failed:{type(exc).__name__}"
            )

        if resp.status_code == 429:
            if attempt < _RATE_LIMIT_MAX_RETRIES:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
                continue
            return SocialDispatchResult(False, "x", error="x_rate_limited")

        if resp.status_code in (200, 201):
            try:
                tweet_id = str((resp.json() or {}).get("data", {}).get("id") or "")
            except ValueError:
                tweet_id = ""
            return SocialDispatchResult(True, "x", post_id=tweet_id)

        return SocialDispatchResult(False, "x", error=f"x_http_{resp.status_code}")

    return SocialDispatchResult(False, "x", error="x_rate_limited")


# ---------------------------------------------------------------------------
# Public API — unified dispatch
# ---------------------------------------------------------------------------


def dispatch_post(
    post: PlannedPost,
    *,
    image_url: str = "",
    video_url: str = "",
    image_urls: list[str] | None = None,
) -> SocialDispatchResult:
    """Dispatch one planned post to its platform. Fail-closed; never raises.

    * **DRY-RUN** (default): records to the ledger and returns ``dry_run=True``
      without running guardrails-as-refusal or touching any network — the
      dormancy control.
    * **LinkedIn / Facebook**: delegated to ``outreach.social_adapter`` (which
      runs its own gate + ledger); the result is mapped back. No double-audit
      here for those two.
    * **Instagram / X**: guardrails (G1 stake + G2 moderation) -> token gate ->
      live send, audited to the shared ledger. Routing by ``post.fmt``:

      - ``ig_carousel`` -> carousel publish (requires ``image_urls`` with >= 2)
      - ``ig_story``    -> story publish (image or video)
      - ``ig_reel`` / default -> photo or reel publish

      A ``video_url`` (a publicly hosted reel MP4) makes the Instagram send
      publish a Reel; otherwise an ``image_url`` publishes a photo.
    """
    if _DRY_RUN:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        result = SocialDispatchResult(
            sent=True, platform=post.platform, post_id=f"dry-{ts}", dry_run=True
        )
        _audit(post, result, video_url or image_url)
        return result

    # --- LIVE PATH (operator opted in) -------------------------------------
    if post.platform in ("linkedin", "facebook"):
        return _dispatch_via_outreach(post)

    # Instagram / X handled locally.
    refusal = _guardrail_reason(post)
    if refusal is not None:
        result = SocialDispatchResult(False, post.platform, error=refusal)
        _audit(post, result, video_url or image_url)
        return result

    if post.platform == "instagram":
        if post.fmt == "ig_carousel":
            urls = image_urls or ([image_url] if image_url else [])
            result = _send_instagram_carousel(post, urls)
        elif post.fmt == "ig_story":
            result = _send_instagram_story(post, image_url, video_url)
        else:
            result = _send_instagram(post, image_url, video_url)
    elif post.platform == "x":
        result = _send_x(post)
    else:  # pragma: no cover — Platform literal is exhaustive
        result = SocialDispatchResult(
            False, post.platform, error=f"unsupported_platform:{post.platform}"
        )
    _audit(post, result, video_url or image_url)
    return result


def _dispatch_via_outreach(post: PlannedPost) -> SocialDispatchResult:
    """Route a LinkedIn/Facebook post through the outreach adapter and map the
    result. The outreach adapter owns the gate + ledger for those platforms."""
    outreach_post = _OutreachPost(
        platform=post.platform,  # type: ignore[arg-type] — narrowed to li/fb above
        body=post.body,
        scheduled_at=post.scheduled_at,
        stake_sentence=post.stake_sentence,
    )
    r = _outreach_send_post(outreach_post)
    return SocialDispatchResult(
        sent=r.sent,
        platform=post.platform,
        post_id=r.post_id,
        error=r.error,
        dry_run=r.dry_run,
    )


def dispatch_plan(
    posts: list[PlannedPost],
    *,
    limit: int | None = None,
    image_urls_map: dict[int, list[str]] | None = None,
) -> list[SocialDispatchResult]:
    """Dispatch a list of planned posts (DRY-RUN by default). ``limit`` caps how
    many are dispatched in one run (sender-reputation / rate-limit hygiene).

    ``image_urls_map`` maps post indices to lists of image URLs, used for
    carousel posts that require multiple images.
    """
    selected = posts[:limit] if limit is not None else posts
    urls_map = image_urls_map or {}
    results: list[SocialDispatchResult] = []
    for idx, p in enumerate(selected):
        extra_urls = urls_map.get(idx)
        results.append(dispatch_post(p, image_urls=extra_urls))
    return results


__all__ = [
    "dispatch_post",
    "dispatch_plan",
    "SocialDispatchResult",
]


def asdict_result(result: SocialDispatchResult) -> dict:
    """Convenience for serialising a result (used by CLIs / HTTP layers)."""
    return asdict(result)
