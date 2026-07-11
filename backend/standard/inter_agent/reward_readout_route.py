"""Reward/harm readout endpoint (Samus side) — the W5 revenue-feedback producer.

Darwin's EvolutionLoop needs a cross-agent FITNESS signal grounded in real
revenue outcomes. Samus already computes an ADR-004 ``RewardComputation`` per
opportunity and appends it to a JSONL audit ledger
(``reward_density._persist_path()``) — but that ledger lives in a docker-named
volume with NO host-readable / cross-agent path. This endpoint exposes an
AGGREGATE summary (never per-opportunity PII / customer rows): mean reward,
reward spread, terminal-paid count, and the harm totals (retracted claims /
unsubscribes / complaints). Darwin pulls it to bias which governed mutation it
PROPOSES (observation-only per Darwin ADR-0004 — this is advisory data, never a
command).

Transport: PULL over HMAC, mirroring ``agora_contribute_route`` exactly but for
CALLER_AGENT == ``darwin`` (``RotatingHMACKey.for_agent("darwin")`` reads
``SS_HMAC_KEY_DARWIN``). This deliberately uses the lighter HMAC rail rather than
the Ed25519 quorum roster, so the reward SIGNAL does not require admitting Samus
to the roster (an operator ceremony) — a reward readout is advisory data, not a
quorum vote. Fail-closed: 401 missing, 403 forged / wrong sender, 503
key-unprovisioned. Gated by ``SN_REWARD_READOUT_ENABLED`` (default OFF) — a
disabled endpoint still authenticates the caller, then returns an honest
``have_signal=False`` so Darwin simply sees no fitness signal this cycle.

NOTE: no ``from __future__ import annotations`` — it breaks FastAPI/pydantic
forward-ref resolution of the ``Request`` parameter type (same caveat as
agora_contribute_route / Optimus quorum_assess_route).
"""

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.gateway.reward_readout")

CALLER_AGENT = "darwin"  # Darwin's EvolutionLoop signs the readout request
SOURCE_KIND = "samus.reward"
ENV_READOUT_ENABLED = "SN_REWARD_READOUT_ENABLED"

# Bound how many recent ledger rows an aggregate may span (a caller-supplied
# window is clamped into this range so a request can never force an unbounded read).
_DEFAULT_WINDOW = 100
_MAX_WINDOW = 1000

# Harm component keys carried in each RewardComputation.components dict
# (see reward_density.compute_reward).
_HARM_KEYS = ("retracted_claims", "unsubscribes", "complaints", "harm_count")

_SHARED_CLIENT_PATH_ENSURED = False


