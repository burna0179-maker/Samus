"""Heat-band classification + send-posture decision logic.

Pure logic — no I/O. Maps a heat score in ``[0,1]`` to a qualitative band and
the band to a send multiplier + posture, exactly as ``entropy/controller.py``
maps instability to ordered countermeasures. The cadence loop multiplies its
per-sweep send budget by ``send_multiplier(band)``; ``critical`` trips safe-mode
(multiplier 0.0 → pause).
"""
from __future__ import annotations

# ── Band identifiers ─────────────────────────────────────────────────────────
BAND_COOL: str = "cool"
BAND_WARM: str = "warm"
BAND_HOT: str = "hot"
BAND_CRITICAL: str = "critical"

# ── Band thresholds (lower bound of each band, on the [0,1] heat score) ──────
# cool   : score <  WARM_THRESHOLD   — full send rate
# warm   : score >= WARM_THRESHOLD   — gentle throttle (early caution)
# hot    : score >= HOT_THRESHOLD    — hard throttle
# critical: score >= CRITICAL_THRESHOLD — pause (safe-mode); reputation triage
WARM_THRESHOLD: float = 0.25
HOT_THRESHOLD: float = 0.50
CRITICAL_THRESHOLD: float = 0.75

# ── Per-band send multipliers (applied to the cadence's per-sweep budget) ────
_SEND_MULTIPLIER: dict[str, float] = {
    BAND_COOL: 1.0,
    BAND_WARM: 0.75,
    BAND_HOT: 0.40,
    BAND_CRITICAL: 0.0,
}

# ── Per-band posture labels (for logs / the status surface) ──────────────────
_POSTURE: dict[str, str] = {
    BAND_COOL: "normal",
    BAND_WARM: "throttle_light",
    BAND_HOT: "throttle_hard",
    BAND_CRITICAL: "pause_safe_mode",
}


def band_for_score(score: float) -> str:
    """Classify a heat score into a band. Higher score = more reputation stress."""
    if score >= CRITICAL_THRESHOLD:
        return BAND_CRITICAL
    if score >= HOT_THRESHOLD:
        return BAND_HOT
    if score >= WARM_THRESHOLD:
        return BAND_WARM
    return BAND_COOL


def send_multiplier(band: str) -> float:
    """Cadence send-budget multiplier for a band. Unknown band -> conservative 0.4."""
    return _SEND_MULTIPLIER.get(band, 0.40)


def recommend_send_posture(band: str) -> str:
    """Human-readable posture label for a band."""
    return _POSTURE.get(band, "throttle_hard")


def is_send_paused(band: str) -> bool:
    """True iff the band trips safe-mode (no autonomous sends)."""
    return band == BAND_CRITICAL


__all__ = [
    "BAND_COOL",
    "BAND_WARM",
    "BAND_HOT",
    "BAND_CRITICAL",
    "WARM_THRESHOLD",
    "HOT_THRESHOLD",
    "CRITICAL_THRESHOLD",
    "band_for_score",
    "send_multiplier",
    "recommend_send_posture",
    "is_send_paused",
]
