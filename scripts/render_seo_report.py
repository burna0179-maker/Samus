"""Render a client-ready SEO report from a raw audit_site JSON + client record.

Used by Run-SeoDelivery.ps1. Standalone (stdlib only). The pipeline is:
  audit_site (in samus-seo) -> audit JSON -> this renderer -> dated .md report.
Kept separate from backend.seo.report because that path is gated behind the
codex validator + LLM broker (fail-closed when the ecosystem is partial); this
is a deterministic, always-available formatter over the real audit findings.

Usage: python render_seo_report.py <audit.json> <client.json> <out.md> [prev_audit.json]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime


def _sev_icon(sev: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get((sev or "").lower(), "•")


def main() -> int:
    audit_path, client_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    prev_path = sys.argv[4] if len(sys.argv) > 4 else None

    # utf-8-sig tolerates the UTF-8 BOM that PowerShell 5.1's Out-File -Encoding
    # utf8 prepends to _client.json (json.load rejects a raw BOM).
    audit = json.load(open(audit_path, encoding="utf-8-sig"))
    client = json.load(open(client_path, encoding="utf-8-sig"))
    prev = None
    if prev_path:
        try:
            prev = json.load(open(prev_path, encoding="utf-8-sig"))
        except Exception:
            prev = None

    f = audit.get("findings", {})
    score = audit.get("seo_score")
    issues = audit.get("issues", [])
    today = datetime.now().date().isoformat()

    # score movement vs prior cycle
    delta = ""
    if prev and isinstance(prev.get("seo_score"), (int, float)) and isinstance(score, (int, float)):
        d = score - prev["seo_score"]
        delta = f" ( {'+' if d >= 0 else ''}{d} vs last cycle )"

    L = []
    L.append(f"# SEO Service Report — {client.get('name','')}")
    L.append(f"**Client:** {client.get('contact','')} · **Site:** {client.get('url','')} · "
             f"**Location:** {client.get('location','')}")
    L.append(f"**Plan:** {client.get('plan','SEO Optimization')} — ${client.get('mrr_usd','')}/mo · "
             f"**Report date:** {today}")
    nm = client.get("next_meeting")
    if nm:
        L.append(f"**Next review:** {nm}")
    L.append("")
    L.append(f"## Overall SEO health: {score} / 100{delta}")
    L.append("")

    # sort issues high -> low
    order = {"high": 0, "medium": 1, "low": 2}
    issues_sorted = sorted(issues, key=lambda i: order.get((i.get("severity") or "").lower(), 3))
    if issues_sorted:
        L.append("## Priority fixes this cycle")
        for i in issues_sorted:
            L.append(f"- {_sev_icon(i.get('severity'))} **[{(i.get('severity') or '').upper()}] "
                     f"{i.get('category','')}** — {i.get('message','')}")
        L.append("")

    # local-SEO + structured data (high-value, computed from findings)
    L.append("## Local SEO & structured data")
    if not f.get("has_local_business_schema"):
        L.append("- 🔴 **Missing LocalBusiness/School schema** — the top lever for local queries "
                 "(\"near me\", city terms) and Google's local pack. Add `School` structured data "
                 "(name, address, phone, geo, hours, grades).")
    else:
        L.append("- 🟢 LocalBusiness/School schema present.")
    L.append(f"- Schema present: {', '.join(f.get('schema_types', [])) or 'none'}")
    L.append(f"- Analytics: GA4 {'✓' if f.get('has_ga4') else '✗'}")
    L.append("")

    # performance
    L.append("## Performance (Core Web Vitals)")
    if f.get("pagespeed_error") == "api_key_unset":
        L.append("- ⚠️ Not measured this cycle — PageSpeed API key not configured (our task to fix). "
                 "Next cycle will include LCP/CLS/speed scores.")
    else:
        for lbl, k in [("Performance", "pagespeed_performance_score"), ("LCP (ms)", "pagespeed_lcp_ms"),
                       ("CLS", "pagespeed_cls")]:
            if f.get(k) is not None:
                L.append(f"- {lbl}: {f.get(k)}")
    L.append("")

    # security note (infra, not SEO, but customer-visible value)
    sec = f.get("security") or {}
    if sec.get("grade") and sec.get("grade") not in ("A", "A+"):
        L.append("## Security (beyond SEO, worth awareness)")
        L.append(f"- ⚠️ Site security graded **{sec.get('grade')}** "
                 f"(infra health rating: {sec.get('infrastructure_health',{}).get('rating','?')}). "
                 "A down/compromised site tanks rankings — recommend a hardening pass.")
        L.append("")

    L.append("## Plan for next cycle")
    L.append("- Apply the priority fixes above (WordPress) and re-audit to show score movement.")
    L.append("- Local-SEO groundwork: Google Business Profile + city-targeted terms.")
    L.append("- Include Core Web Vitals once the PageSpeed key is set.")
    L.append("")
    L.append("*Prepared by HustleForge — your recurring monthly SEO deliverable.*")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"rendered report -> {out_path} (score={score}, issues={len(issues)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
