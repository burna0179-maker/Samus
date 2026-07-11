"""HOTL close -> checkout handoff.

When a voice call CLOSES (Morgan's end-of-call analysis sets
``lead_summary.validated_product_name`` — the offer the prospect agreed to buy),
this drafts a checkout-link email using the EXISTING Stripe payment link
(reusing :func:`backend.outreach.flyer.buy_url`, which carries
``client_reference_id=<prospect_id>`` for webhook attribution) and QUEUES it for
one-click operator approval.

It NEVER sends. Emailing a payment request is a financial + outward action; a
mis-classified "close" must not dun anyone. So the draft lands in an approval
queue (``<storage.root>/voice/close_handoffs/pending_<call_id>.json``) and the
operator does the actual send. This is the HOTL wire; the operator is the fire.

Called autonomously from the reconcile sweep, so a real close is turned into a
ready-to-send checkout email within minutes of hanging up — no operator having
to notice the close, look up the link, and compose from scratch.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.voice.close_handoff")

_QUEUE_DIR = "voice/close_handoffs"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ClosePending:
    call_id: str
    company: str
    prospect_id: str
    product_name: str
    product_price: str
    email: str                 # "" when it couldn't be extracted — operator fills it
    email_confidence: str      # "structured" | "transcript" | "missing"
    checkout_url: str
    subject: str
    body: str
    queued_ts: str = ""
    needs_email: bool = False


def _g(call: Any, name: str, default: Any = None) -> Any:
    return call.get(name, default) if isinstance(call, dict) else getattr(call, name, default)


def _lead_summary(call: Any) -> dict[str, Any]:
    analysis = _g(call, "analysis") or {}
    if not isinstance(analysis, dict):
        return {}
    sd = analysis.get("structuredData") or {}
    ls = sd.get("lead_summary") if isinstance(sd, dict) else None
    return ls if isinstance(ls, dict) else {}


def _extract_email(call: Any, ls: dict[str, Any]) -> tuple[str, str]:
    """Return (email, confidence). Prefer a structured field (once the schema
    adds contact_email); else best-effort scan the transcript for a typed email
    (spoken/spelled emails won't match — operator confirms those)."""
    for key in ("contact_email", "email", "prospect_email"):
        v = ls.get(key)
        if isinstance(v, str) and _EMAIL_RE.fullmatch(v.strip()):
            return v.strip().lower(), "structured"
    tx = str(_g(call, "transcript", "") or "")
    for m in _EMAIL_RE.findall(tx):
        if "hustleforge" not in m.lower():   # skip Morgan's own address
            return m.strip().lower(), "transcript"
    return "", "missing"


def _offer_for(ls: dict[str, Any]):
    """Resolve the purchasable Offer (with a live Stripe payment link) for this
    close, honoring the agreed product when it maps to a catalog SKU, else the
    finding-matched add-on. Reuses flyer's catalog+matching wholesale."""
    from backend.outreach import flyer

    finding = " ".join(str(p) for p in (ls.get("pain_points") or []))
    row = {
        "callsheet_finding": finding,
        "company_name": ls.get("company") or "",
        "security_grade": "",
    }
    return flyer.match_offer(row)


def detect_close(call: Any) -> ClosePending | None:
    """Build a ClosePending iff the call analysis shows a purchase agreement."""
    ls = _lead_summary(call)
    product = (ls.get("validated_product_name") or "").strip()
    if not product:                       # null/empty => no agreement => not a close
        return None
    from backend.outreach import flyer

    offer = _offer_for(ls)
    if offer is None:
        _LOG.warning("close on %s but no purchasable offer/link resolved",
                     _g(call, "id", ""))
        return None

    call_id = str(_g(call, "id", "") or "")
    company = str(ls.get("company") or "")
    prospect_id = _prospect_id(call)
    price = str(ls.get("validated_product_price") or offer.price_usd)
    email, conf = _extract_email(call, ls)
    checkout = flyer.buy_url(offer, prospect_id=prospect_id, email=email)

    subject = f"Your {offer.label} — checkout link inside"
    greeting = "Hi," if not company else f"Hi {company} team,"
    body = (
        f"{greeting}\n\n"
        f"Great talking just now. As promised, here's the secure checkout for the "
        f"{offer.label} (${offer.price_usd:.0f}):\n\n"
        f"{checkout}\n\n"
        f"Once you're through, our team gets the notification and the work kicks "
        f"off. Any questions, just reply to this email.\n\n"
        f"— Alex, HustleForge"
    )
    return ClosePending(
        call_id=call_id,
        company=company,
        prospect_id=prospect_id,
        product_name=product,
        product_price=price,
        email=email,
        email_confidence=conf,
        checkout_url=checkout,
        subject=subject,
        body=body,
        needs_email=(conf == "missing"),
    )


def _prospect_id(call: Any) -> str:
    for container in ("metadata", "assistantOverrides"):
        c = _g(call, container)
        if isinstance(c, dict):
            if c.get("prospect_id"):
                return str(c["prospect_id"])
            vv = c.get("variableValues")
            if isinstance(vv, dict) and vv.get("prospect_id"):
                return str(vv["prospect_id"])
    return ""


def _queue_path(call_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", call_id or "unknown")
    return storage.root() / _QUEUE_DIR / f"pending_{safe}.json"


def queue_close_handoffs(calls: list[Any]) -> list[ClosePending]:
    """Detect closes and queue an approval-pending draft per NEW close.

    Idempotent: a call already queued is skipped. Returns the newly-queued
    closes. Never raises — a handoff failure must not disturb the sweep.
    """
    queued: list[ClosePending] = []
    for call in calls:
        try:
            pending = detect_close(call)
            if pending is None:
                continue
            path = _queue_path(pending.call_id)
            if path.exists():
                continue
            pending.queued_ts = iso_now()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
            queued.append(pending)
            _LOG.warning(
                "CLOSE queued for operator send: %s (%s $%s) email=%s [%s] -> %s",
                pending.company, pending.product_name, pending.product_price,
                pending.email or "MISSING", pending.email_confidence, path.name,
            )
        except Exception as exc:  # noqa: BLE001 — never disturb the sweep
            _LOG.warning("close_handoff failed for a call: %s", exc)
    return queued


def scan_recent(*, client: Any = None, limit: int = 100) -> list[ClosePending]:
    """Pull recent Vapi calls (read-only) and queue any NEW closes for operator
    send. Entry point for the reconcile sweep — turns a close into a ready-to-
    send checkout draft within minutes. Never raises."""
    try:
        if client is None:
            from backend.voice.call_batch_analyzer import _build_client
            client = _build_client()
        calls = client.list_calls(limit=min(limit, 100))
    except Exception as exc:  # noqa: BLE001 — read-only; a failure just yields nothing
        _LOG.warning("close_handoff scan_recent: list_calls failed: %s", exc)
        return []
    return queue_close_handoffs(calls)
