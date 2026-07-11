"""Prior-inbound legitimacy collector.

Reads the existing intake (onboarding leads) and outreach event ledger
for prior touches keyed by email or phone. A hit yields a medium-confidence
``prior_inbound`` signal — medium rather than high because email/phone
collisions are possible and we don't want a shared corporate inbox to
backstop the entire warmth claim.

Pure read: never writes. Fail-OPEN — any persistence error returns None.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from ..legitimacy import LegitimacySignal

_LOG = logging.getLogger("samus.prospecting.sources.prior_inbound")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _scan_onboarding_leads(email: str) -> dict[str, Any] | None:
    """Scan samus_onboarding_leads for the email. None on miss / DDB error."""
    try:
        from backend.common.config import get_settings
        from backend.crm import persistence as crm_persistence

        s = get_settings()
        table_name = getattr(s, "ddb_onboarding_leads_table", "") or ""
        if not table_name:
            return None
        table = crm_persistence._onboarding_leads_table()
        if table is None:
            return None
        resp = table.scan(Limit=200)
        for item in resp.get("Items") or []:
            row_email = str(item.get("email") or "").strip().lower()
            if row_email == email:
                return {
                    "lead_id": str(item.get("lead_id") or ""),
                    "created_at": str(item.get("created_at") or ""),
                }
    except Exception as exc:  # noqa: BLE001 — fail-OPEN
        _LOG.info("prior_inbound onboarding-leads scan failed: %s", exc)
    return None


def _scan_outreach_ledger(email: str) -> dict[str, Any] | None:
    """Scan the local outreach campaign ledger(s) for a prior send/open."""
    root = os.path.join(_artifact_root(), "outreach")
    if not os.path.isdir(root):
        return None
    try:
        for fname in sorted(os.listdir(root)):
            if not fname.startswith("campaign_") or not fname.endswith(".jsonl"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if str(rec.get("email") or "").strip().lower() == email:
                            return {
                                "ledger_file": fname,
                                "ts": str(rec.get("ts") or ""),
                            }
            except OSError:
                continue
    except OSError as exc:
        _LOG.info("prior_inbound outreach-ledger scan failed: %s", exc)
    return None


def lookup_prior_inbound(
    *,
    email: str = "",
    phone: str = "",
) -> LegitimacySignal | None:
    """Return a prior_inbound signal if email/phone matches a prior touch."""
    email_norm = (email or "").strip().lower()
    if not email_norm and not (phone or "").strip():
        return None

    evidence: dict[str, Any] = {}
    source = ""
    if email_norm:
        onboarding = _scan_onboarding_leads(email_norm)
        if onboarding is not None:
            evidence = {"email": email_norm, "match": "onboarding_lead", **onboarding}
            source = "intake/onboarding_leads"
        else:
            outreach = _scan_outreach_ledger(email_norm)
            if outreach is not None:
                evidence = {"email": email_norm, "match": "outreach_ledger", **outreach}
                source = "outreach/campaign_ledger"

    if not evidence:
        return None

    return LegitimacySignal(
        kind="prior_inbound",
        source=source,
        discovered_at=datetime.now(timezone.utc),
        evidence=evidence,
        confidence="medium",
    )


__all__ = ["lookup_prior_inbound"]
