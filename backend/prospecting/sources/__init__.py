"""Legitimacy-signal collectors for G8.

Each module exposes a single ``lookup_*`` callable that returns a
``LegitimacySignal`` or ``None``. Collectors are deliberately small and
fail-OPEN on transient errors (network, missing files) — the upstream
aggregator treats "no signal" as cold-cold by design, so a flaky
collector must not falsify a warmth verdict.
"""

from __future__ import annotations

__all__: list[str] = []
