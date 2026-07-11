"""Walk-through driver for the website-build capability.

The supervised walk-through is "show -> approve -> advance" per stage. This CLI
is the per-step command surface, one verb per action:

    python -m backend.website.cli start   --order path/to/order.json
    python -m backend.website.cli status  --order-id wb-xxxx
    python -m backend.website.cli approve --order-id wb-xxxx --stage provision
    python -m backend.website.cli advance --order-id wb-xxxx
    python -m backend.website.cli run     --order-id wb-xxxx        # autonomous walk

It reads the Wix credentials + flags from the environment via ``get_settings()``
— the ``Run-WebsiteBuild.ps1`` wrapper loads them from the DPAPI secret store
into the subprocess env, so no key is ever printed here. The CLI only ever
prints the durable build STATE (which contains no secrets).

``start`` takes a JSON file matching :class:`backend.website.models.WebsiteOrder`
(see ``scripts/website_orders/_template.json``). The state is keyed by
``order_id`` under ``SAMUS_STATE_ROOT`` so every later verb resumes it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from backend.website import service
from backend.website.models import WebsiteOrder
from backend.website.state import load_state

_LOG = logging.getLogger("samus.website.cli")


def _ensure_codex() -> None:
    """Load the Codex registry in this standalone process.

    The registry is a per-PROCESS singleton loaded only by the FastAPI lifespan
    (and the test conftest). A bare CLI process has it unloaded, so every
    stage's Codex gate would fail-closed to CODEX_UNAVAILABLE and escalate. Load
    it here (same fix as backend/prospecting/run_daily). On a genuine parse
    failure the gates still fail-closed — we never bypass — so this is loud but
    non-fatal.
    """
    from backend.common.codex import REGISTRY

    if not REGISTRY.is_loaded():
        try:
            REGISTRY.load()
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            _LOG.error(
                "codex registry load failed (%s) — every build stage will "
                "fail-closed and escalate. Fix the Codex parse error, do not "
                "bypass.",
                exc,
            )


def _emit(obj: Any) -> None:
    """Print a JSON view of a build state (or a message). No secrets in state."""
    if obj is None:
        print(json.dumps({"error": "no_state"}, indent=2))
        return
    if hasattr(obj, "model_dump"):
        # Trim the embedded order's brief to keep the per-step view readable;
        # the full order is still on disk.
        data = obj.model_dump()
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(obj, indent=2, default=str))


def _summary(state: Any) -> None:
    """One-line human cue after the JSON, so the operator knows the next move."""
    if state is None:
        return
    status = state.status
    stage = state.stage
    if status == "awaiting_approval":
        msg = f"  -> stage '{stage}' needs sign-off:  approve --order-id {state.order_id} --stage {stage}"
    elif status == "parked":
        reason = (state.park or {}).get("reason", "?")
        msg = f"  -> PARKED at '{stage}' ({reason}). Resolve, then re-run advance."
    elif status == "escalated":
        esc = state.escalation or {}
        msg = f"  -> ESCALATED at '{stage}' (codex {esc.get('violated_rule_id')}). Operator must resolve."
    elif status == "done":
        msg = "  -> DONE. Build settled."
    else:
        msg = f"  -> status={status} stage={stage}. Next: advance --order-id {state.order_id}"
    print(msg, file=sys.stderr)


def cmd_start(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.order).read_text(encoding="utf-8"))
    order = WebsiteOrder.model_validate(raw)
    state = service.start_order(order)
    _emit(state)
    _summary(state)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.order_id)
    _emit(state)
    _summary(state)
    return 0 if state is not None else 1


def cmd_approve(args: argparse.Namespace) -> int:
    state = service.approve_stage(args.order_id, args.stage)
    _emit(state)
    _summary(state)
    return 0 if state is not None else 1


def cmd_advance(args: argparse.Namespace) -> int:
    state = service.advance(args.order_id)
    _emit(state)
    _summary(state)
    return 0 if state is not None else 1


def cmd_run(args: argparse.Namespace) -> int:
    state = service.run(args.order_id)
    _emit(state)
    _summary(state)
    return 0 if state is not None else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backend.website.cli", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="open a build from an order JSON")
    s.add_argument("--order", required=True, help="path to a WebsiteOrder JSON")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status", help="show a build's current state")
    s.add_argument("--order-id", required=True)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="operator sign-off for one stage")
    s.add_argument("--order-id", required=True)
    s.add_argument("--stage", required=True)
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("advance", help="run the next approved stage (one step)")
    s.add_argument("--order-id", required=True)
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser("run", help="autonomous walk (needs website_autonomous_enabled)")
    s.add_argument("--order-id", required=True)
    s.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_codex()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
