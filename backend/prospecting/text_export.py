"""Human-readable morning call list export.

Mirrors the prior-iteration text report a sales rep would print and work
through call-by-call:

    MORNING CALL LIST — Thursday, May 07, 2026
    75 prospects ready | Sorted by priority + score
    ============================================================

    🔴 #1  Acme Corp
       📞  (555) 123-4567
       📍  Yuba City, CA  |  finance
       🌐  https://acme.example/
       📊  Lead: 78/100  |  SEO: 32/100

       WHY WE'RE CALLING (what we found):
       ⚠  ...
       ⚠  ...

       HOW WE CAN HELP:
       🧭  Likely pain: ...
       💼  Offer:  ...
       🎯  Pitch:  ...
       ↗   If that misses, pivot:  ...

       WHAT TO LISTEN FOR (qualify — one question at a time):
       ❔  ...
       ❔  ...

       CALL SCRIPT:
       OPENER:
       "..."

       VOICEMAIL:
       "..."

       OBJECTION HANDLERS:
       • ...
       • ...

    ------------------------------------------------------------

Output: ``<artifact_root>/daily_calls/morning_call_list_YYYY-MM-DD.txt``

Sort key (per the prior production behavior):
  1. priority bucket: hot → warm → low (call the qualified leads first)
  2. lead_score DESCENDING (highest qualified first)
  3. seo_score ASCENDING (worst SEO first — most pitch material)

Lines are CRLF on disk; readers on Windows/Linux/macOS all handle that.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

from backend.common import storage

from .callsheet_intel import derive_callsheet_intel
from .models import ProspectRecord as Prospect


_PRIORITY_RANK: dict[str, int] = {"hot": 0, "warm": 1, "low": 2}
_PRIORITY_DOT: dict[str, str] = {"hot": "🔴", "warm": "🟡", "low": "🟢"}
_DEFAULT_DOT = "⚪"

# Security grade as a call-list tie-breaker — a worse grade ranks a prospect
# higher (more trust problems to pitch a fix for). Ungraded prospects (cold
# leads with no Step 2.7 audit) sort last within a lead-score tie.
_GRADE_RANK: dict[str, int] = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}
_GRADE_RANK_DEFAULT = 9

# A non-live website is itself the strongest cold-call hook, so it gets a loud
# line right under the URL. "live" and "" (unchecked) produce no alert. Keyed
# on ProspectRecord.website_status — see crawler.WEBSITE_STATUSES.
_WEBSITE_ALERT: dict[str, str] = {
    "domain_unresolved": "WEBSITE DOWN — the domain doesn't resolve. They may not know — lead with this.",
    "unreachable_timeout": "WEBSITE UNREACHABLE — the site timed out (down, or painfully slow).",
    "unreachable": "WEBSITE UNREACHABLE — couldn't connect to the site.",
    "server_error": "WEBSITE BROKEN — the server is returning errors (5xx).",
    "gone": "WEBSITE GONE — the homepage returns HTTP 410, permanently removed.",
    "http_error": "WEBSITE BROKEN — the homepage returns an error page.",
    "empty": "WEBSITE BLANK — the page loads but has no content.",
    "parked": "NO REAL WEBSITE — the domain is parked / listed for sale.",
    "social_only": "NO REAL WEBSITE — only a social-media page, no owned site.",
    "no_website": "NO WEBSITE — this business has no site at all.",
}

# access_blocked is deliberately NOT in _WEBSITE_ALERT: the site is healthy for
# a human visitor, only a WAF stopped our crawl — so it gets a quiet
# informational note, never a "your site is down" cold-call hook.
_ACCESS_BLOCKED_NOTE = (
    "Site blocks automated audits (loads fine in a browser) — "
    "our SEO score is unavailable; don't pitch this as a broken site."
)

_SEP_RULE = "=" * 60
_BLOCK_RULE = "-" * 60

# Outcomes that mark a prospect as warm — operator already engaged, so they
# don't belong on tomorrow's cold morning_call_list. Active buying_signal
# enrollments are checked separately (see _excluded_warm_prospects).
_WARM_OUTCOMES: frozenset[str] = frozenset(
    {
        "booked",
        "interested",
        "interested_warm",
        "closed_won",
        "quote_pending",
        "warm_call_held",
        "negotiating",
    }
)


def _active_warm_prospect_ids() -> set[str]:
    """Prospect IDs currently in an active warm buying_signal enrollment.

    Best-effort: any import / read fault returns an empty set so the
    morning list still renders. Soft-imported so the prospecting package
    keeps no hard dependency on the outreach package's runtime state.
    """
    try:
        from backend.outreach.buying_signal_route import (  # noqa: PLC0415
            active_warm_prospect_ids,
        )

        return active_warm_prospect_ids()
    except Exception:  # noqa: BLE001
        return set()


def _excluded_warm_prospects(
    prospects: Iterable[Prospect],
) -> tuple[list[Prospect], list[tuple[Prospect, str]]]:
    """Split a prospect iterable into (cold, warm).

    A prospect is warm when either:
      * its ``prospect_id`` has an active buying_signal enrollment, OR
      * its ``last_contact_outcome`` is in :data:`_WARM_OUTCOMES`.

    Returns ``(cold_list, warm_list_with_reason)``. The reason string is
    rendered in the EXCLUDED footer so the operator can see why a prospect
    was held back rather than wondering where they went.
    """
    warm_ids = _active_warm_prospect_ids()
    cold: list[Prospect] = []
    warm: list[tuple[Prospect, str]] = []
    for p in prospects:
        pid = (getattr(p, "prospect_id", "") or "").strip()
        outcome = (getattr(p, "last_contact_outcome", "") or "").strip().lower()
        if pid and pid in warm_ids:
            warm.append((p, "buying_signal enrollment active"))
        elif outcome in _WARM_OUTCOMES:
            warm.append((p, f"outcome={outcome}"))
        else:
            cold.append(p)
    return cold, warm


def _render_excluded_footer(warm: list[tuple[Prospect, str]]) -> list[str]:
    """Render the EXCLUDED block — warm prospects held back from cold-call sort."""
    if not warm:
        return []
    out: list[str] = []
    out.append("")
    out.append(_SEP_RULE)
    out.append(
        f"EXCLUDED FROM COLD LIST — {len(warm)} warm prospect(s) "
        "(handled via outreach follow-ups, not cold dialling)"
    )
    out.append(_SEP_RULE)
    for p, reason in warm:
        name = p.company_name or "(unnamed)"
        phone = p.phone or "—"
        out.append(f"  • {name}  {phone}  — {reason}")
    out.append("")
    return out


def _safe_int(value: str | int, *, default: int) -> int:
    if isinstance(value, int):
        return value
    text = (value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


_RECENT_CONTACT_DAYS = 7  # operator-worked prospects sink to bottom this many days


def _days_since_contact(p: Prospect) -> int | None:
    """Return whole days since the prospect's last_contact_at, or None.

    Empty / unparseable timestamps return None — caller treats as 'never
    contacted' (does not influence the sort).
    """
    raw = (getattr(p, "last_contact_at", "") or "").strip()
    if not raw:
        return None
    from datetime import datetime, timezone  # noqa: PLC0415 — local import

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            ts = datetime.strptime(raw, fmt)
            break
        except ValueError:
            ts = None
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    return max(0, delta.days)


def _sort_key(p: Prospect) -> tuple[int, int, int, int, int]:
    """Sequencing for the morning call list.

    Outer rank: prospects the operator already worked in the last
    ``_RECENT_CONTACT_DAYS`` days sink to the bottom (so today's effort isn't
    spent re-dialling yesterday's calls). Within each cohort the original
    priority+score+grade+seo ordering still applies.
    """
    priority = (p.call_priority or "low").lower()
    days_since = _days_since_contact(p)
    recent_contact = 0 if days_since is None or days_since >= _RECENT_CONTACT_DAYS else 1
    return (
        recent_contact,
        _PRIORITY_RANK.get(priority, 99),
        -_safe_int(p.lead_score, default=0),
        _GRADE_RANK.get((p.security_grade or "").upper(), _GRADE_RANK_DEFAULT),
        _safe_int(p.seo_score, default=100),
    )


def _split_joined(value: str, sep: str) -> list[str]:
    """Split a string that's joined with `sep`; drop empties; preserve order."""
    if not value:
        return []
    return [piece.strip() for piece in value.split(sep) if piece.strip()]


def _format_block(index: int, p: Prospect) -> str:
    """Render one prospect's block matching the prior production format."""
    dot = _PRIORITY_DOT.get((p.call_priority or "").lower(), _DEFAULT_DOT)
    location_industry = f"{p.city or '—'}, {p.state or '—'}  |  {p.industry or '—'}"
    lead = _safe_int(p.lead_score, default=0)
    seo = _safe_int(p.seo_score, default=0)

    issues = _split_joined(p.callsheet_issues, "; ")
    objections = _split_joined(p.callsheet_objections, " | ")

    lines: list[str] = []
    lines.append(f"{dot} #{index}  {p.company_name or '(unnamed)'}")
    lines.append(f"   📞  {p.phone or '—'}")
    lines.append(f"   📍  {location_industry}")
    lines.append(f"   🌐  {p.website_url or '—'}")
    website_alert = _WEBSITE_ALERT.get(p.website_status or "")
    if website_alert:
        lines.append(f"   🚨  {website_alert}")
    elif (p.website_status or "") == "access_blocked":
        lines.append(f"   ℹ  {_ACCESS_BLOCKED_NOTE}")
    score_line = f"   📊  Lead: {lead}/100  |  SEO: {seo}/100"
    if p.security_grade:
        score_line += f"  |  Security: {p.security_grade}"
    lines.append(score_line)
    if p.seo_report_path:
        lines.append(f"   📄  Full audit: {p.seo_report_path}")
    # Operator-worked marker — populated post-finalize from the CRM's
    # CallState (service.py). Surfaces here so the operator can see why a
    # prospect sank in priority and what the prior outcome was.
    days_since = _days_since_contact(p)
    if days_since is not None:
        last_outcome = (getattr(p, "last_contact_outcome", "") or "").strip() or "—"
        ago = "today" if days_since == 0 else f"{days_since}d ago"
        lines.append(f"   📓  Already contacted {ago} — last outcome: {last_outcome}")
    lines.append("")

    # WHO YOU'RE CALLING — owner + business intel from the website
    # investigation (enrichment cascade + Google Places). Only lines with
    # discovered data are shown; an un-enriched prospect simply omits them.
    intel: list[str] = []
    if p.owner_name:
        owner_line = f"   👤  {p.owner_name}"
        if p.owner_title:
            owner_line += f", {p.owner_title}"
        intel.append(owner_line)
    if p.owner_email:
        intel.append(f"   ✉   {p.owner_email}")
    if p.review_rating:
        rev = f"   ⭐  {p.review_rating}"
        if p.review_count:
            rev += f"  ({p.review_count} reviews)"
        intel.append(rev)
    if p.business_description:
        intel.append(f"   🏢  {p.business_description}")
    socials: list[str] = []
    if p.social_facebook:
        socials.append(f"FB: {p.social_facebook}")
    if p.social_instagram:
        socials.append(f"IG: {p.social_instagram}")
    if p.owner_linkedin_url:
        socials.append(f"LinkedIn: {p.owner_linkedin_url}")
    if socials:
        intel.append(f"   📱  {'   '.join(socials)}")
    if intel:
        lines.append("   WHO YOU'RE CALLING:")
        lines.extend(intel)
        lines.append("")

    lines.append("   WHY WE'RE CALLING (what we found):")
    if issues:
        for issue in issues:
            lines.append(f"   ⚠  {issue}")
    else:
        lines.append("   ⚠  (no audit issues recorded)")
    lines.append("")

    # HOW WE CAN HELP — derived per prospect from observed discovery signals
    # via the same qualification heuristics the Vapi phone agent uses
    # (callsheet_intel). The offer + pitch come off the record's callsheet
    # fields (which an LLM run may have personalized); the pain hypothesis,
    # pivot, and qualifying questions are derived fresh at render time.
    intel = derive_callsheet_intel(p)
    lines.append("   HOW WE CAN HELP:")
    lines.append(f"   🧭  Likely pain: {intel.pain_summary}")
    lines.append(f"   💼  Offer:  {p.callsheet_offer or intel.offer or '—'}")
    lines.append(f"   🎯  Pitch:  {p.callsheet_pitch or intel.pitch or '—'}")
    if intel.pivot:
        lines.append(f"   ↗   If that misses, pivot:  {intel.pivot}")
    lines.append("")

    # WHAT TO LISTEN FOR — the Vapi agent's qualify/pain-discovery questions
    # (config Nodes 2-3), pre-selected for this prospect's dominant gap so the
    # operator captures the same signals the phone agent would.
    lines.append("   WHAT TO LISTEN FOR (qualify — one question at a time):")
    for prompt in intel.qualify_prompts:
        lines.append(f"   ❔  {prompt}")
    lines.append("")

    lines.append("   CALL SCRIPT:")
    lines.append("   OPENER:")
    lines.append(f'   "{p.callsheet_opener or ""}"')
    lines.append("")
    lines.append("   VOICEMAIL:")
    lines.append(f'   "{p.callsheet_voicemail or ""}"')
    lines.append("")
    lines.append("   OBJECTION HANDLERS:")
    if objections:
        for objection in objections:
            lines.append(f"   • {objection}")
    else:
        lines.append("   • (none recorded)")
    return "\n".join(lines)


def render_morning_call_list(
    prospects: Iterable[Prospect],
    *,
    run_date: date | None = None,
) -> str:
    """Render the full call-list text as a single string."""
    run_date = run_date or date.today()
    cold_prospects, warm_prospects = _excluded_warm_prospects(prospects)
    sorted_prospects = sorted(cold_prospects, key=_sort_key)
    total = len(sorted_prospects)
    header_date = run_date.strftime("%A, %B %d, %Y")

    out: list[str] = []
    out.append(f"MORNING CALL LIST — {header_date}")
    out.append(f"{total} prospects ready | Sorted by priority + score")
    out.append(_SEP_RULE)
    out.append("")
    for index, prospect in enumerate(sorted_prospects, start=1):
        out.append(_format_block(index, prospect))
        out.append("")
        out.append(_BLOCK_RULE)
        out.append("")
    out.extend(_render_excluded_footer(warm_prospects))
    return "\n".join(out)


def write_morning_call_list(
    prospects: Iterable[Prospect],
    *,
    run_date: date | None = None,
) -> Path:
    """Write the rendered call list to ``<artifact_root>/daily_calls/``."""
    run_date = run_date or date.today()
    body = render_morning_call_list(prospects, run_date=run_date)
    target_dir = storage.root() / "daily_calls"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"morning_call_list_{run_date.isoformat()}.txt"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path
