"""One-shot enrichment smoke test against today's prospects."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# Run from Samus root regardless of CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.prospecting.crawler import fetch_homepage  # noqa: E402
from backend.prospecting.enrichment import enrich_from_page_with_fallback  # noqa: E402


def main() -> None:
    csv_path = r"D:\Hustleforge\Samus\.data\host_artifacts\daily_calls\call_list_2026-05-19.csv"
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    skip_hosts = ("zillow.", "allstate.", "farmers.", "agents.", "kw.com")
    picks = []
    for r in rows:
        url = r.get("website_url", "")
        if url and not any(s in url for s in skip_hosts):
            picks.append(r)
        if len(picks) >= 6:
            break

    print(f"Testing {len(picks)} prospects with own-domain websites")
    print("=" * 80)

    for r in picks:
        name = r["company_name"][:50]
        url = r["website_url"]
        print()
        print(f">>> {name}  ({url})")
        page = fetch_homepage(url)
        status = page.get("status_code")
        html_len = len(page.get("html") or "")
        print(f"    homepage: status={status} bytes={html_len}")
        if status != 200 or not page.get("html"):
            err = page.get("fetch_error") or "no html"
            print(f"    SKIP (homepage unreachable: {err})")
            continue
        sig = enrich_from_page_with_fallback(page, url, enable_facebook=True)
        fields = [(k, v) for k, v in sig.items() if v]
        if fields:
            for k, v in fields:
                print(f"    {k:24} {v[:80]}")
        else:
            print("    (no signals extracted)")


if __name__ == "__main__":
    main()
