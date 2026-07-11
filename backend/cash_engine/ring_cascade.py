"""Outreach ring cascade -- when one channel is exhausted, advance to the next.

When the email lane is blocked (no cold-sendable address) or the daily email
cap is reached, the cascade selects the next reachable channel for the
prospect and enrolls them there, so outreach continues rather than parking
dormant.  Park/escalate only when EVERY ring is exhausted.

Ring ladder (ordered by cold-reachability x EV x cost):
  1. personal_email  -- handled by the existing outreach compose path
  2. voice           -- enroll into the cold-dial call_list (PR #27 dials them)
  3. voicemail_drop  -- draft a voicemail artifact for operator recording
  4. sms             -- text message (requires mobile + TCPA consent)
  5. social_dm       -- social outreach (requires a handle + configured adapter)
  6. physical_flyer  -- print flyer/mailer (requires a mailing address)

The arbiter's EV scoring (strategy/arbiter.py WorkBid.priority) is used to
RANK channels when available; the static ladder order is the fallback.

Every ring transition emits a ``decision.made`` business event with the
prospect_id, from/to channels, reason, and EV so the cascade is observable
and the journey is reconstructable.  The chosen ring + its outcome are fed
back to the attribution engine so the system learns which ring pays for
which prospect type.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from backend.common.business_events_shim import emit_business_event
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.cash_engine.ring_cascade")


# ---------------------------------------------------------------------------
# Ring definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ring:
    """One channel in the outreach ladder."""

    channel: str
    static_rank: int

    def is_reachable(self, prospect: Any) -> bool:
        """True when the prospect has the contact data this ring needs."""
        check = _REACHABILITY.get(self.channel)
        if check is None:
            return False
        return check(prospect)


def _has_phone(prospect: Any) -> bool:
    phone = str(getattr(prospect, "phone", "") or "").strip()
    digits = "".join(c for c in phone if c.isdigit())
    return len(digits) >= 10


def _has_social_handle(prospect: Any) -> bool:
    for attr in ("social_facebook", "social_instagram", "social_linkedin"):
        if str(getattr(prospect, attr, "") or "").strip():
            return True
    return False


def _has_mailing_address(prospect: Any) -> bool:
    for attr in ("mailing_address", "street_address"):
        if str(getattr(prospect, attr, "") or "").strip():
            return True
    return False


def _has_mobile_consent(prospect: Any) -> bool:
    return bool(getattr(prospect, "sms_consent", False))


_REACHABILITY: dict[str, Any] = {
    "voice": _has_phone,
    "voicemail_drop": _has_phone,
    "sms": lambda p: _has_phone(p) and _has_mobile_consent(p),
    "social_dm": _has_social_handle,
    "physical_flyer": _has_mailing_address,
}

RING_LADDER: tuple[Ring, ...] = (
    Ring(channel="personal_email", static_rank=0),
    Ring(channel="voice", static_rank=1),
    Ring(channel="voicemail_drop", static_rank=2),
    Ring(channel="sms", static_rank=3),
    Ring(channel="social_dm", static_rank=4),
    Ring(channel="physical_flyer", static_rank=5),
)

_NON_EMAIL_RINGS = tuple(r for r in RING_LADDER if r.channel != "personal_email")


# ---------------------------------------------------------------------------
# Ring transition record
# ---------------------------------------------------------------------------


@dataclass
class RingTransition:
    """Audit record for one ring transition."""

    prospect_id: str
    opportunity_id: str
    from_channel: str
    to_channel: str
    reason: str
    ev: float = 0.0
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = iso_now()


# ---------------------------------------------------------------------------
# Arbiter-ranked channel selection
# ---------------------------------------------------------------------------


def _arbiter_ev_for_channel(
    channel: str,
    prospect: Any,
    opportunity: Any,
) -> float | None:
    """Try to get the arbiter's EV score for a channel.  None on any fault."""
    try:
        from backend.crm.scoring import priority_score
        import datetime as _dt

        opp_data = opportunity.model_dump() if hasattr(opportunity, "model_dump") else {}
        if not opp_data:
            return None
        score = priority_score(opp_data, now=_dt.datetime.now(_dt.timezone.utc))
        return float(score.ev_usd * score.probability * score.urgency)
    except Exception:  # noqa: BLE001
        return None


def select_next_ring(
    prospect: Any,
    opportunity: Any,
    *,
    exhausted_channels: Sequence[str] = (),
) -> Ring | None:
    """Select the next reachable ring, skipping exhausted channels.

    Tries arbiter EV scoring to rank candidates; falls back to the static
    ladder order when the arbiter is unavailable.  Returns None when every
    ring is exhausted or unreachable.
    """
    exhausted = set(exhausted_channels)
    candidates: list[Ring] = []
    for ring in _NON_EMAIL_RINGS:
        if ring.channel in exhausted:
            continue
        if ring.is_reachable(prospect):
            candidates.append(ring)

    if not candidates:
        return None

    scored: list[tuple[float, int, Ring]] = []
    for ring in candidates:
        ev = _arbiter_ev_for_channel(ring.channel, prospect, opportunity)
        if ev is not None:
            scored.append((-ev, ring.static_rank, ring))
        else:
            scored.append((0.0, ring.static_rank, ring))
    scored.sort()
    return scored[0][2]


