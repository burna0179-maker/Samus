"""Daily USD budget gate for paid AI media generation (Gemini image / Veo video).

This is SEPARATE from the Anthropic $1/day LLM cap (``llm_global_budget``):
that meters text generation; media is Google-billed and far pricier per asset
(a single Veo clip can dwarf a day of text), so it gets its own ceiling.

Lean JSON-file store under the Samus state root, reset per UTC day. **Fail-closed:**
if the store cannot be read or the cap is non-positive, spending is denied — a
paid call never goes out on a "maybe". Mirrors the conservative posture of
``llm_global_budget`` / ``apollo_budget`` without their DDB coupling.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.media_budget")


@dataclass
class MediaSpendDecision:
    allowed: bool
    reason: str
    spent_today_usd: float
    cap_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "spent_today_usd": round(self.spent_today_usd, 4),
            "cap_usd": round(self.cap_usd, 4),
        }


class MediaBudgetStore:
    """Process-wide daily $-cap for paid media generation."""

    def __init__(self, cap_usd: float, *, path: Path | None = None, today: str | None = None) -> None:
        self.cap_usd = float(cap_usd)
        self._path = path or state_path("media", "budget.json")
        self._today_override = today  # tests inject a fixed UTC date

    def _today(self) -> str:
        if self._today_override:
            return self._today_override
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict[str, Any] | None:
        """Return the stored {date, spent_usd}, or None when it cannot be read.

        A missing file is a clean zero (returns a fresh dict); a corrupt/unreadable
        file returns None so callers can fail closed.
        """
        try:
            if not self._path.exists():
                return {"date": self._today(), "spent_usd": 0.0}
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return {"date": str(data.get("date", "")), "spent_usd": float(data.get("spent_usd", 0.0))}
        except (OSError, ValueError, TypeError) as exc:
            _LOG.error("media budget load failed (%s): %s", self._path, exc)
            return None

    def _spent_today(self, data: dict[str, Any]) -> float:
        return float(data["spent_usd"]) if data.get("date") == self._today() else 0.0

    def can_spend(self, estimated_usd: float) -> MediaSpendDecision:
        """Pre-spend gate. Fail-closed on non-positive cap or unreadable store."""
        if self.cap_usd <= 0:
            return MediaSpendDecision(False, "media_cap_zero", 0.0, self.cap_usd)
        data = self._load()
        if data is None:
            return MediaSpendDecision(False, "budget_store_unreadable", 0.0, self.cap_usd)
        spent = self._spent_today(data)
        if spent + max(0.0, float(estimated_usd)) > self.cap_usd:
            return MediaSpendDecision(
                False, f"daily_media_cap_exceeded ({spent:.2f}+{estimated_usd:.2f}>{self.cap_usd:.2f})",
                spent, self.cap_usd,
            )
        return MediaSpendDecision(True, "ok", spent, self.cap_usd)

    def record(self, actual_usd: float) -> None:
        """Best-effort post-spend accounting. Resets the tally on a new UTC day."""
        data = self._load() or {"date": self._today(), "spent_usd": 0.0}
        spent = self._spent_today(data)
        new = {"date": self._today(), "spent_usd": round(spent + max(0.0, float(actual_usd)), 6)}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            _LOG.error("media budget save failed (%s): %s", self._path, exc)


__all__ = ["MediaBudgetStore", "MediaSpendDecision"]
