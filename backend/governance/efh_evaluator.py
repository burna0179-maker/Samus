"""EFH (Ethical Failure Handler) evaluator for Samus.

Load-bearing for outreach/CRM/subscription paths. Samus is the commercial
workcell; his prospect outreach is the exact attack surface
inviolable_axioms.counterparty_data_minimum + no_unconsented_influence are
designed to catch.

Per SCHEMA.md, EthicalReviewVeto records:
  - escalation MUST equal "gate_only" (asserted at construction)
  - inviolable_axioms_breached MUST be non-empty
  - escape Darwin fitness reads by structural rule (not enforced here)

Two-layer evaluation (fail-closed by construction):

  1. A deterministic keyword heuristic (:data:`_PATTERNS`) — zero-cost, no
     network, always runs. This is the LOAD-BEARING floor: it can only flag
     breaches, never clear them.
  2. A semantic LLM classifier (:mod:`backend.governance.efh_semantic`) that,
     when armed (``samus_efh_semantic_enabled`` + a configured Anthropic key),
     reads the proposed action against the inviolable-axiom catalogue and can
     ONLY ADD breaches the heuristic missed (paraphrased manipulation, indirect
     extraction, etc.). It can never remove a heuristic breach. An LLM outage,
     a missing key, or the flag being off leaves layer 1 fully intact — so the
     gate is strictly monotonic: arming the semantic layer can only make EFH
     stricter, never weaker. This is why a fail-OPEN semantic call is safe here.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.governance.efh")

_AXIOMS_DIR = Path(__file__).resolve().parents[2] / "axioms"
# Veto records persist on the writable data volume in-container (the image
# root /opt/samus is read-only). See backend/common/state_paths.py.
_VETO_SINK = state_path("ethical_vetoes")


# Heuristic keyword patterns mapped to axiom ids. Replace with semantic eval.
_PATTERNS: dict[str, list[str]] = {
    "axiom.inviolable.no_unconsented_influence": [
        r"\bmanipulat",
        r"\bdark[\s-]?pattern",
        r"\bcovert",
        r"\bdeceiv",
        r"\bnudge\b",
        r"\bundisclosed",
        r"\bemotional appeal",
    ],
    "axiom.inviolable.no_zero_sum_extraction": [
        r"\bextract",
        r"\bworse off\b",
        r"\bzero[\s-]?sum",
    ],
    "axiom.inviolable.no_self_concealment": [
        r"\bhide\b",
        r"\bconceal",
        r"\bsuppress(\s+log|\s+event)",
        r"\bredact(\s+from\s+alex)?",
    ],
    "axiom.inviolable.no_irreversible_harm_at_scale": [
        r"\birreversibl",
        r"\bunrecoverabl",
        r"\bcannot be undone",
    ],
    "axiom.inviolable.human_dignity_floor": [
        r"\boptimize (over|against)\b",
        r"\binstrumental",
        r"\btarget\s+vuln",
    ],
    "axiom.inviolable.counterparty_data_minimum": [
        r"\bscrape\b",
        r"\bharvest\b",
        r"\bretain.*pii",
        r"\bcross[\s-]?reference",
        r"\benrich.*without",
    ],
    "axiom.inviolable.no_adversarial_host_action": [
        r"\bprivilege escalat",
        r"\bbypass acl",
        r"\bexfiltrat",
        r"\btamper\s+with\s+log",
    ],
}


class EthicalFailureHandler:
    """Pre-commit ethical gate. Returns EthicalReviewVeto or None."""

    def __init__(self, axioms_dir: Path | None = None) -> None:
        d = Path(axioms_dir) if axioms_dir else _AXIOMS_DIR
        with (d / "inviolable_axioms.yaml").open("r", encoding="utf-8") as f:
            self.inviolable = yaml.safe_load(f)
        with (d / "informed_consent.spec.yaml").open("r", encoding="utf-8") as f:
            self.consent_spec = yaml.safe_load(f)
        self._axiom_index = {a["id"]: a for a in self.inviolable.get("inviolable_axioms", [])}

    # ------------------------------------------------------------------
    # Consent evaluation
    # ------------------------------------------------------------------
    def informed_consent_check(self, action_disclosure: dict) -> dict:
        """Evaluate the 4 consent elements as a conjunction.

        action_disclosure shape (caller's responsibility):
          {
            "disclosed": bool,
            "material_consequences_disclosed": bool,
            "refusal_without_retaliation": bool,
            "affirmative": bool,
          }
        """
        elements = [e["id"] for e in self.consent_spec["informed_consent_elements"]]
        results = {e: bool(action_disclosure.get(e, False)) for e in elements}
        results["valid_consent"] = all(results.values())
        results["failing_elements"] = [e for e, ok in results.items() if e in elements and not ok]
        return results

    # ------------------------------------------------------------------
    # Layer 1 -- deterministic fail-closed floor (pure, no side effects)
    # ------------------------------------------------------------------
    def heuristic_breaches(self, proposed_action: dict) -> list[str]:
        """Return the Layer-1 breached axiom ids -- pure, no persist/publish.

        Runs the always-on fail-closed floor (keyword axiom patterns + the
        consent gate) that :meth:`evaluate` builds on. Callers that only need
        the verdict -- e.g. an adversarial self-test that feeds the gate a
        deliberately manipulative payload and asserts it is flagged -- can use
        this without the veto-ledger write / Quorum Hub publish :meth:`evaluate`
        performs.
        """
        body_text = self._flatten_text(proposed_action)
        breached: list[str] = []
        for axiom_id, patterns in _PATTERNS.items():
            if any(re.search(p, body_text, re.IGNORECASE) for p in patterns):
                breached.append(axiom_id)

        # Consent gate: if action claims a counterparty interaction without
        # valid consent on a consent-bound axiom.
        disclosure = proposed_action.get("consent_disclosure")
        if disclosure is not None:
            cres = self.informed_consent_check(disclosure)
            if not cres["valid_consent"]:
                for axiom in self.inviolable.get("inviolable_axioms", []):
                    if axiom.get("consent_spec_ref") and axiom["id"] not in breached:
                        breached.append(axiom["id"])
        return breached

    # ------------------------------------------------------------------
    # Veto evaluation
    # ------------------------------------------------------------------
    def evaluate(self, proposed_action: dict) -> dict | None:
        """Return EthicalReviewVeto record or None.

        Layer 1 (deterministic heuristic) always runs and is the fail-closed
        floor. Layer 2 (semantic LLM classifier) runs only when armed and can
        ONLY ADD breaches — see the module docstring for why this is monotonic.
        """
        body_text = self._flatten_text(proposed_action)
        breached: list[str] = self.heuristic_breaches(proposed_action)
        evaluator_layers: list[str] = ["heuristic"]

        # Layer 2 — semantic classifier. Additive only: it may flag axioms the
        # keyword heuristic missed (e.g. paraphrased manipulation), never clear
        # one. Default OFF + fail-open (a None/empty result leaves layer 1
        # untouched), so arming it can only tighten the gate.
        valid_axiom_ids = set(self._axiom_index)
        semantic_breaches = self._semantic_breaches(body_text, valid_axiom_ids)
        if semantic_breaches:
            evaluator_layers.append("semantic")
            for axiom_id in semantic_breaches:
                if axiom_id not in breached:
                    breached.append(axiom_id)

        if not breached:
            return None

        veto_id = str(uuid.uuid4())
        veto = {
            "veto_id": veto_id,
            "ts": _dt.datetime.utcnow().isoformat() + "Z",
            "proposing_agent": proposed_action.get("proposing_agent", "samus"),
            "proposed_action": {
                "kind": proposed_action.get("kind", "goal_commit"),
                "body": proposed_action.get("body", {}),
            },
            "projected_success_outcome": proposed_action.get(
                "projected_success_outcome", "unspecified"
            ),
            "projected_failure_outcome": proposed_action.get(
                "projected_failure_outcome", "unspecified"
            ),
            "inviolable_axioms_breached": breached,
            "asymmetry_score": float(proposed_action.get("asymmetry_score", 0.0)),
            "escalation": "gate_only",
            "gate_response": "pending",
            "conditions": None,
            "evaluator_layers": evaluator_layers,
            "notes": proposed_action.get("notes", f"breach via {'+'.join(evaluator_layers)}"),
        }
        assert veto["escalation"] == "gate_only", (
            "EthicalReviewVeto.escalation MUST be gate_only (SCHEMA invariant)"
        )
        assert veto["inviolable_axioms_breached"], (
            "EthicalReviewVeto.inviolable_axioms_breached MUST be non-empty"
        )

        self._persist(veto)
        # Best-effort publish to the cross-agent Quorum Hub. Fail-open —
        # SAMUS_QUORUM_PUBLISH_ENABLED=off short-circuits inside the publisher.
        try:
            from backend.common.quorum_publisher import publish_efh_veto

            publish_efh_veto(veto)
        except Exception:  # noqa: BLE001
            pass
        # Cross-agent Anita ethical_vetoes sink — best-effort, flag-gated inside
        # the publisher (fail-open). FLAG: the dedicated Anita sink requires a
        # cross-agent signed envelope + the Anita base URL that is not available
        # in this offline/standalone repo, so this delegates to the quorum-hub
        # publisher above (which IS the wired cross-agent fan-out). When the
        # Anita ethical_vetoes endpoint + its peer-signing envelope land, point
        # publish_efh_veto's sink at it; no change is needed here.
        return veto

    # ------------------------------------------------------------------
    # Layer 2 — semantic classifier
    # ------------------------------------------------------------------
    def _semantic_breaches(
        self,
        body_text: str,
        valid_axiom_ids: set[str],
    ) -> list[str]:
        """Return the axiom ids the semantic classifier flags (additive only).

        Returns ``[]`` whenever the semantic layer is disabled, unconfigured,
        or fails for any reason — the deterministic heuristic remains the
        load-bearing gate, so a fail-open here can only ever miss a breach the
        heuristic already caught or not, never clear one. Every returned id is
        validated against the real axiom catalogue so the LLM cannot invent a
        breach for an axiom that does not exist.
        """
        try:
            from backend.common.config import get_settings

            if not getattr(get_settings(), "samus_efh_semantic_enabled", False):
                return []
            from backend.governance.efh_semantic import classify_breaches

            flagged = classify_breaches(
                body_text,
                axiom_index=self._axiom_index,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open onto the heuristic floor
            _LOG.warning("efh semantic layer failed (heuristic stands): %s", exc)
            return []
        return [a for a in flagged if a in valid_axiom_ids]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _persist(self, veto: dict) -> Path:
        _VETO_SINK.mkdir(parents=True, exist_ok=True)
        path = _VETO_SINK / f"{veto['veto_id']}.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(veto, f, sort_keys=False)
        return path

    @staticmethod
    def _flatten_text(action: dict) -> str:
        parts: list[str] = []

        def _walk(v: Any) -> None:
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    _walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    _walk(x)

        _walk(action)
        return " ".join(parts)
