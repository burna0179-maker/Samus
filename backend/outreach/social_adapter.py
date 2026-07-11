"""Social channel adapter for LinkedIn + Facebook outbound posts.

Mirrors the SES email adapter shape. **DRY-RUN BY DEFAULT** — the live
network path is unreachable unless the operator both sets
``SAMUS_SOCIAL_DRY_RUN=false`` *and* populates platform credentials
(``LINKEDIN_ACCESS_TOKEN`` + ``LINKEDIN_AUTHOR_URN`` /
``FACEBOOK_PAGE_TOKEN`` + ``FACEBOOK_PAGE_ID``).

CAPABILITY-IN-PLACE, NOT ACTIVATED
----------------------------------
The live LinkedIn (``POST /v2/ugcPosts``) and Facebook
(``POST /{page-id}/feed``) wiring below is real but inert by default.
Activation is an explicit, deliberate operator decision because:

* **TOS RISK (LinkedIn):** LinkedIn's User Agreement forbids automated
  posting via unofficial means and aggressively bans accounts that do it.
  Turning ``SAMUS_SOCIAL_DRY_RUN=false`` for LinkedIn risks a permanent
  account ban. Only flip it with a Marketing Developer Platform–approved
  app + a token whose scopes you control. This is an operator call, not a
  default.
* **Facebook** is less hostile but still rate-limited and policy-gated.

Every send (dry-run, refusal, live) is appended to the JSONL ledger at
``SAMUS_SOCIAL_LEDGER_PATH`` for audit.

GUARDRAILS (fail-closed, mirroring email):
* G1 — every post must carry a non-empty ``stake_sentence`` or it is
  refused (``stake_sentence_required``) before any API call.
* G2 — content moderation precheck rejects banned-phrase / templated
  content (``moderation_blocked: ...``) before any API call. Reuses the
  outreach banned-phrase list from ``stake_sentence_guard``.
* G3/G8 — daily-cap + legitimacy gate. See ``moderate_post`` /
  ``_enforce_send_guardrails`` notes: the stake-sentence cap
  (``stake_sentence_budget``) and legitimacy signal are EMAIL-path gates
  today; the attachment point for social is flagged inline for parent
  review rather than silently bolted on.

The dispatch order is: dry-run short-circuit → stake gate → moderation
gate → token gate → live send. Any failure returns a clean
``SocialSendResult(sent=False, error=...)`` — never an exception, never a
silent send.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx

_LOG = logging.getLogger("samus.outreach.social_adapter")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SocialPlatform = Literal["linkedin", "facebook"]

# ---------------------------------------------------------------------------
# Platform endpoints
# ---------------------------------------------------------------------------

_LINKEDIN_UGC_URL = "https://api.linkedin.com/v2/ugcPosts"
_LINKEDIN_API_VERSION = "202401"  # LinkedIn-Version header (REST)
_FACEBOOK_GRAPH_VERSION = "v19.0"

# Bounded retry/backoff on rate-limit. Small + capped so we never hang.
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_BACKOFF_SECONDS = (1.0, 2.0)  # one entry per retry attempt
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

# Facebook transient/over-quota error codes (Graph API).
_FACEBOOK_RATE_LIMIT_CODES = {4, 17, 32, 613}

# ---------------------------------------------------------------------------
# Module-level config helpers
#
# Read at import time so a fresh import (tests reload the module) re-evaluates
# them. The dispatch-level DRY_RUN gate is the primary dormancy control.
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() not in ("false", "0", "no", "")


_DRY_RUN: bool = os.getenv("SAMUS_SOCIAL_DRY_RUN", "true").lower() not in (
    "false",
    "0",
    "no",
)
_LINKEDIN_TOKEN: str = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
_LINKEDIN_AUTHOR_URN: str = os.getenv("LINKEDIN_AUTHOR_URN", "")
_FACEBOOK_TOKEN: str = os.getenv("FACEBOOK_PAGE_TOKEN", "")
_FACEBOOK_PAGE_ID: str = os.getenv("FACEBOOK_PAGE_ID", "")
_LEDGER_PATH: Path = Path(
    os.getenv("SAMUS_SOCIAL_LEDGER_PATH", "/opt/samus/data/social_posts.jsonl")
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SocialPost:
    """A single outbound social post ready to be dispatched.

    ``stake_sentence`` is the operator-authored opener that turns this from
    automated spam into a human-vouched post (G1). It is REQUIRED for a live
    send — a post with no stake_sentence is refused at dispatch.
    """

    platform: SocialPlatform
    body: str
    link: str = ""
    image_url: str = ""
    scheduled_at: str = ""
    tags: list[str] = field(default_factory=list)
    stake_sentence: str = ""


@dataclass
class SocialSendResult:
    """Result of a single send_post call."""

    sent: bool
    platform: SocialPlatform
    post_id: str = ""
    error: str = ""
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _audit_send(post: SocialPost, result: SocialSendResult) -> None:
    """Append a JSONL audit line to the ledger path."""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "platform": post.platform,
        "sent": result.sent,
        "post_id": result.post_id,
        "error": result.error,
        "dry_run": result.dry_run,
        "body_preview": post.body[:120],
        "link": post.link,
        "image_url": post.image_url,
        "scheduled_at": post.scheduled_at,
        "tags": post.tags,
        "has_stake_sentence": bool(post.stake_sentence.strip()),
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        _LOG.warning("social_adapter audit append failed: %s", exc)


# ---------------------------------------------------------------------------
# G2 — content moderation precheck
# ---------------------------------------------------------------------------


def moderate_post(post: SocialPost) -> tuple[bool, str]:
    """Pre-send content moderation. Returns ``(ok, reason)``.

    Rejects prohibited / templated content BEFORE any network call. Reuses
    the outreach banned-phrase list (``STAKE_SENTENCE_BANNED_PHRASES``) so the
    same SDR-cliche tells that fail an email stake also fail a social post,
    plus a couple of hard-prohibited content categories.

    Fail-closed: any unexpected error returns ``(False, ...)`` so a broken
    moderator never lets content through.
    """
    try:
        from backend.common.stake_sentence_guard import STAKE_SENTENCE_BANNED_PHRASES

        haystack = f"{post.body}\n{post.stake_sentence}".lower()
        if not post.body.strip():
            return False, "empty_body"
        for phrase in STAKE_SENTENCE_BANNED_PHRASES:
            if phrase in haystack:
                return False, f"banned_phrase:{phrase}"
        # Hard-prohibited content classes (extensible). Kept deliberately
        # small + deterministic — this is a precheck, not a full classifier.
        for term in _PROHIBITED_TERMS:
            if term in haystack:
                return False, f"prohibited_term:{term}"
        return True, ""
    except Exception as exc:  # noqa: BLE001 — fail-closed on a broken moderator
        _LOG.warning("moderate_post failed-closed: %s", exc)
        return False, f"moderation_error:{type(exc).__name__}"


# Deterministic prohibited-content terms. Adding/removing requires a
# Decisions Log entry per the guardrails chapter convention.
_PROHIBITED_TERMS: tuple[str, ...] = (
    "guaranteed returns",
    "get rich quick",
    "miracle cure",
)


# ---------------------------------------------------------------------------
# G1/G3/G8 — outbound guardrail gate
# ---------------------------------------------------------------------------


def _enforce_send_guardrails(post: SocialPost) -> SocialSendResult | None:
    """Run the fail-closed outbound guardrails. Returns a refusal
    ``SocialSendResult`` to short-circuit the send, or ``None`` to proceed.

    G1 (stake sentence required) is enforced inline here — a social post with
    no stake_sentence is refused, mirroring ``campaign.compose_body`` for
    email.

    G3 (daily cap) / G8 (legitimacy signal): these live on the EMAIL campaign
    path today (``stake_sentence_budget`` + ``run_campaign._apply_warmth_gate``)
    and are NOT reachable from this adapter without a heavier refactor of the
    /work payload shape (the social /work actions don't carry a prospect
    legitimacy_signal or a campaign budget context). The attachment point is
    flagged for parent review: when social moves past dormant, route
    ``send_post`` through the same budget reservation + legitimacy gate the
    campaign uses. Until then the stake-sentence (G1) + moderation (G2) gates
    are the enforced floor.
    """
    # G1 — stake sentence required (fail-closed).
    if not post.stake_sentence or not post.stake_sentence.strip():
        return SocialSendResult(
            sent=False,
            platform=post.platform,
            error="stake_sentence_required",
        )

    # G2 — content moderation precheck (fail-closed).
    ok, reason = moderate_post(post)
    if not ok:
        return SocialSendResult(
            sent=False,
            platform=post.platform,
            error=f"moderation_blocked: {reason}",
        )

    return None


# ---------------------------------------------------------------------------
# Live platform senders (DORMANT by default — unreachable unless DRY_RUN off
# AND credentials present)
# ---------------------------------------------------------------------------


def _send_linkedin(post: SocialPost) -> SocialSendResult:
    """LinkedIn live send via ``POST /v2/ugcPosts`` (UGC share).

    Fail-closed: missing token / author URN returns a clean refusal and never
    touches the network. HTTP 429 returns ``linkedin_rate_limited`` after a
    small bounded backoff. Any transport / non-2xx error returns a clean
    ``linkedin_send_failed:...`` result rather than raising.

    DORMANT: only reachable when ``SAMUS_SOCIAL_DRY_RUN=false`` (the dispatch
    gate) AND a token is set. See the module docstring TOS warning.
    """
    if not _LINKEDIN_TOKEN:
        _LOG.warning("social_adapter linkedin token unset")
        return SocialSendResult(
            sent=False, platform="linkedin", error="linkedin_token_unset"
        )
    if not _LINKEDIN_AUTHOR_URN:
        _LOG.warning("social_adapter linkedin author urn unset")
        return SocialSendResult(
            sent=False, platform="linkedin", error="linkedin_author_urn_unset"
        )

    share_content: dict = {
        "shareCommentary": {"text": post.body},
        "shareMediaCategory": "ARTICLE" if post.link else "NONE",
    }
    if post.link:
        share_content["media"] = [
            {"status": "READY", "originalUrl": post.link}
        ]
    payload = {
        "author": _LINKEDIN_AUTHOR_URN,
        "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }
    headers = {
        "Authorization": f"Bearer {_LINKEDIN_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": _LINKEDIN_API_VERSION,
    }

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(
                    _LINKEDIN_UGC_URL, json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            _LOG.warning("linkedin send transport error: %s", exc)
            return SocialSendResult(
                sent=False,
                platform="linkedin",
                error=f"linkedin_send_failed:{type(exc).__name__}",
            )

        if resp.status_code == 429:
            if attempt < _RATE_LIMIT_MAX_RETRIES:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
                continue
            _LOG.warning("linkedin rate limited after %d attempts", attempt + 1)
            return SocialSendResult(
                sent=False, platform="linkedin", error="linkedin_rate_limited"
            )

        if resp.status_code in (200, 201):
            post_id = _parse_linkedin_post_id(resp)
            _LOG.info("linkedin send ok post_id=%s", post_id)
            return SocialSendResult(
                sent=True, platform="linkedin", post_id=post_id
            )

        # Any other non-2xx — clean failure, no raise.
        _LOG.warning(
            "linkedin send non-2xx status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return SocialSendResult(
            sent=False,
            platform="linkedin",
            error=f"linkedin_http_{resp.status_code}",
        )

    # Unreachable, but keep the contract.
    return SocialSendResult(
        sent=False, platform="linkedin", error="linkedin_rate_limited"
    )


def _parse_linkedin_post_id(resp: httpx.Response) -> str:
    """Extract the created UGC post URN. LinkedIn returns it in the
    ``x-restli-id`` header (and sometimes the JSON ``id``)."""
    header_id = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
    if header_id:
        return str(header_id)
    try:
        body = resp.json()
        return str(body.get("id") or "")
    except ValueError:
        return ""


def _send_facebook(post: SocialPost) -> SocialSendResult:
    """Facebook live send via ``POST /{page-id}/feed``.

    Fail-closed: missing page token / page id returns a clean refusal and
    never touches the network. Facebook error codes 32 & 613 (and other
    transient throttles) return ``facebook_rate_limited`` after a small
    bounded backoff. Any transport / error returns a clean
    ``facebook_send_failed:...`` rather than raising.

    DORMANT: only reachable when ``SAMUS_SOCIAL_DRY_RUN=false`` AND a page
    token + page id are set.
    """
    if not _FACEBOOK_TOKEN:
        _LOG.warning("social_adapter facebook token unset")
        return SocialSendResult(
            sent=False, platform="facebook", error="facebook_token_unset"
        )
    if not _FACEBOOK_PAGE_ID:
        _LOG.warning("social_adapter facebook page id unset")
        return SocialSendResult(
            sent=False, platform="facebook", error="facebook_page_id_unset"
        )

    url = (
        f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/"
        f"{_FACEBOOK_PAGE_ID}/feed"
    )
    data: dict = {"message": post.body, "access_token": _FACEBOOK_TOKEN}
    if post.link:
        data["link"] = post.link

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(url, data=data)
        except httpx.HTTPError as exc:
            _LOG.warning("facebook send transport error: %s", exc)
            return SocialSendResult(
                sent=False,
                platform="facebook",
                error=f"facebook_send_failed:{type(exc).__name__}",
            )

        # Facebook signals rate-limit / over-quota via 429 OR an error code in
        # the JSON body (codes 4/17/32/613) even on a 200/4xx envelope.
        fb_err_code = _facebook_error_code(resp)
        if resp.status_code == 429 or fb_err_code in _FACEBOOK_RATE_LIMIT_CODES:
            if attempt < _RATE_LIMIT_MAX_RETRIES:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
                continue
            _LOG.warning(
                "facebook rate limited (code=%s) after %d attempts",
                fb_err_code,
                attempt + 1,
            )
            return SocialSendResult(
                sent=False, platform="facebook", error="facebook_rate_limited"
            )

        if resp.status_code == 200 and fb_err_code is None:
            post_id = _parse_facebook_post_id(resp)
            _LOG.info("facebook send ok post_id=%s", post_id)
            return SocialSendResult(
                sent=True, platform="facebook", post_id=post_id
            )

        # Any other error — clean failure, no raise.
        _LOG.warning(
            "facebook send error status=%s code=%s body=%s",
            resp.status_code,
            fb_err_code,
            resp.text[:200],
        )
        detail = f"code_{fb_err_code}" if fb_err_code is not None else f"http_{resp.status_code}"
        return SocialSendResult(
            sent=False, platform="facebook", error=f"facebook_{detail}"
        )

    return SocialSendResult(
        sent=False, platform="facebook", error="facebook_rate_limited"
    )


def _facebook_error_code(resp: httpx.Response) -> int | None:
    """Pull the Graph API error code from a response body, if present."""
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
            try:
                return int(code)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def _parse_facebook_post_id(resp: httpx.Response) -> str:
    """Facebook returns ``{"id": "<page_id>_<post_id>"}`` on success."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("id") or "")
    except ValueError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_post(post: SocialPost) -> SocialSendResult:
    """Dispatch a social post. Fail-closed at every gate; never raises.

    Dispatch order:

    1. **Dry-run short-circuit** (default): records the post to the JSONL
       ledger and returns a successful ``dry_run=True`` result without
       running guardrails-as-refusal or touching any external API. This is
       the dormancy control — by default the live path is unreachable.
    2. **Guardrails** (live path only): G1 stake-sentence + G2 moderation.
       A failure returns a clean refusal result.
    3. **Token gate** (live path only): missing platform credentials return a
       clean ``*_token_unset`` refusal — even with DRY_RUN off, no creds means
       no send.
    4. **Live send** to the platform helper.

    Every outcome (dry-run, refusal, send) is appended to the ledger.
    """
    if _DRY_RUN:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        post_id = f"dry-{ts}"
        result = SocialSendResult(
            sent=True,
            platform=post.platform,
            post_id=post_id,
            dry_run=True,
        )
        _audit_send(post, result)
        _LOG.debug(
            "social_adapter dry_run platform=%s post_id=%s", post.platform, post_id
        )
        return result

    # --- LIVE PATH (operator opted in) -------------------------------------
    # Guardrails first so a refusal never burns an API call.
    refusal = _enforce_send_guardrails(post)
    if refusal is not None:
        _audit_send(post, refusal)
        _LOG.warning(
            "social_adapter guardrail refusal platform=%s error=%s",
            post.platform,
            refusal.error,
        )
        return refusal

    # Token gate + dispatch to platform helper. The helper re-checks the
    # token (defense in depth) and performs the real network call.
    if post.platform == "linkedin":
        result = _send_linkedin(post)
    else:
        result = _send_facebook(post)

    _audit_send(post, result)
    return result


