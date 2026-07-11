"""Portable peer-side relief forwarder (agent-agnostic) — Samus port.

Ported verbatim from Anita's canonical
``backend/core/oversight/relief_forwarder.py`` (the SENDING half of cross-agent
operator-relief routing). The ONLY divergence from the donor is two
``# canon-carve-out`` markers on the state-file mkdir + write_text (Samus's
governance forbids unmarked raw filesystem mutation inside ``backend/``). The
``state_path`` is supplied by the caller (task.py) via
``backend.common.state_paths.state_path`` so the dedup-set lands under the
writable ``SAMUS_STATE_ROOT`` — Samus containers run read-only except that root.

Depends only on the stdlib + two injected callables, making no assumption about
Samus's framework, HMAC scheme, or pending-item store:

    pending_source() -> iterable of dicts, each one of Samus's operator-pending
        items. Required keys: ``ticket_id``, ``created_ts``. Optional:
        ``action``, ``reason``, ``payload``, ``participants``. (Samus's adapter
        reads CRM opportunities awaiting an operator Stake Sentence.)
    post_fn(request: dict) -> bool: sign + POST one relief request to Anita's
        ``POST /api/relief/intake`` with Samus's own AgentEnvelope + replay
        headers. Return True iff Anita accepted it.

Policy: only a COARSE local staleness filter + a persistent dedup set. The
authoritative staleness + operator-absence gate lives on Anita. The forwarder
NEVER resolves anything; it mirrors stale pending items outward for
deliberation. Best-effort: a failed post is retried next run.

Settings attributes (read live via getattr):
    samus_agora_relief_forward_enabled        bool  (default False — fail-closed)
    samus_agora_relief_forward_staleness_sec  int   (default 21600 = 6h)
    samus_agora_relief_forward_max_per_run    int   (default 5)
"""
# AXIOM-2a: boundary defender — mirrors operator-pending work outward, never resolves it.
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

_LOG = logging.getLogger("samus.inter_agent.relief.forwarder")

PendingSource = Callable[[], Iterable[Dict[str, Any]]]
PostFn = Callable[[Dict[str, Any]], bool]


class ReliefForwarder:
    def __init__(
        self,
        *,
        agent_id: str,
        pending_source: PendingSource,
        post_fn: PostFn,
        settings: Any,
        state_path: Any,
        clock: Optional[Callable[[], float]] = None,
        ledger: Any = None,
    ) -> None:
        self._agent_id = (agent_id or "").strip().lower()
        self._pending_source = pending_source
        self._post_fn = post_fn
        self._settings = settings
        self._state_path = Path(state_path)
        self._clock = clock or time.time
        self._ledger = ledger

    def _flag(self, name: str, default: Any) -> Any:
        return getattr(self._settings, name, default)

    def _load_forwarded(self) -> set:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("forwarded"), list):
                return {str(x) for x in raw["forwarded"]}
        except FileNotFoundError:
            return set()
        except Exception:  # noqa: BLE001
            _LOG.debug("relief_forwarder: unreadable state %s", self._state_path, exc_info=True)
        return set()

    def _save_forwarded(self, forwarded: set) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)  # canon-carve-out: relief_forwarder_state
            trimmed = list(forwarded)[-5000:]
            self._state_path.write_text(  # canon-carve-out: relief_forwarder_state
                json.dumps({"forwarded": trimmed}), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            _LOG.debug("relief_forwarder: state flush failed", exc_info=True)

    def _record(self, event: str, payload: Dict[str, Any]) -> None:
        if self._ledger is None:
            return
        try:
            self._ledger.record(event, payload)
        except Exception:  # noqa: BLE001
            _LOG.debug("relief_forwarder ledger record failed: %s", event, exc_info=True)

    def run_once(self) -> Dict[str, Any]:
        try:
            return self._run_once()
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("relief_forwarder scan failed")
            return {"ran": False, "reason": f"error:{exc!r}"}

    def _run_once(self) -> Dict[str, Any]:
        if not bool(self._flag("samus_agora_relief_forward_enabled", False)):
            return {"ran": False, "reason": "disabled"}

        now = float(self._clock())
        staleness = float(self._flag("samus_agora_relief_forward_staleness_sec", 21600))
        max_per_run = max(0, int(self._flag("samus_agora_relief_forward_max_per_run", 5)))

        try:
            items = list(self._pending_source() or [])
        except Exception:  # noqa: BLE001
            _LOG.exception("relief_forwarder: pending_source raised")
            return {"ran": False, "reason": "pending_source_error"}

        forwarded = self._load_forwarded()
        eligible: List[Dict[str, Any]] = []
        for it in items:
            tid = str(it.get("ticket_id") or "").strip()
            if not tid or tid in forwarded:
                continue
            created = it.get("created_ts")
            try:
                if created is not None and (now - float(created)) < staleness:
                    continue
            except (TypeError, ValueError):
                pass
            eligible.append(it)

        eligible.sort(key=lambda x: float(x.get("created_ts") or 0.0))
        selected = eligible[:max_per_run] if max_per_run else []

        sent = 0
        for it in selected:
            request = self._to_request(it, now=now)
            try:
                ok = bool(self._post_fn(request))
            except Exception:  # noqa: BLE001
                _LOG.debug("relief_forwarder: post_fn raised for %s",
                           request.get("remote_ticket_id"), exc_info=True)
                ok = False
            if ok:
                forwarded.add(request["remote_ticket_id"])
                sent += 1

        if sent:
            self._save_forwarded(forwarded)
            self._record(
                "relief_forwarder.sent",
                {"agent": self._agent_id, "sent": sent, "eligible": len(eligible)},
            )
        return {"ran": True, "eligible": len(eligible), "sent": sent}

    def _to_request(self, item: Dict[str, Any], *, now: float) -> Dict[str, Any]:
        tid = str(item.get("ticket_id") or "")
        req: Dict[str, Any] = {
            "origin_agent": self._agent_id,
            "remote_ticket_id": tid,
            "action": str(item.get("action") or "operator_action"),
            "reason": str(item.get("reason") or ""),
            "created_ts": float(item.get("created_ts") or now),
            "payload": dict(item.get("payload") or {}),
        }
        participants = item.get("participants")
        if isinstance(participants, list) and participants:
            req["participants"] = [str(p).strip().lower() for p in participants if str(p).strip()]
        return req


__all__ = ["ReliefForwarder", "PendingSource", "PostFn"]
