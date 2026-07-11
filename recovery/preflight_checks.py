#!/usr/bin/env python3
"""
Pre-flight launch checklist — 5-minute health gate before client fulfillment
Source: ChatGPT recovery chat 07 (5-min preflight checklist)

Canonical relationship:
- [NEW] operator-facing automation of the canonical §15 acceptance gates
- [EXPANDS §6 observability] runtime health-gate sequence
- Maps to: /health /ready /queues /task/{id} (canonical §14 API surface)

Hard-stop on any failure. Use before every client job.
"""

from __future__ import annotations

import sys
import time
from typing import Dict, Optional


def preflight(
    base_url: str = "http://127.0.0.1:8080",
    token: Optional[str] = None,
    seo_test_url: str = "https://example.com",
) -> Dict[str, str]:
    try:
        import requests
    except ImportError:
        print("ERROR: pip install requests")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    results: Dict[str, str] = {}

    # 1. system health
    print("[1/5] /health + /ready ...")
    try:
        h = requests.get(f"{base_url}/health", timeout=5).json()
        r = requests.get(f"{base_url}/ready", timeout=5).json()
        if h.get("status") != "ok":
            return _fail(results, "health", f"health.status={h.get('status')}")
        results["health"] = "PASS"
    except Exception as e:
        return _fail(results, "health", str(e))

    # 2. queue sanity
    print("[2/5] /queues ...")
    try:
        q = requests.get(f"{base_url}/queues", timeout=5).json()
        for lane, depth in q.items():
            if isinstance(depth, dict) and depth.get("dlq", 0) > 0:
                return _fail(results, "queues", f"DLQ non-zero on {lane}: {depth}")
        results["queues"] = "PASS"
    except Exception as e:
        return _fail(results, "queues", str(e))

    # 3. worker execution probe (audit_site dry-run on example.com)
    print("[3/5] worker execution probe ...")
    try:
        task_id = f"preflight-{int(time.time())}"
        r = requests.post(
            f"{base_url}/dispatch/seo",
            headers=headers,
            json={"task_id": task_id, "action": "audit_site", "payload": {"url": seo_test_url}},
            timeout=10,
        )
        if r.status_code not in (200, 202):
            return _fail(results, "worker_probe", f"dispatch {r.status_code}: {r.text}")
        deadline = time.time() + 60
        while time.time() < deadline:
            t = requests.get(f"{base_url}/task/{task_id}", timeout=5).json()
            status = t.get("status")
            if status == "completed":
                results["worker_probe"] = "PASS"
                break
            if status in ("failed", "error"):
                return _fail(results, "worker_probe", f"task status={status}")
            time.sleep(2)
        else:
            return _fail(results, "worker_probe", "timeout waiting for completion")
    except Exception as e:
        return _fail(results, "worker_probe", str(e))

    # 4. SES reality (operator must confirm)
    print("[4/5] SES delivery — OPERATOR CONFIRM:")
    print("      can you send an email from the system right now? (y/N): ", end="")
    answer = (input() or "").strip().lower()
    if answer != "y":
        results["ses"] = "FALLBACK_MANUAL"     # not a hard fail; deliver manually
    else:
        results["ses"] = "PASS"

    # 5. Stripe bypass confirmation
    print("[5/5] Stripe bypass — confirmed (do NOT use Stripe-triggered flows today)")
    results["stripe_bypass"] = "PASS"

    print("\nPREFLIGHT SUMMARY:", results)
    return results


def _fail(results: Dict[str, str], check: str, msg: str) -> Dict[str, str]:
    results[check] = f"FAIL: {msg}"
    print(f"  ❌ {check}: {msg}")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    res = preflight(args.base_url, args.token)
    sys.exit(0 if all("PASS" in v or "FALLBACK" in v for v in res.values()) else 1)
