"""Action calendar loader + bucket math.

``actions.yaml`` is operator-curated PII (gitignored). Open actions are
bucketed against a pinned ``today``:

  - overdue       : due_date < today AND status == open
  - due_today     : due_date == today AND status == open
  - due_this_week : today < due_date <= today + week_window_days AND status == open

Items with status != open are excluded from all buckets.
"""
from __future__ import annotations

import logging
import os
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

import yaml

from .models import (
    ActionItem,
    ActionsRegistry,
    ActionsSummary,
)


_LOG = logging.getLogger("samus.finance.actions")
_DEFAULT_ACTIONS_PATH = Path(__file__).resolve().parent / "actions.yaml"


def actions_path() -> Path:
    override = os.getenv("SAMUS_ACTIONS_PATH")
    return Path(override) if override else _DEFAULT_ACTIONS_PATH


def load_registry() -> tuple[ActionsRegistry, bool]:
    path = actions_path()
    if not path.exists():
        _LOG.info("actions.yaml not present at %s -- returning empty registry", path)
        return ActionsRegistry(), False
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return ActionsRegistry.model_validate(raw), True


def _by_due(items: list[ActionItem]) -> list[ActionItem]:
    """Sort by due_date ascending."""
    return sorted(items, key=lambda a: a.due_date)


def summarize(registry: ActionsRegistry, registry_loaded: bool, *,
              ts: str, today: _date | None = None,
              week_window_days: int = 7) -> ActionsSummary:
    """Bucket open actions relative to ``today``."""
    anchor = today or _date.today()
    week_end = anchor + timedelta(days=week_window_days)

    open_items = [a for a in registry.actions if a.status == "open"]
    overdue = [a for a in open_items if a.due_date < anchor]
    due_today = [a for a in open_items if a.due_date == anchor]
    due_week = [a for a in open_items if anchor < a.due_date <= week_end]

    return ActionsSummary(
        open_total=len(open_items),
        overdue_count=len(overdue),
        due_today_count=len(due_today),
        due_this_week_count=len(due_week),
        overdue_actions=_by_due(overdue),
        due_today_actions=_by_due(due_today),
        due_this_week_actions=_by_due(due_week),
        ts=ts,
        registry_loaded=registry_loaded,
    )