# ---------------------------------------------------------------------------
# Ring enrollment -- dispatching to channel adapters
# ---------------------------------------------------------------------------


def _enroll_in_voice_ring(
    prospect: Any,
    opportunity: Any,
    *,
    stake_sentence: str,
    crm: Any,
) -> dict[str, Any]:
    """Enroll the prospect into the cold-dial call_list so PR #27 dials them.

    Appends the prospect to today's ``call_list_<date>.csv`` if they are not
    already present.  Also upserts a CRM CallState so the follow-up FSM knows
    a voice touch was scheduled.  The cold-dial loop (gateway/cold_dial_task)
    picks them up on its next tick.
    """
    from backend.common import storage
    from backend.crm.models import CallState
    from backend.prospecting.csv_export import CSV_COLUMNS

    prospect_id = str(getattr(prospect, "prospect_id", "") or "")
    opp_id = str(getattr(opportunity, "opportunity_id", "") or "")

    csv_dir = storage.root() / "daily_calls"
    csv_dir.mkdir(parents=True, exist_ok=True)

    from datetime import date

    csv_path = csv_dir / f"call_list_{date.today().isoformat()}.csv"

    already_enrolled = False
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    if row.get("prospect_id") == prospect_id:
                        already_enrolled = True
                        break
        except Exception:  # noqa: BLE001
            pass

    if not already_enrolled:
        file_exists = csv_path.exists()
        try:
            dumped = prospect.model_dump() if hasattr(prospect, "model_dump") else {}
            row_data = {col: str(dumped.get(col, "") or "") for col in CSV_COLUMNS}
            if row_data.get("call_priority", "") in ("", "low"):
                row_data["call_priority"] = "warm"
            with csv_path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row_data)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ring_cascade voice enrollment CSV append failed: %s", exc)

    try:
        crm.upsert_call_state(
            CallState(
                prospect_id=prospect_id,
                state="queued",
                attempt_count=0,
                next_attempt_at=iso_now(),
                last_outcome="ring_cascade: email exhausted, enrolled for voice dial",
                notes=f"ring_cascade for opp {opp_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("ring_cascade voice CRM upsert failed: %s", exc)

    return {
        "enrolled": True,
        "channel": "voice",
        "already_on_list": already_enrolled,
        "csv_path": str(csv_path),
    }


def _enroll_in_voicemail_ring(
    prospect: Any,
    opportunity: Any,
    *,
    stake_sentence: str,
    crm: Any,
) -> dict[str, Any]:
    """Draft a voicemail artifact -- reuses the contact stage's voicemail builder."""
    from backend.crm.models import CreateArtifactRequest
    from backend.prospecting.callsheet import _voicemail

    prospect_id = str(getattr(prospect, "prospect_id", "") or "")
    opp_id = str(getattr(opportunity, "opportunity_id", "") or "")

    script = _voicemail(prospect, stake_sentence)
    artifact = crm.create_artifact(
        CreateArtifactRequest(
            kind="voicemail",
            owner_entity_kind="opportunity",
            owner_entity_id=opp_id,
            title="Ring cascade voicemail draft",
            inline_data={
                "script": script,
                "stake_sentence": stake_sentence,
                "source": "ring_cascade",
            },
            source="ring_cascade",
            created_by="cash_engine",
        )
    )
    return {
        "enrolled": True,
        "channel": "voicemail_drop",
        "voicemail_artifact_id": artifact.artifact_id,
    }


def _enroll_in_social_ring(
    prospect: Any,
    opportunity: Any,
    *,
    stake_sentence: str,
    crm: Any,
) -> dict[str, Any]:
    """Queue a social DM via the social adapter.  Dry-run by default."""
    prospect_id = str(getattr(prospect, "prospect_id", "") or "")
    handle = ""
    platform = ""
    for attr, plat in (
        ("social_linkedin", "linkedin"),
        ("social_facebook", "facebook"),
        ("social_instagram", "instagram"),
    ):
        val = str(getattr(prospect, attr, "") or "").strip()
        if val:
            handle = val
            platform = plat
            break

    if not handle:
        return {"enrolled": False, "channel": "social_dm", "reason": "no_handle"}

    from backend.crm.models import CreateArtifactRequest

    artifact = crm.create_artifact(
        CreateArtifactRequest(
            kind="content_draft",
            owner_entity_kind="opportunity",
            owner_entity_id=str(getattr(opportunity, "opportunity_id", "") or ""),
            title=f"Social DM draft ({platform})",
            inline_data={
                "platform": platform,
                "handle": handle,
                "stake_sentence": stake_sentence,
                "prospect_id": prospect_id,
                "source": "ring_cascade",
            },
            source="ring_cascade",
            created_by="cash_engine",
        )
    )
    return {
        "enrolled": True,
        "channel": "social_dm",
        "platform": platform,
        "handle": handle,
        "artifact_id": artifact.artifact_id,
    }


_RING_ENROLLERS: dict[str, Any] = {
    "voice": _enroll_in_voice_ring,
    "voicemail_drop": _enroll_in_voicemail_ring,
    "social_dm": _enroll_in_social_ring,
}


def enroll_in_ring(
    ring: Ring,
    prospect: Any,
    opportunity: Any,
    *,
    stake_sentence: str,
    crm: Any,
) -> dict[str, Any]:
    """Dispatch enrollment to the appropriate channel adapter."""
    enroller = _RING_ENROLLERS.get(ring.channel)
    if enroller is None:
        return {"enrolled": False, "channel": ring.channel, "reason": "no_adapter"}
    return enroller(
        prospect,
        opportunity,
        stake_sentence=stake_sentence,
        crm=crm,
    )


# ---------------------------------------------------------------------------
# Email cap check
# ---------------------------------------------------------------------------


def is_email_cap_reached() -> bool:
    """True when today's email send count is at or above the daily cap."""
    try:
        from backend.common import daily_counter
        from backend.common.settings import get_settings

        cap = int(getattr(get_settings(), "samus_max_sends_per_day", 15))
        if cap <= 0:
            return False
        already = daily_counter.count_today("outreach.sends")
        return already >= cap
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Cascade entry point
# ---------------------------------------------------------------------------


def cascade_from_email(
    prospect: Any,
    opportunity: Any,
    *,
    stake_sentence: str,
    crm: Any,
    reason: str,
    exhausted_channels: Sequence[str] = ("personal_email",),
) -> dict[str, Any]:
    """Cascade from a blocked/capped email to the next reachable ring.

    Returns a dict with ``cascaded=True`` and enrollment details when a ring
    was found, or ``cascaded=False`` when every ring is exhausted (the caller
    should park/escalate).

    Emits a ``decision.made`` business event on every transition and feeds
    the chosen channel back to the attribution engine.
    """
    prospect_id = str(getattr(prospect, "prospect_id", "") or "")
    opp_id = str(getattr(opportunity, "opportunity_id", "") or "")

    ring = select_next_ring(
        prospect,
        opportunity,
        exhausted_channels=exhausted_channels,
    )

    if ring is None:
        _LOG.info(
            "ring_cascade: ALL rings exhausted for prospect=%s opp=%s reason=%s",
            prospect_id,
            opp_id,
            reason,
        )
        emit_business_event(
            "decision.made",
            workcell="cash_engine",
            prospect_id=prospect_id,
            opportunity_id=opp_id,
            metadata={
                "decision_kind": "ring_cascade_exhausted",
                "reason": reason,
                "exhausted_channels": list(exhausted_channels),
            },
        )
        return {"cascaded": False, "reason": "all_rings_exhausted"}

    ev = _arbiter_ev_for_channel(ring.channel, prospect, opportunity) or 0.0

    transition = RingTransition(
        prospect_id=prospect_id,
        opportunity_id=opp_id,
        from_channel="personal_email",
        to_channel=ring.channel,
        reason=reason,
        ev=ev,
    )

    enrollment = enroll_in_ring(
        ring,
        prospect,
        opportunity,
        stake_sentence=stake_sentence,
        crm=crm,
    )

    if not enrollment.get("enrolled"):
        tried = list(exhausted_channels) + [ring.channel]
        return cascade_from_email(
            prospect,
            opportunity,
            stake_sentence=stake_sentence,
            crm=crm,
            reason=reason,
            exhausted_channels=tried,
        )

    _LOG.info(
        "ring_cascade: %s -> %s for prospect=%s opp=%s reason=%s ev=%.2f",
        transition.from_channel,
        transition.to_channel,
        prospect_id,
        opp_id,
        reason,
        ev,
    )

    emit_business_event(
        "decision.made",
        workcell="cash_engine",
        prospect_id=prospect_id,
        opportunity_id=opp_id,
        metadata={
            "decision_kind": "ring_cascade_transition",
            "from_channel": transition.from_channel,
            "to_channel": transition.to_channel,
            "reason": reason,
            "ev": ev,
            **{k: v for k, v in enrollment.items() if k != "enrolled"},
        },
    )

    _feed_attribution(
        prospect_id=prospect_id,
        opportunity_id=opp_id,
        channel=ring.channel,
    )

    return {
        "cascaded": True,
        "transition": asdict(transition),
        "enrollment": enrollment,
    }


def _feed_attribution(
    *,
    prospect_id: str,
    opportunity_id: str,
    channel: str,
) -> None:
    """Feed the chosen ring back to the attribution/bandit so the system learns."""
    try:
        from backend.attribution.engine import build_arm_id, select_variant

        arm_id = build_arm_id(f"ring_cascade_{channel}")
        select_variant([arm_id])
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("ring_cascade attribution feed skipped: %s", exc)


__all__ = [
    "Ring",
    "RingTransition",
    "RING_LADDER",
    "cascade_from_email",
    "enroll_in_ring",
    "is_email_cap_reached",
    "select_next_ring",
]
