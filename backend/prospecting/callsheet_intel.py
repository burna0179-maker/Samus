"""Deterministic call-sheet intelligence — the operator-side analogue of the
Vapi sales agent's qualification heuristics.

The Vapi phone agent ("Morgan", ``recovery/vapi_sales_agent_config.md``)
qualifies a prospect live: it captures signals (manual ops, lead volume,
automation level, follow-up gaps), discovers pain, scores the lead into a
tier, and dynamically branches the pitch to the dominant pain. The human
operator working the morning call list needs the same heuristics — but
*before* the call, derived from the discovery + enrichment data Samus already
holds on each :class:`~backend.prospecting.models.ProspectRecord`.

This module is that bridge. :func:`derive_callsheet_intel` runs a pure,
zero-token, deterministic pass over a prospect's observed signals and returns
a :class:`CallsheetIntel` — the dominant business gap, a plain-language pain
hypothesis, the HustleForge offer that closes it, a prospect-specific pitch,
a secondary-gap pivot, and the Vapi-style qualifying questions to confirm the
hypothesis on the call. It carries no LLM cost, so it runs on every prospect
in the daily 07:30 list (unlike the Top-N-gated callsheet LLM path).

Observable gaps (each scored 0-100 from the prospect's real fields):

  * ``no_presence``    — website is down / parked / social-only / absent
                         (``website_status``). The loudest cold-call hook.
  * ``weak_visibility``— low ``seo_score``: buyers search, find competitors.
  * ``reputation``     — thin or low-rated reviews: no social proof to close.
  * ``trust_posture``  — a poor passive-security grade (``security_grade``).
  * ``manual_ops``     — the universal HustleForge angle (manual lead-gen +
                         follow-up). Scored from customer volume
                         (``review_count`` as a proxy: a busy business has
                         lead flow hand-run ops cannot keep up with), and
                         still the floor gap when nothing else scores.

Consumed by :mod:`backend.prospecting.callsheet` (fills the ``callsheet_*``
record fields) and rendered into the morning call list by
:mod:`backend.prospecting.text_export`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ProspectRecord

__all__ = ["CallsheetIntel", "derive_callsheet_intel"]


# Website-reachability states that mean the prospect has no working owned site
# — see crawler.WEBSITE_STATUSES + text_export._WEBSITE_ALERT. Any of these is
# the single strongest reason to call, so ``no_presence`` scores at the top.
# ``access_blocked`` is deliberately excluded: a WAF stopped our crawl but the
# site is healthy for a human visitor — pitching "you have no website" there
# would be wrong (the 2026-05-21 false-positive sweep).
_NO_PRESENCE_STATUSES: frozenset[str] = frozenset({
    "domain_unresolved", "unreachable_timeout", "unreachable",
    "server_error", "gone", "http_error", "empty", "parked",
    "social_only", "no_website",
})

# A real, generic fallback for "WHY WE CALLED" when no per-prospect signal was
# observed (cold prospect, enrichment + audit disabled). Mirrors the prior
# callsheet defaults so an un-enriched record still renders a usable block.
_GENERIC_ISSUES: tuple[str, ...] = (
    "Inconsistent name/address/phone across directories",
    "No city or neighborhood landing pages",
    "Mobile page speed below 50 — hurts local pack ranking",
)

# Below this score no observed gap is strong enough to anchor the pitch — fall
# back to ``manual_ops`` (the universal lead-gen/follow-up angle the Vapi agent
# leads with) and let the operator confirm pain live via the qualify prompts.
_PRIMARY_FLOOR = 30
# A secondary gap rides as a pitch pivot only if it clears this bar.
_SECONDARY_FLOOR = 40

# Tie-break order when two gaps score equal — loudest hook first. manual_ops
# sits second: it only *reaches* a top score for a genuinely high-volume
# business (see _score_manual_ops), and for one of those the automation angle
# outweighs a trust / visibility gap — so on a tie it should win the lead.
_GAP_PRIORITY: tuple[str, ...] = (
    "no_presence", "manual_ops", "trust_posture", "reputation", "weak_visibility",
)


@dataclass(frozen=True)
class CallsheetIntel:
    """The derived, prospect-specific qualification intel for one call sheet."""

    primary_gap: str
    secondary_gap: str
    gap_scores: dict[str, int]
    pain_summary: str
    offer: str
    pitch: str
    pivot: str
    issues: list[str]
    signal_tags: list[str] = field(default_factory=list)
    qualify_prompts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Field parsing — ProspectRecord stores review_rating / review_count as strings
# ---------------------------------------------------------------------------


def _rating(p: ProspectRecord) -> float | None:
    """Parse ``review_rating`` to a 0-5 float, or None when unset/unparseable."""
    raw = (p.review_rating or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, min(5.0, float(raw)))
    except ValueError:
        return None


def _review_count(p: ProspectRecord) -> int | None:
    """Parse ``review_count`` to a non-negative int, or None when unset."""
    raw = (p.review_count or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return None


def _clamp(value: float) -> int:
    return max(0, min(100, int(round(value))))


# ---------------------------------------------------------------------------
# Gap scoring — each returns 0-100; higher = bigger, more pitchable gap
# ---------------------------------------------------------------------------


def _score_no_presence(p: ProspectRecord) -> int:
    """Score the no-working-website gap from ``website_status``."""
    status = (p.website_status or "").strip().lower()
    if status in ("no_website", "parked", "social_only", "gone"):
        return 100  # there is no working owned site at all — the loudest hook
    if status in _NO_PRESENCE_STATUSES:
        return 85   # a site exists but is down/broken — they may not know
    return 0


def _score_weak_visibility(p: ProspectRecord) -> int:
    """Score the SEO/discoverability gap from ``seo_score`` (higher = better SEO).

    seo_score 0 means *unaudited*, not perfect — it carries no signal, so it
    scores 0 here rather than a spurious 100.
    """
    seo = int(p.seo_score or 0)
    if seo <= 0:
        return 0
    # A site at seo 70+ is fine; below that the gap widens linearly to ~85.
    return _clamp((70 - seo) * 1.2) if seo < 70 else 0


def _score_reputation(p: ProspectRecord) -> int:
    """Score the reputation/social-proof gap from review rating + volume."""
    rating = _rating(p)
    count = _review_count(p)
    if rating is None and count is None:
        return 0
    score = 0.0
    # Rating gap — 4.6★+ is clean, 3.0★ is a ~100 gap.
    if rating is not None:
        score += _clamp((4.6 - rating) / 1.6 * 100)
    # Thin-volume gap — under ~25 reviews there is little proof to close on.
    if count is not None and count < 25:
        score = max(score, 45.0 if count >= 5 else 70.0)
    return _clamp(score)


def _score_trust_posture(p: ProspectRecord) -> int:
    """Score the security/trust gap from the passive-audit ``security_grade``."""
    return {"F": 100, "D": 80, "C": 45, "B": 15, "A": 0}.get(
        (p.security_grade or "").strip().upper(), 0
    )


# Customer-volume bounds for the manual-ops score. ``review_count`` is the
# volume proxy: below the floor the business is too quiet to read manual-ops
# pain confidently (and a thin profile is a *reputation* gap instead — the
# floor matches the 25-review line in _score_reputation); at the ceiling the
# pain is unambiguous and the score saturates.
_MANUAL_OPS_REVIEW_FLOOR = 25
_MANUAL_OPS_REVIEW_CEILING = 250


def _score_manual_ops(p: ProspectRecord) -> int:
    """Score the lead-capture / follow-up automation gap from customer volume.

    manual_ops is the universal HustleForge angle and is not *directly*
    observable pre-call — but high customer volume is a strong proxy: a busy
    business has lead flow that hand-run capture + follow-up cannot keep up
    with. ``review_count`` is the volume signal Samus already holds: at or
    below ~25 reviews the business is too quiet to read the pain confidently
    (that thin profile is a reputation gap instead); from there the score
    ramps to 100 by ~250 reviews. An unset review_count scores 0 — manual_ops
    then survives only as the no-signal floor in :func:`derive_callsheet_intel`.
    """
    count = _review_count(p)
    if count is None or count <= _MANUAL_OPS_REVIEW_FLOOR:
        return 0
    span = _MANUAL_OPS_REVIEW_CEILING - _MANUAL_OPS_REVIEW_FLOOR
    return _clamp((count - _MANUAL_OPS_REVIEW_FLOOR) / span * 100)


# ---------------------------------------------------------------------------
# Per-gap copy — offer, pitch, pain, pivot, qualify prompts, signal tag
# ---------------------------------------------------------------------------


def _industry(p: ProspectRecord) -> str:
    return (p.industry or "local business").strip() or "local business"


def _where(p: ProspectRecord) -> str:
    return (p.city or "their market").strip() or "their market"


# The universal Vapi qualification questions (config Node 2/3) — every call
# sheet carries these so the operator captures the same signals the phone
# agent would. Gap-specific prompts are prepended to this set.
_BASE_QUALIFY: tuple[str, ...] = (
    "How are you bringing in new clients right now?",
    "Roughly how many leads come in a week — and what happens to the ones you can't get to?",
)


def _gap_copy(gap: str, p: ProspectRecord) -> dict[str, object]:
    """Return the offer / pitch / pain / pivot / prompts / tag for one gap."""
    company = p.company_name or "this business"
    industry = _industry(p)
    where = _where(p)
    seo = int(p.seo_score or 0)
    rating = _rating(p)
    count = _review_count(p)

    if gap == "no_presence":
        return {
            "tag": "no-web-presence",
            "pain": (
                "Customers searching for them can't find a working website — "
                "every one of those searches is landing on a competitor instead."
            ),
            # A no-website prospect has nothing to AUDIT — never offer the SEO
            # Audit here (it requires an existing site). The coherent offer is a
            # Business Website Build (or the 48-Hour Rescue for speed): get them a
            # working, lead-capturing presence live. Audit/SEO only makes sense
            # once a site exists (a natural follow-on, not the opener).
            "offer": "Business Website Build — a working, lead-capturing site live in days",
            "pitch": (
                f"{company} has no working site right now, so {industry} buyers in "
                f"{where} simply never reach them — we can build a presence that "
                "captures and qualifies those leads live within days."
            ),
            "prompts": [
                "When someone Googles your service, what do they find today?",
                "Where are your leads coming from while the site's down?",
            ],
        }
    if gap == "weak_visibility":
        return {
            "tag": "weak-local-seo",
            "pain": (
                "They rank below competitors in local search, so buyers ready to "
                "hire are finding someone else first."
            ),
            "offer": "SEO Audit — the prioritized fixes that move them up local search",
            "pitch": (
                f"{company}'s site scores {seo}/100 on local SEO — {industry} "
                f"buyers in {where} are finding competitors first; the audit shows "
                "exactly which 3-5 fixes recover that visibility."
            ),
            "prompts": [
                "When did you last check where you rank for your main service?",
                "How much of your new business comes from someone finding you online?",
            ],
        }
    if gap == "reputation":
        # The gap can be rating-driven (low stars) or volume-driven (few
        # reviews despite good stars) — the pitch must not call a 5.0★ profile
        # "below the bar". Pick the wording that matches the real signal.
        low_rating = rating is not None and rating < 4.0
        if low_rating:
            on_count = f" across {count} reviews" if count is not None else ""
            pitch = (
                f"{company} sits at {rating:.1f}★{on_count} — below the bar most "
                f"{industry} buyers screen for before they call; we build the "
                "review flow that lifts that rating without you chasing customers."
            )
        else:
            volume = f"only {count} reviews" if count is not None else "a thin review profile"
            stars = f" (even at {rating:.1f}★)" if rating is not None else ""
            pitch = (
                f"{company} has {volume}{stars} — too little social proof for "
                f"{industry} buyers comparing options; we build the review flow "
                "that turns happy customers into a steady stream."
            )
        return {
            "tag": "thin-reputation",
            "pain": (
                "Their review profile is too thin or low-rated to win the buyers "
                "who shop on reputation before they ever call."
            ),
            "offer": "Reputation + review-velocity workflow — a steady stream of new reviews on autopilot",
            "pitch": pitch,
            "prompts": [
                "Do you have a system for asking happy customers for a review?",
                "How often does a new review actually come in?",
            ],
        }
    if gap == "trust_posture":
        grade = (p.security_grade or "?").strip().upper()
        return {
            "tag": "broken-trust-signals",
            "pain": (
                "Visible security and trust gaps on their site quietly cost them "
                "the cautious buyers who notice."
            ),
            "offer": "Security & trust-posture fix from the free audit — close the gaps eroding buyer confidence",
            "pitch": (
                f"{company}'s site graded {grade} on our trust-posture check — the "
                "kind of gaps that make a careful buyer hesitate; we can close them "
                "and turn the audit into a trust signal that helps you close."
            ),
            "prompts": [
                "Has a customer ever mentioned a security warning on your site?",
                "Who handles your website's technical upkeep today?",
            ],
        }
    # manual_ops — the universal HustleForge / Vapi-core angle.
    return {
        "tag": "manual-ops",
        "pain": (
            "Lead capture, qualification and follow-up are likely run by hand — "
            "so leads slip and the owner's week disappears into busywork."
        ),
        "offer": "48-Hour Workflow Rescue — automate the lead capture + follow-up that's done by hand today",
        "pitch": (
            f"Most {industry} owners in {where} are still capturing and chasing "
            "leads manually — we automate that whole flow so nothing slips and "
            f"{company} books more of the demand it already has."
        ),
        "prompts": [
            "Are you doing outreach and follow-up manually or with a system?",
            "How much of your week goes to scheduling and chasing leads?",
        ],
    }


# ---------------------------------------------------------------------------
# Observed-issue derivation — the prospect-specific "WHY WE CALLED" lines
# ---------------------------------------------------------------------------


def _observed_issues(p: ProspectRecord, scores: dict[str, int]) -> list[str]:
    """Build the prospect-specific issue list from what discovery observed.

    Falls back to the generic SEO defaults only when nothing concrete was
    observed, so an un-enriched cold prospect still renders a usable block.
    """
    issues: list[str] = []
    status = (p.website_status or "").strip().lower()
    if status in ("no_website", "parked", "social_only", "gone"):
        issues.append("No working owned website — invisible to customers searching online")
    elif status in _NO_PRESENCE_STATUSES:
        issues.append("Website is down or broken — visitors hit an error, the owner may not know")
    seo = int(p.seo_score or 0)
    if scores["weak_visibility"] > 0:
        issues.append(f"Local SEO score {seo}/100 — ranking below competitors in local search")
    rating = _rating(p)
    count = _review_count(p)
    if scores["reputation"] > 0:
        if rating is not None and rating < 4.0:
            issues.append(
                f"Review rating {rating:.1f}★ — below the bar most buyers screen for"
            )
        if count is not None and count < 25:
            issues.append(
                f"Only {count} review{'s' if count != 1 else ''} — too little social proof to close on"
            )
    grade = (p.security_grade or "").strip().upper()
    if grade in ("C", "D", "F"):
        issues.append(f"Security & trust-posture grade {grade} — visible gaps that erode buyer confidence")
    if scores["manual_ops"] >= _PRIMARY_FLOOR:
        vol = f"~{count} reviews" if count is not None else "steady demand"
        issues.append(
            f"High customer volume ({vol}) — lead capture and follow-up are "
            "hard to keep up with by hand at that scale"
        )
    return issues or list(_GENERIC_ISSUES)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def derive_callsheet_intel(p: ProspectRecord) -> CallsheetIntel:
    """Run the deterministic Vapi-style qualification pass over one prospect.

    Pure + zero-token: maps the prospect's observed discovery/enrichment
    signals to a dominant business gap and the HustleForge offer that closes
    it. Tolerant of a wholly-empty record — an un-enriched prospect resolves
    to the ``manual_ops`` angle so the block is never blank.
    """
    scores: dict[str, int] = {
        "no_presence": _score_no_presence(p),
        "weak_visibility": _score_weak_visibility(p),
        "reputation": _score_reputation(p),
        "trust_posture": _score_trust_posture(p),
        "manual_ops": _score_manual_ops(p),
    }

    # Primary gap — highest score, tie-broken by loudest-hook priority. All
    # five gaps compete now that manual_ops is volume-scored; when nothing
    # clears the floor, manual_ops still anchors the pitch.
    ranked = sorted(
        scores,
        key=lambda g: (-scores[g], _GAP_PRIORITY.index(g)),
    )
    top = ranked[0]
    primary = top if scores[top] >= _PRIMARY_FLOOR else "manual_ops"

    # Secondary gap — the next distinct gap clearing the pivot bar. manual_ops
    # is the pivot of last resort when a single gap dominates.
    secondary = ""
    for g in ranked:
        if g != primary and scores[g] >= _SECONDARY_FLOOR:
            secondary = g
            break
    if not secondary and primary != "manual_ops":
        secondary = "manual_ops"

    primary_copy = _gap_copy(primary, p)
    pivot = ""
    if secondary:
        secondary_copy = _gap_copy(secondary, p)
        pivot = str(secondary_copy["pitch"])

    # Vapi-style qualifying prompts — the gap-specific questions first, then
    # the universal lead-gen/follow-up pair the phone agent always asks.
    qualify: list[str] = list(primary_copy["prompts"]) + list(_BASE_QUALIFY)

    # Signal tags — the compact, Vapi-flavoured signal list (Node 4 ``signals``).
    tags = [str(primary_copy["tag"])]
    if secondary:
        tags.append(str(_gap_copy(secondary, p)["tag"]))

    return CallsheetIntel(
        primary_gap=primary,
        secondary_gap=secondary,
        gap_scores=scores,
        pain_summary=str(primary_copy["pain"]),
        offer=str(primary_copy["offer"]),
        pitch=str(primary_copy["pitch"]),
        pivot=pivot,
        issues=_observed_issues(p, scores),
        signal_tags=tags,
        qualify_prompts=qualify,
    )
