"""Info-gap registry loader + open-gap counting.

``info_gaps.yaml`` is operator-curated PII (gitignored). Open gaps are
counted by priority; critical-priority gaps are always surfaced as a
separate list so they appear in /snapshot without being hidden in counts.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

import yaml

from .models import (
    InfoGap,
    InfoGapsRegistry,
    InfoGapsSummary,
)


_LOG = logging.getLogger("samus.finance.info_gaps")
_DEFAULT_INFO_GAPS_PATH = Path(__file__).resolve().parent / "info_gaps.yaml"

_PRIORITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def info_gaps_path():
    override = os.getenv("SAMUS_INFO_GAPS_PATH")
    return Path(override) if override else _DEFAULT_INFO_GAPS_PATH


def load_registry() -> tuple[InfoGapsRegistry, bool]:
    path = info_gaps_path()
    if not path.exists():
        _LOG.info("info_gaps.yaml not present at %s -- returning empty registry", path)
        return InfoGapsRegistry(), False
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return InfoGapsRegistry.model_validate(raw), True


def _open_gaps(gaps: list[InfoGap]) -> list[InfoGap]:
    return [g for g in gaps if g.status == "open"]


def summarize(registry: InfoGapsRegistry, registry_loaded: bool,
              ts: str) -> InfoGapsSummary:
    open_gaps = _open_gaps(registry.gaps)
    by_pri: Counter = Counter(g.priority for g in open_gaps)
    critical = sorted(
        [g for g in open_gaps if g.priority == "critical"],
        key=lambda g: (_PRIORITY_RANK.get(g.priority, 99), g.id),
    )
    return InfoGapsSummary(
        open_total=len(open_gaps),
        open_by_priority=dict(by_pri),
        critical_open=critical,
        ts=ts,
        registry_loaded=registry_loaded,
    )
