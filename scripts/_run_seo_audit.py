"""Fire the SEO audit_and_report pipeline against a single URL.

Inputs via environment (set by the PS wrapper):
  SAMUS_AUDIT_URL      (required)
  SAMUS_AUDIT_LABEL    (optional, defaults to derived from URL)
  SAMUS_AUDIT_KW       (optional, comma-separated keywords)

Prints a one-screen summary + writes the full markdown report to disk.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from anywhere — make sure backend.* resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.seo.models import AuditRequest  # noqa: E402
from backend.seo.service import audit_and_report  # noqa: E402


def main() -> int:
    url = os.environ.get("SAMUS_AUDIT_URL", "").strip()
    if not url:
        print("ERROR: SAMUS_AUDIT_URL not set", file=sys.stderr)
        return 2

    label = (os.environ.get("SAMUS_AUDIT_LABEL") or "").strip() or None
    kw_raw = (os.environ.get("SAMUS_AUDIT_KW") or "").strip()
    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()] if kw_raw else None

    req = AuditRequest(url=url, keywords=keywords or [])
    result = audit_and_report(req, target_keywords=keywords, customer_label=label)

    audit = result["audit"]
    opt = result["optimize"]
    content = result["content"]

    print()
    print("=" * 72)
    print(f"SEO AUDIT  {audit['url']}")
    print("=" * 72)
    print(f"seo_score        : {audit['seo_score']}/100")
    print(f"issues found     : {len(audit['issues'])}")
    print(f"recommendations  : {len(opt.get('recommendations') or [])}")
    drafts = content.get("drafts") or {}
    print(f"content drafts   : {len(drafts)} sections")
    print(f"report path      : {result['report_path']}")
    print(f"customer slug    : {result['customer_slug']}")

    print()
    print("--- top 10 issues (severity desc) ---")
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    issues = sorted(
        audit["issues"],
        key=lambda i: (sev_rank.get(i.get("severity", "info"), 5), i.get("id", "")),
    )
    for issue in issues[:10]:
        sev = issue.get("severity", "")
        iid = issue.get("id", "")
        msg = (issue.get("message") or "")[:90]
        print(f"  [{sev:>8}] {iid:<36} {msg}")

    print()
    print("--- top 6 recommendations ---")
    for rec in (opt.get("recommendations") or [])[:6]:
        cat = rec.get("category", "")
        title = (rec.get("title") or "")[:100]
        print(f"  [{cat}] {title}")

    print()
    print("--- artifact tree under report dir ---")
    rpath = Path(result["report_path"])
    for entry in sorted(rpath.parent.iterdir()):
        size = entry.stat().st_size if entry.is_file() else 0
        kind = "F" if entry.is_file() else "D"
        print(f"  [{kind}] {entry.name:40} {size} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
