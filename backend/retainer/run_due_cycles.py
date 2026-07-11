"""Cron entry point — runs every retainer monthly cycle whose due-date has passed.

Looks up each due customer's email + name from CustomerStore so the report send
step has what it needs. Designed to be invoked once daily by
``scripts/Run-Retainer-Cycles.ps1`` (the wrapper handles DPAPI secrets).

Exit code:
    0   nothing due, OR every due cycle completed (ok=True)
    1   at least one cycle failed (the rest still ran)
    2   a hard crash before/during the iteration
"""
from __future__ import annotations

import argparse
import logging
import sys

from backend.retainer.monthly_cycle import find_due_cycles, run_monthly_cycle


_LOG = logging.getLogger("samus.retainer.run_due")


def _resolve_intake(customer_id: str, sku_id: str) -> dict[str, str]:
    """Pull customer email + name + audit_url metadata for cycle inputs."""
    from backend.memory.customers import CustomerStore
    store = CustomerStore()
    cust = store.get_customer(customer_id)
    if cust is None:
        return {"customer_email": "", "customer_name": "", "audit_url": ""}
    md = cust.metadata or {}
    return {
        "customer_email": cust.email,
        "customer_name": cust.name or "",
        "audit_url": md.get("audit_url", ""),
        "operations_summary": md.get("operations_summary", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every retainer monthly cycle whose due-date has passed.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print due cycles without executing them.",
    )
    args = parser.parse_args(argv)

    due = find_due_cycles()
    if not due:
        print("retainer: no cycles due.")
        return 0

    if args.dry_run:
        print(f"retainer: {len(due)} cycle(s) due (dry-run):")
        for d in due:
            print(f"  {d['customer_id']} / {d['sku_id']}  next_cycle_at={d['next_cycle_at']}")
        return 0

    print(f"retainer: running {len(due)} cycle(s)")
    failures = 0
    for d in due:
        cid, sku = d["customer_id"], d["sku_id"]
        intake = _resolve_intake(cid, sku)
        try:
            result = run_monthly_cycle(
                customer_id=cid,
                sku_id=sku,
                customer_email=intake["customer_email"],
                customer_name=intake["customer_name"],
                audit_url=intake["audit_url"],
                operations_summary=intake["operations_summary"],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {cid} / {sku}: {exc}", file=sys.stderr)
            failures += 1
            continue
        badge = "OK" if result.ok else "FAIL"
        print(f"  [{badge}] {cid} / {sku}  plan={result.plan_id}")
        if not result.ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
