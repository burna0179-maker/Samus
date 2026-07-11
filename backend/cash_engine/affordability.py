"""Affordability layer — pace marketing by what the company can actually afford
RIGHT NOW, using the SAME headroom + safety reserve the CODB investment reasoner
(backend/cognitive/codb_reasoner.py) already enforces. This is the missing half
of "take the financial situation into account": behind_pace answers *how far
behind on revenue are we* (argues for MORE push); affordability answers *what can
we responsibly spend on that push* (caps it). The two multiply — the engine
pushes as hard as it can WITHIN its means, never breaching the cash reserve.

Postures, from the reasoner's ``headroom_usd`` (spendable AFTER the safety
reserve):
  - conserve (headroom <= 0, at/below reserve): FREE channels only (voicemail
    drafts). No discretionary marketing spend — the reserve is protected exactly
    as the reasoner protects it for CODB upgrades. Still generates call
    opportunities for Alex, at zero marginal cost.
  - lean (0 < headroom < invest floor): free + low-cost channels (email incl.
    LLM personalization), moderate volume. No paid/live-call spend.
  - invest (headroom >= invest floor): everything, full volume.
  - unavailable financials => cautious 'lean' default, never a blind full push.

Consuming the reasoner's own headroom keeps the two systems CONSISTENT: the
pacer can no longer say "push max" while the reasoner says "cash is tight."
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

_LOG = logging.getLogger("samus.cash_engine.affordability")

# Headroom at/above which we consider the company able to fund the full push
# (incl. paid/live channels). Env-tunable, same posture-tuning spirit as the
# reasoner's SAMUS_CODB_SAFETY_RESERVE_PCT.
_INVEST_MIN_HEADROOM = float(os.getenv("SAMUS_PACE_INVEST_MIN_HEADROOM_USD", "300"))

_TIERS = {
    "conserve": frozenset({"free"}),
    "lean": frozenset({"free", "low"}),
    "invest": frozenset({"free", "low", "paid"}),
}
_FACTOR = {"conserve": 0.3, "lean": 0.6, "invest": 1.0}


@dataclass
class Affordability:
    """What the company can responsibly spend on marketing this tick.

    ``allowed_tiers`` gates campaigns by cost tier; ``intensity_factor`` scales
    per-campaign volume + the campaign count; ``marketing_budget_usd`` is the
    hard per-tick spend ceiling (= headroom, which already excludes the reserve).
    """

    posture: str
    allowed_tiers: frozenset
    intensity_factor: float
    marketing_budget_usd: float
    headroom_usd: float
    available_cash_usd: float
    source: str

    def to_dict(self) -> dict:
        return {
            "posture": self.posture,
            "allowed_tiers": sorted(self.allowed_tiers),
            "intensity_factor": self.intensity_factor,
            "marketing_budget_usd": round(self.marketing_budget_usd, 2),
            "headroom_usd": round(self.headroom_usd, 2),
            "available_cash_usd": round(self.available_cash_usd, 2),
            "source": self.source,
        }


def derive_affordability(
    *,
    headroom_usd: float,
    available_cash_usd: float,
    source: str = "",
    invest_min: Optional[float] = None,
) -> Affordability:
    """Pure: map the reasoner's headroom to a marketing-spend posture."""
    floor = _INVEST_MIN_HEADROOM if invest_min is None else invest_min
    h = float(headroom_usd or 0.0)
    if h <= 0:
        posture = "conserve"
    elif h < floor:
        posture = "lean"
    else:
        posture = "invest"
    return Affordability(
        posture=posture,
        allowed_tiers=_TIERS[posture],
        intensity_factor=_FACTOR[posture],
        marketing_budget_usd=max(0.0, h),
        headroom_usd=h,
        available_cash_usd=float(available_cash_usd or 0.0),
        source=source or "codb_reasoner",
    )


def _cautious_unknown() -> Affordability:
    """When financials can't be read: a cautious LEAN posture (cheap channels,
    moderate volume, no paid spend) — never a blind full push."""
    return Affordability(
        posture="lean",
        allowed_tiers=_TIERS["lean"],
        intensity_factor=0.5,
        marketing_budget_usd=0.0,
        headroom_usd=0.0,
        available_cash_usd=0.0,
        source="unavailable-cautious",
    )


def _default_financials_reader():
    """Reuse the CODB reasoner's own financials surface (finance /runway +
    /snapshot, with its fallbacks) so pacing sees the SAME numbers the reasoner
    ranks CODB actions with."""
    from backend.cognitive.codb_reasoner import read_financials

    return read_financials()


def assess_affordability(
    *,
    financials_reader: Optional[Callable[[], object]] = None,
) -> Affordability:
    """Read financials + derive the posture. Defensive: any failure => cautious
    lean, so a finance outage never becomes a blind full-capacity push."""
    reader = financials_reader or _default_financials_reader
    try:
        fin = reader()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("affordability: financials unavailable (%s) -> cautious lean", exc)
        return _cautious_unknown()
    if fin is None:
        return _cautious_unknown()
    return derive_affordability(
        headroom_usd=getattr(fin, "headroom_usd", 0.0),
        available_cash_usd=getattr(fin, "available_cash_usd", 0.0),
        source=getattr(fin, "source", "codb_reasoner"),
    )
