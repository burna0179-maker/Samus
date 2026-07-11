"""One boot-integration entry the gateway lifespan calls.

Fuses the three ecosystem-core skeleton pieces into a single, fail-safe,
flag-gated boot step:

1. **Signed identity & charter** — load + validate ``charter.json`` and report
   its operator-Ed25519 signature posture (operator-verifiable).
2. **Immutable integrity gate** — verify the source manifest against the
   sealed baseline + report the baseline's operator-signature posture. The
   gate's *consequence* (abort vs audit vs off) is governed by
   ``SAMUS_IMMUTABLE_GATE_MODE`` (default ``off``).
3. **Shared autonomy contract** — build the ECO-ADR-0001 ``_shared.autonomy``
   contract for Samus (posture-gated, shadow-mode), stashed on the app so
   other surfaces can ``app.state.samus_autonomy.drive(...)``.

EVERYTHING IS ADDITIVE + DORMANT BY DEFAULT. This function NEVER raises: any
failure is logged and the gateway boots exactly as before. The ONLY way it
can abort boot is the explicit ``SAMUS_IMMUTABLE_GATE_MODE=enforce`` +
``env=production`` + detected drift/tamper path — the deliberate fail-closed
posture an operator opts into.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

log = logging.getLogger("samus.identity.boot")


class ImmutableGateAbort(RuntimeError):
    """Raised ONLY when the operator-armed enforce gate detects drift/tamper."""


def _is_production(env: Mapping[str, str]) -> bool:
    return (env.get("SAMUS_ENV") or env.get("ENV") or "").strip().lower() in (
        "production",
        "prod",
    )


def run_identity_boot(app: Any = None, *, env: Mapping[str, str] | None = None) -> dict:
    """Run the charter + immutable-gate + autonomy-layer boot step.

    Returns a structured report (also stashed on ``app.state.samus_identity``).
    Raises :class:`ImmutableGateAbort` ONLY under the explicit enforce path.
    """
    env = env if env is not None else os.environ
    report: dict[str, Any] = {
        "charter": None,
        "charter_signature": None,
        "immutable_gate": {"mode": "off"},
        "autonomy": {"built": False},
    }

    # ---- (1) Signed identity & charter ---------------------------------
    try:
        from .charter import charter_hash, charter_path, load_charter

        charter = load_charter()
        report["charter"] = {
            "agent_id": charter.agent_id,
            "agent_name": charter.agent_name,
            "version": charter.version,
            "domain": charter.domain,
            "charter_hash": charter_hash(charter),
            "values": list(charter.values),
            "invariants_count": len(charter.invariants),
        }
        try:
            from .charter_signature import check_charter_signature

            sig = check_charter_signature(charter_path())
            report["charter_signature"] = sig.to_dict()
            if sig.production_ready:
                log.info("samus identity: charter operator-Ed25519 VERIFIED")
            elif sig.tamper:
                log.error(
                    "samus identity: charter signature TAMPER/unavailable: %s",
                    sig.detail,
                )
            else:
                log.warning(
                    "samus identity: charter UNSIGNED (non-production trust). %s",
                    sig.detail,
                )
        except Exception:  # noqa: BLE001
            log.exception("samus identity: charter signature check failed (non-fatal)")
    except Exception:  # noqa: BLE001
        log.exception("samus identity: charter load failed (non-fatal)")

    # ---- (2) Immutable integrity gate (flag-gated) ---------------------
    try:
        from .immutable_manifest import check_baseline_trust, gate_mode, verify_manifest

        mode = gate_mode(env)
        gate_report: dict[str, Any] = {"mode": mode}
        if mode != "off":
            result = verify_manifest()
            trust = check_baseline_trust()
            gate_report["verify"] = result.to_dict()
            gate_report["baseline_trust"] = trust.to_dict()

            drift = not result.ok
            # A present-but-broken baseline signature in production is tamper.
            sig_tamper = trust.tamper
            production = _is_production(env)

            if drift:
                log.error(
                    "samus immutable gate: DRIFT detected (%d paths) — %s",
                    len(result.drift),
                    result.to_dict(),
                )
            if sig_tamper:
                log.error(
                    "samus immutable gate: baseline signature TAMPER — %s",
                    trust.detail,
                )
            if not trust.production_ready and not trust.tamper:
                log.warning(
                    "samus immutable gate: baseline NON-PRODUCTION trust (unsigned). %s",
                    trust.detail,
                )

            if mode == "enforce" and (drift or (sig_tamper and production)):
                gate_report["aborted"] = True
                report["immutable_gate"] = gate_report
                if app is not None:
                    try:
                        app.state.samus_identity = report
                    except Exception:  # noqa: BLE001
                        pass
                raise ImmutableGateAbort(
                    "samus immutable gate (enforce): "
                    + ("source drift detected" if drift else "baseline tamper in production")
                    + " — refusing to boot. Re-seal + re-sign the baseline "
                    "(scripts/seed_immutable_manifest.py + "
                    "scripts/sign_immutable_manifest.py --ed25519 --yes)."
                )
        report["immutable_gate"] = gate_report
    except ImmutableGateAbort:
        raise
    except Exception:  # noqa: BLE001 — gate machinery failure must not break boot
        log.exception("samus immutable gate: check failed (non-fatal; mode!=enforce)")

    # ---- (3) Shared autonomy contract (posture-gated, shadow-mode) -----
    try:
        from .autonomy_layer import build_samus_autonomy, layer_enabled, resolve_samus_mode

        if layer_enabled(env):
            mode = resolve_samus_mode(env=env)
            contract = build_samus_autonomy(env=env)
            report["autonomy"] = {
                "built": contract is not None,
                "mode": getattr(mode, "value", str(mode)) if mode is not None else None,
                "enabled": True,
            }
            if app is not None and contract is not None:
                try:
                    app.state.samus_autonomy = contract
                except Exception:  # noqa: BLE001
                    pass
        else:
            report["autonomy"] = {"built": False, "enabled": False}
    except Exception:  # noqa: BLE001
        log.exception("samus autonomy: layer boot failed (non-fatal)")

    if app is not None:
        try:
            app.state.samus_identity = report
        except Exception:  # noqa: BLE001
            pass
    return report


__all__ = ["run_identity_boot", "ImmutableGateAbort"]
