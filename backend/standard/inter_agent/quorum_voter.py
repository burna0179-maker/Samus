"""Samus's cross-agent Quorum Vote logic — the VOTER side of the protocol.

Canonical spec: ``.consolidation/QUORUM_VOTE_PROTOCOL.md`` (2026-06-01 rework).

This module is the *pure, deterministic* reasoning core. It takes a peer's
CROSS_AGENT proposal and produces a 3-axis ballot. The HTTP route
(``POST /quorum/vote``) that carries this onto the wire — with HMAC
``AgentEnvelope`` verification + the dormant flag gate — lives in
``backend/gateway/app.py`` (the operator-facing workcell where the rest of
the inter-agent surface is mounted). This split mirrors the broker
(``broker_client`` = logic, gateway = route) and keeps the decision logic
trivially unit-testable with no FastAPI / HMAC scaffolding.

What this module deliberately does NOT do
------------------------------------------
* It does not verify HMAC — that is the route's boundary contract.
* It does not call an LLM — the spec's LLM escalation is *fail-closed-
  equivalent* to "LLM down → use the policy result", so the policy result
  is itself a conformant terminal answer. The single extension hook
  (:func:`_should_escalate_to_llm`) is documented for a future budget-capped
  tiebreak but is NEVER allowed to turn a "harm / high" axis into APPROVE.

Samus's self-interest lens (the per-agent posture the spec asks each voter
to define)
-----------------------------------------------------------------------
Samus is the **revenue / sales engine**. Its posture:

* ``self_benefit`` weighs the proposal against Samus's mission (revenue,
  customer pipeline), its resources, and its security / compliance posture.
  Anything that would degrade Samus's revenue path, exfiltrate or expose its
  customer data, compromise outreach integrity (CAN-SPAM / provider TOS), or
  leak its secrets is ``harm``. Pure-peer changes that don't touch Samus are
  ``neutral``; changes that strengthen Samus's pipeline / safety are
  ``benefit``.
* ``ecosystem_benefit`` weighs the net effect against the shared axioms
  (fail-closed, least-privilege, auditability, no parallel systems).
* ``abuse_risk`` asks whether the ``target_agent`` / consumer could misuse
  or over-privilege itself with this capability — Samus weighs this HIGH for
  anything touching its customer data, outreach surface, secrets, or that
  hands a consumer broad / unbounded reach.

Fail-closed is the spine: unknown / ambiguous signals resolve to the LESS
permissive value (``harm`` / ``high``), so an unrecognised proposal can
never be APPROVE — only REJECT or ABSTAIN.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

_LOG = logging.getLogger("samus.inter_agent.quorum_voter")

VOTER_ID = "samus"

Vote = Literal["APPROVE", "REJECT", "ABSTAIN"]
Polarity = Literal["benefit", "neutral", "harm"]
AbuseRisk = Literal["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Keyword lexicons. Deterministic signal extraction from the proposal's
# free-text ``action`` (+ a light scan of the payload). These are
# intentionally conservative: a match raises sensitivity, a non-match never
# *lowers* it below the fail-closed floor.
# ---------------------------------------------------------------------------

# Tokens that, when they appear in the action/target, mean the proposal
# touches something Samus guards HIGH (its data / outreach / secrets) OR is a
# security-control / privilege change → abuse_risk escalates toward high.
_HIGH_ABUSE_TOKENS: frozenset[str] = frozenset(
    {
        "secret",
        "secrets",
        "credential",
        "credentials",
        "key",
        "keys",
        "token",
        "password",
        "auth",
        "rbac",
        "scope",
        "scopes",
        "privilege",
        "privileges",
        "permission",
        "permissions",
        "grant",
        "sudo",
        "root",
        "admin",
        "disable",
        "bypass",
        "waiver",
        "override",
        "unsafe",
        "unrestricted",
        "unbounded",
        "unsandboxed",
        "exfiltrate",
        "delete",
        "drop",
        "wipe",
        "purge",
        "weaken",
        "loosen",
        "open",
        "expose",
        "public",
    }
)

# Tokens meaning the proposal touches Samus's revenue / customer / outreach
# surface specifically — these make self_benefit sensitive (Samus's mission &
# compliance posture). Touching these without a clear upside is harm.
_SAMUS_SENSITIVE_TOKENS: frozenset[str] = frozenset(
    {
        "customer",
        "customers",
        "prospect",
        "prospects",
        "lead",
        "leads",
        "outreach",
        "email",
        "campaign",
        "send",
        "ses",
        "sendgrid",
        "vapi",
        "crm",
        "pipeline",
        "revenue",
        "billing",
        "stripe",
        "payment",
        "can-spam",
        "canspam",
        "unsubscribe",
        "suppression",
        "contact",
        "contacts",
        "conversation",
        "conversations",
        "pii",
    }
)

# Tokens that signal a clearly-beneficial, well-bounded change to Samus
# (additive capability, observability, sandboxing, hardening). Only used to
# lift self_benefit to ``benefit`` when NO harm/high signal is present.
_SAMUS_BENEFIT_TOKENS: frozenset[str] = frozenset(
    {
        "sandbox",
        "sandboxed",
        "harden",
        "hardening",
        "audit",
        "observability",
        "fail-closed",
        "failclosed",
        "least-privilege",
        "metric",
        "metrics",
        "monitor",
        "monitoring",
        "backup",
        "redundancy",
        "resilience",
    }
)

# Tokens meaning the change is broadly good for the ecosystem (shared
# infra / governance integrity / coordination). Used to lift
# ecosystem_benefit to ``benefit`` ONLY when no harm signal is present.
_ECOSYSTEM_BENEFIT_TOKENS: frozenset[str] = frozenset(
    {
        "audit",
        "auditability",
        "fail-closed",
        "failclosed",
        "least-privilege",
        "harden",
        "hardening",
        "observability",
        "consensus",
        "quorum",
        "governance",
        "integrity",
        "resilience",
        "redundancy",
        "interop",
    }
)


def _tokens(proposal: dict[str, Any]) -> set[str]:
    """Lowercased token bag drawn from action + target + payload keys/values.

    We scan ``action`` (the primary verb), ``target_agent``, ``rationale``,
    and the top-level keys + scalar string values of ``payload``. Nested
    structure is not deeply walked — the action/rationale carry the intent
    and the payload keys carry the shape; a deep walk would add nondeterminism
    without changing the fail-closed outcome (unknown → less-permissive).
    """
    bag: list[str] = []

    def _split(text: str) -> list[str]:
        # Split on any non-alphanumeric boundary but KEEP hyphenated compounds
        # (so "fail-closed" / "can-spam" survive as single tokens AND as
        # split halves). We add both the raw hyphenated form and the pieces.
        out: list[str] = []
        for chunk in text.replace("/", " ").replace("_", " ").split():
            c = chunk.strip().lower()
            if not c:
                continue
            out.append(c)
            if "-" in c:
                out.extend(p for p in c.split("-") if p)
        return out

    for field in ("action", "target_agent", "rationale"):
        val = proposal.get(field)
        if isinstance(val, str):
            bag.extend(_split(val))

    payload = proposal.get("payload")
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str):
                bag.extend(_split(k))
            if isinstance(v, str):
                bag.extend(_split(v))

    return set(bag)


def _impact(proposal: dict[str, Any]) -> str:
    raw = str(proposal.get("impact") or "").strip().upper()
    return raw if raw in ("LOW", "MEDIUM", "HIGH") else "HIGH"  # unknown → HIGH (fail-closed)


# ---------------------------------------------------------------------------
# Per-axis assessors. Each returns its axis value PLUS a short reason
# fragment. Fail-closed: ambiguity resolves to the less-permissive value.
# ---------------------------------------------------------------------------


def _assess_abuse_risk(tokens: set[str], impact: str) -> tuple[AbuseRisk, str]:
    """abuse_risk — could the consumer / target misuse or over-privilege?"""
    hits = tokens & _HIGH_ABUSE_TOKENS
    if hits:
        return "high", f"touches privilege/security/data surface ({sorted(hits)[:4]})"
    # Touching Samus's revenue/customer surface without a security token is
    # still elevated — a peer reaching into the sales engine is medium risk.
    if tokens & _SAMUS_SENSITIVE_TOKENS:
        return "medium", "touches Samus revenue/customer/outreach surface"
    if impact == "HIGH":
        return "medium", "HIGH-impact proposal — elevated consumer-misuse risk"
    # No recognised risk signal AND not high-impact → low. This is the only
    # path to 'low', and it requires the action to be legible (tokenisable).
    if not tokens:
        # An empty / opaque proposal is NOT legible → cannot certify low.
        return "medium", "opaque proposal — cannot certify low abuse risk"
    return "low", "no privilege/data/security signal detected"


def _assess_self_benefit(tokens: set[str], impact: str) -> tuple[Polarity, str]:
    """self_benefit — beneficial / neutral / harmful to Samus specifically?"""
    # Security/privilege/data tokens touching Samus's own surface = harm to
    # Samus's compliance + security posture.
    danger = tokens & _HIGH_ABUSE_TOKENS
    samus_surface = tokens & _SAMUS_SENSITIVE_TOKENS
    if danger and samus_surface:
        return "harm", "weakens Samus's own data/outreach/secret posture"
    if danger:
        # A privilege/control change in the ecosystem that doesn't name
        # Samus's surface still erodes Samus's security posture indirectly.
        return "harm", "security/privilege change degrades Samus's posture"
    if samus_surface:
        # Reaching into Samus's revenue surface with no clear safeguard =
        # harm (the sales engine is Samus's mission-critical asset).
        return "harm", "external reach into Samus's revenue/customer surface"
    if tokens & _SAMUS_BENEFIT_TOKENS:
        return "benefit", "additive hardening/observability for Samus"
    # Doesn't touch Samus at all → neutral.
    return "neutral", "no direct effect on Samus's mission/resources"


def _assess_ecosystem_benefit(tokens: set[str], impact: str) -> tuple[Polarity, str]:
    """ecosystem_benefit — net effect vs the shared axioms?"""
    danger = tokens & _HIGH_ABUSE_TOKENS
    if danger:
        return "harm", f"erodes shared axioms (privilege/security: {sorted(danger)[:4]})"
    if tokens & _ECOSYSTEM_BENEFIT_TOKENS:
        return "benefit", "strengthens shared governance/auditability/resilience"
    # Neutral by default — a peer's internal-leaning change with no clear
    # ecosystem upside is neither harm nor benefit. Note: a 'neutral'
    # ecosystem axis is NOT enough for APPROVE (APPROVE requires benefit),
    # so this still fails closed toward ABSTAIN.
    return "neutral", "no clear ecosystem-wide benefit or harm"


# ---------------------------------------------------------------------------
# Aggregation — the spec's fail-closed, security-first rule.
# ---------------------------------------------------------------------------


def _aggregate(
    self_benefit: Polarity,
    ecosystem_benefit: Polarity,
    abuse_risk: AbuseRisk,
) -> Vote:
    """Map the three axes to a vote per QUORUM_VOTE_PROTOCOL.md §3-axis.

    * REJECT if ANY axis = harm OR abuse_risk == high (security-first).
    * APPROVE iff self_benefit != harm AND ecosystem_benefit == benefit
      AND abuse_risk == low.
    * else ABSTAIN.
    """
    if self_benefit == "harm" or ecosystem_benefit == "harm" or abuse_risk == "high":
        return "REJECT"
    if self_benefit != "harm" and ecosystem_benefit == "benefit" and abuse_risk == "low":
        return "APPROVE"
    return "ABSTAIN"


def _confidence(
    vote: Vote,
    self_benefit: Polarity,
    ecosystem_benefit: Polarity,
    abuse_risk: AbuseRisk,
    legible: bool,
) -> float:
    """A bounded, deterministic confidence (0..1).

    High when the signals are unambiguous (a clear harm/high → confident
    REJECT; a clean all-good → confident APPROVE). Lower for ABSTAIN, and
    lowered further when the proposal was opaque (illegible).
    """
    if vote == "REJECT" and (
        self_benefit == "harm" or ecosystem_benefit == "harm" or abuse_risk == "high"
    ):
        base = 0.9
    elif vote == "APPROVE":
        base = 0.8
    else:  # ABSTAIN — genuinely uncertain
        base = 0.4
    if not legible:
        base = min(base, 0.5)
    return round(base, 3)


# ---------------------------------------------------------------------------
# LLM-escalation extension hook (DOCUMENTED, NOT STUBBED).
# ---------------------------------------------------------------------------


def _should_escalate_to_llm(impact: str, axes: tuple[Polarity, Polarity, AbuseRisk]) -> bool:
    """Whether a future budget-capped LLM tiebreak WOULD be invoked.

    Per the spec, escalation fires when ``impact == HIGH`` or the axes
    conflict (e.g. self=benefit but abuse_risk=high). We compute the
    predicate so the route can log "would-escalate" telemetry, but we do
    NOT call an LLM here — policy-only is a conformant terminal answer
    (LLM-down ⇒ fall back to policy ⇒ identical result). When an operator
    later wires a budget-capped tiebreak, it MUST still respect
    "any harm/high ⇒ not APPROVE"; it may only break ABSTAIN↔(REJECT|stay)
    ambiguity, never manufacture an APPROVE the policy denied.
    """
    self_b, eco_b, abuse = axes
    if impact == "HIGH":
        return True
    benefit_present = self_b == "benefit" or eco_b == "benefit"
    risk_present = abuse in ("medium", "high") or self_b == "harm" or eco_b == "harm"
    return benefit_present and risk_present


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def decide_ballot(proposal: dict[str, Any]) -> dict[str, Any]:
    """Reason about a peer's CROSS_AGENT proposal → a 3-axis ballot dict.

    Pure + deterministic. Returns the exact ballot shape the collector
    (Major) expects::

        {
          "voter": "samus",
          "proposal_id": str,
          "vote": "APPROVE|REJECT|ABSTAIN",
          "confidence": 0..1,
          "self_benefit": "benefit|neutral|harm",
          "ecosystem_benefit": "benefit|neutral|harm",
          "abuse_risk": "low|medium|high",
          "reasoning": str,
          "method": "policy"
        }

    Fail-closed: a malformed / empty / opaque proposal NEVER yields APPROVE.
    ``proposal_id`` is echoed as a string ("" if absent) so the collector can
    always correlate the ballot.
    """
    if not isinstance(proposal, dict):
        proposal = {}

    proposal_id = str(proposal.get("proposal_id") or "")
    tokens = _tokens(proposal)
    legible = bool(tokens)
    impact = _impact(proposal)

    abuse_risk, abuse_reason = _assess_abuse_risk(tokens, impact)
    self_benefit, self_reason = _assess_self_benefit(tokens, impact)
    ecosystem_benefit, eco_reason = _assess_ecosystem_benefit(tokens, impact)

    vote = _aggregate(self_benefit, ecosystem_benefit, abuse_risk)
    confidence = _confidence(
        vote,
        self_benefit,
        ecosystem_benefit,
        abuse_risk,
        legible,
    )

    would_escalate = _should_escalate_to_llm(
        impact,
        (self_benefit, ecosystem_benefit, abuse_risk),
    )

    reasoning = (
        f"[samus/revenue-engine policy] vote={vote}: "
        f"self_benefit={self_benefit} ({self_reason}); "
        f"ecosystem_benefit={ecosystem_benefit} ({eco_reason}); "
        f"abuse_risk={abuse_risk} ({abuse_reason}); impact={impact}."
    )
    if would_escalate:
        reasoning += (
            " note: impact/axis-conflict would trigger budget-capped LLM "
            "tiebreak when wired — policy result stands (fail-closed)."
        )

    return {
        "voter": VOTER_ID,
        "proposal_id": proposal_id,
        "vote": vote,
        "confidence": confidence,
        "self_benefit": self_benefit,
        "ecosystem_benefit": ecosystem_benefit,
        "abuse_risk": abuse_risk,
        "reasoning": reasoning,
        "method": "policy",
    }


__all__ = ["decide_ballot", "VOTER_ID"]
