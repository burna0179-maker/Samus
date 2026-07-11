"""Deterministic CAN-SPAM / FTC compliance guard for outbound email.

This is the single deterministic *structural* content gate that runs
immediately before any email leaves Samus. It is wired into
``backend.common.email_backend.send_email`` — the universal send chokepoint
through which every workcell's mail funnels — so it cannot be bypassed.

It does NOT replace the Codex ``outreach_send`` intent gate (banned-phrase /
legitimacy / stake-sentence). It sits beside it and enforces the CAN-SPAM
structural requirements that nothing else checks today:

  * recipient is not on the suppression list (honor prior opt-outs / bounces /
    complaints) — applies to EVERY kind of mail;
  * commercial mail carries a working unsubscribe mechanism (a configured
    unsubscribe URL present in the body, or an explicit reply-to-opt-out
    instruction);
  * commercial mail carries the sender's physical postal address;
  * commercial mail emits a ``List-Unsubscribe`` / ``List-Unsubscribe-Post``
    header pair (RFC 8058 one-click) — a deliverability + compliance win that
    no code path injects today.

Transactional / relationship mail (receipts, onboarding acks, fulfillment
deliverables) is exempt from the unsubscribe + postal-address requirements per
CAN-SPAM, so callers tag those sends ``kind="transactional"``. The suppression
check still applies to every kind — a hard-bounced or complained address
should not be re-mailed even transactionally.

Three operating modes (mirrors the codebase's own ``SAMUS_AUTHZ_MODE``
three-state pattern; read from ``settings.compliance_guard_mode``):

  * ``off``     — guard not consulted at all (fully dormant; the default).
  * ``audit``   — guard evaluates, logs would-block reasons, AND injects the
                  List-Unsubscribe headers, but NEVER blocks a send.
  * ``enforce`` — guard blocks (the caller raises ``EmailBackendError``) on any
                  hard violation; headers still injected.

Default ``off`` honors the house WIRE-not-ARM rule: ship wired + dormant, let
the operator ramp ``off -> audit -> enforce`` on a live revenue path. ``audit``
is the safe first step — it surfaces every current violation and turns on the
deliverability headers without risking a single blocked send.

This module is pure + deterministic except a single fail-open suppression read
(``backend.common.suppression.is_email_suppressed``). ``evaluate`` never raises
on normal input; mode handling (block vs. log) is the caller's responsibility.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from backend.common.config import get_settings
from backend.common.suppression import is_email_suppressed

_LOG = logging.getLogger("samus.common.compliance_guard")

MessageKind = Literal["commercial", "transactional"]
GuardMode = Literal["off", "audit", "enforce"]

# Recognizable opt-out instruction in a body when no explicit unsubscribe URL
# is present — reply-to-opt-out is a valid CAN-SPAM mechanism. Intentionally
# broad: a missed-but-present unsubscribe (false negative) is the costly error.
_UNSUB_RE = re.compile(
    r"\b(unsubscribe|opt[\s\-]?out|reply\s+(?:with\s+)?stop|to\s+stop\s+receiving)\b",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")

# Penalty weights for the informational 0..1 score (1.0 == clean). Hard
# reasons drive ok=False regardless; warnings only shave the score.
_HARD_PENALTY = 0.34
_SOFT_PENALTY = 0.15


def _norm(text: str | None) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


@dataclass(frozen=True)
class ComplianceMessage:
    """The minimal view of an outbound email the guard needs."""

    to: str
    subject: str
    body: str
    html_body: str | None = None
    from_addr: str | None = None
    reply_to: str | None = None
    kind: MessageKind = "commercial"


@dataclass(frozen=True)
class ComplianceVerdict:
    ok: bool
    kind: str
    score: float
    reasons: tuple[str, ...] = ()  # hard-block reasons (drive ok=False)
    warnings: tuple[str, ...] = ()  # soft reasons (score penalty only)
    headers: dict[str, str] = field(default_factory=dict)  # List-Unsubscribe to inject

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "headers": dict(self.headers),
        }


def coerce_kind(kind: str | None) -> MessageKind:
    """Map any caller value to a known kind. Unknown -> ``commercial`` (strict),
    so a forgotten/garbled tag fails toward the stricter ruleset, never away."""
    return "transactional" if str(kind or "").strip().lower() == "transactional" else "commercial"


def get_mode() -> GuardMode:
    """Resolve the active guard mode from settings. Unknown -> ``off`` (the
    documented dormant default), so a misconfiguration never silently *enables*
    blocking on a live send path."""
    raw = str(getattr(get_settings(), "compliance_guard_mode", "off") or "off").strip().lower()
    return raw if raw in ("off", "audit", "enforce") else "off"  # type: ignore[return-value]


def _configured_sender_domains(settings) -> set[str]:
    domains: set[str] = set()
    for fld in ("sendgrid_from_email", "ses_from_email"):
        val = str(getattr(settings, fld, "") or "").strip().lower()
        if "@" in val:
            domains.add(val.rsplit("@", 1)[-1])
    extra = str(getattr(settings, "compliance_from_domains", "") or "")
    for tok in extra.replace(",", " ").split():
        dom = tok.strip().lower().lstrip("@")
        if dom:
            domains.add(dom)
    return domains


def _has_unsubscribe(message: ComplianceMessage, unsub_url: str) -> bool:
    blob = f"{message.body or ''}\n{message.html_body or ''}"
    if unsub_url and unsub_url.lower() in blob.lower():
        return True
    return bool(_UNSUB_RE.search(blob))


def build_list_unsubscribe_headers(settings, kind: MessageKind) -> dict[str, str]:
    """Build the List-Unsubscribe header pair for commercial mail.

    Prefers a configured https unsubscribe URL (one-click eligible per RFC
    8058) and appends a mailto fallback to the configured reply-to when present.
    Returns ``{}`` for transactional mail or when no mechanism is configured.
    """
    if kind != "commercial":
        return {}
    unsub_url = str(getattr(settings, "unsubscribe_url", "") or "").strip()
    reply_to = str(getattr(settings, "sendgrid_reply_to", "") or "").strip()
    parts: list[str] = []
    one_click = False
    if unsub_url:
        parts.append(f"<{unsub_url}>")
        if unsub_url.lower().startswith(("http://", "https://")):
            one_click = True
    if reply_to and "@" in reply_to and not reply_to.lower().startswith("mailto:"):
        parts.append(f"<mailto:{reply_to}?subject=unsubscribe>")
    elif reply_to.lower().startswith("mailto:"):
        parts.append(f"<{reply_to}>")
    if not parts:
        return {}
    headers = {"List-Unsubscribe": ", ".join(parts)}
    if one_click:
        # One-click POST is only meaningful when an http(s) target exists.
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return headers


def evaluate(message: ComplianceMessage) -> ComplianceVerdict:
    """Deterministically evaluate one outbound email against the structural
    CAN-SPAM/FTC rules. Returns a verdict; the caller applies the mode.

    Pure + side-effect-free except one fail-open suppression read. Does not
    raise on normal input.
    """
    settings = get_settings()
    kind = coerce_kind(message.kind)
    reasons: list[str] = []
    warnings: list[str] = []

    # Subject must be non-empty (defensive; the adapters also enforce this).
    if not (message.subject or "").strip():
        reasons.append("empty_subject")

    # (1) Suppression — applies to every kind (honor opt-outs / bounces / complaints).
    if is_email_suppressed(message.to):
        reasons.append("recipient_suppressed")

    # (2) Commercial-only structural requirements.
    if kind == "commercial":
        unsub_url = str(getattr(settings, "unsubscribe_url", "") or "").strip()
        if not _has_unsubscribe(message, unsub_url):
            reasons.append("missing_unsubscribe")

        postal = str(getattr(settings, "sender_postal_address", "") or "").strip()
        if not postal:
            reasons.append("postal_address_unconfigured")
        elif _norm(postal) not in (_norm(message.body) + " " + _norm(message.html_body)):
            reasons.append("postal_address_missing_from_body")

        # Deceptive thread-prefix subject on commercial mail is a CAN-SPAM
        # deception flag. Soft (warning) — legitimate re-engagement may thread.
        subj = (message.subject or "").strip().lower()
        if subj.startswith(("re:", "fwd:", "fw:")):
            warnings.append("subject_thread_prefix")

    # (3) From-domain sanity (soft, all kinds). Skipped when no sender or no
    #     configured domains to compare against.
    eff_from = (message.from_addr or "").strip().lower()
    if eff_from and "@" in eff_from:
        allowed = _configured_sender_domains(settings)
        if allowed and eff_from.rsplit("@", 1)[-1] not in allowed:
            warnings.append("from_domain_unconfigured")

    headers = build_list_unsubscribe_headers(settings, kind)
    score = max(0.0, 1.0 - _HARD_PENALTY * len(reasons) - _SOFT_PENALTY * len(warnings))
    return ComplianceVerdict(
        ok=not reasons,
        kind=kind,
        score=score,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        headers=headers,
    )


__all__ = [
    "MessageKind",
    "GuardMode",
    "ComplianceMessage",
    "ComplianceVerdict",
    "coerce_kind",
    "get_mode",
    "evaluate",
    "build_list_unsubscribe_headers",
]
