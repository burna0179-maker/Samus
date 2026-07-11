#!/usr/bin/env python3
"""
1-Click SEO client execution runner — full audit → optimize → content chain
Source: ChatGPT recovery chat 07 (run_seo_client.py production-safe script)

Canonical relationship:
- [NEW] operator entry-point for supervised SEO fulfillment
- [EXPANDS §6 orchestration] sequential dependency enforcement (no parallel overload)
- Designed for: Stripe bypass + manual delivery mode (production-safe per chat 07)

Usage:
  python run_seo_client.py --client-name "Acme" --url https://acme.com --keywords "foo,bar"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any, Dict


def dispatch(base_url: str, headers: Dict[str, str], task_id: str, action: str, payload: Dict[str, Any]) -> None:
    import requests
    r = requests.post(f"{base_url}/dispatch/seo", headers=headers,
                      json={"task_id": task_id, "action": action, "payload": payload})
    if r.status_code not in (200, 202):
        print(f"[FAIL] dispatch {action}: {r.text}")
        sys.exit(1)
    print(f"[OK] dispatched: {task_id} ({action})")


def wait_for_completion(base_url: str, task_id: str, timeout_sec: int = 300) -> Dict[str, Any]:
    import requests
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = requests.get(f"{base_url}/task/{task_id}", timeout=5)
        if r.status_code != 200:
            print(f"[FAIL] fetch {task_id}")
            sys.exit(1)
        data = r.json()
        status = data.get("status")
        print(f"  ⏳ {task_id}: {status}")
        if status == "completed":
            return data
        if status in ("failed", "error"):
            print(f"[FAIL] {task_id}: {data}")
            sys.exit(1)
        time.sleep(2)
    print(f"[FAIL] timeout on {task_id}")
    sys.exit(1)


def run_workflow(base_url: str, token: str, url: str, keywords: list) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    run_id = uuid.uuid4().hex[:8]

    audit_id = f"audit-{run_id}"
    dispatch(base_url, headers, audit_id, "audit_site", {"url": url})
    audit = wait_for_completion(base_url, audit_id)

    optimize_id = f"optimize-{run_id}"
    dispatch(base_url, headers, optimize_id, "optimize_page", {"url": url, "audit_data": audit})
    optimization = wait_for_completion(base_url, optimize_id)

    content_id = f"content-{run_id}"
    dispatch(base_url, headers, content_id, "generate_content",
             {"url": url, "keywords": keywords, "optimization_data": optimization})
    content = wait_for_completion(base_url, content_id)

    print("\n✅ WORKFLOW COMPLETE")
    return {"audit": audit, "optimization": optimization, "content": content}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--keywords", default="", help="comma-separated")
    parser.add_argument("--out", default="seo_output.json")
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    results = run_workflow(args.base_url, args.token, args.url, keywords)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"📁 Results saved to {args.out}")
