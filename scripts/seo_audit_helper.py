"""Run inside samus-seo by Run-SeoDelivery.ps1. Env-driven so PowerShell never
has to quote a Python one-liner. Runs the pure audit_site (crawl + on-page +
schema + security; no LLM/codex, so it works while the ecosystem is partial)
and writes the raw audit JSON to a fixed volume path for the host to copy out.

Env: SEO_URL (required), SEO_KEYWORDS ('|'-separated), SEO_INDUSTRY, SEO_CID.
Out: /opt/samus/data/artifacts/_seo_delivery_audit.json
"""
import json
import os
import sys

from backend.seo.models import AuditRequest
from backend.seo.service import audit_site

OUT = "/opt/samus/data/artifacts/_seo_delivery_audit.json"

url = os.environ.get("SEO_URL", "").strip()
if not url:
    print("SEO_URL not set"); sys.exit(2)
kws = [k for k in os.environ.get("SEO_KEYWORDS", "").split("|") if k.strip()]

req = AuditRequest(
    url=url,
    keywords=kws,
    industry=os.environ.get("SEO_INDUSTRY", "") or None,
    prospect_id=os.environ.get("SEO_CID", "") or None,
)
try:
    res = audit_site(req).model_dump()
    json.dump(res, open(OUT, "w", encoding="utf-8"), default=str)
    print(f"AUDIT_OK url={url} score={res.get('seo_score')} issues={len(res.get('issues', []))}")
except Exception as exc:  # noqa: BLE001
    print(f"AUDIT_FAILED {type(exc).__name__}: {exc}")
    sys.exit(1)