def _ensure_security_client_on_path() -> None:
    """Add ``_shared/`` to sys.path so ``security_client`` imports (idempotent)."""
    global _SHARED_CLIENT_PATH_ENSURED
    if _SHARED_CLIENT_PATH_ENSURED:
        return
    for up in range(2, 9):
        try:
            cand = Path(__file__).resolve().parents[up] / "_shared"
        except IndexError:
            break
        if (cand / "security_client" / "agent_envelope.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            _SHARED_CLIENT_PATH_ENSURED = True
            return


def is_readout_enabled() -> bool:
    """Read ``SN_REWARD_READOUT_ENABLED`` live. Default OFF (dormant)."""
    return os.environ.get(ENV_READOUT_ENABLED, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "y",
    )


class _VerificationUnavailable(Exception):
    """Verifier cannot run (no key / security_client missing) → 503."""


class _EnvelopeRejected(Exception):
    """Envelope absent/malformed/forged/wrong-sender → 401/403."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _verify_darwin_envelope(body: Any) -> dict[str, Any]:
    """Verify an inbound wire ``AgentEnvelope`` from Darwin. Returns the inner
    payload dict on success. Fail-closed (mirrors agora_contribute_route._verify_
    anita_envelope but for CALLER_AGENT="darwin")."""
    if not isinstance(body, dict):
        raise _EnvelopeRejected("missing envelope", status_code=401)

    _ensure_security_client_on_path()
    try:
        from security_client.agent_envelope import (  # type: ignore
            AgentEnvelope,
            EnvelopeError,
            EnvelopeReplay,
            EnvelopeSignatureInvalid,
            EnvelopeStale,
        )
        from security_client.rotating_hmac import (  # type: ignore
            RotatingHMACKey,
            RotatingHMACKeyError,
        )
    except Exception as exc:  # noqa: BLE001 — import failure ⇒ cannot verify ⇒ 503
        _LOG.error("reward_readout: security_client unavailable: %s", exc)
        raise _VerificationUnavailable("security_client_unavailable") from exc

    try:
        verifying_key = RotatingHMACKey.for_agent(CALLER_AGENT)  # SS_HMAC_KEY_DARWIN
    except RotatingHMACKeyError as exc:
        _LOG.error("reward_readout: caller '%s' key unprovisioned: %s", CALLER_AGENT, exc)
        raise _VerificationUnavailable("caller_key_unprovisioned") from exc

    try:
        env = AgentEnvelope.from_wire(body)
    except EnvelopeError as exc:
        raise _EnvelopeRejected(f"malformed_envelope: {exc}", status_code=403) from exc

    if env.from_agent != CALLER_AGENT:
        raise _EnvelopeRejected(
            f"envelope from_agent='{env.from_agent}', expected '{CALLER_AGENT}'",
            status_code=403,
        )

    try:
        env.verify(verifying_key)
    except (EnvelopeSignatureInvalid, EnvelopeStale, EnvelopeReplay) as exc:
        raise _EnvelopeRejected(
            f"envelope_verification_failed: {type(exc).__name__}", status_code=403
        ) from exc
    except EnvelopeError as exc:
        raise _EnvelopeRejected(f"envelope_verification_failed: {exc}", status_code=403) from exc

    payload = env.payload
    if not isinstance(payload, dict):
        raise _EnvelopeRejected("envelope_payload_not_object", status_code=403)
    return dict(payload)


def _read_ledger_tail(window: int) -> list[dict[str, Any]]:
    """Return up to the last ``window`` reward-computation rows from the JSONL
    ledger (honours SAMUS_REWARD_PERSIST_PATH via reward_density._persist_path).
    Missing file / unparseable lines degrade to an empty/partial list — never
    raises (a readout fault must not 500 the gateway)."""
    try:
        from backend.strategy.reward_density import _persist_path

        path = _persist_path()
    except Exception:  # noqa: BLE001
        return []
    try:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-window:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _summarize_rewards(window: int) -> dict[str, Any]:
    """Aggregate the recent reward ledger into a cross-agent fitness summary.

    AGGREGATE ONLY — no opportunity_id / customer rows leave Samus. Empty ledger
    => an honest ``have_signal=False`` (Darwin sees no fitness this cycle)."""
    rows = _read_ledger_tail(window)
    if not rows:
        return {"have_signal": False, "reason": "no_reward_data", "window_n": 0}

    rewards: list[float] = []
    terminal_paid = 0
    harm = {k: 0 for k in _HARM_KEYS}
    last_at = ""
    for r in rows:
        try:
            rewards.append(float(r.get("reward", 0.0)))
        except (TypeError, ValueError):
            pass
        comp = r.get("components") if isinstance(r.get("components"), dict) else {}
        try:
            if float(comp.get("terminal_paid", 0)) > 0:
                terminal_paid += 1
        except (TypeError, ValueError):
            pass
        for k in _HARM_KEYS:
            try:
                harm[k] += int(float(comp.get(k, 0)))
            except (TypeError, ValueError):
                pass
        at = str(r.get("computed_at", ""))
        if at > last_at:
            last_at = at

    n = len(rewards)
    mean_reward = round(sum(rewards) / n, 6) if n else 0.0
    summary = {
        "have_signal": n > 0,
        "window_n": n,
        "mean_reward": mean_reward,
        "min_reward": round(min(rewards), 6) if rewards else 0.0,
        "max_reward": round(max(rewards), 6) if rewards else 0.0,
        "terminal_paid": terminal_paid,  # # of terminal won+paid outcomes in window
        "harm": harm,  # aggregate harm-signal totals
        "harm_total": sum(harm.values()),
        "last_computed_at": last_at,
    }
    if n == 0:
        summary["reason"] = "no_parseable_rewards"
    return summary


def _build_readout(payload: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return {
        "agent": "samus",
        "source_kind": SOURCE_KIND,
        "summary": summary,
        "summary_hash": hashlib.sha256(canonical).hexdigest(),
        "request_id": str(payload.get("request_id", "")),
        "ts": time.time(),
    }


def _clamp_window(payload: dict[str, Any]) -> int:
    try:
        w = int(payload.get("window", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        w = _DEFAULT_WINDOW
    return max(1, min(_MAX_WINDOW, w))


def register(app) -> None:
    """Mount ``POST /inter_agent/reward-summary`` (mirrors agora_contribute_route.register)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.post("/inter_agent/reward-summary")
    async def reward_summary(request: Request):  # noqa: ANN202
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"detail": "invalid_json"})

        # Verify FIRST (auth gate) — even a disabled endpoint must not serve an
        # unauthenticated caller; only after a verified Darwin envelope do we
        # decide enabled-vs-disabled (an honest no-signal readout).
        try:
            payload = _verify_darwin_envelope(body)
        except _EnvelopeRejected as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
        except _VerificationUnavailable as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})

        if not is_readout_enabled():
            return _build_readout(
                payload, {"have_signal": False, "reason": "reward_readout_disabled"}
            )

        summary = _summarize_rewards(_clamp_window(payload))
        result = _build_readout(payload, summary)
        _LOG.info(
            "reward_summary to=darwin n=%s mean=%s harm=%s",
            summary.get("window_n"),
            summary.get("mean_reward"),
            summary.get("harm_total"),
        )
        return result


__all__ = [
    "CALLER_AGENT",
    "SOURCE_KIND",
    "ENV_READOUT_ENABLED",
    "is_readout_enabled",
    "register",
    "_verify_darwin_envelope",
    "_summarize_rewards",
    "_read_ledger_tail",
]