# ---------------------------------------------------------------------------
# LLM-assisted post composition
# ---------------------------------------------------------------------------

_LINKEDIN_TEMPLATE = (
    "Excited to share an update: {summary} — reach out to learn more. #business"
)
_FACEBOOK_TEMPLATE = "Big news: {summary}! Let us know your thoughts."

# Max body lengths by platform (chars, approximate platform limits)
_PLATFORM_CHAR_LIMIT: dict[str, int] = {
    "linkedin": 250,
    "facebook": 150,
}


def _truncate_to_limit(text: str, platform: SocialPlatform) -> str:
    limit = _PLATFORM_CHAR_LIMIT.get(platform, 250)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"  # ellipsis


def compose_post_via_llm(
    intel: dict,
    platform: SocialPlatform,
    *,
    stake_sentence: str = "",
) -> SocialPost:
    """Generate a SocialPost from prospect intel using the LLM budget gate.

    Falls back to a short per-platform template if the LLM budget denies the
    call or any error occurs. The returned post is ready to pass directly to
    :func:`send_post`. The operator-authored ``stake_sentence`` (G1) is carried
    through verbatim so the live path can enforce it.
    """
    summary = str(intel.get("summary") or intel.get("business_name") or "our service")

    # Try LLM composition
    try:
        from backend.common.llm_client import BudgetExceeded, LlmCallError, anthropic_messages

        # LM Studio backend needs no auth — always pass "unused".
        api_key = "unused"

        char_limit = _PLATFORM_CHAR_LIMIT.get(platform, 250)
        prompt = (
            f"Write a {platform.capitalize()} post for the following prospect intel."
            f" Keep it under {char_limit} characters. Be professional and concise.\n\n"
            f"Intel: {json.dumps(intel, default=str)}"
        )
        body_text, _usage = anthropic_messages(
            workcell="outreach",
            api_key=api_key,
            prompt=prompt,
            max_tokens=300,
        )
        body_text = _truncate_to_limit(body_text.strip(), platform)
        _LOG.debug("compose_post_via_llm llm success platform=%s", platform)

    except Exception as exc:  # noqa: BLE001 — covers BudgetExceeded + LlmCallError + anything else
        _LOG.info(
            "compose_post_via_llm llm unavailable (%s), falling back to template",
            type(exc).__name__,
        )
        template = (
            _LINKEDIN_TEMPLATE if platform == "linkedin" else _FACEBOOK_TEMPLATE
        )
        raw = template.format(summary=summary)
        body_text = _truncate_to_limit(raw, platform)

    return SocialPost(platform=platform, body=body_text, stake_sentence=stake_sentence)


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "SocialPlatform",
    "SocialPost",
    "SocialSendResult",
    "send_post",
    "moderate_post",
    "compose_post_via_llm",
]
