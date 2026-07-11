"""Single resolver for the writable governance/observability state root.

The container image root (``/opt/samus``) is mounted **read-only**; only the
``samus-data`` volume at ``/opt/samus/data`` is writable (compose sets
``read_only: true`` + ``tmpfs: /tmp`` on every workcell). The v0.5/v0.6
governance + observability layer persists durable state — value-exchange
records, EFH vetoes, the dual-channel audit queue, the ISV active/inbox
spool, PDC findings, template-usage counters, confusion events — and that
state historically anchored under ``<code root>/state`` (``/opt/samus/state``
in the container), which is on the read-only root. Every such write
``OSError``'d (``[Errno 30] Read-only file system``), silently losing the
audit trail and failing the commercial-commit path at the ValueExchangeRecord
write.

This module routes those writes through one resolver:

  * In the container, compose sets ``SAMUS_STATE_ROOT=/opt/samus/data/state``
    so durable governance state lands on the writable, persistent volume.
  * On the host (tests, host-venv prospecting/morning runs) the env var is
    unset and this falls back to ``<code root>/state`` — exactly the prior
    behaviour, which is writable there.

Mirrors the existing ``SAMUS_DATA_ROOT`` / ``SAMUS_ARTIFACT_ROOT`` /
``SAMUS_DLQ_ROOT`` env-driven path conventions in ``backend/common``.
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/common/state_paths.py -> parents[2] == the Samus code root
# (``/opt/samus`` in the container image, the worktree ``Samus/`` on the host).
_CODE_ROOT = Path(__file__).resolve().parents[2]

ENV_STATE_ROOT = "SAMUS_STATE_ROOT"


def state_root() -> Path:
    """Resolve the writable root for governance/observability state.

    Honors ``SAMUS_STATE_ROOT`` (set to the data volume in compose); falls
    back to ``<code root>/state`` so host + test behaviour is unchanged.
    """
    override = os.getenv(ENV_STATE_ROOT, "").strip()
    if override:
        return Path(override)
    return _CODE_ROOT / "state"


def state_path(*parts: str) -> Path:
    """Join ``parts`` under the resolved state root.

    e.g. ``state_path("value_exchanges")`` ->
    ``/opt/samus/data/state/value_exchanges`` in the container.
    """
    return state_root().joinpath(*parts)
